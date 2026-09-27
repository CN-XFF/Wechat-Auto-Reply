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

    def get(self):
        return self.value

    def delete(self, _start, _end):
        self.value = ""

    def insert(self, _index, value):
        self.value = str(value)


def test_status_window_exposes_keyboard_and_mouse_cancel_settings():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'text="检测到键盘输入时取消"' in source
    assert 'text="鼠标移动超过"' in source
    assert 'text="像素时取消"' in source
    assert "mouse_move_threshold_px" in source
    assert "cancel_on_keyboard_input" in source
    assert "cancel_on_mouse_move" in source


def test_cancel_toggles_persist_and_mouse_threshold_is_validated(tmp_path):
    app = Application.__new__(Application)
    app.config = {
        "cancel_on_keyboard_input": True,
        "cancel_on_mouse_move": True,
        "mouse_move_threshold_px": 150,
    }
    app.config_path = tmp_path / "config.json"
    app.status_detail_var = FakeVar("")
    app.cancel_keyboard_var = FakeVar(False)
    app.cancel_mouse_var = FakeVar(False)
    app.mouse_threshold_widget = FakeEntry("45")

    app._save_cancel_toggle(
        "cancel_on_keyboard_input", app.cancel_keyboard_var, "键盘输入"
    )
    app._save_cancel_toggle("cancel_on_mouse_move", app.cancel_mouse_var, "鼠标移动")
    app._save_mouse_threshold_from_widget()

    saved = json.loads(app.config_path.read_text(encoding="utf-8"))
    assert saved["cancel_on_keyboard_input"] is False
    assert saved["cancel_on_mouse_move"] is False
    assert saved["mouse_move_threshold_px"] == 45
    assert "45 像素" in app.status_detail_var.get()


def test_invalid_mouse_threshold_keeps_existing_value(tmp_path):
    app = Application.__new__(Application)
    app.config = {"mouse_move_threshold_px": 150}
    app.config_path = tmp_path / "config.json"
    app.status_detail_var = FakeVar("")
    app.mouse_threshold_widget = FakeEntry("-1")

    app._save_mouse_threshold_from_widget()

    assert app.config["mouse_move_threshold_px"] == 150
    assert not app.config_path.exists()
    assert app.mouse_threshold_widget.get() == "150"
    assert "未更改设置" in app.status_detail_var.get()


def test_mouse_threshold_uses_configured_pixel_distance(monkeypatch):
    app = Application.__new__(Application)
    app.config = {"mouse_move_threshold_px": 20}
    monkeypatch.setattr(app, "_cursor_pos", lambda: (121, 100))

    assert app._mouse_moved((100, 100)) is True

    monkeypatch.setattr(app, "_cursor_pos", lambda: (120, 100))
    assert app._mouse_moved((100, 100)) is False


def test_disabled_keyboard_cancel_skips_key_state_poll():
    app = Application.__new__(Application)
    app.config = {"cancel_on_keyboard_input": False}

    assert app._keyboard_input_detected() is False
