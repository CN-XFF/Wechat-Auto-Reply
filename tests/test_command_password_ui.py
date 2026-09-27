import json
from pathlib import Path

from app import Application


ROOT = Path(__file__).parents[1]


class FakeVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeEntry:
    def __init__(self, value):
        self.value = str(value)
        self.options = {}

    def get(self):
        return self.value

    def delete(self, _start, _end):
        self.value = ""

    def insert(self, _index, value):
        self.value = str(value)

    def configure(self, **options):
        self.options.update(options)


class FakeButton:
    def __init__(self):
        self.options = {}

    def configure(self, **options):
        self.options.update(options)


def test_dedicated_command_panel_has_contact_selection_and_shared_password():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert '_make_collapsible_section(\n            content, "# 指令设置"' in source
    assert 'text="# 指令统一密码："' in source
    assert 'text="允许使用 # 指令的联系人（只对已配置并匹配微信账号 ID 的联系人生效）："' in source
    assert 'text="启用 # 指令通道"' in source
    assert 'text="允许执行 Codex 指令"' in source
    assert 'show="*"' in source
    assert 'text="保存密码"' in source
    assert "_toggle_command_password_visibility" in source
    assert "_save_command_password_from_widget" in source
    assert "self.target_command_vars[name] = command_var" in source


def test_saving_password_persists_new_value_without_logging_it(tmp_path, caplog):
    app = Application.__new__(Application)
    app.config = {"command_password": "TEST_ONLY_SECRET_002"}
    app.config_path = tmp_path / "config.json"
    app.status_detail_var = FakeVar("")
    app.command_password_widget = FakeEntry("new-value")

    app._save_command_password_from_widget()

    saved = json.loads(app.config_path.read_text(encoding="utf-8"))
    assert saved["command_password"] == "new-value"
    assert "new-value" not in caplog.text
    assert "立即使用新密码" in app.status_detail_var.get()


def test_empty_password_is_rejected_and_old_value_restored(tmp_path):
    app = Application.__new__(Application)
    app.config = {"command_password": "TEST_ONLY_SECRET_002"}
    app.config_path = tmp_path / "config.json"
    app.status_detail_var = FakeVar("")
    app.command_password_widget = FakeEntry("  ")

    app._save_command_password_from_widget()

    assert app.config["command_password"] == "old-value"
    assert not app.config_path.exists()
    assert app.command_password_widget.get() == "old-value"
    assert "不能为空" in app.status_detail_var.get()


def test_password_show_button_toggles_masking():
    app = Application.__new__(Application)
    app.command_password_widget = FakeEntry("secret")
    app.command_password_visibility_var = FakeVar(False)
    app.command_password_visibility_button = FakeButton()

    app._toggle_command_password_visibility()
    assert app.command_password_widget.options["show"] == ""
    assert app.command_password_visibility_button.options["text"] == "隐藏"

    app._toggle_command_password_visibility()
    assert app.command_password_widget.options["show"] == "*"
    assert app.command_password_visibility_button.options["text"] == "显示"
