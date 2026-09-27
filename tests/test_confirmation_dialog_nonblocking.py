import queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "wechatauto-replica"))
sys.path.insert(0, str(ROOT))

from app import Application  # noqa: E402


def test_confirmation_button_does_not_run_slow_checks_or_send_in_tk_callback():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    dialog = source.split("def _confirm_dialog", 1)[1].split(
        "def _send_confirmed_reply_background", 1
    )[0]

    assert "threading.Thread(" in dialog
    assert "send_state[\"sending\"]" in dialog
    assert "self._cancel_reason_before_reply(" not in dialog
    assert "self.bridge.send_with_pre_submit_check(" not in dialog


def test_confirmed_send_runs_in_worker_and_returns_ui_event():
    app = Application.__new__(Application)
    app.send_lock = threading.Lock()
    app.events = queue.Queue()
    app._cancel_reason_before_reply = lambda *_args: ""
    send_threads = []

    def send_with_check(_target, _text, check):
        send_threads.append(threading.get_ident())
        assert check() == ""

    app.bridge = SimpleNamespace(send_with_pre_submit_check=send_with_check)
    decision = SimpleNamespace(risk_categories=["sensitive_topic"])
    msg = {"sort_seq": 42}
    ui_context = {"test": True}
    main_thread_id = threading.get_ident()

    worker = threading.Thread(
        target=app._send_confirmed_reply_background,
        args=("Test Account", msg, "候选回复", decision, ui_context),
    )
    worker.start()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert send_threads and send_threads[0] != main_thread_id
    kind, target, event_msg, context, detail = app.events.get_nowait()
    assert (kind, target, event_msg, context, detail) == (
        "confirm_send_success", "Test Account", msg, ui_context, ""
    )


def test_uncertain_confirm_send_failure_disables_retry_and_keeps_dialog_for_check():
    class StubWidget:
        def __init__(self):
            self.options = []
            self.destroyed = False

        def winfo_exists(self):
            return True

        def configure(self, **options):
            self.options.append(options)

        def destroy(self):
            self.destroyed = True

    app = Application.__new__(Application)
    status = []
    released = []
    app._set_status = status.append
    app._release_target_task = released.append
    win = StubWidget()
    progress = StubWidget()
    send_button = StubWidget()
    ignore_button = StubWidget()
    send_state = {"sending": True, "failed": False}
    context = {
        "window": win,
        "progress": progress,
        "send_button": send_button,
        "ignore_button": ignore_button,
        "send_state": send_state,
    }

    app._finish_confirm_send(
        "confirm_send_failed", "Test Account", {"sort_seq": 42}, context, "校验超时"
    )

    assert send_state == {"sending": False, "failed": True}
    assert progress.options[-1]["text"].startswith("发送异常：校验超时")
    assert ignore_button.options[-1] == {"state": "normal", "text": "关闭（先核实）"}
    assert not send_button.options
    assert not win.destroyed
    assert not released
    assert status and "先核实微信会话" in status[-1]
