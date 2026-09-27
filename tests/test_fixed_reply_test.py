import queue
import threading
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


def make_app(target_label="小号", text="固定测试内容"):
    app = Application.__new__(Application)
    app.test_reply_active = False
    app.test_reply_target_labels = {target_label: "测试账号"}
    app.test_reply_target_var = FakeVar(target_label)
    app.test_reply_content_widget = FakeEntry(text)
    app.test_reply_button = FakeButton()
    app.test_search_button = FakeButton()
    app.test_reply_send_var = FakeVar(False)
    app.send_lock = threading.Lock()
    app.bridge = SimpleNamespace(
        targets={"测试账号": "wxid_example_009"},
        prepare_message=lambda _target, _text: None,
        prepare_by_search=lambda _target, _text: None,
    )
    app.events = queue.Queue()
    app.status_detail_var = FakeVar("")
    app._send_reply_messages = lambda target, reply: None
    return app


def test_fixed_test_uses_preferred_contact_display_name():
    app = Application.__new__(Application)
    app.config = {
        "targets": [
            {"name": "测试账号", "ui_name": "旧备注"},
            {"name": "联系人示例", "ui_name": "联系人示例"},
            {"name": "不在桥接映射中的联系人", "ui_name": "无效"},
        ]
    }
    app.bridge = SimpleNamespace(
        targets={"测试账号": "wxid_example_009", "联系人示例": "wxid_example_005"},
        ui_names={"测试账号": "小号", "联系人示例": "联系人示例"},
    )

    labels = app._build_test_reply_target_labels()

    assert labels == {"小号": "测试账号", "联系人示例": "联系人示例"}


def test_checked_start_test_uses_list_first_send_path(monkeypatch):
    app = make_app()
    app.test_reply_send_var.set(True)
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
    assert app.test_search_button.options == {"state": "disabled"}
    assert started[0].kwargs["args"] == ("测试账号", "固定测试内容", True, False)
    sent = []
    app._send_reply_messages = lambda target, reply, **kwargs: sent.append(
        (target, reply, kwargs.get("contact_search_only"))
    )
    started[0].kwargs["target"](*started[0].kwargs["args"])

    assert sent == [("测试账号", "固定测试内容", False)]
    assert app.events.get_nowait() == ("fixed_test_sent", "测试账号", None, None, "")


def test_unchecked_start_test_uses_list_first_and_fills_draft(monkeypatch):
    app = make_app()
    prepared = []
    sent = []
    app.bridge.prepare_message = lambda target, text: prepared.append((target, text))
    app._send_reply_messages = lambda *args, **kwargs: sent.append((args, kwargs))
    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            started.append(self)

    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)

    app._start_fixed_reply_test()
    assert started[0].kwargs["args"] == ("测试账号", "固定测试内容", False, False)
    started[0].kwargs["target"](*started[0].kwargs["args"])

    assert prepared == [("测试账号", "固定测试内容")]
    assert sent == []
    assert app.events.get_nowait() == ("fixed_test_drafted", "测试账号", None, None, "")


def test_search_test_button_uses_search_only_route_and_shared_send_checkbox(monkeypatch):
    app = make_app()
    app.test_reply_send_var.set(True)
    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            started.append(self)

    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)

    app._start_search_contact_test()

    assert started[0].kwargs["args"] == ("测试账号", "固定测试内容", True, True)
    assert app.test_reply_button.options == {"state": "disabled"}
    assert app.test_search_button.options == {"state": "disabled"}
    sent = []
    app._send_reply_messages = lambda target, reply, **kwargs: sent.append(
        (target, reply, kwargs.get("contact_search_only"))
    )
    started[0].kwargs["target"](*started[0].kwargs["args"])

    assert sent == [("测试账号", "固定测试内容", True)]
    assert app.events.get_nowait() == ("fixed_test_sent", "测试账号", None, None, "")


def test_unchecked_search_test_button_only_fills_draft_using_search_route(monkeypatch):
    app = make_app()
    prepared = []
    app.bridge.prepare_by_search = lambda target, text: prepared.append((target, text))
    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            started.append(self)

    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)

    app._start_search_contact_test()
    assert started[0].kwargs["args"] == ("测试账号", "固定测试内容", False, True)
    started[0].kwargs["target"](*started[0].kwargs["args"])

    assert prepared == [("测试账号", "固定测试内容")]
    assert app.events.get_nowait() == ("fixed_test_drafted", "测试账号", None, None, "")


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
    app.test_search_button.configure(state="disabled")
    app.events.put(("fixed_test_sent", "测试账号", None, None, ""))

    app._poll()

    assert app.test_reply_active is False
    assert app.test_reply_button.options["state"] == "normal"
    assert app.test_search_button.options["state"] == "normal"
    assert "固定内容测试已发送给 测试账号" in app.status_detail_var.get()
    assert app.root.after_calls[0][0] == 250


def test_draft_result_reenables_button_and_states_not_sent():
    app = make_app()
    app.root = FakeRoot()
    app.test_reply_active = True
    app.test_reply_button.configure(state="disabled")
    app.test_search_button.configure(state="disabled")
    app.events.put(("fixed_test_drafted", "测试账号", None, None, ""))

    app._poll()

    assert app.test_reply_button.options["state"] == "normal"
    assert app.test_search_button.options["state"] == "normal"
    assert "已定位并将内容填入 测试账号 输入框" in app.status_detail_var.get()
    assert "未发送" in app.status_detail_var.get()


def test_status_ui_explains_fixed_test_is_immediate_and_does_not_call_model():
    source = (app_module.ROOT / "app.py").read_text(encoding="utf-8")

    assert '_make_collapsible_section(\n            content, "固定内容测试"' in source
    assert "text=\"测试联系人：\"" in source
    assert "text=\"固定回复内容：\"" in source
    assert 'text="是否发送消息"' in source
    assert 'text="开始测试"' in source
    assert 'text="使用搜索查找测试"' in source
    assert "优先查聊天列表" in source
    assert "直接使用搜索框" in source
    assert "不调用模型" in source
    assert "self.bridge.prepare_by_search" in source
    assert "self.bridge.prepare_message" in source
    worker = source.split("def _send_fixed_reply_test", 1)[1].split("def _set_fixed_test_button_state", 1)[0]
    assert "contact_search_only=contact_search_only" in worker
    assert ".engine." not in worker
    assert ".decide(" not in worker
