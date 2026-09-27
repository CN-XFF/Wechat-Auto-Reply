from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "wechatauto-replica"))
sys.path.insert(0, str(ROOT))

import wechatauto.db as db_module  # noqa: E402
import reply_core.wechat_bridge as bridge_module  # noqa: E402


def test_text_reply_bridge_skips_media_key_scan(monkeypatch, tmp_path):
    captured = {}

    class FakeDB:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeListener:
        def __init__(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(bridge_module, "WeChatDB", FakeDB)
    monkeypatch.setattr(bridge_module, "Listener", FakeListener)
    monkeypatch.setattr(bridge_module.WeChatBridge, "_resolve_targets", lambda _self: {})

    bridge_module.WeChatBridge(
        {"db_dir": "db", "account": "account", "targets": []}, tmp_path
    )

    assert captured["extract_media_key"] is False


def test_cached_text_database_does_not_scan_for_media_key(monkeypatch, tmp_path):
    db = db_module.WeChatDB.__new__(db_module.WeChatDB)
    db.db_dir = str(tmp_path)
    db.account = "account"
    db.workdir = str(tmp_path)
    db.keys_file = str(tmp_path / "keys.json")
    db._keys = {}
    db._db_files = []
    db.master_key = None
    db.cfg_dword = None
    db.extract_media_key = False

    monkeypatch.setattr(db, "_stable_key_file", lambda: None)
    monkeypatch.setattr(db, "extract_master_key", lambda: pytest.fail("unexpected memory scan"))
    monkeypatch.setattr(db_module, "_find_account_dirs", lambda _db_dir: [])

    db._load_or_extract_keys()

    assert db.unkeyed == []
