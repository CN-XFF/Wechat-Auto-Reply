import json
from pathlib import Path


def test_example_config_is_generic_and_safe():
    root = Path(__file__).parents[1]
    config = json.loads((root / "config.example.json").read_text(encoding="utf-8"))

    assert config["enabled"] is False
    assert config["dry_run"] is True
    assert config["targets"] == []
    assert config["command_channel_enabled"] is False
    assert config["codex_command_enabled"] is False
    assert config["command_password"] == ""

    assert config["account"].startswith("wxid_example_011")
    assert config["command_contact"] == ""
