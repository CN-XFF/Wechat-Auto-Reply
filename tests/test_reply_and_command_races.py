import queue
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "wechatauto-replica"))
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
import reply_core.wechat_bridge as bridge_module  # noqa: E402
from app import Application  # noqa: E402
from reply_core.wechat_bridge import SendCancelled, WeChatBridge  # noqa: E402


def _message(seq: int, text: str) -> dict:
    return {"sender_id": 27, "type": "文本", "sort_seq": seq, "content": text}


def test_manual_reply_check_failure_is_unknown_and_cancels_reply():
    app = Application.__new__(Application)
    app.config = {"enabled": True, "cancel_if_user_replied": True}
    app.bridge = SimpleNamespace(has_self_reply_after=lambda *_args: None)
    app._target_auto_reply_enabled = lambda _name: True
    app._keyboard_input_detected = lambda: False

    assert app._cancel_reason_before_reply("联系人示例", _message(10, "问题")) == (
        "无法确认你是否已手动回复，已取消自动发送"
    )


def test_missing_incoming_sequence_fails_closed():
    app = Application.__new__(Application)
    app.config = {"enabled": True, "cancel_if_user_replied": True}
    app.bridge = SimpleNamespace(has_self_reply_after=lambda *_args: pytest.fail("should not query without seq"))
    app._target_auto_reply_enabled = lambda _name: True
    app._keyboard_input_detected = lambda: False

    assert app._cancel_reason_before_reply("联系人示例", {"type": "文本"}) == (
        "无法确认你是否已手动回复，已取消自动发送"
    )


def test_database_error_in_manual_reply_check_returns_unknown():
    bridge = WeChatBridge.__new__(WeChatBridge)
    bridge.targets = {"联系人示例": "wxid_example_005"}

    def fail_read(*_args, **_kwargs):
        raise OSError("snapshot temporarily unavailable")

    bridge.db = SimpleNamespace(get_messages=fail_read)
    assert bridge.has_self_reply_after("联系人示例", 10) is None


def test_final_send_gate_cancels_and_clears_unsent_draft(monkeypatch):
    bridge = WeChatBridge.__new__(WeChatBridge)
    bridge.config = {"dry_run": False, "send_submit_method": "enter"}
    bridge.targets = {"联系人示例": "wxid_example_005"}
    bridge.ui_names = {"联系人示例": "联系人示例"}
    bridge.db = object()
    bridge._reusable_wechat_gui = lambda: None
    bridge._window_rect = lambda _hwnd: (100, 100, 1200, 900)
    bridge._send_mark = lambda _username: set()
    bridge._verify_sent_db = lambda *_args, **_kwargs: False
    bridge._clear_cached_wechat = lambda: None
    sent_actions = []
    bridge._paste_text_win32 = lambda _wx, text: sent_actions.append(("paste", text))
    bridge._submit_once = lambda *_args: sent_actions.append(("submit",))

    class FakeWeChat:
        main_hwnd = 42
        _cached_db = None

        def ensure_visible(self):
            return True

        def open_chat(self, _name):
            return True

        def _chat_is_open(self, _name):
            return True

    monkeypatch.setattr(bridge_module, "WeChatGUI", lambda **_kwargs: FakeWeChat())
    checks = iter(("", "检测到你已经手动回复"))

    with pytest.raises(SendCancelled, match="手动回复"):
        bridge.send_with_pre_submit_check(
            "联系人示例", "候选回复", lambda: next(checks)
        )

    assert sent_actions == [("paste", "候选回复"), ("paste", "")]


def test_duplicate_display_name_is_refused_before_opening_wechat():
    bridge = WeChatBridge.__new__(WeChatBridge)
    bridge.config = {"dry_run": False}
    bridge.targets = {"联系人示例": "wxid_example_005", "Other": "wxid_example_006"}
    bridge.ui_names = {"联系人示例": "同名", "Other": "同名"}
    bridge._reusable_wechat_gui = lambda: pytest.fail("must refuse before touching WeChat UI")

    with pytest.raises(SendCancelled, match="重名"):
        bridge.send_with_pre_submit_check("联系人示例", "候选回复", lambda: "")


def test_two_contacts_keep_independent_batches_and_are_both_processed():
    app = Application.__new__(Application)
    app.config = {
        "enabled": True,
        "targets": [
            {"name": "测试账号", "auto_reply_enabled": True, "wait_seconds": 0},
            {"name": "联系人示例", "auto_reply_enabled": True, "wait_seconds": 0},
        ],
    }
    app.events = queue.Queue()
    app.pending_batches = {}
    app.inflight_targets = set()
    app.task_condition = threading.Condition()
    app.shutting_down = False
    app._target_auto_reply_enabled = lambda _name: True
    app._wait_seconds_for_target = lambda _name: 0
    app._cursor_pos = lambda: (10, 10)
    app._set_status = lambda _text: None
    app._handle_command_if_any = lambda _name, _content: False
    app._user_replied_after_incoming = lambda _name, _msg: False

    app.on_message("测试账号", _message(11, "给小号的消息"), None)
    app.on_message("联系人示例", _message(22, "给联系人示例的消息"), None)
    assert set(app.pending_batches) == {"测试账号", "联系人示例"}

    processed = []
    both_done = threading.Event()

    def generate(target_name, _msg, content):
        processed.append((target_name, content))
        app._release_target_task(target_name)
        if len(processed) == 2:
            both_done.set()

    app._generate = generate
    worker = threading.Thread(target=app._message_worker, daemon=True)
    worker.start()
    assert both_done.wait(3)
    with app.task_condition:
        app.shutting_down = True
        app.task_condition.notify_all()
    worker.join(3)

    assert not worker.is_alive()
    assert dict(processed) == {"测试账号": "给小号的消息", "联系人示例": "给联系人示例的消息"}


def test_simultaneous_contact_sends_are_serialized_without_target_mixup():
    bridge = WeChatBridge.__new__(WeChatBridge)
    bridge.config = {"dry_run": False}
    bridge._send_queue = queue.Queue()
    bridge._send_worker = None
    bridge._send_worker_ident = None
    bridge._send_worker_lock = threading.Lock()
    bridge._send_worker_stopping = False
    bridge.listener = SimpleNamespace(stop=lambda: None)
    state_lock = threading.Lock()
    active = 0
    peak_active = 0
    sent = []

    def fake_send_once(target_name, text, pre_submit_check=None):
        nonlocal active, peak_active
        assert pre_submit_check is None
        with state_lock:
            active += 1
            peak_active = max(peak_active, active)
        time.sleep(0.02)
        sent.append((target_name, text))
        with state_lock:
            active -= 1

    bridge._send_once = fake_send_once
    errors = []

    def send(target_name, text):
        try:
            bridge.send(target_name, text)
        except Exception as exc:
            errors.append(exc)

    callers = [
        threading.Thread(target=send, args=("测试账号", "发给小号")),
        threading.Thread(target=send, args=("联系人示例", "发给联系人示例")),
    ]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(2)
    bridge.stop()

    assert not errors
    assert all(not caller.is_alive() for caller in callers)
    assert set(sent) == {("测试账号", "发给小号"), ("联系人示例", "发给联系人示例")}
    assert peak_active == 1


def test_trusted_command_is_dispatched_to_codex_using_resolved_executable(monkeypatch, tmp_path):
    trusted_username = "wxid_example_004"
    app = Application.__new__(Application)
    app.config = {
        "command_contact": "测试账号",
        "command_contact_username": trusted_username,
        "codex_command_enabled": True,
        "codex_command_workdir": str(tmp_path),
        "codex_command_timeout_seconds": 30,
        "targets": [{"name": "测试账号", "username": trusted_username, "command_enabled": True}],
    }
    app.bridge = SimpleNamespace(targets={"测试账号": trusted_username})
    replies = []
    app._send_command_reply = lambda target, text: replies.append((target, text))
    seen = {}

    monkeypatch.setattr(app_module.shutil, "which", lambda _name: r"C:\Codex\codex.exe")

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="完成", stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    app._run_codex_instruction("测试账号", "检查工作区状态")

    assert seen["command"][0] == r"C:\Codex\codex.exe"
    assert seen["command"][1:2] == ["exec"]
    assert "--sandbox" in seen["command"]
    assert "workspace-write" in seen["command"]
    assert seen["kwargs"]["cwd"] == str(tmp_path.resolve())
    assert seen["kwargs"]["creationflags"] & app_module.subprocess.CREATE_NO_WINDOW
    startupinfo = seen["kwargs"]["startupinfo"]
    assert startupinfo.dwFlags & app_module.subprocess.STARTF_USESHOWWINDOW
    assert startupinfo.wShowWindow == app_module.subprocess.SW_HIDE
    assert replies == [("测试账号", "Codex 执行完成（退出码 0）：\n完成")]


def test_codex_command_result_reports_nonzero_exit_and_both_output_streams(monkeypatch, tmp_path):
    trusted_username = "wxid_example_004"
    app = Application.__new__(Application)
    app.config = {
        "command_contact": "测试账号",
        "command_contact_username": trusted_username,
        "codex_command_enabled": True,
        "codex_command_workdir": str(tmp_path),
        "targets": [{"name": "测试账号", "username": trusted_username, "command_enabled": True}],
    }
    app.bridge = SimpleNamespace(targets={"测试账号": trusted_username})
    replies = []
    app._send_command_reply = lambda target, text: replies.append((target, text)) or True
    monkeypatch.setattr(app_module.shutil, "which", lambda _name: r"C:\Codex\codex.exe")
    monkeypatch.setattr(
        app_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=17, stdout="部分输出", stderr="权限被拒绝"
        ),
    )

    app._run_codex_instruction("测试账号", "执行一个会失败的操作")

    assert len(replies) == 1
    assert "执行失败（退出码 17）" in replies[0][1]
    assert "部分输出" in replies[0][1]
    assert "权限被拒绝" in replies[0][1]


def test_codex_command_timeout_sends_partial_output(monkeypatch, tmp_path):
    trusted_username = "wxid_example_004"
    app = Application.__new__(Application)
    app.config = {
        "command_contact": "测试账号",
        "command_contact_username": trusted_username,
        "codex_command_enabled": True,
        "codex_command_workdir": str(tmp_path),
        "codex_command_timeout_seconds": 9,
        "targets": [{"name": "测试账号", "username": trusted_username, "command_enabled": True}],
    }
    app.bridge = SimpleNamespace(targets={"测试账号": trusted_username})
    replies = []
    app._send_command_reply = lambda target, text: replies.append((target, text)) or True
    monkeypatch.setattr(app_module.shutil, "which", lambda _name: r"C:\Codex\codex.exe")

    def timeout(*_args, **_kwargs):
        raise app_module.subprocess.TimeoutExpired(
            "codex", 9, output="已生成部分结果".encode("utf-8"),
            stderr="仍在等待".encode("utf-8"),
        )

    monkeypatch.setattr(app_module.subprocess, "run", timeout)
    app._run_codex_instruction("测试账号", "一个需要超时的操作")

    assert len(replies) == 1
    assert "执行超时（9 秒）" in replies[0][1]
    assert "已生成部分结果" in replies[0][1]
    assert "仍在等待" in replies[0][1]


def test_codex_command_missing_executable_still_sends_failure_result(monkeypatch, tmp_path):
    trusted_username = "wxid_example_004"
    app = Application.__new__(Application)
    app.config = {
        "command_contact": "测试账号",
        "command_contact_username": trusted_username,
        "codex_command_enabled": True,
        "codex_command_workdir": str(tmp_path),
        "targets": [{"name": "测试账号", "username": trusted_username, "command_enabled": True}],
    }
    app.bridge = SimpleNamespace(targets={"测试账号": trusted_username})
    replies = []
    app._send_command_reply = lambda target, text: replies.append((target, text)) or True
    monkeypatch.setattr(app_module.shutil, "which", lambda _name: None)

    app._run_codex_instruction("测试账号", "查看状态")

    assert len(replies) == 1
    assert "执行失败（FileNotFoundError）" in replies[0][1]
    assert "找不到 codex.exe" in replies[0][1]


def test_trusted_command_starts_codex_without_waiting_for_wechat_ack(monkeypatch):
    trusted_username = "wxid_example_004"
    app = Application.__new__(Application)
    app.config = {
        "command_contact": "测试账号",
        "command_contact_username": trusted_username,
        "codex_command_enabled": True,
        "targets": [{"name": "测试账号", "username": trusted_username, "command_enabled": True}],
    }
    app.bridge = SimpleNamespace(targets={"测试账号": trusted_username})
    app._send_command_reply = lambda *_args: None
    started = []

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            started.append((self.target, self.args, self.daemon))

    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)
    app._handle_command("测试账号", "检查工作区状态")

    assert len(started) == 2
    assert started[0][0] == app._send_command_reply
    assert started[1][0] == app._run_codex_instruction
    assert started[1][1] == ("测试账号", "检查工作区状态")
    assert all(item[2] for item in started)
