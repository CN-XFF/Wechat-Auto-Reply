import json
import queue
import threading
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_each_target_has_auto_reply_checkbox_config():
    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert config["targets"] == []
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "self.target_auto_reply_vars" in source
    assert "auto_reply_enabled" in source


def test_status_window_has_contact_checkboxes_and_persists_changes():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "近期联系人设置" in source
    assert "tk.Checkbutton" in source
    assert "self.target_listen_vars" in source
    assert "self.target_auto_reply_vars" in source
    assert "def _set_target_listen" in source
    assert "def _set_target_auto_reply" in source
    assert "target[\"listen_enabled\"] = bool(enabled)" in source
    assert "target[\"auto_reply_enabled\"] = bool(enabled)" in source
    assert "self._persist_config()" in source


def test_fixed_test_command_and_recent_contacts_sections_are_collapsible():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    setup_body = source.split("def _setup_status_window", 1)[1].split(
        "def _build_test_reply_target_labels", 1
    )[0]

    assert '_make_collapsible_section(\n            content, "固定内容测试"' in setup_body
    assert '_make_collapsible_section(\n            content, "# 指令设置"' in setup_body
    assert '_make_collapsible_section(\n            content, "近期联系人设置"' in setup_body
    assert "body.grid_remove()" in setup_body
    assert "body.grid()" in setup_body
    assert "parent.update_idletasks()" in setup_body


def test_status_page_has_outer_scroll_and_routes_wheel_to_nested_panels():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    setup_body = source.split("def _setup_status_window", 1)[1].split(
        "def _build_test_reply_target_labels", 1
    )[0]

    assert 'page_scrollbar = tk.Scrollbar(' in setup_body
    assert 'scrollregion=page_canvas.bbox("all")' in setup_body
    assert 'self.root.bind_all("<MouseWheel>", _on_page_mousewheel)' in setup_body
    assert "command_canvas.yview_scroll(steps, \"units\")" not in setup_body
    assert "target_canvas.yview_scroll(steps, \"units\")" in setup_body
    assert 'canvas.bind("<Leave>"' not in setup_body
    assert "tk.Canvas(list_outer, height=300" in setup_body


def test_contact_remarks_refresh_from_wechat_each_time_the_app_starts():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    init_body = source.split("class Application:", 1)[1].split(
        "def _unique_target_name", 1
    )[0]
    refresh_body = source.split("def _refresh_target_remarks", 1)[1].split(
        "def _merge_recent_self_contacts", 1
    )[0]

    assert init_body.index("self._refresh_target_remarks()") < init_body.index(
        "self._merge_recent_self_contacts()"
    )
    assert "display_name_for_username(username" in refresh_body
    assert 'target["ui_name"] = current' in refresh_body
    assert "self.bridge.ui_names[target_name] = current" in refresh_body
    assert 'target["name"]' not in refresh_body
    assert 'label = ui_name or name' in source
    assert 'auto_reply_label = f"自动回复：{ui_name or name}"' in source


def test_contact_remarks_refresh_each_time_auto_reply_is_enabled_or_resumed():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    enable_body = source.split("def _set_target_auto_reply", 1)[1].split(
        "def _set_command_enabled", 1
    )[0]
    resume_body = source.split("if normalized in resume_commands:", 1)[1].split(
        "if normalized in status_commands:", 1
    )[0]
    global_switch_body = source.split("def _set_global_auto_reply_enabled", 1)[1].split(
        "def _sync_global_auto_reply_control", 1
    )[0]

    assert 'self._refresh_target_remarks("开启自动回复时")' in enable_body
    assert enable_body.index('self._refresh_target_remarks("开启自动回复时")') < enable_body.index(
        "self._ensure_listen_for_auto_reply(target_name)"
    )
    assert "self._set_global_auto_reply_enabled(True)" in resume_body
    assert 'self._refresh_target_remarks("恢复自动回复时")' in global_switch_body
    assert global_switch_body.index('self._refresh_target_remarks("恢复自动回复时")') < global_switch_body.index(
        "self.bridge.skip_existing_messages"
    )


def test_auto_reply_and_listen_checkboxes_stay_consistent():
    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert config["targets"] == []
    assert "def _ensure_listen_for_auto_reply" in source
    assert "target[\"listen_enabled\"] = True" in source
    assert "target[\"auto_reply_enabled\"] = False" in source
    assert "listen_var.set(True)" in source
    assert "auto_var.set(False)" in source
    assert "self._normalize_auto_reply_listen_settings(self.config)" in source


def test_startup_normalizes_auto_reply_to_listening_without_enabling_other_targets():
    from app import Application

    config = {
        "targets": [
            {"name": "回复目标", "auto_reply_enabled": True, "listen_enabled": False},
            {"name": "只监听", "auto_reply_enabled": False, "listen_enabled": True},
            {"name": "都关闭", "auto_reply_enabled": False, "listen_enabled": False},
        ]
    }

    changed = Application._normalize_auto_reply_listen_settings(config)

    assert changed == ["回复目标"]
    assert config["targets"][0]["listen_enabled"] is True
    assert config["targets"][1]["listen_enabled"] is True
    assert config["targets"][2]["listen_enabled"] is False


class _FakeVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _FakeBridge:
    def __init__(self):
        self.targets = {"联系人": "wxid_example_010"}
        self.added = []
        self.removed = []
        self.skipped = []

    def add_target_listener(self, name, _callback_factory):
        self.added.append(name)

    def remove_target_listener(self, name):
        self.removed.append(name)

    def skip_existing_messages(self, names):
        self.skipped.extend(names)


def _make_switch_app(auto_reply=False, listen=False):
    from app import Application

    app = Application.__new__(Application)
    app.config = {
        "enabled": True,
        "targets": [{
            "name": "联系人",
            "username": "wxid_example_010",
            "auto_reply_enabled": auto_reply,
            "listen_enabled": listen,
        }],
    }
    app.bridge = _FakeBridge()
    app.target_listen_vars = {"联系人": _FakeVar(listen)}
    app.target_auto_reply_vars = {"联系人": _FakeVar(auto_reply)}
    app.pending_batches = {}
    app.task_condition = threading.Condition()
    app.events = queue.Queue()
    app._persist_config = lambda: None
    app._set_status = lambda _status: None
    app._refresh_target_remarks = lambda _trigger: None
    app.callback_for = lambda name: name
    app._handle_command_if_any = lambda _name, _content: False
    return app


def test_listen_can_be_enabled_without_auto_reply():
    app = _make_switch_app()

    app._set_target_listen("联系人", True)
    assert app._active_listen_targets() == ["联系人"]
    assert app.config["targets"][0]["listen_enabled"] is True
    assert app.config["targets"][0]["auto_reply_enabled"] is False
    assert app.bridge.added == ["联系人"]

    app.on_message("联系人", {"sender_id": 3, "type": "文本", "content": "在吗", "sort_seq": 1}, None)

    event = app.events.get_nowait()
    assert event[0] == "status"
    assert "自动回复未勾选" in event[-1]
    assert app.pending_batches == {}


def test_enabling_auto_reply_also_enables_listening():
    app = _make_switch_app()

    app._set_target_auto_reply("联系人", True)

    assert app.config["targets"][0]["auto_reply_enabled"] is True
    assert app.config["targets"][0]["listen_enabled"] is True
    assert app.target_listen_vars["联系人"].get() is True
    assert app.bridge.skipped == ["联系人"]
    assert app.bridge.added == ["联系人"]


def test_disabling_listening_also_disables_auto_reply():
    app = _make_switch_app(auto_reply=True, listen=True)

    app._set_target_listen("联系人", False)

    assert app.config["targets"][0]["listen_enabled"] is False
    assert app.config["targets"][0]["auto_reply_enabled"] is False
    assert app.target_auto_reply_vars["联系人"].get() is False
    assert app.bridge.removed == ["联系人"]


def test_disabling_auto_reply_preserves_listening():
    app = _make_switch_app(auto_reply=True, listen=True)

    app._set_target_auto_reply("联系人", False)

    assert app.config["targets"][0]["listen_enabled"] is True
    assert app.config["targets"][0]["auto_reply_enabled"] is False
    assert app.bridge.removed == []


def test_status_window_has_style_rule_checkbox_and_editable_profile():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "self.target_style_rule_vars" in source
    assert "self.target_profile_widgets" in source
    assert "self.target_wait_widgets" in source
    assert "使用风格规则" in source
    assert "等待秒数" in source
    assert "def _save_target_wait_from_widget" in source
    assert "def _wait_seconds_for_target" in source
    assert "自定义风格" in source
    assert "def _set_target_style_rules" in source
    assert "target[\"use_style_rules\"] = bool(enabled)" in source
    assert "def _save_target_profile_from_widget" in source
    assert "target[\"profile\"] = profile" in source
    assert "保存风格" in source
    assert "修改自定义风格" in source
    assert "def _toggle_profile_panel" in source
    assert "启用 # 指令" in source
    assert "def _set_command_enabled" in source


def test_generation_uses_custom_profile_only_when_style_rules_enabled():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "def _profile_for_target" in source
    assert "if not bool(target.get(\"use_style_rules\", True)):" in source
    assert "不要套用任何情侣、兄弟、同学等单独风格" in source
    assert "self.engine.decide(target_name, self._profile_for_target(target_name), content, context)" in source


def test_unchecked_contact_skips_normal_auto_reply_but_command_path_stays_first():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    on_message = source.split("def on_message", 1)[1].split("def _handle_command_if_any", 1)[0]
    assert "if self._handle_command_if_any(target_name, content):" in on_message
    assert "if not self._target_auto_reply_enabled(target_name):" in on_message
    assert on_message.index("if self._handle_command_if_any(target_name, content):") < on_message.index(
        "if not self._target_auto_reply_enabled(target_name):"
    )
    assert "自动回复未勾选" in source


def test_unchecked_listen_contacts_are_not_registered_with_listener():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    bridge_source = (ROOT / "wechat_reply" / "wechat_bridge.py").read_text(encoding="utf-8")
    assert "def _target_listen_enabled" in source
    assert "def _active_listen_targets" in source
    assert "active_targets = self._active_listen_targets()" in source
    assert "self.bridge.listen(self.callback_for, active_targets)" in source
    assert "def listen(self, callback_factory, target_names" in bridge_source
    assert "def add_target_listener" in bridge_source
    assert "def remove_target_listener" in bridge_source


def test_rule_cancellation_is_marked_handled_instead_of_pending():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "def _complete_without_reply" in source
    assert "处理完成：target=%s" in source
    assert "(\"status\", target_name, msg, None" in source


def test_close_clears_pending_work_and_advances_watermarks():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    stop_body = source.split("def stop", 1)[1].split("if __name__", 1)[0]
    assert "def _clear_pending_work" in source
    assert "self.pending_batches.clear()" in source
    assert "self._drain_queue(self.events)" in source
    assert "self.pending_resend = None" in source
    assert "self.bridge.skip_existing_messages(list(self.bridge.targets))" in source
    assert "self._clear_pending_work()" in stop_body


def test_live_log_and_collapsible_style_editor_are_present():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "实时日志" in source
    assert "def _poll_live_log" in source
    assert "修改自定义风格" in source
    assert "panel.grid_remove()" in source
    assert "启用 # 指令" in source
    assert "def _set_command_enabled" in source
