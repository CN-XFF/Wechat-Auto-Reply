import json
import queue
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "wechatauto-replica"))
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
from app import Application  # noqa: E402


def test_send_failure_asks_test_account_before_resend():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    delayed = source.split("def _delayed_send", 1)[1].split("def _confirm_dialog", 1)[0]
    assert "self._ask_resend_confirmation(" in delayed
    assert "window_diagnosis," in delayed
    assert 'getattr(exc, "sent_reply_parts", ())' in delayed
    assert "自动回复发送失败" in source
    assert "是否重发？如果要重发" in source
    assert "#{self.config.get('command_password', '密码')}+是" in source


def test_yes_command_confirms_pending_resend_before_codex_command():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    handle_command = source.split("def _handle_command", 1)[1].split("def _run_codex_instruction", 1)[0]
    assert 'confirm_resend_commands = {"是", "重发", "重新发送", "yes", "y"}' in handle_command
    assert "self._confirm_pending_resend(target_name)" in handle_command
    assert handle_command.index("self._confirm_pending_resend(target_name)") < handle_command.index(
        "if self.config.get(\"codex_command_enabled\", True):"
    )


def test_pending_resend_is_kept_on_retry_failure_and_cleared_on_success():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    confirm = source.split("def _confirm_pending_resend", 1)[1].split("def _run_pending_resend", 1)[0]
    worker = source.split("def _run_pending_resend", 1)[1].split("def _finish_pending_resend", 1)[0]
    finish = source.split("def _finish_pending_resend", 1)[1].split("def _run_codex_instruction", 1)[0]
    assert "target=self._run_pending_resend" in confirm
    assert "self._send_reply_messages" not in confirm
    assert "self._send_reply_messages(target_name, reply)" in worker
    assert '"pending_resend_failed"' in worker
    assert "self.pending_resend = None" in finish
    assert "self.pending_resend = failed_pending" in finish
    assert "未自动再次提交" in finish


def test_confirm_resend_starts_background_work_and_rejects_duplicate(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            started.append(self)

    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)
    app = Application.__new__(Application)
    app.config = {"command_password": "test"}
    app.pending_resend = {"target_name": "李昊阳", "reply": "候选回复"}
    app._set_status = lambda _text: None
    receipts = []
    app._send_command_reply_async = lambda target, text: receipts.append((target, text))

    app._confirm_pending_resend("测试账号")
    app._confirm_pending_resend("测试账号")

    assert len(started) == 1
    assert started[0].kwargs["target"] == app._run_pending_resend
    assert app.pending_resend["resending"] is True
    assert receipts == [("测试账号", "这条消息正在后台重发处理中，请稍候")]


def test_resend_worker_failure_preserves_retry_for_explicit_confirmation():
    app = Application.__new__(Application)
    app.config = {"command_password": "test"}
    app.events = queue.Queue()
    app.bridge = SimpleNamespace(diagnose_wechat_window=lambda *_args, **_kwargs: {})
    app._short_error = lambda _exc, _diagnosis: "微信会话未能确认"
    app._set_status = lambda _text: None
    receipts = []
    app._send_command_reply_async = lambda target, text: receipts.append((target, text))
    error = RuntimeError("发送结果不确定")
    error.remaining_reply_text = "尚未确认的部分"
    error.sent_reply_parts = ("此前已发送",)

    def fail_send(_target, _reply):
        raise error

    app._send_reply_messages = fail_send
    original_pending = {
        "target_name": "李昊阳",
        "reply": "原始回复",
        "resending": True,
        "resend_operation_id": "op-1",
    }
    app.pending_resend = dict(original_pending)

    app._run_pending_resend("测试账号", original_pending, "op-1")
    kind, _sender, result, _decision, detail = app.events.get_nowait()
    assert kind == "pending_resend_failed"
    app._finish_pending_resend(kind, "测试账号", result, detail)

    assert app.pending_resend["reply"] == "尚未确认的部分"
    assert app.pending_resend["resending"] is False
    assert "resend_operation_id" not in app.pending_resend
    assert app.pending_resend["sent_parts"] == ("此前已发送",)
    assert "此前已确认发送：此前已发送" in receipts[0][1]
    assert "请先检查微信会话" in receipts[0][1]


def test_resend_worker_success_clears_only_the_matching_pending_item():
    app = Application.__new__(Application)
    app.config = {"command_password": "test"}
    app.events = queue.Queue()
    sent = []
    receipts = []
    app._send_reply_messages = lambda target, reply: sent.append((target, reply))
    app._send_command_reply_async = lambda target, text: receipts.append((target, text))
    app._set_status = lambda _text: None
    pending = {
        "target_name": "李昊阳",
        "reply": "候选回复",
        "resending": True,
        "resend_operation_id": "op-2",
    }
    app.pending_resend = dict(pending)

    app._run_pending_resend("测试账号", pending, "op-2")
    kind, _sender, result, _decision, detail = app.events.get_nowait()
    app._finish_pending_resend(kind, "测试账号", result, detail)

    assert sent == [("李昊阳", "候选回复")]
    assert app.pending_resend is None
    assert receipts == [("测试账号", "已重发给李昊阳")]


def test_resend_confirmation_contact_configured():
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert config["resend_confirmation_contact"] == "测试账号"
