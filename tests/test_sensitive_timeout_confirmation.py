import queue
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "wechatauto-replica"))
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
from app import Application  # noqa: E402


class FakeWidget:
    def __init__(self, *_args, **kwargs):
        self.value = str(kwargs.get("text") or "")
        self.command = kwargs.get("command")
        self.options = []
        self.callbacks = []
        self.destroyed = False

    def pack(self, *_args, **_kwargs):
        return None

    def insert(self, _index, text):
        self.value += text

    def get(self, *_args):
        return self.value

    def configure(self, **kwargs):
        self.options.append(kwargs)

    def protocol(self, *_args):
        return None

    def geometry(self, *_args):
        return None

    def title(self, *_args):
        return None

    def attributes(self, *_args):
        return None

    def lift(self):
        return None

    def focus_set(self):
        return None

    def after(self, _delay, callback):
        self.callbacks.append(callback)
        return len(self.callbacks)

    def winfo_exists(self):
        return True

    def destroy(self):
        self.destroyed = True


class FakeThread:
    started = []

    def __init__(self, *, target, args, **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.started.append((self.target, self.args))


def _patch_fake_tk(monkeypatch):
    window = FakeWidget()
    monkeypatch.setattr(app_module.tk, "Toplevel", lambda _root: window)
    monkeypatch.setattr(app_module.tk, "Label", FakeWidget)
    monkeypatch.setattr(app_module.tk, "Message", FakeWidget)
    monkeypatch.setattr(app_module.tk, "Text", FakeWidget)
    monkeypatch.setattr(app_module.tk, "Frame", FakeWidget)
    monkeypatch.setattr(app_module.tk, "Button", FakeWidget)
    return window


def test_sensitive_dialog_notifies_after_five_seconds_without_a_choice(monkeypatch):
    FakeThread.started = []
    window = _patch_fake_tk(monkeypatch)
    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)
    app = Application.__new__(Application)
    app.root = object()
    app.config = {"confirm_timeout_notify_small_account": True}
    app._release_target_task = lambda _name: None
    notices = []
    app._begin_sensitive_confirmation_request = lambda *args: notices.append(args)
    msg = {"content": "需要确认的来信", "sort_seq": 100}
    decision = SimpleNamespace(reply="拟发送内容", reason="敏感话题", risk_categories=["sensitive"])

    app._confirm_dialog("Contact A", msg, decision)

    for _ in range(5):
        callback = window.callbacks.pop(0)
        callback()

    assert len(notices) == 1
    target, seen_msg, seen_decision, reply, ui_context = notices[0]
    assert (target, seen_msg, seen_decision, reply) == (
        "Contact A", msg, decision, "拟发送内容"
    )
    assert ui_context["send_state"]["notifying"] is False


def test_selecting_send_before_deadline_prevents_small_account_notice(monkeypatch):
    window = _patch_fake_tk(monkeypatch)
    buttons = []
    monkeypatch.setattr(
        app_module.tk,
        "Button",
        lambda *args, **kwargs: buttons.append(FakeWidget(*args, **kwargs)) or buttons[-1],
    )
    FakeThread.started = []
    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)
    app = Application.__new__(Application)
    app.root = object()
    app.config = {"confirm_timeout_notify_small_account": True}
    app.pending_reply_confirmations = {}
    app._release_target_task = lambda _name: None
    notices = []
    app._begin_sensitive_confirmation_request = lambda *args: notices.append(args)
    app._send_confirmed_reply_background = lambda *_args: None
    msg = {"content": "来信", "sort_seq": 101}
    decision = SimpleNamespace(reply="回复", reason="敏感话题", risk_categories=[])

    app._confirm_dialog("Contact A", msg, decision)
    buttons[0].command()
    window.callbacks.pop(0)()

    assert not notices
    assert FakeThread.started


def test_timeout_request_uses_trusted_small_account_and_numbered_choices(monkeypatch):
    FakeThread.started = []
    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)
    app = Application.__new__(Application)
    app.config = {"command_contact": "Test Account", "command_password": "TEST_ONLY_SECRET_001"}
    app._target_config = lambda _name: {"command_enabled": True}
    app._is_trusted_command_contact = lambda name: name == "Test Account"
    app.pending_reply_confirmations = {}
    app.events = queue.Queue()
    app._set_status = lambda _text: None
    state = {"sending": False, "failed": False, "resolved": False, "notifying": False, "request_id": None}
    ui_context = {
        "window": FakeWidget(),
        "progress": FakeWidget(),
        "send_button": FakeWidget(),
        "ignore_button": FakeWidget(),
        "editor": FakeWidget(),
        "send_state": state,
    }
    msg = {"content": "来信内容", "sort_seq": 20}
    decision = SimpleNamespace(reply="候选内容", reason="敏感话题", risk_categories=["sensitive"])

    app._begin_sensitive_confirmation_request("Contact A", msg, decision, "候选内容", ui_context)

    assert len(app.pending_reply_confirmations) == 1
    pending = next(iter(app.pending_reply_confirmations.values()))
    assert pending["target_name"] == "Contact A"
    assert state["notifying"] is True
    assert ui_context["send_button"].options[-1] == {"state": "disabled"}
    job_target, args = FakeThread.started[-1]
    assert job_target == app._send_sensitive_confirmation_notice
    recipient, original_target, _msg, prompt, request_id, _context = args
    assert recipient == "Test Account"
    assert original_target == "Contact A"
    assert f"#{app.config['command_password']}+确认发送 {request_id}" in prompt
    assert f"#{app.config['command_password']}+确认不发送 {request_id}" in prompt
    assert "不会自动发送" in prompt


def test_remote_confirmation_commands_are_distinct_from_existing_resend_yes(monkeypatch):
    app = Application.__new__(Application)
    app.config = {"codex_command_enabled": True}
    app._target_config = lambda _name: {"command_enabled": True}
    app._is_trusted_command_contact = lambda _name: True
    app._send_command_reply_async = lambda *_args: None
    resolved = []
    app._resolve_pending_reply_confirmation = lambda *args, **kwargs: resolved.append((args, kwargs))
    app._confirm_pending_resend = lambda *_args: resolved.append(("resend",))

    app._handle_command("Test Account", "确认发送 a1b2c3")
    app._handle_command("Test Account", "确认不发送 d4e5f6")
    app._handle_command("Test Account", "是")

    assert resolved == [
        (("Test Account", "a1b2c3"), {"approve": True}),
        (("Test Account", "d4e5f6"), {"approve": False}),
        ("resend",),
    ]


def test_remote_deny_resolves_pending_item_without_sending(monkeypatch):
    class StubWindow(FakeWidget):
        pass

    app = Application.__new__(Application)
    app.pending_reply_confirmations = {
        "a1b2c3": {
            "request_id": "A1B2C3",
            "target_name": "Contact A",
            "msg": {"sort_seq": 100},
            "reply": "候选回复",
            "decision": object(),
            "ui_context": {
                "window": StubWindow(),
                "send_state": {"sending": False, "resolved": False, "notifying": False},
            },
        }
    }
    statuses = []
    released = []
    acks = []
    app._set_status = statuses.append
    app._release_target_task = released.append
    app._send_command_reply_async = lambda *args: acks.append(args)

    app._resolve_pending_reply_confirmation("Test Account", "a1b2c3", approve=False)

    assert app.pending_reply_confirmations == {}
    assert released == ["Contact A"]
    assert statuses == ["处理完成：Contact A：授权联系人确认不发送"]
    assert acks == [("Test Account", "已取消发送给Contact A，编号 A1B2C3")]


def test_remote_approve_queues_the_original_candidate_for_background_send(monkeypatch):
    FakeThread.started = []
    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)
    decision = SimpleNamespace(reply="候选回复", reason="敏感话题", risk_categories=[])
    msg = {"sort_seq": 105, "content": "需要确认"}
    state = {"sending": False, "resolved": False, "notifying": False}
    context = {
        "window": FakeWidget(),
        "progress": FakeWidget(),
        "send_button": FakeWidget(),
        "ignore_button": FakeWidget(),
        "editor": FakeWidget(),
        "send_state": state,
    }
    app = Application.__new__(Application)
    app.pending_reply_confirmations = {
        "a1b2c3": {
            "request_id": "A1B2C3",
            "target_name": "Contact A",
            "msg": msg,
            "reply": "确认时的候选回复",
            "decision": decision,
            "ui_context": context,
        }
    }
    acks = []
    app._send_command_reply_async = lambda *args: acks.append(args)
    app._set_status = lambda _text: None
    app._release_target_task = lambda _name: None

    app._resolve_pending_reply_confirmation("Test Account", "a1b2c3", approve=True)

    assert app.pending_reply_confirmations == {}
    assert state["resolved"] is True
    assert state["sending"] is True
    worker_target, worker_args = FakeThread.started[-1]
    assert worker_target == app._send_confirmed_reply_background
    assert worker_args == ("Contact A", msg, "确认时的候选回复", decision, context)
    assert acks == [("Test Account", "已收到，正在发送给Contact A（编号 A1B2C3）")]


def test_timeout_can_leave_local_confirmation_open_without_notifying_small_account(monkeypatch):
    window = _patch_fake_tk(monkeypatch)
    app = Application.__new__(Application)
    app.root = object()
    app.config = {"confirm_timeout_notify_small_account": False}
    app._release_target_task = lambda _name: None
    notices = []
    app._begin_sensitive_confirmation_request = lambda *args: notices.append(args)

    app._confirm_dialog(
        "Contact A",
        {"content": "来信", "sort_seq": 106},
        SimpleNamespace(reply="候选回复", reason="敏感话题", risk_categories=[]),
    )
    for _ in range(5):
        window.callbacks.pop(0)()

    assert not notices


def test_confirmation_queue_slot_is_released_only_once():
    import threading

    app = Application.__new__(Application)
    app.inflight_targets = {"Contact A"}
    app.task_condition = threading.Condition()
    context = {"send_state": {"task_slot_released": False}}

    app._release_confirmation_task_slot("Contact A", context)
    assert "Contact A" not in app.inflight_targets

    # A later batch for the same person now owns the slot. Resolving the older
    # dialog must not accidentally release the newer batch's work marker.
    app.inflight_targets.add("Contact A")
    app._release_confirmation_task_slot("Contact A", context)
    assert "Contact A" in app.inflight_targets


def test_confirmation_timeout_toggle_persists_choice(tmp_path):
    class FakeVar:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    app = Application.__new__(Application)
    app.config = {}
    app.config_path = tmp_path / "config.json"
    app.confirm_timeout_notify_var = FakeVar(False)
    app._set_status = lambda _text: None

    app._save_confirmation_timeout_notice_toggle()

    import json
    assert app.config["confirm_timeout_notify_small_account"] is False
    assert json.loads(app.config_path.read_text(encoding="utf-8"))[
        "confirm_timeout_notify_small_account"
    ] is False
