import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor" / "wechatauto-replica"))

from first_run import (account_folders, choose_account, needs_setup,
                       normalize_data_location, prepare_configuration,
                       save_configuration, validate_account, ensure_configuration)
from reply_core.wechat_bridge import WeChatBridge


def make_account(root, name="wxid_fixture"):
    storage = root / name / "db_storage" / "session"
    storage.mkdir(parents=True)
    (storage / "session.db").write_bytes(b"metadata-only fixture")
    return root / name


@pytest.mark.parametrize("level", ["root", "account", "storage", "database"])
def test_detects_selected_directory_level_without_opening_database(tmp_path, level):
    root = tmp_path / "xwechat_files"
    account = make_account(root)
    selected = {"root": root, "account": account, "storage": account / "db_storage",
                "database": account / "db_storage/session/session.db"}[level]
    detected, preferred = normalize_data_location(selected)
    assert detected == root
    assert preferred == ("" if level == "root" else account.name)
    validate_account(root, account.name)


def test_rejects_account_with_no_session_database_and_path_traversal(tmp_path):
    (tmp_path / "account/db_storage").mkdir(parents=True)
    with pytest.raises(ValueError, match="session.db"):
        validate_account(tmp_path, "account")
    with pytest.raises(ValueError):
        validate_account(tmp_path, "../account")


def test_multiple_accounts_require_selection_and_preserve_user_choice(tmp_path):
    make_account(tmp_path, "account_a")
    make_account(tmp_path, "account_b")
    accounts = account_folders(tmp_path)
    assert choose_account(accounts, "", "", "") == ""
    assert choose_account(accounts, "", "account_b", "account_a") == "account_b"


def test_new_account_does_not_inherit_contact_or_command_access(tmp_path):
    make_account(tmp_path, "new_account")
    old = {"db_dir": str(tmp_path), "account": "old_account", "targets": [{"name": "fixture"}],
           "command_password": "fixture-secret", "command_contact": "fixture", "enabled": True}
    result = prepare_configuration(old, tmp_path, "new_account", True)
    assert result["targets"] == []
    assert result["command_password"] == result["command_contact"] == ""
    assert not result["enabled"] and result["dry_run"]
    assert not result["command_channel_enabled"] and not result["codex_command_enabled"]
    assert result["show_recent_self_contacts"]
    assert old["command_password"] == "fixture-secret"


def test_reconfiguration_disables_permissions_and_backs_up_original(tmp_path):
    make_account(tmp_path, "account")
    original = {"db_dir": str(tmp_path), "account": "account", "targets": [
        {"name": "fixture", "listen_enabled": True, "auto_reply_enabled": True, "wait_seconds": 6}],
        "enabled": True, "dry_run": False}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    result = prepare_configuration(original, tmp_path, "account", False)
    assert result["targets"][0]["name"] == "fixture"
    assert not result["targets"][0]["listen_enabled"]
    assert not result["targets"][0]["auto_reply_enabled"]
    assert result["targets"][0]["wait_seconds"] == 6
    save_configuration(path, result)
    assert json.loads(path.read_text()) == result
    backups = list((tmp_path / "backups").rglob("config.json"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == original
    assert not needs_setup(result)


def test_installer_template_triggers_setup_without_enabling_actions():
    sample = json.loads((ROOT / "config.example.json").read_text())
    assert needs_setup(sample)
    assert sample["db_dir"] == sample["account"] == ""
    assert sample["targets"] == [] and not sample["enabled"] and sample["dry_run"]
    assert not sample["codex_command_enabled"] and not sample["command_channel_enabled"]
    assert not sample["show_recent_self_contacts"]


def test_recent_contacts_include_incoming_only_personal_sessions():
    bridge = WeChatBridge.__new__(WeChatBridge)
    bridge.config = {"targets": []}
    requested = []
    def messages(username, limit):
        requested.append(username)
        return [{"type": "文本", "sender_id": 99}] if username != "system" else [{"type": "系统"}]
    bridge.db = SimpleNamespace(get_sessions=lambda limit: [
        {"username": "person"}, {"username": "group@chatroom"},
        {"username": "gh_official"}, {"username": "system"}], get_messages=messages)
    bridge.display_name_for_username = lambda username, fallback: "Fixture contact"
    found = bridge.recent_self_contacts()
    assert [x["username"] for x in found] == ["person"]
    assert requested == ["person", "system"]


def test_cancelled_wizard_leaves_config_untouched_and_fits_window(monkeypatch, tmp_path):
    import tkinter as tk
    sample = (ROOT / "config.example.json").read_bytes()
    (tmp_path / "config.example.json").write_bytes(sample)
    config_path = tmp_path / "config.json"
    config_path.write_bytes(sample)
    def cancel(window):
        window.withdraw()
        window.update_idletasks()
        assert window.winfo_reqheight() <= 560
        window.destroy()
    monkeypatch.setattr(tk.Tk, "mainloop", cancel)
    assert ensure_configuration(tmp_path) is False
    assert config_path.read_bytes() == sample
    assert not (tmp_path / "backups").exists()
