"""Local first-run configuration; folder checks never open database contents."""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def normalize_data_location(selected: Path) -> tuple[Path, str]:
    selected = selected.expanduser().resolve()
    for directory in (selected, *selected.parents):
        if directory.name.lower() == "db_storage":
            return directory.parent.parent, directory.parent.name
    if (selected / "db_storage").is_dir():
        return selected.parent, selected.name
    if (selected / "xwechat_files").is_dir():
        return selected / "xwechat_files", ""
    return selected, ""


def account_folders(root: Path) -> list[str]:
    try:
        return sorted(p.name for p in root.iterdir() if p.is_dir() and (p / "db_storage").is_dir())
    except OSError:
        return []


def choose_account(accounts: list[str], preferred: str, current: str, previous: str) -> str:
    for candidate in (preferred, current, previous):
        if candidate in accounts:
            return candidate
    return accounts[0] if len(accounts) == 1 else ""


def validate_account(root: Path, account: str) -> None:
    if not account or account in {".", ".."} or any(c in account for c in "\\/:"):
        raise ValueError("请选择当前微信账号的文件夹。")
    storage = root / account / "db_storage"
    if not storage.is_dir():
        raise ValueError("所选账号下面没有 db_storage，请重新选择微信数据目录。")
    try:
        found = any(p.is_file() for p in storage.rglob("session.db"))
    except OSError as exc:
        raise ValueError("无法访问所选账号目录，请检查文件夹权限。") from exc
    if not found:
        raise ValueError("没有找到 session.db。请确认微信 4.x 已登录，并选择当前账号的数据目录。")


def needs_setup(config: dict) -> bool:
    if not isinstance(config, dict) or not isinstance(config.get("targets"), list):
        return True
    directory = str(config.get("db_dir") or "")
    account = str(config.get("account") or "")
    if not directory or not account or "PATH\\TO\\WECHAT" in directory or account.startswith("wxid_example"):
        return True
    try:
        validate_account(Path(directory), account)
    except (ValueError, OSError):
        return True
    return False


def prepare_configuration(config: dict, root: Path, account: str, read_recent: bool) -> dict:
    root = root.resolve()
    validate_account(root, account)
    result = copy.deepcopy(config)
    previous_root = str(config.get("db_dir") or "")
    same_account = (
        bool(previous_root)
        and os.path.normcase(os.path.abspath(previous_root)) == os.path.normcase(str(root))
        and config.get("account") == account
    )
    if not same_account or not isinstance(result.get("targets"), list):
        result["targets"] = []
        for key in ("command_contact", "command_contact_username", "command_password", "codex_command_workdir", "resend_confirmation_contact"):
            result[key] = ""
    for target in result.get("targets", []):
        target["listen_enabled"] = False
        target["auto_reply_enabled"] = False
    result.update(
        db_dir=str(root), account=account, enabled=False, dry_run=True,
        command_channel_enabled=False, codex_command_enabled=False,
        show_recent_self_contacts=bool(read_recent), first_run_completed=True,
    )
    return result


def save_configuration(path: Path, config: dict) -> None:
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.parent / "backups" / "first-run" / stamp / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
            temporary = Path(handle.name)
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def ensure_configuration(app_root: Path, force: bool = False) -> bool:
    config_path = app_root / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("Expected an object")
    except (OSError, ValueError):
        config = json.loads((app_root / "config.example.json").read_text(encoding="utf-8-sig"))
    if not force and not needs_setup(config):
        return True

    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    window = tk.Tk()
    window.title("微信自动回复助手 · 首次配置")
    window.geometry("760x560")
    window.minsize(720, 540)
    frame = ttk.Frame(window, padding=20)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="连接这台电脑上的微信", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
    ttk.Label(frame, text="先登录 Windows 微信 4.x，再选择微信文件夹。可在微信设置的文件管理中打开该位置。", wraplength=640).pack(anchor="w", pady=(8, 16))
    directory = tk.StringVar(value="" if "PATH\\TO\\WECHAT" in str(config.get("db_dir")) else str(config.get("db_dir") or ""))
    account = tk.StringVar()
    status = tk.StringVar(value="选择 xwechat_files、账号文件夹或 db_storage，程序会识别目录层级。")
    ttk.Label(frame, text="微信数据位置").pack(anchor="w")
    path_row = ttk.Frame(frame)
    path_row.pack(fill="x", pady=(4, 12))
    entry = ttk.Entry(path_row, textvariable=directory)
    entry.pack(side="left", fill="x", expand=True)
    ttk.Label(frame, text="当前账号文件夹（多账号电脑请核对当前登录账号）").pack(anchor="w")
    selector = ttk.Combobox(frame, textvariable=account, state="readonly")
    selector.pack(fill="x", pady=(4, 8))
    ttk.Label(frame, textvariable=status, wraplength=640).pack(anchor="w", pady=4)

    def refresh() -> None:
        if not directory.get().strip():
            return
        try:
            root, preferred = normalize_data_location(Path(directory.get().strip()))
        except (OSError, ValueError):
            selector["values"] = []
            account.set("")
            status.set("目录无法访问，请重新选择微信数据文件夹。")
            return
        directory.set(str(root))
        accounts = account_folders(root)
        selector["values"] = accounts
        account.set(choose_account(accounts, preferred, account.get(), str(config.get("account") or "")))
        status.set(f"找到 {len(accounts)} 个账号文件夹；保存前会检查会话数据库。" if accounts else "未找到账号文件夹，请重新选择包含 xwechat_files 或 db_storage 的位置。")

    def browse() -> None:
        selected = filedialog.askdirectory(parent=window, title="选择本机微信数据文件夹")
        if selected:
            directory.set(selected)
            refresh()

    ttk.Button(path_row, text="选择文件夹…", command=browse).pack(side="left", padx=(8, 0))
    entry.bind("<FocusOut>", lambda _event: refresh())
    entry.bind("<Return>", lambda _event: refresh())
    ttk.Button(frame, text="刷新账号列表", command=refresh).pack(anchor="w", pady=4)
    ttk.Label(frame, text="读取近期联系人后只显示名单，首次不监听、不自动回复。AI 回复需要本机 Codex CLI 已安装并登录。", wraplength=640).pack(anchor="w", pady=(12, 8))
    ttk.Label(frame, text=f"配置文件：{config_path}", wraplength=640).pack(anchor="w", pady=4)
    completed = False

    def save(read_recent: bool) -> None:
        nonlocal completed
        try:
            refresh()
            if not directory.get().strip():
                raise ValueError("请先选择微信数据文件夹。")
            data_root = Path(directory.get())
            validate_account(data_root, account.get())
            if config.get("targets") and (str(config.get("db_dir")) != str(data_root) or config.get("account") != account.get()):
                if not messagebox.askyesno("切换微信账号", "切换账号会清空本程序原有联系人和指令授权，请重新选择使用对象。继续吗？", parent=window):
                    return
            prepared = prepare_configuration(config, data_root, account.get(), read_recent)
            save_configuration(config_path, prepared)
        except (ValueError, OSError) as exc:
            messagebox.showerror("配置未保存", str(exc), parent=window)
            return
        completed = True
        window.destroy()

    actions = ttk.Frame(frame)
    actions.pack(fill="x", side="bottom", pady=(12, 0))
    ttk.Button(actions, text="保存并读取近期联系人", command=lambda: save(True)).pack(side="left")
    ttk.Button(actions, text="保存配置，稍后读取", command=lambda: save(False)).pack(side="left", padx=8)
    ttk.Button(actions, text="取消", command=window.destroy).pack(side="right")
    refresh()
    window.mainloop()
    return completed
