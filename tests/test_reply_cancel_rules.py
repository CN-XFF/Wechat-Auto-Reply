import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_wait_and_cancel_rules_are_configured():
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert config["initial_reply_wait_seconds"] == 10.0
    assert config["cancel_on_mouse_move"] is True
    assert config["cancel_if_user_replied"] is True
    assert config["mouse_move_threshold_px"] == 150
    assert isinstance(config["cancel_on_keyboard_input"], bool)


def test_app_checks_mouse_and_manual_reply_before_generating_and_sending():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "def _cursor_pos" in source
    assert "def _mouse_moved" in source
    assert "def _keyboard_input_detected" in source
    assert "def _user_replied_after_incoming" in source
    assert "initial_reply_wait_seconds" in source
    assert "等待期间检测到键盘输入" in source
    assert "检测到你已经手动回复" in source
    assert "处理完成" in source
    assert "def _wait_seconds_for_target" in source


def test_bridge_can_detect_user_reply_after_incoming_message():
    source = (ROOT / "reply_core" / "wechat_bridge.py").read_text(encoding="utf-8")
    assert "def has_self_reply_after" in source
    assert "sort_seq > int(after_sort_seq)" in source
    assert "row.get(\"sender_id\") in {1, 2}" in source


def test_desktop_start_stop_files_exist():
    desktop = Path("D:/Desktop")
    start_cmd = (desktop / "开启自动回复.cmd").read_text(encoding="utf-8")
    stop_cmd = (desktop / "关闭自动回复.cmd").read_text(encoding="utf-8")
    assert "start_auto_reply.ps1" in start_cmd
    assert "stop_auto_reply.ps1" in stop_cmd
    assert (ROOT / "scripts" / "start_auto_reply.ps1").exists()
    assert (ROOT / "scripts" / "stop_auto_reply.ps1").exists()
