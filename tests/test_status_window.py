from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_status_window_is_visible_and_stops_autoreply_when_closed():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "def _setup_status_window" in source
    assert "微信自动回复运行中" in source
    assert "关闭这个窗口会停止自动回复" in source
    assert "PID" in source
    assert "GPT-6 Luna" in source
    assert "应用模型" in source
    assert "模型强度：" in source
    assert "应用强度" in source
    assert "低档更快，高档思考更多" in source
    assert "self.root.withdraw()" not in source
    stop_body = source.split("def stop", 1)[1].split("if __name__", 1)[0]
    assert "self.config[\"enabled\"] = False" in stop_body
    assert "self._persist_config()" in stop_body


def test_desktop_launcher_uses_pythonw_so_gui_window_can_show():
    script = (ROOT / "scripts" / "start_auto_reply.ps1").read_text(encoding="utf-8")
    assert "pythonw.exe" in script
    assert "-WindowStyle Hidden" not in script
    assert "scripts\\start_auto_reply.ps1" in (ROOT / "start.ps1").read_text(encoding="utf-8")
    assert "scripts\\stop_auto_reply.ps1" in (ROOT / "stop.ps1").read_text(encoding="utf-8")
    assert "MainWindowTitle" in script
    assert "微信自动回复运行中" in script


def test_start_and_stop_target_only_this_apps_python_process():
    start = (ROOT / "scripts" / "start_auto_reply.ps1").read_text(encoding="utf-8")
    stop = (ROOT / "scripts" / "stop_auto_reply.ps1").read_text(encoding="utf-8")
    for script in (start, stop):
        assert "Get-WeChatAutoReplyProcesses" in script
    helper = (ROOT / "scripts" / "auto_reply_processes.ps1").read_text(encoding="utf-8")
    assert "$process.ExecutablePath -ieq $PythonwPath" in helper
    assert "$process.ExecutablePath -ieq $PythonPath" in helper
    assert "$process.CommandLine -match $scriptPattern" in helper


def test_model_selector_applies_to_future_codex_replies_and_persists(tmp_path):
    import json

    from app import Application

    class FakeVar:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class FakeEngine:
        model_name = "gpt-5.5"
        reasoning_effort = "low"
        settings = {"codex_model": "gpt-5.5"}

    app = Application.__new__(Application)
    app.model_choice_var = FakeVar("GPT-6 Luna")
    app.header_status_var = FakeVar("状态：开启    PID：1    模型：gpt-5.5（low）")
    app.status_detail_var = FakeVar("")
    app.config = {"enabled": True, "codex_model": "gpt-5.5"}
    app.config_path = tmp_path / "config.json"
    app.engine = FakeEngine()

    app._apply_model_selection()

    assert app.engine.model_name == "gpt-6-luna"
    assert app.engine.settings["codex_model"] == "gpt-6-luna"
    assert json.loads(app.config_path.read_text(encoding="utf-8"))["codex_model"] == "gpt-6-luna"
    assert "gpt-6-luna" in app.header_status_var.get()
    assert "之后新生成的回复" in app.status_detail_var.get()


def test_reasoning_effort_selector_applies_and_persists_without_changing_model(tmp_path):
    import json

    from app import Application

    class FakeVar:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class FakeEngine:
        model_name = "gpt-6-luna"
        reasoning_effort = "medium"
        settings = {"codex_model": "gpt-6-luna", "codex_reasoning_effort": "medium"}

    app = Application.__new__(Application)
    app.reasoning_choice_var = FakeVar("低（low）")
    app.header_status_var = FakeVar("状态：开启    PID：1    模型：gpt-6-luna（medium）")
    app.status_detail_var = FakeVar("")
    app.config = {
        "enabled": True,
        "codex_model": "gpt-6-luna",
        "codex_reasoning_effort": "medium",
    }
    app.config_path = tmp_path / "config.json"
    app.engine = FakeEngine()

    app._apply_reasoning_effort_selection()

    assert app.engine.model_name == "gpt-6-luna"
    assert app.engine.reasoning_effort == "low"
    assert app.engine.settings["codex_reasoning_effort"] == "low"
    saved = json.loads(app.config_path.read_text(encoding="utf-8"))
    assert saved["codex_model"] == "gpt-6-luna"
    assert saved["codex_reasoning_effort"] == "low"
    assert "gpt-6-luna（low）" in app.header_status_var.get()
    assert "之后新生成的回复将使用模型强度" in app.status_detail_var.get()


def test_reasoning_effort_options_are_supported_across_available_models():
    from app import REASONING_EFFORT_OPTIONS

    assert set(REASONING_EFFORT_OPTIONS.values()) == {"low", "medium", "high", "xhigh"}
