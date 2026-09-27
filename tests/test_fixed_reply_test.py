import queue
from types import SimpleNamespace

import app as app_module
from app import Application


class FakeVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeEntry:
    def __init__(self, value):
        self.value = value
        self.focused = False

    def get(self):
        return self.value

    def focus_set(self):
        self.focused = True


class FakeButton:
    def __init__(self):
        self.options = {}

    def configure(self, **options):
        self.options.update(options)


class FakeRoot:
    def __init__(self):
        self.after_calls = []

    def after(self, delay, callback):
        self.after_calls.append((delay, callback))


def make_app(target_label="测试账号", text="固定测试内容"):
    app = Application.__new__(Application)
    app.test_reply_active = False
    app.test_reply_target_labels = {target_label: "Test Account"}
    app.test_reply_target_var = FakeVar(target_label)
    app.test_reply_content_widget = FakeEntry(text)
    app.test_reply_button = FakeButton()
    app.bridge = SimpleNamespace(targets={"Test Account": "wxid_example_002"})
    app.events = queue.Queue()
    app.status_detail_var = FakeVar("")
    app._send_reply_messages = lambda target, reply: None
    return app


def test_fixed_test_uses_preferred_contact_display_name():
    app = Application.__new__(Application)
    app.config = {
        "targets": [
            {"name": "Test Account", "ui_name": "旧备注"},
            {"name": "Contact A", "ui_name": "Contact A"},
            {"name": "不在桥接映射中的联系人", "ui_name": "无效"},
        ]
    }
    app.bridge = SimpleNamespace(
        targets={"Test Account": "wxid_example_002", "Contact A": "wxid_example_003"},
        ui_names={"Test Account": "测试账号", "Contact A": "Contact A"},
    )

    labels = app._build_test_reply_target_labels()

    assert labels == {"测试账号": "Test Account", "Contact A": "Contact A"}


def test_fixed_test_starts_background_worker_and_routes_fixed_text_through_normal_sender(monkeypatch):
    app = make_app()
    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            started.append(self)

    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)

    app._start_fixed_reply_test()

    assert app.test_reply_active is True
    assert app.test_reply_button.options == {"state": "disabled"}
    assert started[0].kwargs["args"] == ("Test Account", "固定测试内容")
    sent = []
    app._send_reply_messages = lambda target, reply: sent.append((target, reply))
    started[0].kwargs["target"](*started[0].kwargs["args"])

    assert sent == [("Test Account", "固定测试内容")]
    assert app.events.get_nowait() == ("fixed_test_sent", "Test Account", None, None, "")


def test_fixed_test_rejects_empty_text_or_unmapped_contacts_without_sending(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            started.append(self)

    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)

    empty_app = make_app(text="  ")
    empty_app._start_fixed_reply_test()
    assert empty_app.test_reply_active is False
    assert empty_app.test_reply_content_widget.focused is True

    invalid_app = make_app()
    invalid_app.test_reply_target_var.set("不存在")
    invalid_app._start_fixed_reply_test()

    assert started == []


def test_fixed_test_result_reenables_button_and_updates_status():
    app = make_app()
    app.root = FakeRoot()
    app.test_reply_active = True
    app.test_reply_button.configure(state="disabled")
    app.events.put(("fixed_test_sent", "Test Account", None, None, ""))

    app._poll()

    assert app.test_reply_active is False
    assert app.test_reply_button.options["state"] == "normal"
    assert "固定内容测试已发送给 Test Account" in app.status_detail_var.get()
    assert app.root.after_calls[0][0] == 250


def test_status_ui_explains_fixed_test_is_immediate_and_does_not_call_model():
    source = (app_module.ROOT / "app.py").read_text(encoding="utf-8")

    assert '_make_collapsible_section(\n            content, "固定内容测试"' in source
    assert "text=\"测试联系人：\"" in source
    assert "text=\"固定回复内容：\"" in source
    assert "text=\"开始测试\"" in source
    assert "不调用模型" in source
    worker = source.split("def _send_fixed_reply_test", 1)[1].split("def _apply_model_selection", 1)[0]
    assert "self._send_reply_messages(target_name, text)" in worker
    assert ".engine." not in worker
    assert ".decide(" not in worker
