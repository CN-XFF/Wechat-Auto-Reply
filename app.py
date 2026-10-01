from __future__ import annotations

import ctypes
import json
import logging
import msvcrt
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from first_run import ensure_configuration, save_configuration
from reply_core.engine import CodexReplyEngine, ReplyDecision
from reply_core.codex_cli import run_codex
from reply_core.codex_catalog import refresh_cli_catalog
from reply_core.wechat_bridge import SendCancelled, WeChatBridge


ROOT = Path(__file__).resolve().parent
APP_VERSION = "1.0.9"
RUNTIME = ROOT / "runtime"
LOGS = ROOT / "logs"
RUNTIME.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)
logging.basicConfig(
    filename=LOGS / "wechat-auto-reply.log", level=logging.INFO, encoding="utf-8",
    format="%(asctime)s %(levelname)s %(message)s",
    force=True,
)
LIVE_LOG_QUEUE: queue.Queue[str] = queue.Queue(maxsize=3000)
REASONING_EFFORT_OPTIONS = {
    "低（low）": "low",
    "中（medium）": "medium",
    "高（high）": "high",
    "极高（xhigh）": "xhigh",
}
REASONING_EFFORT_LABEL_BY_ID = {
    effort_id: label for label, effort_id in REASONING_EFFORT_OPTIONS.items()
}
DEFAULT_MOUSE_MOVE_THRESHOLD_PX = 150
UI_COLORS = {
    "page": "#F2F5F7",
    "surface": "#FFFFFF",
    "text": "#24323C",
    "muted": "#687781",
    "border": "#D8E1E6",
    "accent": "#188A55",
    "accent_hover": "#116E43",
    "accent_soft": "#E7F4EC",
    "danger": "#C43D4B",
    "danger_hover": "#A92D3B",
    "log_text": "#24323C",
}


def split_reply_messages(reply: str) -> list[str]:
    """Treat non-empty lines as separate chat bubbles, capped at three bubbles."""
    normalized = str(reply).replace("\r\n", "\n").replace("\r", "\n")
    parts = [part.strip() for part in normalized.split("\n") if part.strip()]
    if len(parts) > 3:
        parts = parts[:2] + [" ".join(parts[2:])]
    return parts or [str(reply).strip()]


class LiveLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LIVE_LOG_QUEUE.put_nowait(self.format(record))
        except queue.Full:
            try:
                LIVE_LOG_QUEUE.get_nowait()
                LIVE_LOG_QUEUE.put_nowait(self.format(record))
            except (queue.Empty, queue.Full):
                pass


live_log_handler = LiveLogHandler()
live_log_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
logging.getLogger().addHandler(live_log_handler)
INSTANCE_MUTEX = None
INSTANCE_LOCK_FILE = None


def ensure_single_instance() -> bool:
    """Prevent two auto-reply GUI processes from reading/sending at the same time."""
    global INSTANCE_MUTEX, INSTANCE_LOCK_FILE
    lock_path = RUNTIME / "wechat-auto-reply.lock"
    try:
        INSTANCE_LOCK_FILE = lock_path.open("a+b")
        msvcrt.locking(INSTANCE_LOCK_FILE.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        logging.error("自动回复锁文件已被占用，拒绝启动第二个实例：%s", lock_path)
        return False

    mutex_name = "Global\\WeChatAutoReplySingleInstance"
    INSTANCE_MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
    if not INSTANCE_MUTEX:
        return True
    already_exists = ctypes.windll.kernel32.GetLastError() == 183
    if already_exists:
        logging.error("自动回复已有一个实例在运行，拒绝启动第二个实例")
        return False
    return True


class Application:
    def __init__(self):
        startup_started = time.perf_counter()
        logging.info("启动阶段：读取配置")
        self.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
        self.config_path = ROOT / "config.json"
        normalized_targets = self._normalize_auto_reply_listen_settings(self.config)
        if normalized_targets:
            self._persist_config()
            logging.warning(
                "配置修复：自动回复已开启但监听关闭，已自动补开监听：%s",
                "、".join(normalized_targets),
            )
        logging.info("启动阶段：初始化微信连接")
        self.bridge = WeChatBridge(self.config, RUNTIME)
        logging.info("启动阶段：刷新联系人备注")
        self._refresh_target_remarks()
        logging.info("启动阶段：更新近期联系人缓存")
        self._merge_recent_self_contacts()
        self.profiles = {t["name"]: t.get("profile", "按近期聊天自然回复。") for t in self.config["targets"]}
        self.engine = CodexReplyEngine(ROOT, int(self.config.get("codex_timeout_seconds", 120)))
        self.events: queue.Queue = queue.Queue()
        self.pending_batches: dict[str, dict] = {}
        self.inflight_targets: set[str] = set()
        self.reply_epoch = 0
        self.task_condition = threading.Condition()
        self.shutting_down = False
        self.pending_resend: dict | None = None
        self.pending_reply_confirmations: dict[str, dict] = {}
        self.worker = threading.Thread(target=self._message_worker, daemon=True)
        self.send_lock = threading.Lock()
        self.target_listen_vars: dict[str, tk.BooleanVar] = {}
        self.target_auto_reply_vars: dict[str, tk.BooleanVar] = {}
        self.target_style_rule_vars: dict[str, tk.BooleanVar] = {}
        self.target_profile_widgets: dict[str, tk.Text] = {}
        self.target_profile_generate_buttons: dict[str, tk.Button] = {}
        self.target_profile_panels: dict[str, tk.Frame] = {}
        self.target_command_vars: dict[str, tk.BooleanVar] = {}
        self.command_channel_var: tk.BooleanVar | None = None
        self.codex_command_var: tk.BooleanVar | None = None
        self.command_password_widget: tk.Entry | None = None
        self.command_password_visibility_var: tk.BooleanVar | None = None
        self.command_password_visibility_button: tk.Button | None = None
        self.target_profile_panel_rows: dict[str, int] = {}
        self.target_wait_widgets: dict[str, tk.Entry] = {}
        self.status_detail_var: tk.StringVar | None = None
        self.header_status_var: tk.StringVar | None = None
        self.global_auto_reply_var: tk.BooleanVar | None = None
        self.model_choice_var: tk.StringVar | None = None
        self.reasoning_choice_var: tk.StringVar | None = None
        self.model_options: dict[str, str] = {}
        self.model_catalog: dict[str, dict] = {}
        self.model_verified = False
        self.reasoning_options = dict(REASONING_EFFORT_OPTIONS)
        self._catalog_busy = False
        self._catalog_generation = 0
        self._catalog_cancel = threading.Event()
        self.cancel_keyboard_var: tk.BooleanVar | None = None
        self.cancel_mouse_var: tk.BooleanVar | None = None
        self.confirm_timeout_notify_var: tk.BooleanVar | None = None
        self.mouse_threshold_widget: tk.Entry | None = None
        self.live_log_widget: tk.Text | None = None
        self.test_reply_target_var: tk.StringVar | None = None
        self.test_reply_content_widget: tk.Entry | None = None
        self.test_reply_target_labels: dict[str, str] = {}
        self.test_reply_button: tk.Button | None = None
        self.test_search_button: tk.Button | None = None
        self.test_reply_send_var: tk.BooleanVar | None = None
        self.test_reply_active = False
        logging.info("启动阶段：创建状态窗口")
        self.root = tk.Tk()
        self._setup_status_window()
        self.root.protocol("WM_DELETE_WINDOW", self.stop)
        logging.info("启动阶段：状态窗口已创建，用时 %.1f 秒", time.perf_counter() - startup_started)

    @staticmethod
    def _normalize_auto_reply_listen_settings(config: dict) -> list[str]:
        """自动回复依赖监听；修复旧配置中两项开关不一致的状态。"""
        normalized: list[str] = []
        for target in config.get("targets", []):
            auto_enabled = bool(target.get("auto_reply_enabled", True))
            listen_enabled = bool(
                target.get("listen_enabled", target.get("auto_reply_enabled", True))
            )
            if auto_enabled and not listen_enabled:
                target["listen_enabled"] = True
                normalized.append(str(target.get("name") or target.get("username") or "未命名联系人"))
        return normalized

    def _unique_target_name(self, display_name: str) -> str:
        existing = {str(target.get("name")) for target in self.config.get("targets", [])}
        if display_name not in existing:
            return display_name
        index = 2
        while f"{display_name} ({index})" in existing:
            index += 1
        return f"{display_name} ({index})"

    def _refresh_target_remarks(self, trigger: str = "启动时") -> None:
        """按稳定微信号刷新当前联系人备注，内部目标名保持不变。"""
        changes = []
        for target in self.config.get("targets", []):
            target_name = str(target.get("name") or "").strip()
            username = str(
                target.get("username") or self.bridge.targets.get(target_name) or ""
            ).strip()
            if not target_name or not username:
                continue
            previous = str(target.get("ui_name") or target_name)
            current = self.bridge.display_name_for_username(username, fallback=previous).strip()
            if not current:
                continue
            target["ui_name"] = current
            self.bridge.ui_names[target_name] = current
            if current != previous:
                changes.append((target_name, current))
        if changes:
            self._persist_config()
            logging.info(
                "%s刷新微信备注：%s",
                trigger,
                "、".join(f"{name}→{remark}" for name, remark in changes),
            )
        else:
            logging.info("%s刷新微信备注：无变化", trigger)

    def _merge_recent_self_contacts(self) -> None:
        if not self.config.get("show_recent_self_contacts", True):
            logging.info("近期联系人读取未开启，可关闭程序后从开始菜单重新配置微信与联系人")
            return
        existing_usernames = {target.get("username") for target in self.config.get("targets", [])}
        added = False
        contacts = self.bridge.recent_self_contacts(
            session_limit=int(self.config.get("recent_contact_scan_sessions", 80)),
            max_contacts=int(self.config.get("recent_contact_max_contacts", 30)),
        )
        logging.info("近期联系人读取完成：找到 %d 个可用个人会话", len(contacts))
        for contact in contacts:
            username = contact["username"]
            if username in existing_usernames:
                continue
            name = self._unique_target_name(str(contact.get("name") or username))
            target = {
                "name": name,
                "username": username,
                "ui_name": str(contact.get("name") or name),
                "listen_enabled": False,
                "auto_reply_enabled": False,
                "use_style_rules": True,
                "wait_seconds": float(self.config.get("initial_reply_wait_seconds", 10.0)),
                "profile": (
                    f"这是用户近期联系过的人 {name}。关系未单独建模，回复必须短、自然、谨慎，"
                    "不要使用情侣称呼或过度亲密语气；不确定时少说或需要确认。涉及金钱、隐私、"
                    "账号、定位、见面承诺、健康、自伤或伤人时必须 requires_confirmation。"
                ),
            }
            self.config["targets"].append(target)
            self.bridge.targets[name] = username
            self.bridge.ui_names[name] = target["ui_name"]
            existing_usernames.add(username)
            added = True
        if added:
            self._persist_config()
            logging.info("已加入近期联系人：%s", "、".join(t["name"] for t in self.config["targets"]))

    def _apply_ui_theme(self) -> None:
        colors = UI_COLORS
        self.root.configure(bg=colors["page"])
        self.root.option_add("*Font", ("Microsoft YaHei UI", 9))
        self.root.option_add("*background", colors["surface"])
        self.root.option_add("*foreground", colors["text"])
        self.root.option_add("*Button.background", colors["surface"])
        self.root.option_add("*Button.foreground", colors["text"])
        self.root.option_add("*Button.activeBackground", colors["accent_soft"])
        self.root.option_add("*Button.activeForeground", colors["accent_hover"])
        self.root.option_add("*Button.relief", "raised")
        self.root.option_add("*Button.borderWidth", 1)
        self.root.option_add("*Button.highlightThickness", 0)
        self.root.option_add("*Button.padX", 9)
        self.root.option_add("*Button.padY", 4)
        self.root.option_add("*Entry.background", colors["surface"])
        self.root.option_add("*Entry.relief", "flat")
        self.root.option_add("*Entry.highlightThickness", 1)
        self.root.option_add("*Entry.highlightBackground", colors["border"])
        self.root.option_add("*Entry.highlightColor", colors["accent"])
        self.root.option_add("*Text.background", colors["surface"])
        self.root.option_add("*Text.foreground", colors["text"])
        self.root.option_add("*Text.relief", "flat")
        self.root.option_add("*Text.highlightThickness", 1)
        self.root.option_add("*Text.highlightBackground", colors["border"])
        self.root.option_add("*Text.highlightColor", colors["accent"])

        style = ttk.Style(self.root)
        try:
            if "clam" in style.theme_names():
                style.theme_use("clam")
            style.configure(
                "TCombobox",
                padding=(7, 4),
                fieldbackground=colors["surface"],
                background=colors["surface"],
                foreground=colors["text"],
                bordercolor=colors["border"],
                lightcolor=colors["surface"],
                darkcolor=colors["border"],
                arrowsize=12,
            )
            style.map(
                "TCombobox",
                fieldbackground=[("readonly", colors["surface"])],
                foreground=[("disabled", colors["muted"])],
            )
            style.configure(
                "Vertical.TScrollbar",
                background=colors["border"],
                troughcolor=colors["page"],
                arrowcolor=colors["muted"],
                bordercolor=colors["page"],
                lightcolor=colors["border"],
                darkcolor=colors["border"],
                arrowsize=12,
            )
        except tk.TclError as exc:
            logging.debug("应用 ttk 界面样式失败，使用系统默认样式：%s", exc)

    def _setup_status_window(self) -> None:
        self.root.title("微信自动回复运行中")
        self.root.geometry("760x900")
        self.root.minsize(700, 700)
        self._apply_ui_theme()
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        page_shell = tk.Frame(self.root)
        page_shell.grid(row=0, column=0, sticky="nsew")
        page_shell.grid_columnconfigure(0, weight=1)
        page_shell.grid_rowconfigure(0, weight=1)

        page_canvas = tk.Canvas(
            page_shell, bg=UI_COLORS["page"], highlightthickness=0, borderwidth=0
        )
        page_scrollbar = ttk.Scrollbar(
            page_shell, orient="vertical", command=page_canvas.yview
        )
        page_canvas.configure(yscrollcommand=page_scrollbar.set)
        page_canvas.grid(row=0, column=0, sticky="nsew")
        page_scrollbar.grid(row=0, column=1, sticky="ns")
        sticky_footer = tk.Frame(
            self.root, borderwidth=1, relief="groove", padx=8, pady=4
        )
        sticky_footer.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        sticky_footer.grid_columnconfigure(0, weight=1)
        content = tk.Frame(page_canvas, bg=UI_COLORS["surface"])
        content.grid_columnconfigure(0, weight=1)
        content_window = page_canvas.create_window(
            (0, 0), window=content, anchor="nw"
        )
        content.bind(
            "<Configure>",
            lambda _event: page_canvas.configure(
                scrollregion=page_canvas.bbox("all")
            ),
        )
        page_canvas.bind(
            "<Configure>",
            lambda event: page_canvas.itemconfigure(
                content_window, width=event.width
            ),
        )
        state = "开启" if self.config.get("enabled", True) else "暂停"
        self.status_detail_var = tk.StringVar(value="处理完成：等待消息")
        header_panel = tk.Frame(
            content,
            bg=UI_COLORS["surface"],
            padx=14,
            pady=9,
            highlightthickness=1,
            highlightbackground=UI_COLORS["border"],
        )
        header_panel.grid(row=0, column=0, sticky="ew", padx=30, pady=(14, 6))
        header_panel.grid_columnconfigure(0, weight=1)
        tk.Label(
            header_panel,
            text=f"微信自动回复运行中（版本 {APP_VERSION}）",
            font=("Microsoft YaHei UI", 15, "bold"),
            fg=UI_COLORS["accent"],
            bg=UI_COLORS["surface"],
            anchor="w",
        ).pack(anchor="w")
        self.header_status_var = tk.StringVar(
            value=(
                f"状态：{state}    PID：{os.getpid()}    "
                f"模型：{self.engine.model_name}（{self.engine.reasoning_effort}）"
            )
        )
        status_row = tk.Frame(header_panel, bg=UI_COLORS["surface"])
        status_row.pack(fill="x", pady=(5, 0))
        tk.Label(
            status_row,
            textvariable=self.header_status_var,
            font=("Microsoft YaHei UI", 10),
            bg=UI_COLORS["surface"],
            fg=UI_COLORS["muted"],
        ).pack(side="left")
        self.global_auto_reply_var = tk.BooleanVar(
            value=bool(self.config.get("enabled", True))
        )
        tk.Checkbutton(
            status_row,
            text="自动回复总开关",
            variable=self.global_auto_reply_var,
            command=lambda: self._set_global_auto_reply_enabled(
                self.global_auto_reply_var.get()
            ),
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left", padx=(14, 0))

        model_frame = tk.Frame(content)
        model_frame.grid(row=2, column=0, sticky="w", padx=32, pady=(6, 2))
        model_row = tk.Frame(model_frame)
        model_row.pack(anchor="w")
        tk.Label(
            model_row,
            text="回复模型：",
            font=("Microsoft YaHei UI", 10),
        ).pack(side="left")
        current_model_label = f"{self.engine.model_name}（当前设置，未核实）"
        model_labels = [current_model_label]
        self.model_choice_var = tk.StringVar(value=current_model_label)
        self.model_combo = ttk.Combobox(
            model_row,
            textvariable=self.model_choice_var,
            values=model_labels,
            state="readonly",
            width=24,
        )
        self.model_combo.pack(side="left")
        self.model_combo.bind("<<ComboboxSelected>>", lambda _event: self._sync_reasoning_choices())
        self.model_apply_button = tk.Button(
            model_row,
            text="应用模型",
            command=self._apply_model_selection,
            width=10,
            state="disabled",
        )
        self.model_apply_button.pack(side="left", padx=(8, 0))

        reasoning_row = tk.Frame(model_frame)
        reasoning_row.pack(anchor="w", pady=(4, 0))
        tk.Label(
            reasoning_row,
            text="模型强度：",
            font=("Microsoft YaHei UI", 10),
        ).pack(side="left")
        current_effort_label = REASONING_EFFORT_LABEL_BY_ID.get(
            self.engine.reasoning_effort,
            self.engine.reasoning_effort,
        )
        self.reasoning_choice_var = tk.StringVar(value=current_effort_label)
        self.reasoning_combo = ttk.Combobox(
            reasoning_row,
            textvariable=self.reasoning_choice_var,
            values=list(REASONING_EFFORT_OPTIONS),
            state="readonly",
            width=13,
        )
        self.reasoning_combo.pack(side="left")
        self.reasoning_apply_button = tk.Button(
            reasoning_row,
            text="应用强度",
            command=self._apply_reasoning_effort_selection,
            width=10,
            state="disabled",
        )
        self.reasoning_apply_button.pack(side="left", padx=(8, 10))
        tk.Label(
            reasoning_row,
            text="低档更快，高档思考更多；仅影响之后新回复。",
            font=("Microsoft YaHei UI", 9),
            fg="#555555",
        ).pack(side="left")
        tk.Label(
            model_row,
            text="模型须有当前 Codex 账户权限。",
            font=("Microsoft YaHei UI", 9),
            fg="#555555",
        ).pack(side="left", padx=(10, 0))

        codex_row = tk.Frame(model_frame)
        codex_row.pack(anchor="w", pady=(5, 0))
        tk.Button(
            codex_row, text="选择 Codex 程序…",
            command=self._select_codex_executable,
        ).pack(side="left")
        tk.Button(
            codex_row, text="恢复自动查找",
            command=lambda: self._save_codex_executable(""),
        ).pack(side="left", padx=(8, 0))
        self.codex_path_status_var = tk.StringVar(
            value="已指定本机程序位置" if self.config.get("codex_executable") else "自动查找本机 Codex CLI"
        )
        tk.Label(codex_row, textvariable=self.codex_path_status_var, fg="#555555").pack(side="left", padx=(10, 0))
        catalog_row = tk.Frame(model_frame)
        catalog_row.pack(anchor="w", pady=(5, 0))
        self.catalog_refresh_button = tk.Button(
            catalog_row, text="刷新模型列表", command=self._refresh_codex_catalog,
        )
        self.catalog_refresh_button.pack(side="left", padx=(8, 0))
        self.cli_catalog_status_var = tk.StringVar(value="CLI 版本与模型列表尚未查询；原模型设置暂时保留。")
        tk.Label(model_frame, textvariable=self.cli_catalog_status_var, justify="left", wraplength=540, fg="#555555").pack(anchor="w", pady=(4, 0))

        cancel_row = tk.Frame(model_frame)
        cancel_row.pack(anchor="w", pady=(5, 0))
        tk.Label(
            cancel_row,
            text="取消回复条件：",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")
        self.cancel_keyboard_var = tk.BooleanVar(
            value=bool(self.config.get("cancel_on_keyboard_input", True))
        )
        tk.Checkbutton(
            cancel_row,
            text="检测到键盘输入时取消",
            variable=self.cancel_keyboard_var,
            command=lambda: self._save_cancel_toggle(
                "cancel_on_keyboard_input", self.cancel_keyboard_var, "键盘输入"
            ),
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(side="left")
        self.cancel_mouse_var = tk.BooleanVar(
            value=bool(self.config.get("cancel_on_mouse_move", True))
        )
        tk.Checkbutton(
            cancel_row,
            text="鼠标移动超过",
            variable=self.cancel_mouse_var,
            command=lambda: self._save_cancel_toggle(
                "cancel_on_mouse_move", self.cancel_mouse_var, "鼠标移动"
            ),
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(side="left", padx=(8, 0))
        mouse_threshold_entry = tk.Entry(cancel_row, width=6, font=("Microsoft YaHei UI", 9))
        mouse_threshold = self.config.get(
            "mouse_move_threshold_px", DEFAULT_MOUSE_MOVE_THRESHOLD_PX
        )
        mouse_threshold_entry.insert("0", str(mouse_threshold))
        mouse_threshold_entry.pack(side="left", padx=(3, 0))
        mouse_threshold_entry.bind(
            "<FocusOut>", lambda _event: self._save_mouse_threshold_from_widget()
        )
        mouse_threshold_entry.bind(
            "<Return>", lambda _event: self._save_mouse_threshold_from_widget()
        )
        self.mouse_threshold_widget = mouse_threshold_entry
        tk.Label(
            cancel_row,
            text="像素时取消",
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(3, 0))
        tk.Button(
            cancel_row,
            text="保存像素",
            command=self._save_mouse_threshold_from_widget,
            width=9,
        ).pack(side="left", padx=(6, 0))

        timeout_notice_row = tk.Frame(model_frame)
        timeout_notice_row.pack(anchor="w", pady=(4, 0))
        self.confirm_timeout_notify_var = tk.BooleanVar(
            value=bool(self.config.get("confirm_timeout_notify_small_account", True))
        )
        tk.Checkbutton(
            timeout_notice_row,
            text="敏感确认超时后通知授权联系人",
            variable=self.confirm_timeout_notify_var,
            command=self._save_confirmation_timeout_notice_toggle,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(side="left")
        tk.Label(
            timeout_notice_row,
            text="关闭后只保留本机确认窗口，不会自动发送",
            font=("Microsoft YaHei UI", 9),
            fg="#555555",
        ).pack(side="left", padx=(6, 0))

        def _make_collapsible_section(parent, title: str, *, padx: int = 8, pady: int = 5):
            section = tk.Frame(
                parent,
                bg=UI_COLORS["surface"],
                borderwidth=0,
                relief="solid",
                highlightthickness=1,
                highlightbackground=UI_COLORS["border"],
            )
            section.grid_columnconfigure(0, weight=1)
            body = tk.Frame(section, bg=UI_COLORS["surface"])
            body.grid(row=1, column=0, sticky="ew", padx=padx, pady=pady)
            expanded = {"value": True}

            def toggle_section():
                if expanded["value"]:
                    body.grid_remove()
                    header.configure(text=f"▶ {title}")
                    expanded["value"] = False
                else:
                    body.grid()
                    header.configure(text=f"▼ {title}")
                    expanded["value"] = True
                parent.update_idletasks()

            header = tk.Button(
                section,
                text=f"▼ {title}",
                command=toggle_section,
                font=("Microsoft YaHei UI", 9, "bold"),
                anchor="w",
                relief="flat",
                borderwidth=0,
                highlightthickness=0,
                padx=3,
                pady=5,
                bg=UI_COLORS["surface"],
                fg=UI_COLORS["text"],
                activebackground=UI_COLORS["accent_soft"],
                activeforeground=UI_COLORS["accent_hover"],
                cursor="hand2",
            )
            header.grid(row=0, column=0, sticky="ew", padx=3, pady=(2, 0))
            return section, body

        test_section, test_frame = _make_collapsible_section(
            content, "固定内容测试", padx=8, pady=5
        )
        test_section.grid(row=3, column=0, sticky="ew", padx=30, pady=(6, 2))
        test_frame.grid_columnconfigure(1, weight=1)
        tk.Label(test_frame, text="测试联系人：", font=("Microsoft YaHei UI", 9)).grid(
            row=0, column=0, sticky="w", padx=(0, 4)
        )
        self.test_reply_target_labels = self._build_test_reply_target_labels()
        test_target_choices = list(self.test_reply_target_labels)
        command_contact = str(self.config.get("command_contact") or "测试账号")
        default_test_label = next(
            (
                label
                for label, target_name in self.test_reply_target_labels.items()
                if target_name == command_contact
            ),
            test_target_choices[0] if test_target_choices else "",
        )
        self.test_reply_target_var = tk.StringVar(value=default_test_label)
        test_contact_combo = ttk.Combobox(
            test_frame,
            textvariable=self.test_reply_target_var,
            values=test_target_choices,
            state="readonly" if test_target_choices else "disabled",
            width=34,
        )
        test_contact_combo.grid(row=0, column=1, columnspan=2, sticky="ew")
        tk.Label(test_frame, text="固定回复内容：", font=("Microsoft YaHei UI", 9)).grid(
            row=1, column=0, sticky="w", padx=(0, 4), pady=(4, 0)
        )
        self.test_reply_content_widget = tk.Entry(test_frame, width=46, font=("Microsoft YaHei UI", 9))
        self.test_reply_content_widget.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(4, 0))
        test_actions = tk.Frame(test_frame)
        test_actions.grid(row=1, column=2, sticky="e", pady=(4, 0))
        self.test_reply_button = tk.Button(
            test_actions,
            text="开始测试",
            command=self._start_fixed_reply_test,
            width=12,
            state="normal" if test_target_choices else "disabled",
            bg=UI_COLORS["accent"],
            fg=UI_COLORS["surface"],
            activebackground=UI_COLORS["accent_hover"],
            activeforeground=UI_COLORS["surface"],
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=10,
            pady=5,
            cursor="hand2",
        )
        self.test_reply_button.grid(row=0, column=0, sticky="ew")
        self.test_search_button = tk.Button(
            test_actions,
            text="使用搜索查找测试",
            command=self._start_search_contact_test,
            width=16,
            state="normal" if test_target_choices else "disabled",
            bg=UI_COLORS["accent_soft"],
            fg=UI_COLORS["accent_hover"],
            activebackground="#D4EBDD",
            activeforeground=UI_COLORS["accent_hover"],
            padx=10,
            pady=5,
            cursor="hand2",
        )
        self.test_search_button.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        self.test_reply_send_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            test_frame,
            text="是否发送消息",
            variable=self.test_reply_send_var,
            font=("Microsoft YaHei UI", 9),
        ).grid(row=2, column=0, sticky="w", pady=(3, 0))
        tk.Label(
            test_frame,
            text="开始测试优先查聊天列表；搜索查找测试直接使用搜索框。勾选发送，否则只填入输入框，不调用模型。",
            font=("Microsoft YaHei UI", 8),
            fg="#555555",
            anchor="w",
        ).grid(row=2, column=1, columnspan=2, sticky="ew", pady=(3, 0))

        command_section, command_frame = _make_collapsible_section(
            content, "# 指令设置", padx=8, pady=4
        )
        command_section.grid(row=4, column=0, sticky="ew", padx=30, pady=(5, 2))
        command_frame.grid_columnconfigure(0, weight=1)
        command_options = tk.Frame(command_frame)
        command_options.grid(row=0, column=0, sticky="ew")
        self.command_channel_var = tk.BooleanVar(
            value=bool(self.config.get("command_channel_enabled", True))
        )
        tk.Checkbutton(
            command_options,
            text="启用 # 指令通道",
            variable=self.command_channel_var,
            command=lambda: self._set_command_channel_enabled(
                self.command_channel_var.get()
            ),
            font=("Microsoft YaHei UI", 9, "bold"),
            anchor="w",
        ).pack(side="left")
        self.codex_command_var = tk.BooleanVar(
            value=bool(self.config.get("codex_command_enabled", True))
        )
        tk.Checkbutton(
            command_options,
            text="允许执行 Codex 指令",
            variable=self.codex_command_var,
            command=lambda: self._set_codex_command_enabled(
                self.codex_command_var.get()
            ),
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(side="left", padx=(12, 0))

        password_row = tk.Frame(command_frame)
        password_row.grid(row=1, column=0, sticky="ew", pady=(2, 1))
        tk.Label(
            password_row,
            text="# 指令统一密码：",
            font=("Microsoft YaHei UI", 9),
            fg="#444444",
        ).pack(side="left")
        self.command_password_widget = tk.Entry(
            password_row,
            width=16,
            show="*",
            font=("Microsoft YaHei UI", 9),
        )
        self.command_password_widget.insert(0, str(self.config.get("command_password") or ""))
        self.command_password_widget.pack(side="left")
        self.command_password_visibility_var = tk.BooleanVar(value=False)
        self.command_password_visibility_button = tk.Button(
            password_row,
            text="显示",
            command=self._toggle_command_password_visibility,
            width=6,
        )
        self.command_password_visibility_button.pack(side="left", padx=(4, 0))
        tk.Button(
            password_row,
            text="保存密码",
            command=self._save_command_password_from_widget,
            width=9,
        ).pack(side="left", padx=(4, 0))
        self.command_password_widget.bind(
            "<Return>", self._save_command_password_from_widget
        )

        tk.Label(
            command_frame,
            text="允许使用 # 指令的联系人（只对已配置并匹配微信账号 ID 的联系人生效）：",
            font=("Microsoft YaHei UI", 9),
            fg="#444444",
        ).grid(row=2, column=0, sticky="w", pady=(1, 0))
        command_list_outer = tk.Frame(command_frame, borderwidth=1, relief="solid")
        command_list_outer.grid(row=3, column=0, sticky="ew", pady=(1, 0))
        command_list_outer.grid_columnconfigure(0, weight=1)
        command_list_outer.grid_rowconfigure(0, weight=1)
        command_canvas = tk.Canvas(
            command_list_outer, height=72, highlightthickness=0
        )
        command_scrollbar = ttk.Scrollbar(
            command_list_outer, orient="vertical", command=command_canvas.yview
        )
        command_contacts_frame = tk.Frame(command_canvas)
        command_contacts_frame.bind(
            "<Configure>",
            lambda _event: command_canvas.configure(
                scrollregion=command_canvas.bbox("all")
            ),
        )
        command_canvas_window = command_canvas.create_window(
            (0, 0), window=command_contacts_frame, anchor="nw"
        )
        command_canvas.configure(yscrollcommand=command_scrollbar.set)
        command_canvas.grid(row=0, column=0, sticky="ew")
        command_scrollbar.grid(row=0, column=1, sticky="ns")
        command_canvas.bind(
            "<Configure>",
            lambda event: command_canvas.itemconfigure(
                command_canvas_window, width=event.width
            ),
        )
        for index, target in enumerate(self.config.get("targets", [])):
            name = str(target.get("name") or "").strip()
            if not name:
                continue
            ui_name = str(
                self.bridge.ui_names.get(name) or target.get("ui_name") or name
            ).strip()
            label = ui_name or name
            command_var = tk.BooleanVar(
                value=bool(target.get("command_enabled", False))
            )
            self.target_command_vars[name] = command_var
            command_checkbox = tk.Checkbutton(
                command_contacts_frame,
                text=label,
                variable=command_var,
                command=lambda target_name=name, variable=command_var: self._set_command_enabled(
                    target_name, variable.get()
                ),
                font=("Microsoft YaHei UI", 9),
                anchor="w",
            )
            command_checkbox.grid(
                row=index // 2, column=index % 2, sticky="w", padx=(3, 10)
            )
        command_contacts_frame.grid_columnconfigure(0, weight=1)
        command_contacts_frame.grid_columnconfigure(1, weight=1)
        tk.Label(
            command_frame,
            text="格式：#密码+指令；勾选的联系人可远程执行指令或控制自动回复，请只选可信联系人。",
            font=("Microsoft YaHei UI", 8),
            fg="#666666",
        ).grid(row=4, column=0, sticky="w", pady=(1, 0))

        contacts_section, contacts_body = _make_collapsible_section(
            content, "近期联系人设置", padx=4, pady=4
        )
        contacts_section.grid(row=5, column=0, sticky="ew", padx=30, pady=(6, 6))
        list_outer = tk.Frame(contacts_body, borderwidth=1, relief="solid")
        list_outer.pack(fill="both", expand=True)
        list_outer.grid_columnconfigure(0, weight=1)
        list_outer.grid_rowconfigure(0, weight=1)
        canvas = tk.Canvas(list_outer, height=300, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_outer, orient="vertical", command=canvas.yview)
        contacts_frame = tk.Frame(canvas)
        contacts_frame.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas_window = canvas.create_window((0, 0), window=contacts_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        def _resize_canvas_window(event):
            canvas.itemconfigure(canvas_window, width=event.width)

        canvas.bind("<Configure>", _resize_canvas_window)
        if not self.config["targets"]:
            tk.Label(
                contacts_frame,
                text="尚无联系人。关闭程序后，从开始菜单选择“重新配置微信与联系人”，\n"
                     "再选择“保存并读取近期联系人”。只显示本机近期个人会话，群聊和公众号不列入。",
                justify="left", wraplength=480, padx=12, pady=14,
            ).pack(anchor="w", fill="x")
        for target in self.config["targets"]:
            name = target["name"]
            row = tk.Frame(contacts_frame, borderwidth=1, relief="groove", padx=6, pady=4)
            row.pack(anchor="w", fill="x", padx=6, pady=4)
            row.grid_columnconfigure(0, weight=1)

            controls = tk.Frame(row)
            controls.grid(row=0, column=0, sticky="ew")

            listen_var = tk.BooleanVar(value=self._target_listen_enabled(name))
            self.target_listen_vars[name] = listen_var
            tk.Checkbutton(
                controls,
                text="监听",
                variable=listen_var,
                command=lambda target_name=name, variable=listen_var: self._set_target_listen(target_name, variable.get()),
                font=("Microsoft YaHei UI", 10),
                anchor="w",
            ).pack(side="left", anchor="w")

            auto_var = tk.BooleanVar(value=bool(target.get("auto_reply_enabled", True)))
            self.target_auto_reply_vars[name] = auto_var
            ui_name = str(target.get("ui_name") or name)
            auto_reply_label = f"自动回复：{ui_name or name}"
            tk.Checkbutton(
                controls,
                text=auto_reply_label,
                variable=auto_var,
                command=lambda target_name=name, variable=auto_var: self._set_target_auto_reply(target_name, variable.get()),
                font=("Microsoft YaHei UI", 10),
                anchor="w",
            ).pack(side="left", anchor="w", padx=(14, 0))

            style_var = tk.BooleanVar(value=bool(target.get("use_style_rules", True)))
            self.target_style_rule_vars[name] = style_var
            tk.Checkbutton(
                controls,
                text="使用风格规则",
                variable=style_var,
                command=lambda target_name=name, variable=style_var: self._set_target_style_rules(target_name, variable.get()),
                font=("Microsoft YaHei UI", 10),
                anchor="w",
            ).pack(side="left", anchor="w", padx=(14, 0))

            wait_row = 1
            wait_frame = tk.Frame(row)
            wait_frame.grid(row=wait_row, column=0, sticky="w", pady=(3, 0))
            tk.Label(
                wait_frame,
                text="等待秒数：",
                font=("Microsoft YaHei UI", 9),
                fg="#444444",
            ).pack(side="left")
            wait_entry = tk.Entry(wait_frame, width=8, font=("Microsoft YaHei UI", 9))
            wait_entry.insert("0", str(target.get("wait_seconds", self.config.get("initial_reply_wait_seconds", 10.0))))
            wait_entry.pack(side="left")
            self.target_wait_widgets[name] = wait_entry
            tk.Button(
                wait_frame,
                text="保存等待",
                command=lambda target_name=name: self._save_target_wait_from_widget(target_name),
                width=10,
            ).pack(side="left", padx=(8, 0))
            wait_entry.bind("<FocusOut>", lambda _event, target_name=name: self._save_target_wait_from_widget(target_name))
            wait_entry.bind("<Control-s>", lambda _event, target_name=name: self._save_target_wait_from_widget(target_name))

            tk.Button(
                row,
                text="修改自定义风格",
                command=lambda target_name=name: self._toggle_profile_panel(target_name),
                width=16,
            ).grid(row=wait_row + 1, column=0, sticky="e", pady=(3, 0))
            profile_panel = tk.Frame(row)
            self.target_profile_panels[name] = profile_panel
            self.target_profile_panel_rows[name] = wait_row + 2
            tk.Label(
                profile_panel,
                text="自定义风格：",
                font=("Microsoft YaHei UI", 9),
                fg="#444444",
            ).pack(anchor="w")
            editor_container = tk.Frame(profile_panel, bg=UI_COLORS["surface"])
            editor_container.pack(fill="x", pady=(1, 3))
            editor_container.grid_columnconfigure(0, weight=1)
            editor_container.grid_rowconfigure(0, weight=1)
            profile_scrollbar = ttk.Scrollbar(
                editor_container, orient="vertical"
            )
            editor = tk.Text(
                editor_container,
                height=3,
                wrap="word",
                font=("Microsoft YaHei UI", 9),
                undo=True,
                yscrollcommand=profile_scrollbar.set,
            )
            profile_scrollbar.configure(command=editor.yview)
            editor.insert("1.0", str(target.get("profile") or ""))
            editor.grid(row=0, column=0, sticky="nsew")
            profile_scrollbar.grid(row=0, column=1, sticky="ns")
            self.target_profile_widgets[name] = editor

            buttons = tk.Frame(profile_panel)
            buttons.pack(fill="x")
            generate_button = tk.Button(
                buttons,
                text="读取聊天生成提示词",
                command=lambda target_name=name: self._generate_target_profile_from_history(target_name),
                width=20,
            )
            generate_button.pack(side="left")
            self.target_profile_generate_buttons[name] = generate_button
            tk.Button(
                buttons,
                text="保存风格",
                command=lambda target_name=name: self._save_target_profile_from_widget(target_name),
                width=10,
            ).pack(side="right")
            editor.bind("<Control-s>", lambda _event, target_name=name: self._save_target_profile_from_widget(target_name))
        tk.Label(
            content,
            text="监听和自动回复可分开设置：只开监听不会自动回复；开启自动回复会自动开启监听；关闭监听也会关闭自动回复。风格编辑默认收起，等待时间从最后一条新消息重新计算。",
            wraplength=680,
            justify="left",
            font=("Microsoft YaHei UI", 10),
        ).grid(row=7, column=0, sticky="w", padx=32, pady=(4, 2))
        tk.Label(
            content,
            text="实时日志：接收 → 合并等待 → 生成 → 联系人校验 → 发送/处理完成",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).grid(row=8, column=0, sticky="w", padx=32, pady=(4, 2))
        log_frame = tk.Frame(
            content,
            bg=UI_COLORS["surface"],
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=UI_COLORS["border"],
        )
        log_frame.grid(row=9, column=0, sticky="nsew", padx=30, pady=(0, 6))
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(0, weight=1)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical")
        live_log = tk.Text(
            log_frame, height=8, wrap="word", font=("Consolas", 9),
            yscrollcommand=log_scroll.set, state="disabled",
            bg="#FBFCFD", fg=UI_COLORS["log_text"],
            insertbackground=UI_COLORS["accent"], relief="flat",
            padx=8, pady=6, highlightthickness=0,
        )
        log_scroll.configure(command=live_log.yview)
        live_log.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.live_log_widget = live_log
        tk.Label(
            sticky_footer,
            textvariable=self.status_detail_var,
            wraplength=500,
            justify="left",
            font=("Microsoft YaHei UI", 9),
            fg="#1f6feb",
        ).grid(row=0, column=0, sticky="ew", padx=(6, 10), pady=(2, 1))
        tk.Label(
            sticky_footer,
            text="关闭这个窗口会停止自动回复",
            font=("Microsoft YaHei UI", 9),
            fg="#666666",
        ).grid(row=1, column=0, sticky="w", padx=(6, 10), pady=(1, 2))
        stop_button = tk.Button(
            sticky_footer,
            text="停止自动回复",
            width=16,
            command=self.stop,
            bg=UI_COLORS["danger"],
            fg=UI_COLORS["surface"],
            activebackground=UI_COLORS["danger_hover"],
            activeforeground=UI_COLORS["surface"],
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=10,
            pady=6,
            cursor="hand2",
        )
        stop_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(6, 8), pady=4)

        def _on_page_mousewheel(event):
            widget = event.widget
            while widget is not None:
                if widget in (command_contacts_frame, command_canvas, command_list_outer):
                    target_canvas = command_canvas
                    break
                if widget in (contacts_frame, canvas, list_outer):
                    target_canvas = canvas
                    break
                if any(
                    widget is profile_editor
                    for profile_editor in self.target_profile_widgets.values()
                ):
                    target_canvas = widget
                    break
                if widget in (live_log, log_frame):
                    target_canvas = live_log
                    break
                widget = getattr(widget, "master", None)
            else:
                target_canvas = page_canvas

            delta = getattr(event, "delta", 0)
            steps = int(-delta / 120)
            if not steps and delta:
                steps = -1 if delta > 0 else 1
            if steps:
                target_canvas.yview_scroll(steps, "units")
            return "break"

        self.root.bind_all("<MouseWheel>", _on_page_mousewheel)

    def _build_test_reply_target_labels(self) -> dict[str, str]:
        labels: dict[str, str] = {}
        for target in self.config.get("targets", []):
            name = str(target.get("name") or "").strip()
            if not name or name not in self.bridge.targets:
                continue
            ui_name = str(
                self.bridge.ui_names.get(name) or target.get("ui_name") or ""
            ).strip()
            label = ui_name or name
            base_label = label
            suffix = 2
            while label in labels and labels[label] != name:
                label = f"{base_label} #{suffix}"
                suffix += 1
            labels[label] = name
        return labels

    def _start_fixed_reply_test(self, contact_search_only: bool = False) -> None:
        if self.test_reply_active:
            self._set_status("联系人测试正在执行，请勿重复点击")
            return

        selected_label = str(self.test_reply_target_var.get() or "").strip()
        target_name = self.test_reply_target_labels.get(selected_label)
        if not target_name or target_name not in self.bridge.targets:
            self._set_status("测试联系人无效或已失效，未发送")
            return

        content_widget = self.test_reply_content_widget
        text = content_widget.get().strip() if content_widget is not None else ""
        if not text:
            self._set_status("请先填写固定回复内容，未发送")
            if content_widget is not None:
                content_widget.focus_set()
            return

        should_send = bool(
            self.test_reply_send_var.get()
            if self.test_reply_send_var is not None else False
        )
        self.test_reply_active = True
        self._set_fixed_test_button_state("disabled")
        route = "微信搜索框" if contact_search_only else "聊天列表优先流程"
        if should_send:
            self._set_status(f"联系人测试：使用{route}定位并向 {target_name} 发送固定内容")
            logging.info("开始联系人测试：route=%s target=%s len=%d", route, target_name, len(text))
        else:
            self._set_status(f"联系人测试：使用{route}定位 {target_name} 并填入草稿，不会发送")
            logging.info("开始联系人草稿测试：route=%s target=%s len=%d", route, target_name, len(text))
        try:
            threading.Thread(
                target=self._send_fixed_reply_test,
                args=(target_name, text, should_send, contact_search_only),
                daemon=True,
                name=f"fixed-reply-test-{target_name}",
            ).start()
        except Exception as exc:
            self.test_reply_active = False
            self._set_fixed_test_button_state("normal")
            logging.exception("无法启动固定内容测试发送线程：target=%s", target_name)
            self._set_status(f"固定内容测试启动失败：{exc}")

    def _send_fixed_reply_test(
        self,
        target_name: str,
        text: str,
        should_send: bool = False,
        contact_search_only: bool = False,
    ) -> None:
        try:
            if should_send:
                # Use the regular verified single-submit flow, but force contact
                # lookup through the search box instead of scanning the chat list.
                self._send_reply_messages(
                    target_name, text, contact_search_only=contact_search_only
                )
            else:
                # The unchecked mode leaves an unsent draft in the confirmed chat.
                with self.send_lock:
                    prepare = (
                        self.bridge.prepare_by_search
                        if contact_search_only else self.bridge.prepare_message
                    )
                    prepare(target_name, text)
        except Exception as exc:
            logging.exception("固定内容测试失败：target=%s should_send=%s", target_name, should_send)
            self.events.put(("fixed_test_failed", target_name, None, None, str(exc)))
        else:
            if should_send:
                logging.info("固定内容测试已发送：target=%s len=%d", target_name, len(text))
                self.events.put(("fixed_test_sent", target_name, None, None, ""))
            else:
                logging.info("固定内容测试已填入草稿但未发送：target=%s len=%d", target_name, len(text))
                self.events.put(("fixed_test_drafted", target_name, None, None, ""))

    def _set_fixed_test_button_state(self, state: str) -> None:
        for button in (self.test_reply_button, self.test_search_button):
            if button is not None:
                button.configure(state=state)

    def _start_search_contact_test(self) -> None:
        self._start_fixed_reply_test(contact_search_only=True)

    def _select_codex_executable(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root, title="选择本机 Codex 命令行程序 codex.exe",
            filetypes=[("Codex 程序", "codex.exe")],
        )
        if not selected:
            return
        try:
            candidate = Path(selected)
            if candidate.name.lower() != "codex.exe" or not candidate.is_file():
                raise ValueError("请选择可访问的 codex.exe 文件。")
        except (OSError, ValueError):
            messagebox.showerror("程序路径不可用", "Windows 无法访问所选文件，请选择 Codex CLI 的实际 codex.exe 文件。", parent=self.root)
            return
        self._save_codex_executable(str(candidate))

    def _save_codex_executable(self, selected: str) -> None:
        previous = self.config.get("codex_executable")
        self.config["codex_executable"] = selected
        try:
            save_configuration(self.config_path, self.config)
        except OSError:
            if previous is None:
                self.config.pop("codex_executable", None)
            else:
                self.config["codex_executable"] = previous
            self._set_status("Codex 程序位置保存失败，仍使用原设置")
            return
        self.engine.codex = selected or "codex"
        self.engine.settings["codex_executable"] = selected
        self.codex_path_status_var.set("已指定本机程序位置" if selected else "自动查找本机 Codex CLI")
        self._set_status("Codex 程序位置已保存，之后的 AI 任务使用新设置；原配置已备份")
        self._refresh_codex_catalog(reset=True)

    def _refresh_codex_catalog(self, reset: bool = False) -> None:
        if self.shutting_down:
            return
        if self._catalog_busy and not reset:
            return
        self._catalog_cancel.set()
        self._catalog_cancel = threading.Event()
        cancelled = self._catalog_cancel
        self._catalog_generation += 1
        generation = self._catalog_generation
        self._catalog_busy = True
        self.model_verified = False
        self.model_apply_button.configure(state="disabled")
        self.reasoning_apply_button.configure(state="disabled")
        self.catalog_refresh_button.configure(state="disabled")
        self.cli_catalog_status_var.set("正在查询 CLI 版本、功能与模型目录，界面可以继续操作…")
        executable = str(self.engine.codex)
        workdir = self.engine.sandbox

        def query() -> None:
            try:
                result = refresh_cli_catalog(executable, workdir, cancelled)
            except Exception:
                result = {"version": "未获取", "models": [], "missing_flags": [], "login_state": "未知",
                          "catalog_error": "无法查询 CLI，请选择实际 codex.exe，确认登录和版本后刷新。"}
            self.events.put(("codex_catalog", generation, result, None, ""))

        threading.Thread(target=query, daemon=True).start()

    def _finish_codex_catalog(self, generation: int, result: dict) -> None:
        if generation != self._catalog_generation or self.shutting_down:
            return
        self._catalog_busy = False
        self.catalog_refresh_button.configure(state="normal")
        models = result.get("models") or []
        self.model_catalog = {row["id"]: row for row in models}
        self.model_options = {f"{row['name']} [{row['id']}]": row["id"] for row in models}
        missing = result.get("missing_flags") or []
        self.model_verified = bool(models) and not missing and result.get("login_state") != "未登录"
        notes = [f"CLI：{result.get('version', '未知')}；登录状态：{result.get('login_state', '未知')}。"]
        if missing:
            notes.append("CLI 版本不兼容，请升级后刷新；缺少：" + ", ".join(missing))
        if result.get("login_state") == "未登录":
            notes.append("请先在 Codex CLI 登录，再刷新。")
        if result.get("catalog_error"):
            notes.append(result["catalog_error"])
        if models:
            labels = list(self.model_options)
            current = next((label for label, slug in self.model_options.items() if slug == self.engine.model_name), None)
            if current is None:
                default = next((row["id"] for row in models if row.get("is_default")), models[0]["id"])
                current = next(label for label, slug in self.model_options.items() if slug == default)
                notes.append("原模型不在此目录，请选择并点击“应用模型”；原设置尚未改动。")
            self.model_combo.configure(values=labels)
            self.model_choice_var.set(current)
            notes.append(f"CLI 返回 {len(models)} 个文字模型；目录可能来自缓存，调用权限以实际结果为准。")
        else:
            current = f"{self.engine.model_name}（当前设置，未核实）"
            self.model_combo.configure(values=[current])
            self.model_choice_var.set(current)
            self.model_options = {}
            notes.append("保留原模型设置，不把预设模型当作可用列表。")
        self.model_apply_button.configure(state="normal" if self.model_verified else "disabled")
        self.cli_catalog_status_var.set("\n".join(notes))
        self._sync_reasoning_choices()

    def _sync_reasoning_choices(self) -> None:
        slug = self.model_options.get(self.model_choice_var.get())
        row = self.model_catalog.get(slug, {})
        efforts = row.get("efforts") or []
        self.reasoning_options = {REASONING_EFFORT_LABEL_BY_ID.get(value, value): value for value in efforts}
        if not efforts:
            label = f"{self.engine.reasoning_effort}（未核实）"
            self.reasoning_combo.configure(values=[label])
            self.reasoning_choice_var.set(label)
            self.reasoning_apply_button.configure(state="disabled")
            return
        preferred = self.engine.reasoning_effort
        if preferred not in efforts:
            preferred = row.get("default_effort")
        if preferred not in efforts:
            preferred = efforts[0]
        self.reasoning_combo.configure(values=list(self.reasoning_options))
        self.reasoning_choice_var.set(next(label for label, value in self.reasoning_options.items() if value == preferred))
        self.reasoning_apply_button.configure(state="normal" if self.model_verified else "disabled")

    def _apply_model_selection(self) -> None:
        if self.model_choice_var is None:
            return
        label = self.model_choice_var.get()
        model = self.model_options.get(label)
        if not self.model_verified or model is None:
            self._set_status("请先成功刷新 CLI 模型列表，再选择模型")
            return
        effort = self.reasoning_options.get(self.reasoning_choice_var.get())
        if model != self.engine.model_name and effort is None:
            self._set_status("CLI 未提供此模型的推理档位，请升级后刷新；模型设置未修改")
            return
        if model == self.engine.model_name and (effort is None or effort == self.engine.reasoning_effort):
            self._set_status(f"处理完成：当前模型仍为 {label}")
            return

        previous_model = self.config.get("codex_model")
        previous_effort = self.config.get("codex_reasoning_effort")
        self.config["codex_model"] = model
        if effort is not None:
            self.config["codex_reasoning_effort"] = effort
        try:
            save_configuration(self.config_path, self.config)
        except OSError:
            self.config["codex_model"] = previous_model
            if previous_effort is None:
                self.config.pop("codex_reasoning_effort", None)
            else:
                self.config["codex_reasoning_effort"] = previous_effort
            logging.exception("保存回复模型设置失败")
            self._set_status("模型设置保存失败，仍使用原模型")
            return

        self.engine.model_name = model
        self.engine.settings["codex_model"] = model
        if effort is not None:
            self.engine.reasoning_effort = effort
            self.engine.settings["codex_reasoning_effort"] = effort
        if self.header_status_var is not None:
            state = "开启" if self.config.get("enabled", True) else "暂停"
            self.header_status_var.set(
                f"状态：{state}    PID：{os.getpid()}    模型：{model}（{self.engine.reasoning_effort}）"
            )
        self._set_status(f"处理完成：之后新生成的回复将使用 {label}")
        logging.info("回复模型已切换：model=%s", model)

    def _apply_reasoning_effort_selection(self) -> None:
        if self.reasoning_choice_var is None:
            return
        label = self.reasoning_choice_var.get()
        effort = self.reasoning_options.get(label)
        if not self.model_verified or effort is None:
            self._set_status("模型强度选择无效，未更改当前设置")
            return
        if self.model_options.get(self.model_choice_var.get()) != self.engine.model_name:
            self._set_status("请先点击“应用模型”，模型与推理档位会一起保存")
            return
        if effort == self.engine.reasoning_effort:
            self._set_status(f"处理完成：当前模型强度仍为 {label}")
            return

        previous_effort = self.config.get("codex_reasoning_effort")
        self.config["codex_reasoning_effort"] = effort
        try:
            save_configuration(self.config_path, self.config)
        except OSError:
            if previous_effort is None:
                self.config.pop("codex_reasoning_effort", None)
            else:
                self.config["codex_reasoning_effort"] = previous_effort
            self.reasoning_choice_var.set(
                REASONING_EFFORT_LABEL_BY_ID.get(
                    self.engine.reasoning_effort,
                    self.engine.reasoning_effort,
                )
            )
            logging.exception("保存模型强度设置失败")
            self._set_status("模型强度保存失败，仍使用原设置")
            return

        self.engine.reasoning_effort = effort
        self.engine.settings["codex_reasoning_effort"] = effort
        if self.header_status_var is not None:
            state = "开启" if self.config.get("enabled", True) else "暂停"
            self.header_status_var.set(
                f"状态：{state}    PID：{os.getpid()}    "
                f"模型：{self.engine.model_name}（{effort}）"
            )
        self._set_status(f"处理完成：之后新生成的回复将使用模型强度 {label}")
        logging.info("回复模型强度已切换：effort=%s", effort)

    def _target_config(self, target_name: str) -> dict | None:
        for target in self.config["targets"]:
            if target.get("name") == target_name:
                return target
        return None

    def _target_auto_reply_enabled(self, target_name: str) -> bool:
        target = self._target_config(target_name)
        if target is None:
            return True
        return bool(target.get("auto_reply_enabled", True))

    def _target_listen_enabled(self, target_name: str) -> bool:
        target = self._target_config(target_name)
        if target is None:
            return True
        return bool(target.get("listen_enabled", target.get("auto_reply_enabled", True)))

    def _ensure_listen_for_auto_reply(self, target_name: str) -> None:
        target = self._target_config(target_name)
        if target is None or self._target_listen_enabled(target_name):
            return
        target["listen_enabled"] = True
        listen_var = self.target_listen_vars.get(target_name)
        if listen_var is not None:
            listen_var.set(True)
        self.bridge.skip_existing_messages([target_name])
        self.bridge.add_target_listener(target_name, self.callback_for)
        logging.info("自动回复开启时自动开启监听：target=%s", target_name)

    def _set_target_auto_reply(self, target_name: str, enabled: bool) -> None:
        target = self._target_config(target_name)
        if target is None:
            return
        if enabled:
            self._refresh_target_remarks("开启自动回复时")
        target["auto_reply_enabled"] = bool(enabled)
        if enabled:
            self._ensure_listen_for_auto_reply(target_name)
        else:
            with self.task_condition:
                self.task_condition.notify_all()
        self._persist_config()
        if enabled:
            status = f"处理完成：{target_name} 自动回复已开启（监听已开启）"
        elif self._target_listen_enabled(target_name):
            status = f"处理完成：{target_name} 自动回复已关闭，仍在监听"
        else:
            status = f"处理完成：{target_name} 自动回复已关闭"
        self._set_status(status)
        logging.info("联系人自动回复开关变更：target=%s enabled=%s", target_name, enabled)

    def _set_global_auto_reply_enabled(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        previous_value = bool(self.config.get("enabled", True))
        if enabled == previous_value:
            self._sync_global_auto_reply_control()
            return True

        self.config["enabled"] = enabled
        try:
            self._persist_config()
        except OSError:
            self.config["enabled"] = previous_value
            self._sync_global_auto_reply_control()
            logging.exception("保存自动回复总开关失败")
            self._set_status("自动回复总开关保存失败，已恢复原状态")
            return False

        if enabled:
            try:
                self._refresh_target_remarks("恢复自动回复时")
                self.bridge.skip_existing_messages(self._active_listen_targets())
            except Exception:
                logging.exception("开启自动回复总开关后的联系人/水位刷新失败")
            self._set_status("处理完成：自动回复总开关已开启；仍按各联系人的单独设置执行")
        else:
            self._invalidate_pending_reply_work()
            self._set_status("处理完成：自动回复总开关已关闭；联系人设置保留，未发送旧回复已丢弃")

        self._sync_global_auto_reply_control()
        logging.info("自动回复总开关变更：enabled=%s", enabled)
        return True

    def _sync_global_auto_reply_control(self) -> None:
        enabled = bool(self.config.get("enabled", True))
        variable = getattr(self, "global_auto_reply_var", None)
        if variable is not None:
            variable.set(enabled)
        header = getattr(self, "header_status_var", None)
        engine = getattr(self, "engine", None)
        if header is not None and engine is not None:
            state = "开启" if enabled else "暂停"
            header.set(
                f"状态：{state}    PID：{os.getpid()}    "
                f"模型：{engine.model_name}（{engine.reasoning_effort}）"
            )

    def _set_command_enabled(self, target_name: str, enabled: bool) -> None:
        target = self._target_config(target_name)
        if target is None:
            return
        previous = bool(target.get("command_enabled", False))
        target["command_enabled"] = bool(enabled)
        try:
            self._persist_config()
        except OSError:
            target["command_enabled"] = previous
            variable = self.target_command_vars.get(target_name)
            if variable is not None:
                variable.set(previous)
            logging.exception("保存 # 指令联系人设置失败：target=%s", target_name)
            self._set_status("指令联系人设置保存失败，已恢复原状态")
            return
        self._set_status(f"处理完成：{target_name} 的 # 指令={'开启' if enabled else '关闭'}")
        logging.info("联系人 # 指令开关变更：target=%s enabled=%s", target_name, enabled)

    def _set_command_channel_enabled(self, enabled: bool) -> None:
        previous = bool(self.config.get("command_channel_enabled", True))
        self.config["command_channel_enabled"] = bool(enabled)
        try:
            self._persist_config()
        except OSError:
            self.config["command_channel_enabled"] = previous
            if self.command_channel_var is not None:
                self.command_channel_var.set(previous)
            logging.exception("保存 # 指令总开关失败")
            self._set_status("# 指令总开关保存失败，已恢复原状态")
            return
        self._set_status(f"处理完成：# 指令通道{'已开启' if enabled else '已关闭'}")
        logging.info("# 指令通道总开关变更：enabled=%s", enabled)

    def _set_codex_command_enabled(self, enabled: bool) -> None:
        previous = bool(self.config.get("codex_command_enabled", True))
        self.config["codex_command_enabled"] = bool(enabled)
        try:
            self._persist_config()
        except OSError:
            self.config["codex_command_enabled"] = previous
            if self.codex_command_var is not None:
                self.codex_command_var.set(previous)
            logging.exception("保存 Codex 指令执行开关失败")
            self._set_status("Codex 指令执行开关保存失败，已恢复原状态")
            return
        self._set_status(
            f"处理完成：Codex 操作指令{'已允许' if enabled else '已禁止'}"
        )
        logging.info("Codex 指令执行开关变更：enabled=%s", enabled)

    def _toggle_command_password_visibility(self) -> None:
        widget = self.command_password_widget
        visibility = self.command_password_visibility_var
        button = self.command_password_visibility_button
        if widget is None or visibility is None or button is None:
            return
        visible = not bool(visibility.get())
        visibility.set(visible)
        widget.configure(show="" if visible else "*")
        button.configure(text="隐藏" if visible else "显示")

    def _save_command_password_from_widget(self, _event=None) -> str:
        widget = self.command_password_widget
        if widget is None:
            return "break"
        previous_password = str(self.config.get("command_password") or "")
        new_password = widget.get().strip()
        if not new_password:
            widget.delete(0, "end")
            widget.insert(0, previous_password)
            self._set_status("指令密码不能为空，已保留原密码")
            return "break"
        if new_password == previous_password:
            self._set_status("指令密码未变化")
            return "break"

        self.config["command_password"] = new_password
        try:
            self._persist_config()
        except OSError:
            self.config["command_password"] = previous_password
            widget.delete(0, "end")
            widget.insert(0, previous_password)
            logging.exception("保存控制指令密码失败")
            self._set_status("指令密码保存失败，已恢复原密码")
            return "break"

        self._set_status("指令密码已更新，之后的新指令立即使用新密码")
        logging.info("控制指令密码已更新")
        return "break"

    def _toggle_profile_panel(self, target_name: str) -> None:
        panel = self.target_profile_panels.get(target_name)
        if panel is None:
            return
        if panel.winfo_manager():
            panel.grid_remove()
        else:
            panel.grid(
                row=self.target_profile_panel_rows.get(target_name, 3),
                column=0,
                sticky="ew",
                pady=(2, 0),
            )
            self.target_profile_widgets[target_name].focus_set()

    def _set_target_listen(self, target_name: str, enabled: bool) -> None:
        target = self._target_config(target_name)
        if target is None:
            return
        target["listen_enabled"] = bool(enabled)
        if not enabled:
            with self.task_condition:
                batch = self.pending_batches.pop(target_name, None)
                if batch:
                    logging.info("监听关闭，清除待处理消息：target=%s count=%d", target_name, len(batch["items"]))
                self.task_condition.notify_all()
        if not enabled:
            target["auto_reply_enabled"] = False
            auto_var = self.target_auto_reply_vars.get(target_name)
            if auto_var is not None:
                auto_var.set(False)
        self._persist_config()
        if enabled:
            self.bridge.skip_existing_messages([target_name])
            self.bridge.add_target_listener(target_name, self.callback_for)
            auto_state = "开启" if self._target_auto_reply_enabled(target_name) else "关闭"
            self._set_status(f"处理完成：已开始监听 {target_name}；自动回复{auto_state}")
        else:
            self.bridge.remove_target_listener(target_name)
            self._set_status(f"处理完成：已停止监听 {target_name}，并关闭自动回复")
        logging.info("联系人监听开关变更：target=%s enabled=%s", target_name, enabled)

    def _active_listen_targets(self) -> list[str]:
        return [name for name in self.bridge.targets if self._target_listen_enabled(name)]

    def _set_status(self, text: str) -> None:
        if self.status_detail_var is not None:
            self.status_detail_var.set(text)

    def _poll_live_log(self) -> None:
        widget = self.live_log_widget
        if widget is None or not widget.winfo_exists():
            return
        lines = []
        while True:
            try:
                lines.append(LIVE_LOG_QUEUE.get_nowait())
            except queue.Empty:
                break
        if lines:
            widget.configure(state="normal")
            widget.insert("end", "\n".join(lines) + "\n")
            line_count = int(widget.index("end-1c").split(".")[0])
            if line_count > 500:
                widget.delete("1.0", f"{line_count - 500}.0")
            widget.see("end")
            widget.configure(state="disabled")
        self.root.after(200, self._poll_live_log)

    @staticmethod
    def _drain_queue(work_queue: queue.Queue) -> int:
        count = 0
        while True:
            try:
                work_queue.get_nowait()
                count += 1
            except queue.Empty:
                return count

    def _clear_pending_work(self) -> None:
        with self.task_condition:
            message_count = sum(len(batch["items"]) for batch in self.pending_batches.values())
            self.pending_batches.clear()
            self.shutting_down = True
            self.task_condition.notify_all()
        event_count = self._drain_queue(self.events)
        self.pending_resend = None
        pending_confirmation_count = len(self.pending_reply_confirmations)
        self.pending_reply_confirmations.clear()
        try:
            self.bridge.skip_existing_messages(list(self.bridge.targets))
        except Exception:
            logging.exception("停止时推进监听水位失败")
        logging.info(
            "关闭时清理未处理消息：messages=%d events=%d pending_resend=false pending_confirmations=%d",
            message_count, event_count, pending_confirmation_count,
        )

    def _invalidate_pending_reply_work(self) -> None:
        with self.task_condition:
            message_count = sum(len(batch["items"]) for batch in self.pending_batches.values())
            self.pending_batches.clear()
            self.reply_epoch += 1
            self.task_condition.notify_all()
        logging.info(
            "自动回复总开关关闭，清除待处理回复：messages=%d epoch=%d",
            message_count,
            self.reply_epoch,
        )

    def _complete_without_reply(self, target_name: str, msg: dict, reason: str) -> None:
        logging.info("处理完成：target=%s seq=%s reason=%s", target_name, msg.get("sort_seq"), reason)
        self.events.put(("status", target_name, msg, None, f"处理完成：{target_name}：{reason}"))

    def _set_target_style_rules(self, target_name: str, enabled: bool) -> None:
        target = self._target_config(target_name)
        if target is None:
            return
        target["use_style_rules"] = bool(enabled)
        self._persist_config()
        logging.info("联系人风格规则开关变更：target=%s enabled=%s", target_name, enabled)

    def _save_target_profile_from_widget(self, target_name: str) -> str:
        target = self._target_config(target_name)
        widget = self.target_profile_widgets.get(target_name)
        if target is None or widget is None:
            return "break"
        profile = widget.get("1.0", "end").strip()
        target["profile"] = profile
        self.profiles[target_name] = profile
        self._persist_config()
        logging.info("联系人自定义风格已保存：target=%s len=%d", target_name, len(profile))
        return "break"

    def _generate_target_profile_from_history(self, target_name: str) -> str:
        if target_name not in self.bridge.targets:
            self._set_status(f"无法生成风格：没有找到联系人会话 {target_name}")
            return "break"
        approved = messagebox.askyesno(
            "读取全部聊天并生成详细提示词",
            f"将从本机读取“{target_name}”会话的全部历史文字消息（不限条数），分批分析后生成较详细的关系与口吻规则。"
            f"聊天文字会分批发送给当前 Codex 模型（{self.engine.model_name}），可能耗时较长并消耗额外额度；图片、语音和文件内容不在本次分析范围内。"
            "生成结果只填入自定义风格框，不会自动保存或发送微信消息。\n\n确认继续吗？",
            parent=self.root,
        )
        if not approved:
            self._set_status(f"已取消为 {target_name} 生成风格提示词")
            return "break"

        button = self.target_profile_generate_buttons.get(target_name)
        if button is not None:
            button.configure(state="disabled")
        self._set_status(f"正在读取 {target_name} 的全部历史文字消息并生成详细提示词……")
        logging.info("开始生成联系人风格提示词：target=%s", target_name)
        try:
            threading.Thread(
                target=self._generate_target_profile_worker,
                args=(target_name,),
                daemon=True,
                name=f"profile-generation-{target_name}",
            ).start()
        except Exception as exc:
            if button is not None:
                button.configure(state="normal")
            logging.exception("无法启动联系人风格生成线程：target=%s", target_name)
            self._set_status(f"风格提示词生成启动失败：{exc}")
        return "break"

    def _generate_target_profile_worker(self, target_name: str) -> None:
        try:
            examples = self.bridge.style_profile_examples(target_name)
            if not examples:
                raise RuntimeError("这个联系人的本地记录中没有可用的文字消息")
            progress_callback = lambda text: self.events.put((
                "style_profile_progress", target_name, text, None, ""
            ))
            profile = self.engine.generate_style_profile(
                target_name,
                examples,
                progress_callback=progress_callback,
            )
            self.events.put((
                "style_profile_generated",
                target_name,
                {"profile": profile, "sample_count": len(examples)},
                None,
                "",
            ))
        except Exception as exc:
            logging.exception("联系人风格提示词生成失败：target=%s", target_name)
            self.events.put(("style_profile_generation_failed", target_name, None, None, str(exc)))

    def _profile_for_target(self, target_name: str) -> str:
        target = self._target_config(target_name)
        if target is None:
            return "按近期聊天自然回复。"
        if not bool(target.get("use_style_rules", True)):
            return (
                f"这是用户正在自动回复的联系人 {target_name}。不要套用任何情侣、兄弟、同学等单独风格；"
                "只用中性、简短、自然的中文回复，像普通微信短消息。不确定时少说，涉及金钱、隐私、"
                "账号、定位、见面承诺、健康、自伤或伤人时必须 requires_confirmation。"
            )
        return str(target.get("profile") or self.profiles.get(target_name) or "按近期聊天自然回复。")

    def _save_target_wait_from_widget(self, target_name: str) -> str:
        target = self._target_config(target_name)
        widget = self.target_wait_widgets.get(target_name)
        if target is None or widget is None:
            return "break"
        raw = widget.get().strip()
        try:
            wait_seconds = max(0.0, min(120.0, float(raw)))
        except ValueError:
            wait_seconds = float(self.config.get("initial_reply_wait_seconds", 10.0))
        target["wait_seconds"] = wait_seconds
        widget.delete(0, "end")
        widget.insert(0, str(wait_seconds).rstrip("0").rstrip(".") if wait_seconds % 1 else str(int(wait_seconds)))
        self._persist_config()
        self._set_status(f"处理完成：{target_name} 等待时间={wait_seconds:g}秒")
        logging.info("联系人等待时间已保存：target=%s wait_seconds=%s", target_name, wait_seconds)
        return "break"

    def _save_cancel_toggle(self, config_key: str, variable, setting_label: str) -> None:
        if variable is None:
            return
        previous_value = bool(self.config.get(config_key, True))
        enabled = bool(variable.get())
        self.config[config_key] = enabled
        try:
            self._persist_config()
        except OSError:
            self.config[config_key] = previous_value
            variable.set(previous_value)
            logging.exception("保存%s取消设置失败", setting_label)
            self._set_status(f"{setting_label}取消设置保存失败，已恢复原值")
            return
        state = "开启" if enabled else "关闭"
        self._set_status(f"处理完成：{setting_label}触发取消回复已{state}")
        logging.info("回复取消条件已更新：%s enabled=%s", setting_label, enabled)

    def _save_confirmation_timeout_notice_toggle(self) -> None:
        variable = self.confirm_timeout_notify_var
        if variable is None:
            return
        config_key = "confirm_timeout_notify_small_account"
        previous_value = bool(self.config.get(config_key, True))
        enabled = bool(variable.get())
        self.config[config_key] = enabled
        try:
            self._persist_config()
        except OSError:
            self.config[config_key] = previous_value
            variable.set(previous_value)
            logging.exception("保存敏感确认超时通知设置失败")
            self._set_status("敏感确认超时通知设置保存失败，已恢复原值")
            return
        state = "开启" if enabled else "关闭"
        self._set_status(f"处理完成：敏感确认超时后通知授权联系人已{state}")
        logging.info("敏感确认超时通知设置已更新：enabled=%s", enabled)

    def _save_mouse_threshold_from_widget(self) -> str:
        widget = self.mouse_threshold_widget
        if widget is None:
            return "break"
        raw = widget.get().strip()
        try:
            threshold = int(raw)
            if not 0 <= threshold <= 100000:
                raise ValueError
        except ValueError:
            try:
                saved_threshold = int(
                    self.config.get("mouse_move_threshold_px", DEFAULT_MOUSE_MOVE_THRESHOLD_PX)
                )
            except (TypeError, ValueError):
                saved_threshold = DEFAULT_MOUSE_MOVE_THRESHOLD_PX
            widget.delete(0, "end")
            widget.insert(0, str(saved_threshold))
            self._set_status("鼠标移动像素请输入 0 到 100000 的整数，未更改设置")
            return "break"

        previous_value = self.config.get(
            "mouse_move_threshold_px", DEFAULT_MOUSE_MOVE_THRESHOLD_PX
        )
        self.config["mouse_move_threshold_px"] = threshold
        try:
            self._persist_config()
        except OSError:
            self.config["mouse_move_threshold_px"] = previous_value
            widget.delete(0, "end")
            widget.insert(0, str(previous_value))
            logging.exception("保存鼠标移动取消阈值失败")
            self._set_status("鼠标移动像素保存失败，已恢复原值")
            return "break"

        self._set_status(f"处理完成：鼠标移动超过 {threshold} 像素时取消回复")
        logging.info("鼠标移动取消阈值已保存：threshold_px=%d", threshold)
        return "break"

    def _wait_seconds_for_target(self, target_name: str) -> float:
        target = self._target_config(target_name)
        if target is None:
            return max(0.0, float(self.config.get("initial_reply_wait_seconds", 10.0)))
        try:
            value = float(target.get("wait_seconds", self.config.get("initial_reply_wait_seconds", 10.0)))
        except (TypeError, ValueError):
            value = float(self.config.get("initial_reply_wait_seconds", 10.0))
        return max(0.0, min(120.0, value))

    def _cursor_pos(self) -> tuple[int, int]:
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        point = POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
        return int(point.x), int(point.y)

    def _mouse_moved(self, start_pos: tuple[int, int]) -> bool:
        threshold = int(
            self.config.get("mouse_move_threshold_px", DEFAULT_MOUSE_MOVE_THRESHOLD_PX)
        )
        x, y = self._cursor_pos()
        return abs(x - start_pos[0]) > threshold or abs(y - start_pos[1]) > threshold

    def _keyboard_input_detected(self) -> bool:
        if not self.config.get("cancel_on_keyboard_input", True):
            return False
        user32 = ctypes.windll.user32
        key_ranges = [
            range(0x08, 0x0E),  # Backspace, Tab, Enter
            range(0x20, 0x30),  # Space, navigation, punctuation
            range(0x30, 0x5B),  # 0-9, A-Z
            range(0x60, 0x70),  # Numpad
            range(0x70, 0x88),  # Function keys
            range(0xBA, 0xE0),  # OEM punctuation / IME keys
        ]
        for key_range in key_ranges:
            for vk in key_range:
                if user32.GetAsyncKeyState(vk) & 0x8000:
                    return True
        return False

    def _incoming_sort_seq(self, msg: dict) -> int | None:
        value = msg.get("sort_seq")
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _user_replied_after_incoming(self, target_name: str, msg: dict) -> bool | None:
        if not self.config.get("cancel_if_user_replied", True):
            return False
        if self._incoming_sort_seq(msg) is None:
            return None
        return self.bridge.has_self_reply_after(target_name, self._incoming_sort_seq(msg))

    def _cancel_reason_before_reply(
        self,
        target_name: str,
        msg: dict,
        start_mouse: tuple[int, int] | None = None,
    ) -> str:
        if not self.config.get("enabled", True):
            return "自动回复已暂停"
        task_epoch = msg.get("_reply_epoch")
        if task_epoch is not None:
            try:
                if int(task_epoch) != int(getattr(self, "reply_epoch", 0)):
                    return "自动回复暂停期间的旧任务已丢弃"
            except (TypeError, ValueError):
                return "自动回复任务状态无效，已取消发送"
        if not self._target_auto_reply_enabled(target_name):
            return "该联系人自动回复已关闭"
        if start_mouse is not None and self.config.get("cancel_on_mouse_move", True) and self._mouse_moved(start_mouse):
            return "等待期间检测到鼠标移动"
        if self._keyboard_input_detected():
            return "等待期间检测到键盘输入"
        reply_check = self._user_replied_after_incoming(target_name, msg)
        if reply_check is True:
            return "检测到你已经手动回复"
        if reply_check is None:
            return "无法确认你是否已手动回复，已取消自动发送"
        return ""

    def callback_for(self, target_name: str):
        def callback(msg: dict, listener) -> None:
            self.on_message(target_name, msg, listener)
        return callback

    def on_message(self, target_name: str, msg: dict, _listener) -> None:
        # 本机微信导出的 sender_id 不同库版本可能用 1 或 2 表示“我”。
        # 两者都跳过，否则会把自己刚发出的自动回复当成对方新消息，形成循环刷屏。
        if msg.get("sender_id") in {1, 2}:
            logging.info(
                "跳过自己发送的消息：target=%s sender=%s seq=%s local_id=%s",
                target_name, msg.get("sender_id"), msg.get("sort_seq"), msg.get("local_id"),
            )
            return
        logging.info(
            "收到消息：target=%s type=%s sender=%s seq=%s local_id=%s",
            target_name, msg.get("type"), msg.get("sender_id"),
            msg.get("sort_seq"), msg.get("local_id"),
        )
        if msg.get("type") != "文本":
            logging.info("跳过非文本消息：target=%s type=%s", target_name, msg.get("type"))
            return
        content = str(msg.get("content") or "").replace("\x00", "").strip()
        if not content:
            return
        if self._handle_command_if_any(target_name, content):
            return
        if not self._target_auto_reply_enabled(target_name):
            logging.info("处理完成：target=%s seq=%s reason=自动回复未勾选", target_name, msg.get("sort_seq"))
            self.events.put(("status", target_name, msg, None, f"处理完成：{target_name}：自动回复未勾选"))
            return
        if not self.config.get("enabled", True):
            logging.info("自动回复已暂停，跳过消息：target=%s seq=%s", target_name, msg.get("sort_seq"))
            return
        now = time.monotonic()
        wait_seconds = self._wait_seconds_for_target(target_name)
        with self.task_condition:
            batch = self.pending_batches.get(target_name)
            if batch is None:
                batch = {
                    "items": [],
                    "cursor": self._cursor_pos(),
                    "deadline": now + wait_seconds,
                    "created_at": now,
                    "reply_epoch": getattr(self, "reply_epoch", 0),
                }
                self.pending_batches[target_name] = batch
                queue_kind = "后续任务" if target_name in self.inflight_targets else "新批次"
            else:
                queue_kind = "合并消息"
            batch["items"].append((dict(msg), content))
            batch["deadline"] = now + wait_seconds
            count = len(batch["items"])
            self.task_condition.notify_all()
        self._set_status(f"{target_name}：已收到，{wait_seconds:g} 秒内有新消息会重新计时")
        logging.info(
            "%s：target=%s count=%d seq=%s wait=%.1fs",
            queue_kind, target_name, count, msg.get("sort_seq"), wait_seconds,
        )

    def _is_trusted_command_contact(self, target_name: str) -> bool:
        """信任显式配置且映射到同一稳定微信账号 ID 的联系人。"""
        target = self._target_config(target_name)
        expected_username = str((target or {}).get("username") or "").strip()
        resolved_username = str(
            getattr(self.bridge, "targets", {}).get(target_name) or ""
        ).strip()
        if not expected_username or expected_username != resolved_username:
            return False
        matching_targets = [
            str(item.get("name") or "").strip()
            for item in self.config.get("targets", [])
            if str(item.get("username") or "").strip() == expected_username
        ]
        return bool(
            target
            and target_name in matching_targets
            and len(matching_targets) == 1
        )

    def _is_authorized_command_contact(self, target_name: str) -> bool:
        target = self._target_config(target_name)
        return bool(
            self.config.get("command_channel_enabled", True)
            and target
            and bool(target.get("command_enabled", False))
            and self._is_trusted_command_contact(target_name)
        )

    def _command_response_contact(self) -> str | None:
        """Use the preferred command contact, then another explicitly allowed one."""
        candidates = [
            str(self.config.get("resend_confirmation_contact") or "").strip(),
            str(self.config.get("command_contact") or "").strip(),
            *[
                str(target.get("name") or "").strip()
                for target in self.config.get("targets", [])
            ],
        ]
        seen = set()
        for target_name in candidates:
            if target_name in seen:
                continue
            seen.add(target_name)
            if target_name and self._is_authorized_command_contact(target_name):
                return target_name
        return None

    def _handle_command_if_any(self, target_name: str, content: str) -> bool:
        if not content.startswith("#"):
            return False
        if not self.config.get("command_channel_enabled", True):
            logging.info("# 指令通道总开关已关闭，忽略指令：target=%s", target_name)
            return True

        target = self._target_config(target_name)
        if target is None or not bool(target.get("command_enabled", False)):
            logging.info("未授权联系人发送的 # 消息按普通聊天处理：target=%s", target_name)
            return False

        if not self._is_trusted_command_contact(target_name):
            logging.error("拒绝未绑定到可信微信账号的开发指令：target=%s", target_name)
            return True
        password, sep, instruction = content[1:].partition("+")
        if not sep or not password.strip() or not instruction.strip():
            logging.info("收到疑似控制指令但格式不完整，已忽略：target=%s", target_name)
            return True
        configured_password = str(self.config.get("command_password") or "")
        if not configured_password:
            logging.warning("收到控制指令但 command_password 尚未配置，已忽略")
            return True
        if password.strip() != configured_password:
            logging.warning("控制指令密码错误，已忽略：target=%s", target_name)
            return True
        command = instruction.strip()
        logging.info("授权联系人指令已通过校验并入队：target=%s", target_name)
        try:
            self.events.put(("command", target_name, {}, None, command))
        except Exception:
            logging.exception("授权指令无法进入处理队列：target=%s", target_name)
            self._send_command_reply_async(
                target_name, "指令接收失败：未能进入处理队列，请查看本机日志。"
            )
        return True

    def _persist_config(self) -> None:
        self.config_path.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _send_command_reply(self, target_name: str, text: str) -> bool:
        # Codex results are control-plane receipts, not auto-replies to the
        # contact; deliberately bypass the manual-reply cancellation gate.
        try:
            with self.send_lock:
                self.bridge.send(target_name, text)
            logging.info("控制指令回执已发送：target=%s chars=%d", target_name, len(text))
            return True
        except Exception:
            logging.exception("控制指令回执发送失败")
            return False

    def _send_command_reply_async(self, target_name: str, text: str) -> None:
        try:
            threading.Thread(
                target=self._send_command_reply,
                args=(target_name, text),
                name="command-reply",
                daemon=True,
            ).start()
        except Exception:
            logging.exception("无法启动控制指令回执后台任务")
            # A receipt matters even if the background worker cannot be created.
            # This rare fallback may block the caller, but still makes one
            # deliberate send attempt instead of silently dropping the receipt.
            try:
                self._send_command_reply(target_name, text)
            except Exception:
                logging.exception("控制指令回执同步补发失败")

    def _handle_command(self, target_name: str, command: str) -> None:
        try:
            self._handle_command_inner(target_name, command)
        except Exception as exc:
            logging.exception("控制指令处理异常：target=%s", target_name)
            self._send_command_reply_async(
                target_name,
                f"指令处理异常，未能确认结果（{type(exc).__name__}）；请查看本机日志。",
            )

    def _handle_command_inner(self, target_name: str, command: str) -> None:
        target = self._target_config(target_name)
        if (
            not self._is_authorized_command_contact(target_name)
            or target is None
        ):
            logging.error("拒绝未授权的开发者指令事件：target=%s", target_name)
            self._send_command_reply_async(
                target_name, "指令未执行：处理前联系人授权校验未通过。"
            )
            return
        logging.info("开始处理授权联系人指令：target=%s", target_name)
        normalized = command.strip().lower()
        if normalized.startswith(("确认发送", "确认不发送")):
            match = re.fullmatch(r"(确认发送|确认不发送)\s+([0-9a-f]{6})", normalized)
            if match is None:
                self._send_command_reply_async(
                    target_name,
                    "格式：确认发送 编号，或确认不发送 编号",
                )
                return
            self._resolve_pending_reply_confirmation(
                target_name,
                match.group(2),
                approve=match.group(1) == "确认发送",
            )
            return
        confirm_resend_commands = {"是", "重发", "重新发送", "yes", "y"}
        pause_commands = {"暂停", "停", "关闭", "pause", "stop", "off"}
        resume_commands = {"继续", "开启", "打开", "resume", "start", "on"}
        status_commands = {"状态", "status"}
        reset_commands = {"水位重置", "重置水位", "跳过旧消息", "reset", "reset_watermark"}

        if normalized in confirm_resend_commands:
            self._confirm_pending_resend(target_name)
            return
        if normalized in pause_commands:
            if not self._set_global_auto_reply_enabled(False):
                self._send_command_reply_async(target_name, "暂停失败，设置未保存")
                return
            self._send_command_reply_async(target_name, "已暂停")
            return
        if normalized in resume_commands:
            if not self._set_global_auto_reply_enabled(True):
                self._send_command_reply_async(target_name, "开启失败，设置未保存")
                return
            self._send_command_reply_async(target_name, "已继续")
            return
        if normalized in status_commands:
            state = "开启" if self.config.get("enabled", True) else "暂停"
            self._send_command_reply_async(target_name, f"当前状态：{state}")
            return
        if normalized in reset_commands:
            self.bridge.skip_existing_messages(self._active_listen_targets())
            self._send_command_reply_async(target_name, "已重置水位")
            return

        if self.config.get("codex_command_enabled", True):
            try:
                threading.Thread(
                    target=self._send_command_reply,
                    args=(target_name, "已收到指令，Codex 正在处理；完成后会再发送结果。"),
                    daemon=True,
                ).start()
                threading.Thread(
                    target=self._run_codex_instruction,
                    args=(target_name, command),
                    daemon=True,
                ).start()
            except Exception:
                logging.exception("无法启动 Codex 指令后台任务：target=%s", target_name)
                self._send_command_reply_async(
                    target_name, "指令启动失败，未能交给 Codex 处理；请查看本机日志。"
                )
                return
            logging.info("授权联系人指令已启动后台执行：target=%s", target_name)
            return

        logging.warning("Codex 指令通道未开启，已忽略：%s", command)
        self._send_command_reply_async(target_name, "Codex指令通道未开启")

    def _begin_sensitive_confirmation_request(
        self,
        target_name: str,
        msg: dict,
        decision: ReplyDecision,
        reply: str,
        ui_context: dict,
    ) -> None:
        command_contact = self._command_response_contact()
        send_state = ui_context["send_state"]
        if (
            not self.config.get("command_password")
            or command_contact is None
        ):
            send_state["notifying"] = False
            ui_context["progress"].configure(
                text="没有可用的已授权指令联系人，无法远程确认；请在此窗口选择发送或不发送。"
            )
            ui_context["send_button"].configure(state="normal")
            ui_context["ignore_button"].configure(state="normal")
            logging.warning("敏感回复超时，但授权联系人确认通道不可用：target=%s", target_name)
            return

        request_id = uuid.uuid4().hex[:6].upper()
        request_key = request_id.lower()
        while request_key in self.pending_reply_confirmations:
            request_id = uuid.uuid4().hex[:6].upper()
            request_key = request_id.lower()
        self.pending_reply_confirmations[request_key] = {
            "request_id": request_id,
            "target_name": target_name,
            "msg": dict(msg),
            "reply": reply,
            "decision": decision,
            "ui_context": ui_context,
            "created_at": time.time(),
        }
        send_state["request_id"] = request_key
        send_state["notifying"] = True
        ui_context["editor"].configure(state="disabled")
        ui_context["send_button"].configure(state="disabled")
        ui_context["ignore_button"].configure(state="disabled")
        ui_context["progress"].configure(text=f"5 秒内未选择，正在向授权联系人发送确认请求（编号 {request_id}）……")

        password = str(self.config.get("command_password") or "")
        incoming = str(msg.get("content") or "").strip() or "（空消息）"
        incoming_suffix = "……（已截断）" if len(incoming) > 500 else ""
        reply_suffix = "……（已截断）" if len(reply) > 500 else ""
        prompt = (
            f"敏感回复待确认，编号：{request_id}\n"
            f"联系人：{target_name}\n"
            f"对方消息：\n{incoming[:500]}{incoming_suffix}\n"
            f"拟发送内容：\n{reply[:500]}{reply_suffix}\n"
            "这条内容不会自动发送。请明确选择：\n"
            f"发送：#{password}+确认发送 {request_id}\n"
            f"不发送：#{password}+确认不发送 {request_id}"
        )
        try:
            threading.Thread(
                target=self._send_sensitive_confirmation_notice,
                args=(command_contact, target_name, msg, prompt, request_id, ui_context),
                name=f"sensitive-confirmation-{request_id}",
                daemon=True,
            ).start()
        except Exception as exc:
            self.events.put((
                "sensitive_confirmation_notice_failed", target_name, msg, ui_context,
                f"无法启动授权联系人通知：{exc}",
            ))

    def _send_sensitive_confirmation_notice(
        self,
        command_contact: str,
        target_name: str,
        msg: dict,
        prompt: str,
        request_id: str,
        ui_context: dict,
    ) -> None:
        try:
            with self.send_lock:
                self.bridge.send(command_contact, prompt)
            logging.info("敏感回复确认请求已发送到授权联系人：target=%s request_id=%s", target_name, request_id)
            self.events.put((
                "sensitive_confirmation_notice_sent", target_name, msg, ui_context, request_id,
            ))
        except Exception as exc:
            logging.exception("发送敏感回复确认请求失败：target=%s request_id=%s", target_name, request_id)
            self.events.put((
                "sensitive_confirmation_notice_failed", target_name, msg, ui_context, str(exc),
            ))

    def _finish_sensitive_confirmation_notice(
        self,
        kind: str,
        target_name: str,
        _msg: dict,
        ui_context: dict,
        detail: str,
    ) -> None:
        send_state = ui_context["send_state"]
        send_state["notifying"] = False
        if send_state["resolved"] or send_state["sending"]:
            return
        try:
            if not ui_context["window"].winfo_exists():
                return
        except tk.TclError:
            return

        request_id = str(send_state.get("request_id") or "").upper()
        ui_context["send_button"].configure(state="normal")
        ui_context["ignore_button"].configure(state="normal")
        if kind == "sensitive_confirmation_notice_sent":
            self._set_status(f"{target_name}：已向授权联系人发送敏感回复确认，编号 {request_id}")
            ui_context["progress"].configure(
                text=(
                    f"授权联系人已收到确认请求，编号 {request_id}。"
                    "可在此窗口选择，也可在该联系人会话按提示确认发送或不发送。"
                )
            )
        else:
            self._set_status(f"{target_name}：授权联系人确认通知发送失败或状态不确定")
            ui_context["progress"].configure(
                text=(
                    f"授权联系人通知未能确认：{detail}。请先检查对应会话；"
                    "本条仍未自动发送，可在此窗口选择。"
                )
            )

    def _resolve_pending_reply_confirmation(
        self,
        command_sender: str,
        request_key: str,
        *,
        approve: bool,
    ) -> None:
        pending = self.pending_reply_confirmations.get(request_key)
        if pending is None:
            self._send_command_reply_async(command_sender, "没有这条待确认回复，或它已经处理完毕")
            return
        ui_context = pending["ui_context"]
        send_state = ui_context["send_state"]
        if send_state["sending"] or send_state["resolved"]:
            self._send_command_reply_async(command_sender, "这条确认正在处理中，请稍候")
            return

        self.pending_reply_confirmations.pop(request_key, None)
        send_state["request_id"] = None
        send_state["notifying"] = False
        send_state["resolved"] = True
        target_name = pending["target_name"]
        msg = pending["msg"]
        if not approve:
            self._set_status(f"处理完成：{target_name}：授权联系人确认不发送")
            try:
                if ui_context["window"].winfo_exists():
                    ui_context["window"].destroy()
            except tk.TclError:
                pass
            self._release_confirmation_task_slot(target_name, ui_context)
            self._send_command_reply_async(command_sender, f"已取消发送给{target_name}，编号 {pending['request_id']}")
            logging.info("授权联系人确认不发送敏感回复：target=%s request_id=%s", target_name, pending["request_id"])
            return

        reply = str(pending.get("reply") or "").strip()
        if not reply:
            self._set_status(f"处理完成：{target_name}：候选回复为空，已取消发送")
            try:
                if ui_context["window"].winfo_exists():
                    ui_context["window"].destroy()
            except tk.TclError:
                pass
            self._release_confirmation_task_slot(target_name, ui_context)
            self._send_command_reply_async(command_sender, "候选回复为空，未发送")
            return

        send_state["sending"] = True
        ui_context["send_button"].configure(state="disabled")
        ui_context["ignore_button"].configure(state="disabled")
        ui_context["progress"].configure(text="授权联系人已确认，正在检查并发送……")
        try:
            threading.Thread(
                target=self._send_confirmed_reply_background,
                args=(target_name, msg, reply, pending["decision"], ui_context),
                name=f"remote-confirm-send-{pending['request_id']}",
                daemon=True,
            ).start()
        except Exception as exc:
            send_state["sending"] = False
            send_state["resolved"] = False
            send_state["request_id"] = request_key
            pending["reply"] = reply
            self.pending_reply_confirmations[request_key] = pending
            ui_context["send_button"].configure(state="normal")
            ui_context["ignore_button"].configure(state="normal")
            ui_context["progress"].configure(text=f"无法启动发送任务：{exc}；编号仍可再次确认。")
            self._send_command_reply_async(command_sender, f"发送任务启动失败：{exc}")
            return

        self._send_command_reply_async(command_sender, f"已收到，正在发送给{target_name}（编号 {pending['request_id']}）")
        logging.info("授权联系人确认发送敏感回复：target=%s request_id=%s", target_name, pending["request_id"])

    def _resend_contact(self) -> str | None:
        return self._command_response_contact()

    def _short_error(self, exc: Exception, diagnosis: dict) -> str:
        diagnosis_error = str(diagnosis.get("error") or "").strip()
        parts = [str(exc).strip()]
        if diagnosis_error:
            parts.append(f"窗口检测：{diagnosis_error}")
        if diagnosis.get("ensure_visible") is False:
            if diagnosis.get("window_displayable") is True:
                parts.append("微信窗口仍可见，但未能切到前台，可能被其他窗口遮挡")
            else:
                parts.append("微信窗口被隐藏、最小化或无响应")
        if diagnosis.get("open_chat") is False:
            parts.append(f"无法打开会话 {diagnosis.get('ui_name')}")
        if diagnosis.get("chat_is_open") is False:
            parts.append(f"无法确认当前会话是 {diagnosis.get('ui_name')}")
        return "；".join(p for p in parts if p)[:500]

    def _ask_resend_confirmation(
        self,
        original_target: str,
        reply: str,
        exc: Exception,
        diagnosis: dict,
        sent_parts: tuple[str, ...] = (),
    ) -> None:
        self.pending_resend = {
            "target_name": original_target,
            "reply": reply,
            "error": self._short_error(exc, diagnosis),
            "created_at": time.time(),
        }
        prompt = (
            f"自动回复发送失败\n"
            f"对象：{original_target}\n"
            f"原因：{self.pending_resend['error']}\n"
            + (f"此前已确认发送：{' / '.join(sent_parts)}\n" if sent_parts else "")
            + "当前失败部分可能已经提交，请先检查聊天，再决定是否重发。\n"
            f"内容：{reply[:180]}\n"
            f"是否重发？如果要重发，回复：#{self.config.get('command_password', '密码')}+是"
        )
        resend_contact = self._resend_contact()
        logging.info("发送失败待确认重发：target=%s notify=%s", original_target, resend_contact)
        if resend_contact:
            self._send_command_reply(resend_contact, prompt)
        else:
            logging.warning("没有启用且身份匹配的 # 指令联系人，未发送远程重发确认")

    def _confirm_pending_resend(self, command_sender: str) -> None:
        if not self.pending_resend:
            self._send_command_reply_async(command_sender, "没有待重发消息")
            return
        if self.pending_resend.get("resending"):
            self._send_command_reply_async(command_sender, "这条消息正在后台重发处理中，请稍候")
            return

        pending = dict(self.pending_resend)
        operation_id = uuid.uuid4().hex
        pending["resending"] = True
        pending["resend_operation_id"] = operation_id
        self.pending_resend = pending
        target_name = str(pending["target_name"])
        self._set_status(f"正在后台重发给 {target_name}；界面可继续使用")
        logging.info("用户确认重发，已交给后台：target=%s len=%d", target_name, len(str(pending["reply"])))
        try:
            threading.Thread(
                target=self._run_pending_resend,
                args=(command_sender, pending, operation_id),
                name=f"pending-resend-{operation_id[:8]}",
                daemon=True,
            ).start()
        except Exception as exc:
            pending["resending"] = False
            pending.pop("resend_operation_id", None)
            self.pending_resend = pending
            logging.exception("无法启动确认重发后台任务：target=%s", target_name)
            self._set_status(f"重发任务未能启动：{target_name}")
            self._send_command_reply_async(
                command_sender,
                f"重发任务未能启动（{type(exc).__name__}），消息未提交；请稍后再确认。",
            )

    def _run_pending_resend(
        self,
        command_sender: str,
        pending: dict,
        operation_id: str,
    ) -> None:
        """Perform the explicitly approved retry without blocking Tk's event loop."""
        target_name = str(pending["target_name"])
        reply = str(pending["reply"])
        try:
            self._send_reply_messages(target_name, reply)
        except Exception as exc:
            diagnosis = {}
            try:
                diagnosis = self.bridge.diagnose_wechat_window(target_name, recover=True)
            except Exception:
                logging.exception("确认重发失败后的微信诊断也失败：target=%s", target_name)
            failed_pending = dict(pending)
            failed_pending["reply"] = str(getattr(exc, "remaining_reply_text", reply))
            failed_pending["error"] = self._short_error(exc, diagnosis)
            failed_pending["sent_parts"] = tuple(getattr(exc, "sent_reply_parts", ()))
            logging.exception("确认重发失败：target=%s", target_name)
            self.events.put((
                "pending_resend_failed",
                command_sender,
                {"operation_id": operation_id, "pending": failed_pending},
                None,
                failed_pending["error"],
            ))
            return

        logging.info("确认重发完成：target=%s", target_name)
        self.events.put((
            "pending_resend_succeeded",
            command_sender,
            {"operation_id": operation_id, "pending": pending},
            None,
            target_name,
        ))

    def _finish_pending_resend(
        self,
        kind: str,
        command_sender: str,
        result: dict,
        detail: str,
    ) -> None:
        """Apply the background retry result on Tk's main thread."""
        operation_id = result.get("operation_id")
        result_pending = result.get("pending") or {}
        target_name = str(result_pending.get("target_name") or "联系人")
        current = self.pending_resend
        is_current_operation = bool(
            current
            and current.get("resending")
            and current.get("resend_operation_id") == operation_id
        )

        if kind == "pending_resend_succeeded":
            if is_current_operation:
                self.pending_resend = None
            else:
                logging.warning(
                    "旧待重发任务已完成，但当前待处理记录已变化：target=%s",
                    target_name,
                )
            self._set_status(f"处理完成：已重发给 {target_name}")
            receipt = f"已重发给{target_name}"
            if current and not is_current_operation:
                receipt += "；另一条待处理消息仍保留，请分别核对"
            self._send_command_reply_async(command_sender, receipt)
            return

        if is_current_operation:
            failed_pending = dict(result_pending)
            failed_pending["resending"] = False
            failed_pending.pop("resend_operation_id", None)
            self.pending_resend = failed_pending
        else:
            logging.warning(
                "旧待重发任务失败，未覆盖当前待处理记录：target=%s",
                target_name,
            )
        self._set_status(f"重发失败：{target_name}；未自动再次提交，请先核对微信")
        sent_parts = tuple(result_pending.get("sent_parts") or ())
        receipt = (
            (f"此前已确认发送：{' / '.join(map(str, sent_parts))}\n" if sent_parts else "")
            + f"重发失败（未自动再次提交）：{detail}\n"
            + "请先检查微信会话；确认未发送后，再回复："
            + f"#{self.config.get('command_password', '密码')}+是"
        )
        if current and not is_current_operation:
            receipt += "\n当前待处理记录已变化，请同时核对本机日志。"
        self._send_command_reply_async(command_sender, receipt)

    def _run_codex_instruction(self, target_name: str, instruction: str) -> None:
        outcome = "未完成"
        return_code = None
        reply = "Codex 未能产生执行结果。"

        def _as_text(value) -> str:
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace").strip()
            return str(value or "").strip()

        try:
            target = self._target_config(target_name)
            if (
                not self._is_authorized_command_contact(target_name)
                or target is None
                or not self.config.get("codex_command_enabled", True)
            ):
                outcome = "拒绝"
                reply = "Codex 指令未执行：联系人授权或 Codex 开关已变化。"
                logging.error("拒绝启动未授权或已关闭的 Codex 指令：target=%s", target_name)
            else:
                workdir = Path(str(self.config.get("codex_command_workdir") or ROOT)).resolve()
                timeout = int(self.config.get("codex_command_timeout_seconds", 600))
                prompt = (
                    "你是通过用户在指令设置中授权的微信联系人发来的指令运行的 Codex。\n"
                    f"工作目录：{workdir}\n"
                    "请执行用户指令并给出简短结果。除非指令明确要求，不要发送微信消息，也不要改动无关文件。\n\n"
                    f"用户指令：{instruction}"
                )
                codex_executable = str(self.config.get("codex_executable") or "codex")
                cmd = [
                    codex_executable, "exec",
                    "--cd", str(workdir),
                    "--sandbox", "workspace-write",
                    "--skip-git-repo-check",
                    prompt,
                ]
                logging.info("开始执行 Codex 指令：target=%s executable=%s", target_name, codex_executable)
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                result = run_codex(
                    cmd[1:], executable=codex_executable,
                    cwd=str(workdir),
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    encoding="utf-8",
                    errors="replace",
                    startupinfo=startupinfo,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                return_code = int(result.returncode)
                output = _as_text(result.stdout)
                error = _as_text(result.stderr)
                if return_code == 0:
                    outcome = "成功"
                    detail = output or error or "Codex 已结束，但没有返回文本。"
                    reply = f"Codex 执行完成（退出码 0）：\n{detail}"
                else:
                    outcome = "失败"
                    details = []
                    if output:
                        details.append(f"输出：\n{output}")
                    if error:
                        details.append(f"错误信息：\n{error}")
                    reply = (
                        f"Codex 执行失败（退出码 {return_code}）。\n"
                        + ("\n".join(details) if details else "Codex 未提供错误详情。")
                    )
                logging.info("Codex 指令执行结束：returncode=%s", return_code)
        except subprocess.TimeoutExpired as exc:
            outcome = "超时"
            timeout = int(self.config.get("codex_command_timeout_seconds", 600))
            partial_output = _as_text(exc.stdout)
            partial_error = _as_text(exc.stderr)
            details = []
            if partial_output:
                details.append(f"已输出内容：\n{partial_output}")
            if partial_error:
                details.append(f"错误信息：\n{partial_error}")
            reply = (
                f"Codex 执行超时（{timeout} 秒），未获得最终退出状态。\n"
                + ("\n".join(details) if details else "没有可回传的部分输出。")
            )
            logging.exception("Codex 指令执行超时")
        except Exception as exc:
            outcome = "异常"
            reply = f"Codex 执行失败（{type(exc).__name__}）：{exc}"
            logging.exception("Codex 指令执行失败")
        finally:
            # Always attempt a final result for a Codex command, including
            # authorization changes, launch errors, non-zero exits and timeouts.
            max_chars = 1800
            if len(reply) > max_chars:
                keep = (max_chars - len("\n…中间输出已省略…\n")) // 2
                reply = reply[:keep] + "\n…中间输出已省略…\n" + reply[-keep:]
            logging.info(
                "Codex 最终结果已生成：target=%s outcome=%s returncode=%s chars=%d",
                target_name, outcome, return_code, len(reply),
            )
            try:
                delivered = self._send_command_reply(target_name, reply)
            except Exception:
                delivered = False
                logging.exception("Codex 最终结果发送调用异常：target=%s", target_name)
            if delivered is False:
                logging.error("Codex 最终结果发送失败：target=%s", target_name)
            else:
                logging.info("Codex 最终结果发送流程结束：target=%s", target_name)

    def _cancel_reason_during_wait(self, target_name: str, batch: dict) -> str:
        if not self.config.get("enabled", True):
            return "自动回复已暂停"
        if not self._target_auto_reply_enabled(target_name):
            return "该联系人自动回复已关闭"
        if self.config.get("cancel_on_mouse_move", True) and self._mouse_moved(batch["cursor"]):
            return "等待期间检测到鼠标移动"
        if self._keyboard_input_detected():
            return "等待期间检测到键盘输入"
        return ""

    def _release_target_task(self, target_name: str) -> None:
        with self.task_condition:
            self.inflight_targets.discard(target_name)
            self.task_condition.notify_all()

    def _release_confirmation_task_slot(self, target_name: str, ui_context: dict) -> None:
        """Release a confirmed-message dialog's queue slot at most once."""
        send_state = ui_context.get("send_state", {})
        if send_state.get("task_slot_released", False):
            return
        send_state["task_slot_released"] = True
        self._release_target_task(target_name)

    def _filter_answered_messages(self, target_name: str, items: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
        unanswered = []
        for incoming_msg, incoming_text in items:
            reply_check = self._user_replied_after_incoming(target_name, incoming_msg)
            if reply_check is True:
                self._complete_without_reply(target_name, incoming_msg, "你已回复，这条不再补回")
            elif reply_check is None:
                self._complete_without_reply(target_name, incoming_msg, "无法确认手动回复状态，已取消自动回复")
            else:
                unanswered.append((incoming_msg, incoming_text))
        return unanswered

    def _message_worker(self) -> None:
        while True:
            target_name = ""
            batch = None
            wait_snapshot = None
            with self.task_condition:
                while not self.shutting_down:
                    now = time.monotonic()
                    eligible = [
                        (data["deadline"], name, data)
                        for name, data in self.pending_batches.items()
                        if name not in self.inflight_targets
                    ]
                    if not eligible:
                        self.task_condition.wait(timeout=0.25)
                        continue
                    deadline, target_name, candidate = min(eligible, key=lambda item: item[0])
                    remaining = deadline - now
                    if remaining > 0:
                        self.task_condition.wait(timeout=min(0.25, remaining))
                        wait_snapshot = (target_name, candidate)
                        break
                    batch = self.pending_batches.pop(target_name)
                    self.inflight_targets.add(target_name)
                    break
                if self.shutting_down:
                    return

            if wait_snapshot is not None:
                waiting_target, waiting_batch = wait_snapshot
                try:
                    reason = self._cancel_reason_during_wait(waiting_target, waiting_batch)
                except Exception as exc:
                    logging.exception("等待阶段检查失败：target=%s", waiting_target)
                    reason = f"等待检查异常：{exc}"
                if reason:
                    with self.task_condition:
                        if self.pending_batches.get(waiting_target) is waiting_batch:
                            self.pending_batches.pop(waiting_target, None)
                        else:
                            continue
                    for pending_msg, _ in waiting_batch["items"]:
                        self._complete_without_reply(waiting_target, pending_msg, reason)
                continue

            if batch is None:
                continue
            items_to_answer = []
            latest_msg = batch["items"][-1][0]
            try:
                logging.info("等待结束：target=%s count=%d，开始检查是否需要处理", target_name, len(batch["items"]))
                items_to_answer = self._filter_answered_messages(target_name, batch["items"])
                if not items_to_answer:
                    self._release_target_task(target_name)
                    continue
                combined_content = "\n".join(text for _, text in items_to_answer)
                max_chars = max(200, int(self.config.get("max_incoming_chars", 1200)))
                if len(combined_content) > max_chars:
                    logging.info(
                        "合并消息较长，保留最近内容：target=%s chars=%d limit=%d",
                        target_name, len(combined_content), max_chars,
                    )
                    combined_content = combined_content[-max_chars:]
                combined_msg = dict(items_to_answer[-1][0])
                combined_msg["content"] = combined_content
                combined_msg["_batch_count"] = len(items_to_answer)
                combined_msg["_batch_messages"] = [msg for msg, _ in items_to_answer]
                combined_msg["_reply_epoch"] = batch.get(
                    "reply_epoch", getattr(self, "reply_epoch", 0)
                )
                logging.info(
                    "开始生成合并回复：target=%s count=%d seq=%s",
                    target_name, len(items_to_answer), combined_msg.get("sort_seq"),
                )
                self._generate(target_name, combined_msg, combined_content)
            except Exception as exc:
                # 单条处理异常不能让唯一的后台消费者线程退出。
                logging.exception("自动回复工作线程处理失败：target=%s", target_name)
                self.events.put(("status", target_name, latest_msg, None, f"处理完成：{target_name}：处理异常 {exc}"))
                self._release_target_task(target_name)

    def _generate(self, target_name: str, msg: dict, content: str) -> None:
        try:
            logging.info(
                "开始生成回复：target=%s seq=%s 合并条数=%s",
                target_name, msg.get("sort_seq"), msg.get("_batch_count", 1),
            )
            context = self.bridge.recent_context(target_name,
                int(self.config.get("context_messages", 12)),
                int(self.config.get("context_char_limit", 2400)),
            )
            decision = self.engine.decide(target_name, self._profile_for_target(target_name), content, context)
            logging.info(
                "生成回复完成：target=%s seq=%s confirm=%s risks=%s len=%d",
                target_name, msg.get("sort_seq"), decision.requires_confirmation,
                ",".join(decision.risk_categories), len(decision.reply),
            )
            self.events.put(("decision", target_name, msg, decision, ""))
        except Exception as exc:
            logging.exception("生成回复失败")
            self.events.put(("manual", target_name, msg, None, f"自动生成失败：{exc}"))

    def _poll(self) -> None:
        try:
            while True:
                kind, target_name, msg, decision, reason = self.events.get_nowait()
                if kind == "codex_catalog":
                    self._finish_codex_catalog(target_name, msg)
                elif kind == "decision":
                    self._handle_decision(target_name, msg, decision)
                elif kind == "command":
                    logging.info("授权联系人指令事件已由主线程接收：target=%s", target_name)
                    self._handle_command(target_name, reason)
                elif kind in {"fixed_test_sent", "fixed_test_drafted", "fixed_test_failed"}:
                    self.test_reply_active = False
                    self._set_fixed_test_button_state("normal")
                    if kind == "fixed_test_sent":
                        self._set_status(f"处理完成：固定内容测试已发送给 {target_name}")
                    elif kind == "fixed_test_drafted":
                        self._set_status(f"处理完成：已定位并将内容填入 {target_name} 输入框；未发送")
                    else:
                        self._set_status(f"固定内容测试发送失败：{target_name}：{reason}")
                elif kind == "status":
                    self._set_status(reason)
                elif kind == "style_profile_progress":
                    self._set_status(str(msg or ""))
                elif kind in {"style_profile_generated", "style_profile_generation_failed"}:
                    button = self.target_profile_generate_buttons.get(target_name)
                    if button is not None:
                        button.configure(state="normal")
                    if kind == "style_profile_generated":
                        widget = self.target_profile_widgets.get(target_name)
                        profile = str((msg or {}).get("profile") or "").strip()
                        if widget is not None and widget.winfo_exists() and profile:
                            widget.delete("1.0", "end")
                            widget.insert("1.0", profile)
                            count = int((msg or {}).get("sample_count") or 0)
                            self._set_status(
                                f"已分析 {target_name} 的完整历史，共 {count} 条文字消息；检查提示词后点击“保存风格”生效"
                            )
                            logging.info("联系人风格提示词已生成并填入编辑框：target=%s samples=%d len=%d", target_name, count, len(profile))
                        else:
                            self._set_status(f"风格提示词已生成，但编辑框不可用：{target_name}")
                    else:
                        self._set_status(f"风格提示词生成失败：{target_name}：{reason}")
                elif kind in {"confirm_send_success", "confirm_send_cancelled", "confirm_send_failed"}:
                    self._finish_confirm_send(kind, target_name, msg, decision, reason)
                elif kind in {"sensitive_confirmation_notice_sent", "sensitive_confirmation_notice_failed"}:
                    self._finish_sensitive_confirmation_notice(kind, target_name, msg, decision, reason)
                elif kind in {"pending_resend_succeeded", "pending_resend_failed"}:
                    self._finish_pending_resend(kind, target_name, msg, reason)
                else:
                    task_epoch = msg.get("_reply_epoch")
                    try:
                        stale_task = (
                            task_epoch is not None
                            and int(task_epoch) != int(getattr(self, "reply_epoch", 0))
                        )
                    except (TypeError, ValueError):
                        stale_task = True
                    if not self.config.get("enabled", True) or stale_task:
                        logging.info(
                            "自动回复暂停或旧任务失效，跳过待确认弹窗：target=%s",
                            target_name,
                        )
                        self._set_status(f"处理完成：{target_name}：自动回复已暂停，旧任务已丢弃")
                        self._release_target_task(target_name)
                    else:
                        self._show_manual(target_name, msg, reason)
        except queue.Empty:
            pass
        self.root.after(250, self._poll)

    def _handle_decision(self, target_name: str, msg: dict, decision: ReplyDecision) -> None:
        if not self.config.get("enabled", True):
            logging.info("自动回复已暂停，丢弃已生成回复：target=%s seq=%s", target_name, msg.get("sort_seq"))
            self._release_target_task(target_name)
            return
        cancel_reason = self._cancel_reason_before_reply(target_name, msg)
        if cancel_reason:
            self._set_status(f"处理完成：{target_name}：{cancel_reason}")
            logging.info("处理完成：target=%s seq=%s reason=%s", target_name, msg.get("sort_seq"), cancel_reason)
            self._release_target_task(target_name)
            return
        if decision.requires_confirmation:
            self._confirm_dialog(target_name, msg, decision)
            return
        delay = max(0.0, float(self.config.get("send_delay_seconds", 3.0)))
        threading.Thread(target=self._delayed_send, args=(target_name, msg, decision.reply, delay), daemon=True).start()

    def _delayed_send(self, target_name: str, msg: dict, reply: str, delay: float) -> None:
        time.sleep(delay)
        try:
            cancel_reason = self._cancel_reason_before_reply(target_name, msg)
            if cancel_reason:
                logging.info("处理完成：target=%s len=%d reason=%s", target_name, len(reply), cancel_reason)
                self.events.put(("status", target_name, msg, None, f"处理完成：{target_name}：{cancel_reason}"))
                return
            logging.info("等待发送锁：target=%s len=%d", target_name, len(reply))
            logging.info("开始自动发送：target=%s bubbles=%d", target_name, len(split_reply_messages(reply)))

            def pre_submit_check() -> str | None:
                try:
                    return self._cancel_reason_before_reply(target_name, msg)
                except Exception:
                    logging.exception("发送前校验异常，取消本条自动回复：target=%s", target_name)
                    return "发送前状态校验失败，已取消自动发送"

            self._send_reply_messages(target_name, reply, pre_submit_check)
            logging.info("普通消息已自动发送：target=%s len=%d", target_name, len(reply))
        except SendCancelled as exc:
            sent_parts = tuple(getattr(exc, "sent_reply_parts", ()))
            detail = (
                f"已确认发送 {len(sent_parts)} 条，剩余内容因{exc}而取消"
                if sent_parts else str(exc)
            )
            logging.info("处理完成：target=%s len=%d reason=%s", target_name, len(reply), detail)
            self.events.put(("status", target_name, msg, None, f"处理完成：{target_name}：{detail}"))
        except Exception as exc:
            logging.exception("自动发送失败")
            window_diagnosis = self.bridge.diagnose_wechat_window(target_name, recover=True)
            logging.error("发送失败后的微信窗口检测：%s", json.dumps(window_diagnosis, ensure_ascii=False))
            retry_text = str(getattr(exc, "remaining_reply_text", reply))
            self._ask_resend_confirmation(
                target_name,
                retry_text,
                exc,
                window_diagnosis,
                tuple(getattr(exc, "sent_reply_parts", ())),
            )
            logging.error(
                "自动发送失败但不弹窗阻塞：target=%s error=%s reply=%s",
                target_name, exc, reply,
            )
        finally:
            self._release_target_task(target_name)

    def _confirm_dialog(self, target_name: str, msg: dict, decision: ReplyDecision) -> None:
        incoming = str(msg.get("content") or "")
        win = tk.Toplevel(self.root)
        win.title(f"{target_name} 回复需要确认")
        win.geometry("620x460")
        win.attributes("-topmost", True)
        tk.Label(win, text="检测到敏感/重要话题，确认后才会发送", font=("Microsoft YaHei UI", 12, "bold")).pack(pady=(14, 6))
        tk.Label(win, text=f"原因：{decision.reason or '、'.join(decision.risk_categories)}", wraplength=580, justify="left").pack(anchor="w", padx=18)
        tk.Label(win, text="对方消息：", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=18, pady=(12, 2))
        tk.Message(win, text=incoming, width=580).pack(anchor="w", padx=18)
        tk.Label(win, text="拟发送内容（可直接修改）：", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=18, pady=(12, 2))
        editor = tk.Text(win, height=8, wrap="word", font=("Microsoft YaHei UI", 11))
        editor.insert("1.0", decision.reply)
        editor.pack(fill="both", expand=True, padx=18)
        timeout_notice_enabled = bool(
            self.config.get("confirm_timeout_notify_small_account", True)
        )
        progress = tk.Label(
            win,
            text=(
                "5 秒内请选择发送或不发送；超时将通知授权联系人，不会自动发送。"
                if timeout_notice_enabled
                else "5 秒内请选择发送或不发送；超时只保留本机窗口，不会自动发送。"
            ),
            wraplength=580,
            justify="left",
            fg="#8a4b08",
        )
        progress.pack(anchor="w", padx=18, pady=(4, 0))
        send_state = {
            "sending": False,
            "failed": False,
            "resolved": False,
            "notifying": False,
            "request_id": None,
            "task_slot_released": False,
        }

        def send_now():
            if (
                send_state["sending"]
                or send_state["failed"]
                or send_state["resolved"]
                or send_state["notifying"]
            ):
                return
            text = editor.get("1.0", "end").strip()
            if not text:
                messagebox.showwarning("无法发送", "回复内容不能为空", parent=win)
                return
            request_key = send_state.get("request_id")
            if request_key:
                self.pending_reply_confirmations.pop(request_key, None)
                send_state["request_id"] = None
            send_state["resolved"] = True
            # Reply-state checks may query the local WeChat database. Keep every
            # potentially slow operation off Tk's UI thread.
            send_state["sending"] = True
            send_button.configure(state="disabled")
            ignore_button.configure(state="disabled")
            progress.configure(text="正在检查发送条件并提交到微信，请稍候……")
            ui_context = {
                "window": win,
                "progress": progress,
                "send_button": send_button,
                "ignore_button": ignore_button,
                "editor": editor,
                "send_state": send_state,
            }
            try:
                threading.Thread(
                    target=self._send_confirmed_reply_background,
                    args=(target_name, msg, text, decision, ui_context),
                    name=f"confirm-send-{target_name}",
                    daemon=True,
                ).start()
            except Exception as exc:
                send_state["sending"] = False
                send_state["resolved"] = False
                send_button.configure(state="normal")
                ignore_button.configure(state="normal")
                progress.configure(text=f"无法启动发送任务：{exc}")

        def ignore():
            if send_state["sending"] or send_state["notifying"]:
                progress.configure(text="发送正在处理中，请稍候；此时关闭可能无法阻止已提交的发送。")
                return
            if send_state["resolved"]:
                return
            request_key = send_state.get("request_id")
            if request_key:
                self.pending_reply_confirmations.pop(request_key, None)
                send_state["request_id"] = None
            send_state["resolved"] = True
            if send_state["failed"]:
                logging.warning("发送结果未能确认，用户关闭确认窗口；需手动核实：target=%s", target_name)
            else:
                logging.info("用户忽略待确认回复：target=%s", target_name)
            win.destroy()
            self._release_confirmation_task_slot(target_name, ui_context)

        win.protocol("WM_DELETE_WINDOW", ignore)

        buttons = tk.Frame(win)
        buttons.pack(pady=12)
        send_button = tk.Button(buttons, text="确认发送", command=send_now, width=14)
        send_button.pack(side="left", padx=8)
        ignore_button = tk.Button(buttons, text="不发送", command=ignore, width=14)
        ignore_button.pack(side="left", padx=8)
        ui_context = {
            "window": win,
            "progress": progress,
            "send_button": send_button,
            "ignore_button": ignore_button,
            "editor": editor,
            "send_state": send_state,
        }

        def countdown(seconds_left: int) -> None:
            if send_state["resolved"] or send_state["sending"]:
                return
            if seconds_left <= 0:
                if not bool(self.config.get("confirm_timeout_notify_small_account", True)):
                    progress.configure(
                        text="已超时；通知授权联系人已关闭。本机确认窗口会继续等待，不会自动发送。"
                    )
                    logging.info("敏感确认超时且小号通知已关闭：target=%s", target_name)
                    return
                reply = editor.get("1.0", "end").strip()
                if not reply:
                    progress.configure(text="候选回复为空，无法转交授权联系人确认；请填写内容或选择不发送。")
                    return
                self._begin_sensitive_confirmation_request(
                    target_name, msg, decision, reply, ui_context
                )
                return
            progress.configure(
                text=(
                    f"{seconds_left} 秒内请选择发送或不发送；超时将通知授权联系人，不会自动发送。"
                    if bool(self.config.get("confirm_timeout_notify_small_account", True))
                    else f"{seconds_left} 秒内请选择发送或不发送；超时只保留本机窗口，不会自动发送。"
                )
            )
            win.after(1000, lambda: countdown(seconds_left - 1))

        self._release_confirmation_task_slot(target_name, ui_context)
        win.lift()
        editor.focus_set()
        win.after(1000, lambda: countdown(4))

    def _send_confirmed_reply_background(
        self,
        target_name: str,
        msg: dict,
        text: str,
        decision: ReplyDecision,
        ui_context: dict,
    ) -> None:
        """Run database checks and WeChat UI automation away from Tk's main thread."""
        try:
            logging.info("确认后发送任务进入后台：target=%s", target_name)
            self._send_reply_messages(
                target_name,
                text,
                lambda: self._cancel_reason_before_reply(target_name, msg),
            )
            logging.info("确认后发送完成，风险=%s", decision.risk_categories)
            self.events.put(("confirm_send_success", target_name, msg, ui_context, ""))
        except SendCancelled as exc:
            logging.info("确认回复在发送前取消：target=%s reason=%s", target_name, exc)
            sent_parts = tuple(getattr(exc, "sent_reply_parts", ()))
            detail = (
                f"已确认发送 {len(sent_parts)} 条，剩余内容因{exc}而取消"
                if sent_parts else str(exc)
            )
            self.events.put(("confirm_send_cancelled", target_name, msg, ui_context, detail))
        except Exception as exc:
            # A send exception may happen after Enter/click was submitted. Do not
            # offer another automatic click until the user verifies the chat.
            logging.exception("确认回复发送异常，禁止直接重复提交：target=%s", target_name)
            sent_parts = tuple(getattr(exc, "sent_reply_parts", ()))
            detail = (
                f"前面已确认发送 {len(sent_parts)} 条；当前部分可能已提交。请检查微信。原因为：{exc}"
                if sent_parts else str(exc)
            )
            self.events.put(("confirm_send_failed", target_name, msg, ui_context, detail))

    def _send_reply_messages(
        self,
        target_name: str,
        text: str,
        pre_submit_check=None,
        contact_search_only: bool = False,
    ) -> None:
        """Send one to three newline-separated reply bubbles without interleaving."""
        parts = split_reply_messages(text)
        with self.send_lock:
            for index, part in enumerate(parts):
                try:
                    if pre_submit_check is not None:
                        cancel_reason = pre_submit_check()
                        if cancel_reason:
                            raise SendCancelled(cancel_reason)
                        if contact_search_only:
                            self.bridge.send_by_search_with_pre_submit_check(
                                target_name, part, pre_submit_check
                            )
                        else:
                            self.bridge.send_with_pre_submit_check(
                                target_name, part, pre_submit_check
                            )
                    else:
                        sender = (
                            self.bridge.send_by_search
                            if contact_search_only
                            else self.bridge.send
                        )
                        sender(target_name, part)
                except Exception as exc:
                    # Successfully verified earlier bubbles are not offered for resend.
                    # The currently failing bubble may have been submitted, so the user
                    # still needs to verify the chat before approving a retry.
                    setattr(exc, "remaining_reply_text", "\n".join(parts[index:]))
                    setattr(exc, "sent_reply_parts", tuple(parts[:index]))
                    raise

    def _finish_confirm_send(
        self,
        kind: str,
        target_name: str,
        msg: dict,
        ui_context: dict,
        detail: str,
    ) -> None:
        """Apply worker results on Tk's main thread and release the conversation task."""
        send_state = ui_context["send_state"]
        send_state["sending"] = False
        win = ui_context["window"]
        try:
            window_exists = bool(win.winfo_exists())
        except tk.TclError:
            window_exists = False

        if kind == "confirm_send_success":
            self._set_status(f"处理完成：{target_name}：确认回复已发送")
            if window_exists:
                win.destroy()
            self._release_confirmation_task_slot(target_name, ui_context)
            return

        if kind == "confirm_send_cancelled":
            logging.info("处理完成：target=%s reason=%s", target_name, detail)
            self._set_status(f"处理完成：{target_name}：{detail}")
            if window_exists:
                win.destroy()
            self._release_confirmation_task_slot(target_name, ui_context)
            return

        send_state["failed"] = True
        self._set_status(f"{target_name}：发送结果未能确认，请先核实微信会话")
        if window_exists:
            ui_context["progress"].configure(
                text=(
                    f"发送异常：{detail}\n为避免重复发送，已禁用再次提交。"
                    "请先检查微信中的这条会话，再关闭此窗口。"
                )
            )
            ui_context["ignore_button"].configure(
                state="normal", text="关闭（先核实）"
            )
        logging.error(
            "确认回复结果不确定，保留待核实窗口：target=%s seq=%s error=%s",
            target_name, msg.get("sort_seq"), detail,
        )

    def _show_manual(self, target_name: str, msg: dict, reason: str) -> None:
        try:
            messagebox.showwarning(f"{target_name} 自动回复已暂停", f"{reason}\n\n消息：{str(msg.get('content') or '')[:500]}")
        finally:
            self._release_target_task(target_name)

    def run(self) -> None:
        active_targets = self._active_listen_targets()
        self.bridge.listen(self.callback_for, active_targets)
        self.worker.start()
        self.root.after(250, self._poll)
        self.root.after(100, self._poll_live_log)
        self.root.after(600, self._refresh_codex_catalog)
        self._set_status(f"处理完成：正在监听 {len(active_targets)} 个联系人")
        logging.info("开始监听：%s", "、".join(active_targets) if active_targets else "无")
        self.root.mainloop()

    def stop(self) -> None:
        self._catalog_cancel.set()
        self.config["enabled"] = False
        self._persist_config()
        self._clear_pending_work()
        self.bridge.stop()
        self.root.destroy()


if __name__ == "__main__":
    try:
        if not ensure_single_instance():
            if "--configure" in sys.argv:
                messagebox.showinfo("重新配置微信与联系人", "请先关闭已运行的自动回复程序，再打开配置向导。")
            sys.exit(0)
        if not ensure_configuration(ROOT, force="--configure" in sys.argv):
            sys.exit(0)
        Application().run()
    except Exception as exc:
        logging.exception("启动失败")
        messagebox.showerror("微信自动回复启动失败", str(exc))
        sys.exit(1)
