from pathlib import Path


def test_send_failure_does_not_enqueue_blocking_manual_popup():
    source = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
    delayed = source.split("def _delayed_send", 1)[1].split("def _confirm_dialog", 1)[0]
    assert "logging.exception(\"自动发送失败\")" in delayed
    assert "不弹窗阻塞" in delayed
    assert "self.events.put((\"manual\"" not in delayed
