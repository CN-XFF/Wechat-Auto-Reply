from __future__ import annotations

import ctypes
import logging
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from ctypes import wintypes
from pathlib import Path

import pyperclip

from wechatauto import WeChatDB, WeChatGUI
from wechatauto.db import Listener
from wechatauto.guia import _restore_keep_maximize


class SendCancelled(RuntimeError):
    """用户已回复或发送前状态无法确认，因此取消尚未提交的消息。"""


class WeChatBridge:
    PROFILE_HISTORY_PAGE_SIZE = 5000

    def __init__(self, config: dict, runtime_dir: Path):
        self.config = config
        startup_started = time.perf_counter()
        logging.info("启动阶段：初始化微信数据库（文字消息模式）")
        self.db = WeChatDB(
            db_dir=config["db_dir"],
            account=config["account"],
            extract_media_key=False,
        )
        logging.info("启动阶段：微信数据库就绪，用时 %.1f 秒", time.perf_counter() - startup_started)
        startup_started = time.perf_counter()
        logging.info("启动阶段：校验联系人映射")
        self.targets = self._resolve_targets()
        logging.info("启动阶段：联系人映射就绪，用时 %.1f 秒", time.perf_counter() - startup_started)
        self.ui_names = {
            target["name"]: target.get("ui_name") or target["name"]
            for target in self.config["targets"]
        }
        self.listener = Listener(
            self.db,
            interval=float(config.get("listen_interval_seconds", 1.5)),
            watermark_file=str(runtime_dir / "listener_watermark.json"),
        )
        self.watermark_file = runtime_dir / "listener_watermark.json"
        self.listener_callbacks: dict[str, callable] = {}
        self._send_queue: queue.Queue = queue.Queue()
        self._send_worker: threading.Thread | None = None
        self._send_worker_ident: int | None = None
        self._send_worker_lock = threading.Lock()
        self._send_worker_stopping = False
        self._cached_wechat_gui = None
        self._cached_wechat_hwnd: int | None = None
        self._cached_wechat_pid: int | None = None
        self._cached_wechat_rect: tuple[int, int, int, int] | None = None
        self._cached_last_input_tick: int | None = None
        # Retain the last input point that was verified by a successful send,
        # even when UI activity forces recreation of the WeChatGUI wrapper.
        self._last_confirmed_reply_geometry = None
        # Track messages the application itself successfully sent so a later
        # queued incoming message is not mistaken for a manual user reply.
        self._automated_sent_lock = threading.Lock()
        self._automated_sent_ids: dict[str, dict[tuple[int | None, int | None], None]] = {}
        self._automated_sent_id_limit = 5000

    def _resolve_targets(self) -> dict[str, str]:
        sessions = {s["username"] for s in self.db.get_sessions(limit=500)}
        resolved = {}
        for target in self.config["targets"]:
            name, configured = target["name"], target.get("username", "")
            if configured and configured in sessions and self.db.get_messages(configured, limit=1):
                resolved[name] = configured
                continue
            hits = self.db.search_contact(name)
            viable = [h for h in hits if h["username"] in sessions and self.db.get_messages(h["username"], limit=1)]
            if len(viable) != 1:
                raise RuntimeError(f"无法唯一锁定 {name}：找到 {len(viable)} 个有会话的匹配项")
            resolved[name] = viable[0]["username"]
        return resolved

    def display_name_for_username(self, username: str, fallback: str = "") -> str:
        try:
            hits = self.db.search_contact(username)
        except Exception:
            hits = []
        for hit in hits:
            if hit.get("username") == username:
                return str(hit.get("remark") or hit.get("nick_name") or fallback or username)
        return fallback or username

    def recent_self_contacts(self, session_limit: int = 50, max_contacts: int = 20) -> list[dict]:
        contacts: list[dict] = []
        seen: set[str] = set()
        configured = {target.get("username") for target in self.config.get("targets", [])}
        for session in self.db.get_sessions(limit=session_limit):
            username = str(session.get("username") or "")
            if not username or username in seen:
                continue
            if username.startswith("@") or username in {"brandsessionholder", "notification_messages"}:
                continue
            if username.endswith("@chatroom") or username.startswith("gh_"):
                continue
            try:
                messages = self.db.get_messages(username, limit=20)
            except Exception:
                continue
            if not any(row.get("sender_id") in {1, 2} and row.get("type") != "系统" for row in messages):
                continue
            seen.add(username)
            contacts.append({
                "username": username,
                "name": self.display_name_for_username(username, fallback=str(session.get("last_sender") or username)),
                "configured": username in configured,
                "last_time": session.get("last_time"),
            })
            if len(contacts) >= max_contacts:
                break
        return contacts

    def _send_mark(self, username: str) -> set[tuple[int | None, int | None]]:
        rows = self.db.get_messages(username, limit=20)
        return {(row.get("sort_seq"), row.get("local_id")) for row in rows}

    def _verify_sent_db(
        self,
        username: str,
        text: str,
        before: set[tuple[int | None, int | None]],
    ) -> bool:
        for row in self.db.get_messages(username, limit=20):
            ident = (row.get("sort_seq"), row.get("local_id"))
            content = str(row.get("content") or "").replace("\x00", "").strip()
            if (
                ident not in before
                and row.get("sender_id") in {1, 2}
                and row.get("type") == "文本"
                and content == text
            ):
                self._remember_automated_sent(username, ident)
                return True
        return False

    def _remember_automated_sent(
        self,
        username: str,
        ident: tuple[int | None, int | None],
    ) -> None:
        if ident == (None, None):
            return
        with self._automated_sent_lock:
            sent_ids = self._automated_sent_ids.setdefault(username, {})
            sent_ids[ident] = None
            while len(sent_ids) > self._automated_sent_id_limit:
                sent_ids.pop(next(iter(sent_ids)))

    def _is_automated_sent(self, username: str, ident: tuple) -> bool:
        with self._automated_sent_lock:
            return ident in self._automated_sent_ids.get(username, {})

    def has_self_reply_after(self, target_name: str, after_sort_seq: int | None) -> bool | None:
        if after_sort_seq is None:
            return False
        username = self.targets[target_name]
        # 微信正在写入时，底层快照偶尔会丢失临时文件。这个检查只是
        # “用户是否已手动回复”的附加保护，绝不能因此终止自动回复工作线程。
        try:
            rows = self.db.get_messages(username, limit=100)
        except Exception as exc:
            logging.warning("手动回复校验失败，将取消自动发送：target=%s error=%s", target_name, exc)
            return None
        for row in rows:
            try:
                sort_seq = int(row.get("sort_seq") or 0)
            except (TypeError, ValueError):
                sort_seq = 0
            if (
                sort_seq > int(after_sort_seq)
                and row.get("sender_id") in {1, 2}
                and str(row.get("type") or "").strip() != "系统"
            ):
                ident = (row.get("sort_seq"), row.get("local_id"))
                if self._is_automated_sent(username, ident):
                    logging.info(
                        "排除程序已发送消息，不按手动回复取消：target=%s seq=%s",
                        target_name, sort_seq,
                    )
                    continue
                logging.info(
                    "检测到对应会话的本人消息，取消待发送自动回复：target=%s type=%s seq=%s",
                    target_name, row.get("type"), sort_seq,
                )
                return True
        return False

    def recent_context(self, target_name: str, limit: int, char_limit: int) -> str:
        messages = list(reversed(self.db.get_messages(self.targets[target_name], limit=limit)))
        lines = []
        for msg in messages:
            if msg.get("type") != "文本":
                continue
            who = "我" if msg.get("sender_id") in {1, 2} else target_name
            content = str(msg.get("content") or "").replace("\x00", "").strip()
            if content:
                lines.append(f"{who}: {content}")
        return "\n".join(lines)[-char_limit:]

    def style_profile_examples(self, target_name: str) -> list[dict[str, str]]:
        """Return every available chronological text message for one contact's style profile."""
        username = self.targets.get(target_name)
        if not username:
            raise ValueError(f"没有找到联系人会话：{target_name}")

        # get_messages returns each page newest-first. Page through the full
        # conversation, then reverse once so analysis sees stable chronology.
        messages_newest_first: list[dict] = []
        offset = 0
        while True:
            page = self.db.get_messages(
                username,
                limit=self.PROFILE_HISTORY_PAGE_SIZE,
                offset=offset,
            )
            if not page:
                break
            messages_newest_first.extend(page)
            offset += len(page)
            if len(page) < self.PROFILE_HISTORY_PAGE_SIZE:
                break

        examples: list[dict[str, str]] = []
        for message in reversed(messages_newest_first):
            if message.get("type") != "文本":
                continue
            content = str(message.get("content") or "").replace("\x00", "").strip()
            if not content:
                continue
            speaker = "我" if message.get("sender_id") in {1, 2} else "对方"
            examples.append({"speaker": speaker, "text": content})

        logging.info(
            "联系人完整历史文字记录读取完成：target=%s total_messages=%d text_messages=%d text_chars=%d",
            target_name,
            len(messages_newest_first),
            len(examples),
            sum(len(item["text"]) for item in examples),
        )
        return examples

    def listen(self, callback_factory, target_names: list[str] | None = None) -> None:
        target_names = target_names or list(self.targets)
        if self.config.get("skip_existing_on_start", True):
            self.skip_existing_messages(target_names)
        for name in target_names:
            self.add_target_listener(name, callback_factory)
        self.listener.start()

    def add_target_listener(self, target_name: str, callback_factory) -> None:
        if target_name in self.listener_callbacks:
            return
        username = self.targets[target_name]
        callback = callback_factory(target_name)
        self.listener.add_listener(username, callback)
        self.listener_callbacks[target_name] = callback

    def remove_target_listener(self, target_name: str) -> None:
        callback = self.listener_callbacks.pop(target_name, None)
        if callback is None:
            return
        self.listener.remove_listener(self.targets[target_name], callback)

    def stop(self) -> None:
        self.listener.stop()
        send_queue = getattr(self, "_send_queue", None)
        worker = getattr(self, "_send_worker", None)
        lock = getattr(self, "_send_worker_lock", None)
        if send_queue is None or lock is None:
            return
        with lock:
            if getattr(self, "_send_worker_stopping", False):
                return
            self._send_worker_stopping = True
            # A stop must not let queued, not-yet-started messages send afterward.
            while True:
                try:
                    task = send_queue.get_nowait()
                except queue.Empty:
                    break
                if task is not None:
                    task[0].cancel()
                send_queue.task_done()
            if worker is not None and worker.is_alive():
                send_queue.put(None)
        if worker is not None and worker.is_alive() and worker.ident != threading.get_ident():
            worker.join(timeout=1.0)

    def _send_worker_loop(self) -> None:
        self._send_worker_ident = threading.get_ident()
        while True:
            task = self._send_queue.get()
            try:
                if task is None:
                    return
                if len(task) == 5:
                    future, target_name, text, pre_submit_check, task_kind = task
                else:
                    future, target_name, text, pre_submit_check = task
                    task_kind = "send"
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    if task_kind == "search_send":
                        result = self._send_once(
                            target_name,
                            text,
                            pre_submit_check,
                            contact_search_only=True,
                        )
                    elif task_kind == "search_draft":
                        result = self._send_once(
                            target_name,
                            text,
                            pre_submit_check,
                            contact_search_only=True,
                            submit=False,
                        )
                    elif task_kind == "draft":
                        result = self._send_once(
                            target_name, text, pre_submit_check, submit=False
                        )
                    else:
                        result = self._send_once(target_name, text, pre_submit_check)
                except Exception as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                self._send_queue.task_done()

    def _ensure_send_worker(self) -> None:
        with self._send_worker_lock:
            if self._send_worker_stopping:
                raise RuntimeError("微信发送工作线程已停止")
            if self._send_worker is None or not self._send_worker.is_alive():
                self._send_worker = threading.Thread(
                    target=self._send_worker_loop,
                    name="wechat-send-worker",
                    daemon=True,
                )
                self._send_worker.start()

    def skip_existing_messages(self, target_names: list[str] | None = None) -> None:
        import json

        payload = {}
        if self.watermark_file.exists():
            try:
                payload = json.loads(self.watermark_file.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
        names = target_names or list(self.targets)
        for name in names:
            username = self.targets[name]
            messages = self.db.get_messages(username, limit=1)
            payload[username] = int(messages[0].get("sort_seq") or 0) if messages else 0
        self.watermark_file.parent.mkdir(parents=True, exist_ok=True)
        self.watermark_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if hasattr(self.listener, "_watermark"):
            self.listener._watermark.update(payload)

    @staticmethod
    def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return rect.left, rect.top, rect.right, rect.bottom

    @staticmethod
    def _restore_window(hwnd: int) -> bool:
        if not hwnd:
            return False
        try:
            return bool(_restore_keep_maximize(ctypes.windll.user32, hwnd))
        except Exception:
            logging.exception("微信窗口安全恢复失败：hwnd=%s", hwnd)
            return False

    @staticmethod
    def _last_input_event_tick() -> int | None:
        """Read the last mouse/keyboard input tick for this interactive session."""
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        return int(info.dwTime)

    @staticmethod
    def _window_process_id(hwnd: int) -> int:
        process_id = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        return int(process_id.value)

    def _clear_cached_wechat(self) -> None:
        self._cached_wechat_gui = None
        self._cached_wechat_hwnd = None
        self._cached_wechat_pid = None
        self._cached_wechat_rect = None
        self._cached_last_input_tick = None

    def _cached_wechat_window_is_current(self, wx: WeChatGUI) -> bool:
        hwnd = int(getattr(wx, "main_hwnd", 0) or 0)
        if not hwnd or hwnd != self._cached_wechat_hwnd:
            return False
        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return False
        if self._window_process_id(hwnd) != self._cached_wechat_pid:
            return False
        return self._window_rect(hwnd) == self._cached_wechat_rect

    def _reusable_wechat_gui(self) -> WeChatGUI | None:
        wx = getattr(self, "_cached_wechat_gui", None)
        if wx is None:
            return None
        try:
            current_tick = self._last_input_event_tick()
            if current_tick is None or current_tick != self._cached_last_input_tick:
                self._clear_cached_wechat()
                logging.info("检测到两次发送之间有鼠标/键盘活动，重新定位微信窗口")
                return None
            if not self._cached_wechat_window_is_current(wx):
                self._clear_cached_wechat()
                logging.info("微信窗口句柄、进程或位置变化，重新定位微信窗口")
                return None
        except Exception as exc:
            self._clear_cached_wechat()
            logging.info("无法验证微信窗口缓存，重新定位：%s", exc)
            return None
        logging.info(
            "复用微信窗口 hwnd=%s：无新输入且句柄/进程/位置未变，跳过重新定位",
            self._cached_wechat_hwnd,
        )
        return wx

    def _remember_wechat_gui(self, wx: WeChatGUI) -> None:
        """Cache only after a send is confirmed; cache errors never fail the send."""
        try:
            hwnd = int(getattr(wx, "main_hwnd", 0) or 0)
            tick = self._last_input_event_tick()
            if not hwnd or tick is None:
                self._clear_cached_wechat()
                return
            self._cached_wechat_gui = wx
            self._cached_wechat_hwnd = hwnd
            self._cached_wechat_pid = self._window_process_id(hwnd)
            self._cached_wechat_rect = self._window_rect(hwnd)
            self._cached_last_input_tick = tick
        except Exception as exc:
            self._clear_cached_wechat()
            logging.debug("微信窗口缓存未保存：%s", exc)

    def _wechat_exe_candidates(self) -> list[str]:
        candidates: list[str] = []
        try:
            import winreg
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for exe_name in ("WeChat.exe", "Weixin.exe"):
                    try:
                        key_path = (
                            r"Software\Microsoft\Windows\CurrentVersion\App Paths"
                            + "\\" + exe_name
                        )
                        with winreg.OpenKey(root, key_path) as key:
                            value, _ = winreg.QueryValueEx(key, None)
                            if value:
                                candidates.append(value)
                    except OSError:
                        pass
        except Exception:
            pass
        env_roots = [
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
            os.environ.get("APPDATA", ""),
        ]
        relative_paths = [
            r"Tencent\WeChat\WeChat.exe",
            r"WeChat\WeChat.exe",
            r"Programs\Tencent\WeChat\WeChat.exe",
            r"Tencent\Weixin\Weixin.exe",
            r"Weixin\Weixin.exe",
            r"Programs\Tencent\Weixin\Weixin.exe",
        ]
        for root in env_roots:
            for rel in relative_paths:
                if root:
                    candidates.append(str(Path(root) / rel))
        seen = set()
        existing = []
        for candidate in candidates:
            normalized = str(candidate)
            key = normalized.lower()
            if key not in seen and Path(normalized).exists():
                seen.add(key)
                existing.append(normalized)
        return existing

    def _launch_or_show_wechat(self) -> dict:
        result = {"attempted": False, "started": False, "exe": "", "error": ""}
        for exe in self._wechat_exe_candidates():
            result["attempted"] = True
            result["exe"] = exe
            try:
                subprocess.Popen([exe], close_fds=True)
                result["started"] = True
                time.sleep(2.0)
                return result
            except Exception as exc:
                result["error"] = str(exc)
        if not result["attempted"]:
            result["error"] = "未找到 WeChat.exe 或 Weixin.exe"
        return result

    @staticmethod
    def _click_abs(x: int, y: int) -> None:
        user32 = ctypes.windll.user32
        user32.SetCursorPos(x, y)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP

    @staticmethod
    def _key(vk: int, *, ctrl: bool = False) -> None:
        user32 = ctypes.windll.user32
        if ctrl:
            user32.keybd_event(0x11, 0, 0, 0)  # CTRL
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.04)
        user32.keybd_event(vk, 0, 0x0002, 0)
        if ctrl:
            user32.keybd_event(0x11, 0, 0x0002, 0)

    def _paste_text_win32(self, wx: WeChatGUI, text: str) -> None:
        """把文字粘贴进当前聊天输入框。

        WechatAuto 的输入框像素探测在本机深色微信里会误判，导致“以为输
        入成功但实际没有文字”。这里使用实测稳定的窗口比例坐标：先点到
        输入框文本行，再 Ctrl+A/Delete 清空旧草稿，最后 Ctrl+V 粘贴。
        """
        left, top, right, bottom = self._window_rect(wx.main_hwnd)
        width, height = right - left, bottom - top
        point_ratio = getattr(wx, "_reply_input_point_ratio", None)
        if (not isinstance(point_ratio, (tuple, list)) or len(point_ratio) != 2
                or not all(0.0 < float(value) < 1.0 for value in point_ratio)):
            point_ratio = (
                float(self.config.get("win32_input_x_ratio", 0.260)),
                float(self.config.get("win32_input_y_ratio", 0.788)),
            )
        input_x = left + int(width * float(point_ratio[0]))
        input_y = top + int(height * float(point_ratio[1]))
        pyperclip.copy(text)
        user32 = ctypes.windll.user32
        if user32.GetForegroundWindow() != wx.main_hwnd:
            user32.SetForegroundWindow(wx.main_hwnd)
            deadline = time.monotonic() + 1.0
            while (time.monotonic() < deadline
                   and user32.GetForegroundWindow() != wx.main_hwnd):
                time.sleep(0.05)
            if user32.GetForegroundWindow() != wx.main_hwnd:
                raise RuntimeError("微信未能切到前台，取消粘贴以免发错窗口")
        time.sleep(0.25)
        self._click_abs(input_x, input_y)
        time.sleep(0.15)
        self._key(0x41, ctrl=True)  # Ctrl+A
        self._key(0x2E)             # Delete
        time.sleep(0.1)
        self._key(0x56, ctrl=True)  # Ctrl+V
        time.sleep(0.5)

    def _submit_once(self, wx: WeChatGUI, method: str) -> None:
        """只提交一次发送动作。

        不调用 WechatAuto 的 click_send/send_msg，因为它们内部可能为了确认
        输入框清空而再次按回车或点击；这里宁可失败，也不重复提交。
        """
        if method == "enter":
            self._key(0x0D)
            return
        if method == "ctrl_enter":
            self._key(0x0D, ctrl=True)
            return
        if method != "button":
            raise RuntimeError(f"未知发送方式：{method}")
        left, top, right, bottom = self._window_rect(wx.main_hwnd)
        width, height = right - left, bottom - top
        send_x = left + int(width * float(self.config.get("win32_send_x_ratio", 0.952)))
        send_y = top + int(height * float(self.config.get("win32_send_y_ratio", 0.952)))
        self._click_abs(send_x, send_y)

    def diagnose_wechat_window(self, target_name: str, recover: bool = True) -> dict:
        """发送失败后检测并尽量恢复微信窗口，不执行输入或发送动作。"""
        result: dict = {
            "target": target_name,
            "ui_name": self.ui_names.get(target_name, target_name),
            "recover": recover,
            "recovery_actions": [],
            "wechat_gui_created": False,
            "main_hwnd": None,
            "window_rect": None,
            "ensure_visible": None,
            "window_displayable": None,
            "foreground": None,
            "open_chat": None,
            "chat_is_open": None,
            "error": "",
        }
        try:
            wx = None
            create_error = ""
            for attempt in range(2):
                try:
                    try:
                        wx = WeChatGUI(title="WeChat")
                    except RuntimeError:
                        wx = WeChatGUI()
                    break
                except Exception as exc:
                    create_error = str(exc)
                    if recover and attempt == 0:
                        launch = self._launch_or_show_wechat()
                        result["recovery_actions"].append({"launch_or_show_wechat": launch})
                    else:
                        raise
            if wx is None:
                raise RuntimeError(create_error or "无法创建 WeChatGUI")
            wx._cached_db = self.db
            result["wechat_gui_created"] = True
            result["main_hwnd"] = int(getattr(wx, "main_hwnd", 0) or 0)
            if result["main_hwnd"]:
                result["window_rect"] = self._window_rect(int(result["main_hwnd"]))
            visible = bool(wx.ensure_visible())
            result["ensure_visible"] = visible
            try:
                result["window_displayable"] = bool(wx._window_is_displayable())
                user32 = getattr(getattr(wx, "_input", None), "_user32", None)
                if user32 is not None:
                    result["foreground"] = (
                        user32.GetForegroundWindow()
                        == int(getattr(wx, "main_hwnd", 0) or 0)
                    )
            except Exception as exc:
                logging.debug("诊断微信前台状态失败：%s", exc)
            if recover:
                # WeChatGUI already posts a non-blocking restore request during
                # construction; ensure_visible performs the single activation.
                result["recovery_actions"].append({"restore_window": visible})
                if visible and result["main_hwnd"]:
                    result["window_rect_after_restore"] = self._window_rect(
                        int(result["main_hwnd"]))
            ui_name = str(result["ui_name"])
            if visible:
                opened = bool(wx.open_chat(ui_name))
                result["open_chat"] = opened
                result["chat_is_open"] = bool(wx._chat_is_open(ui_name)) if opened else False
        except Exception as exc:
            result["error"] = str(exc)
        return result

    def send(self, target_name: str, text: str) -> None:
        return self._enqueue_send(target_name, text, None)

    def send_with_pre_submit_check(
        self,
        target_name: str,
        text: str,
        pre_submit_check: Callable[[], str | None],
    ) -> None:
        """在确认会话后、真正提交前再次检查是否应取消发送。"""
        return self._enqueue_send(target_name, text, pre_submit_check)

    def send_by_search(self, target_name: str, text: str) -> None:
        """测试专用：绕过聊天列表，使用搜索框定位后发送一次。"""
        return self._enqueue_send(
            target_name, text, None, contact_search_only=True
        )

    def send_by_search_with_pre_submit_check(
        self,
        target_name: str,
        text: str,
        pre_submit_check: Callable[[], str | None],
    ) -> None:
        """搜索框定位后，在提交前执行同一发送状态检查。"""
        return self._enqueue_send(
            target_name, text, pre_submit_check, contact_search_only=True
        )

    def prepare_by_search(self, target_name: str, text: str) -> None:
        """搜索并确认联系人后填入输入框，保留草稿但绝不提交发送。"""
        return self._enqueue_send(
            target_name, text, None, contact_search_only=True, submit=False
        )

    def prepare_message(self, target_name: str, text: str) -> None:
        """按正常聊天列表优先流程定位后填入输入框，不提交发送。"""
        return self._enqueue_send(target_name, text, None, submit=False)

    def _enqueue_send(
        self,
        target_name: str,
        text: str,
        pre_submit_check: Callable[[], str | None] | None,
        contact_search_only: bool = False,
        submit: bool = True,
    ) -> None:
        if self.config.get("dry_run", False):
            return
        # Keep Windows GUI/input operations and their cached WeChatGUI instance
        # on one persistent worker instead of short-lived threads.
        if getattr(self, "_send_worker_ident", None) == threading.get_ident() or not hasattr(self, "_send_queue"):
            return self._send_once(
                target_name,
                text,
                pre_submit_check,
                contact_search_only=contact_search_only,
                submit=submit,
            )
        self._ensure_send_worker()
        future: Future = Future()
        with self._send_worker_lock:
            if self._send_worker_stopping:
                raise RuntimeError("微信发送工作线程已停止")
            task = (future, target_name, text, pre_submit_check)
            if contact_search_only:
                task += ("search_send" if submit else "search_draft",)
            elif not submit:
                task += ("draft",)
            self._send_queue.put(task)
        return future.result()

    def _send_once(
        self,
        target_name: str,
        text: str,
        pre_submit_check: Callable[[], str | None] | None = None,
        contact_search_only: bool = False,
        submit: bool = True,
    ) -> None:
        if self.config.get("dry_run", False):
            return
        target_username = self.targets[target_name]
        ui_name = self.ui_names.get(target_name, target_name)
        duplicate_labels = [
            other_name
            for other_name, other_username in self.targets.items()
            if other_name != target_name
            and other_username != target_username
            and self.ui_names.get(other_name, other_name) == ui_name
        ]
        if duplicate_labels:
            raise SendCancelled("联系人显示名重名，无法唯一确认会话，已取消发送")
        started = time.perf_counter()
        wx = self._reusable_wechat_gui()
        reused_window = wx is not None
        try:
            if wx is None:
                try:
                    wx = WeChatGUI(title="WeChat")
                except RuntimeError:
                    wx = WeChatGUI()
            wx._cached_db = self.db
            if contact_search_only:
                primary_hwnd = int(getattr(wx, "_primary_hwnd", 0) or 0)
                current_hwnd = int(getattr(wx, "main_hwnd", 0) or 0)
                if primary_hwnd and primary_hwnd != current_hwnd:
                    if not wx.use_window(primary_hwnd):
                        raise RuntimeError("无法切回微信主窗口执行搜索；未执行发送")
            # Minimized/off-screen windows still take the recovery path. A cached
            # window is reused only after input, HWND, PID, visibility and rect checks.
            hwnd = int(getattr(wx, "main_hwnd", 0) or 0)
            if hwnd:
                left, top, right, bottom = self._window_rect(hwnd)
                if right <= 0 or bottom <= 0:
                    raise RuntimeError("微信窗口仍在屏幕外，无法查找会话")
            mark = self._send_mark(target_username)
            if not wx.ensure_visible():
                try:
                    displayable = bool(wx._window_is_displayable())
                except Exception:
                    displayable = False
                if displayable:
                    raise RuntimeError(
                        "微信窗口仍可见，但未能切到前台（可能被其他窗口遮挡）；未执行发送"
                    )
                raise RuntimeError("微信窗口被隐藏、最小化或无响应")
            refresh_sidebar = getattr(wx, "refresh_sidebar_layout_before_reply", None)
            if (not contact_search_only and callable(refresh_sidebar)
                    and not refresh_sidebar()):
                raise RuntimeError("回复前未能确认聊天列表布局，已取消发送")
            if contact_search_only:
                logging.info(
                    "搜索定位测试发送：绕过聊天列表，直接使用搜索框 target=%s",
                    target_name,
                )
                opened = wx.search_chat_only(ui_name)
            else:
                opened = wx.open_chat(ui_name)
            if not opened:
                route = "搜索框" if contact_search_only else "聊天列表或搜索框"
                raise RuntimeError(f"无法通过{route}打开会话：{ui_name}")
            if not wx._chat_is_open(ui_name):
                raise RuntimeError(f"无法确认当前会话就是 {ui_name}；未执行发送")
            seed_geometry = getattr(wx, "seed_reply_input_geometry", None)
            last_geometry = getattr(self, "_last_confirmed_reply_geometry", None)
            if last_geometry and callable(seed_geometry):
                seed_geometry(last_geometry)
            # 目标会话确认后、粘贴文字之前，再测输入框。窗口移动/缩放时
            # 窗口几何不变时复用上次成功发送的输入点；几何变化时重新检测。
            rect = self._window_rect(int(getattr(wx, "main_hwnd", 0) or 0))
            refresh_input = getattr(wx, "refresh_input_geometry_before_reply", None)
            if callable(refresh_input):
                point_ratio = refresh_input(
                    rect,
                    input_x_ratio=float(self.config.get("win32_input_x_ratio", 0.260)),
                    input_y_ratio=float(self.config.get("win32_input_y_ratio", 0.788)),
                )
                if not point_ratio:
                    raise RuntimeError("回复前未能确认输入框位置，已取消发送")
                wx._reply_input_point_ratio = point_ratio
            if not wx._chat_is_open(ui_name):
                raise SendCancelled(
                    f"输入前无法再次确认当前会话是 {ui_name}，已取消发送"
                )
            logging.info(
                "微信窗口准备完成：cache_reused=%s elapsed=%.2fs",
                reused_window, time.perf_counter() - started,
            )
            if pre_submit_check is not None:
                cancel_reason = pre_submit_check()
                if cancel_reason:
                    raise SendCancelled(cancel_reason)
            self._paste_text_win32(wx, text)
            if pre_submit_check is not None:
                cancel_reason = pre_submit_check()
                if cancel_reason:
                    try:
                        if wx._chat_is_open(ui_name):
                            self._paste_text_win32(wx, "")
                        else:
                            logging.warning("取消发送后未能确认输入框所属会话，未清理草稿：target=%s", target_name)
                    except Exception:
                        logging.exception("取消发送后清理未提交草稿失败：target=%s", target_name)
                    raise SendCancelled(cancel_reason)
            if not wx._chat_is_open(ui_name):
                raise SendCancelled(
                    f"提交前检测到会话已切换或无法确认是 {ui_name}，未执行发送"
                )
            if not submit:
                self._remember_wechat_gui(wx)
                logging.info(
                    "搜索框测试已定位并填入草稿：target=%s len=%d；未执行发送",
                    target_name, len(text),
                )
                return
            self._submit_once(wx, self.config.get("send_submit_method", "button"))  # 只执行一次，禁止任何发送重试。
            deadline = time.time() + 12
            while time.time() < deadline:
                if self._verify_sent_db(target_username, text, before=mark):
                    confirm_geometry = getattr(
                        wx, "confirm_reply_input_geometry", None)
                    if callable(confirm_geometry):
                        confirmed_geometry = confirm_geometry()
                        if confirmed_geometry is not None:
                            self._last_confirmed_reply_geometry = confirmed_geometry
                    self._remember_wechat_gui(wx)
                    logging.info(
                        "发送校验完成：target=%s cache_reused=%s total=%.2fs",
                        target_name, reused_window, time.perf_counter() - started,
                    )
                    return
                time.sleep(1)
            raise RuntimeError("已执行一次发送动作，但数据库未能确认；为防重复不会重试")
        except Exception:
            self._clear_cached_wechat()
            raise
