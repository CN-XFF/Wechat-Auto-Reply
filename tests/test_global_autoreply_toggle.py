import json
import threading
from pathlib import Path
from types import SimpleNamespace

from app import Application


ROOT = Path(__file__).parents[1]


class FakeVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _make_app(tmp_path, *, enabled, targets):
    app = Application.__new__(Application)
    app.config = {"enabled": enabled, "targets": targets}
    app.config_path = tmp_path / "config.json"
    app.global_auto_reply_var = FakeVar(enabled)
    app.header_status_var = FakeVar("")
    app.status_detail_var = FakeVar("")
    app.engine = SimpleNamespace(model_name="gpt-6-luna", reasoning_effort="low")
    app.reply_epoch = 0
    app.pending_batches = {}
    app.task_condition = threading.Condition()
    return app


def test_status_window_has_global_autoreply_switch():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'text="自动回复总开关"' in source
    assert "global_auto_reply_var" in source
    assert "def _set_global_auto_reply_enabled" in source


def test_turning_global_switch_off_persists_and_discards_queued_replies(tmp_path):
    targets = [{"name": "联系人示例", "listen_enabled": True, "auto_reply_enabled": True}]
    app = _make_app(tmp_path, enabled=True, targets=targets)
    app.pending_batches = {"联系人示例": {"items": [({}, "waiting message")]}}

    assert app._set_global_auto_reply_enabled(False) is True

    saved = json.loads(app.config_path.read_text(encoding="utf-8"))
    assert saved["enabled"] is False
    assert app.config["targets"] == targets
    assert app.global_auto_reply_var.get() is False
    assert "暂停" in app.header_status_var.get()
    assert app.pending_batches == {}
    assert app.reply_epoch == 1


def test_turning_global_switch_on_preserves_contact_settings_and_skips_old_messages(tmp_path):
    targets = [{"name": "测试账号", "listen_enabled": True, "auto_reply_enabled": False}]
    app = _make_app(tmp_path, enabled=False, targets=targets)
    skipped = []
    remarks = []
    app.bridge = SimpleNamespace(skip_existing_messages=skipped.append)
    app._active_listen_targets = lambda: ["测试账号"]
    app._refresh_target_remarks = remarks.append

    assert app._set_global_auto_reply_enabled(True) is True

    saved = json.loads(app.config_path.read_text(encoding="utf-8"))
    assert saved["enabled"] is True
    assert app.config["targets"] == targets
    assert app.global_auto_reply_var.get() is True
    assert "开启" in app.header_status_var.get()
    assert skipped == [["测试账号"]]
    assert remarks == ["恢复自动回复时"]


def test_old_generation_cannot_send_after_global_pause_and_resume():
    app = Application.__new__(Application)
    app.config = {
        "enabled": True,
        "cancel_on_mouse_move": False,
        "cancel_on_keyboard_input": False,
        "cancel_if_user_replied": False,
    }
    app.reply_epoch = 2
    app._target_auto_reply_enabled = lambda _name: True
    app._keyboard_input_detected = lambda: False
    app._user_replied_after_incoming = lambda _name, _msg: False

    reason = app._cancel_reason_before_reply("联系人示例", {"_reply_epoch": 1})

    assert reason == "自动回复暂停期间的旧任务已丢弃"


def test_trusted_small_account_pause_and_resume_commands_use_global_switch():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    pause_branch = source.split("if normalized in pause_commands:", 1)[1].split(
        "if normalized in resume_commands:", 1
    )[0]
    resume_branch = source.split("if normalized in resume_commands:", 1)[1].split(
        "if normalized in status_commands:", 1
    )[0]
    assert "self._set_global_auto_reply_enabled(False)" in pause_branch
    assert "self._set_global_auto_reply_enabled(True)" in resume_branch
