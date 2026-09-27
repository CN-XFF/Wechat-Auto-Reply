import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_command_channel_is_before_enabled_gate_and_only_for_hash_messages():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    on_message = source.split("def on_message", 1)[1].split("def _handle_command_if_any", 1)[0]
    assert "if self._handle_command_if_any(target_name, content):" in on_message
    assert "if not self.config.get(\"enabled\", True):" in on_message
    assert on_message.index("if self._handle_command_if_any(target_name, content):") < on_message.index(
        "if not self.config.get(\"enabled\", True):"
    )
    assert "not content.startswith(\"#\")" in source


def test_command_password_and_codex_exec_channel_are_configured():
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert config["command_contact"] == "测试账号"
    assert config["command_contact_username"] == "wxid_example_004"
    assert config["command_password"] == "071122"
    assert config["codex_command_enabled"] is True
    assert "codex_executable, \"exec\"" in source
    assert "--sandbox\", \"workspace-write\"" in source


def test_codex_result_receipt_bypasses_manual_reply_cancellation_gate():
    import threading
    from types import SimpleNamespace

    from app import Application

    sent = []
    app = Application.__new__(Application)
    app.send_lock = threading.Lock()
    app._cancel_reason_before_reply = lambda *_args: (_ for _ in ()).throw(
        AssertionError("Codex结果不应经过自动回复取消规则")
    )
    app.bridge = SimpleNamespace(
        send=lambda target, text: sent.append((target, text)),
        send_with_pre_submit_check=lambda *_args: (_ for _ in ()).throw(
            AssertionError("Codex结果不应使用带取消检查的发送入口")
        ),
    )

    assert app._send_command_reply("测试账号", "Codex 执行完成：已处理") is True
    assert sent == [("测试账号", "Codex 执行完成：已处理")]


def test_selected_contacts_can_enqueue_commands_after_username_binding():
    from types import SimpleNamespace

    from app import Application

    trusted_username = "wxid_example_004"
    other_username = "wxid_example_006_contact"
    disabled_username = "wxid_example_003"
    queued = []
    app = Application.__new__(Application)
    app.config = {
        "command_contact": "测试账号",
        "command_contact_username": trusted_username,
        "command_channel_enabled": True,
        "command_password": "071122",
        "targets": [
            {"name": "测试账号", "username": trusted_username, "command_enabled": True},
            {"name": "Other", "username": other_username, "command_enabled": True},
            {"name": "Disabled", "username": disabled_username, "command_enabled": False},
        ],
    }
    app.bridge = SimpleNamespace(
        targets={
            "测试账号": trusted_username,
            "Other": other_username,
            "Disabled": disabled_username,
        }
    )
    app.events = SimpleNamespace(put=queued.append)

    assert app._handle_command_if_any("Other", "#071122+查看状态") is True
    assert queued[0][0:2] == ("command", "Other")
    assert app._handle_command_if_any("Disabled", "#071122+修改电脑文件") is False
    assert app._handle_command_if_any("测试账号", "#wrong+修改电脑文件") is True
    assert len(queued) == 1
    app.bridge.targets["Other"] = "wxid_example_001"
    assert app._handle_command_if_any("Other", "#071122+修改电脑文件") is True
    assert len(queued) == 1
    app.bridge.targets["Other"] = other_username
    app.config["command_channel_enabled"] = False
    assert app._handle_command_if_any("Other", "#071122+查看状态") is True
    assert len(queued) == 1


def test_command_execution_methods_recheck_selected_contact(monkeypatch):
    from types import SimpleNamespace

    import app as app_module
    from app import Application

    app = Application.__new__(Application)
    app.config = {
        "command_contact": "测试账号",
        "command_contact_username": "wxid_example_004",
        "codex_command_enabled": True,
        "targets": [
            {"name": "测试账号", "username": "wxid_example_004", "command_enabled": True},
            {"name": "Other", "username": "wxid_example_006_contact", "command_enabled": False},
        ],
    }
    app.bridge = SimpleNamespace(
        targets={"测试账号": "wxid_example_004", "Other": "wxid_example_006_contact"}
    )
    receipts = []
    app._send_command_reply = lambda target, text: receipts.append((target, text)) or True
    app._send_command_reply_async = lambda target, text: receipts.append((target, text))
    monkeypatch.setattr(app_module.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("未经授权的联系人不得启动 codex exec")
    ))

    app._handle_command("Other", "修改电脑文件")
    app._run_codex_instruction("Other", "修改电脑文件")
    assert len(receipts) == 2
    assert all("未执行" in text for _, text in receipts)


def test_command_response_contact_falls_back_to_another_selected_contact():
    from types import SimpleNamespace

    from app import Application

    app = Application.__new__(Application)
    app.config = {
        "command_channel_enabled": True,
        "resend_confirmation_contact": "测试账号",
        "command_contact": "测试账号",
        "targets": [
            {"name": "测试账号", "username": "wxid_example_009", "command_enabled": False},
            {"name": "Other", "username": "wxid_example_006", "command_enabled": True},
        ],
    }
    app.bridge = SimpleNamespace(targets={"测试账号": "wxid_example_009", "Other": "wxid_example_006"})

    assert app._command_response_contact() == "Other"
    app.config["command_channel_enabled"] = False
    assert app._command_response_contact() is None


def test_duplicate_username_aliases_cannot_be_authorized():
    from types import SimpleNamespace

    from app import Application

    shared_username = "wxid_example_008"
    app = Application.__new__(Application)
    app.config = {
        "command_channel_enabled": True,
        "targets": [
            {"name": "First", "username": shared_username, "command_enabled": True},
            {"name": "Second", "username": shared_username, "command_enabled": True},
        ],
    }
    app.bridge = SimpleNamespace(
        targets={"First": shared_username, "Second": shared_username}
    )

    assert app._is_trusted_command_contact("First") is False
    assert app._is_authorized_command_contact("Second") is False


def test_normal_reply_codex_runs_in_read_only_sandbox():
    source = (ROOT / "reply_core" / "engine.py").read_text(encoding="utf-8")
    assert '"--sandbox", "read-only"' in source


def test_delayed_send_respects_pause():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    delayed = source.split("def _delayed_send", 1)[1].split("def _confirm_dialog", 1)[0]
    assert "self._cancel_reason_before_reply(target_name, msg)" in delayed
    assert "处理完成" in delayed
