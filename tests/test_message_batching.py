import queue
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "wechatauto-replica"))
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
from app import Application  # noqa: E402


def make_app(wait_seconds=1.0):
    app = Application.__new__(Application)
    app.config = {
        "enabled": True,
        "targets": [{"name": "Test Account", "auto_reply_enabled": True, "wait_seconds": wait_seconds}],
    }
    app.events = queue.Queue()
    app.pending_batches = {}
    app.inflight_targets = set()
    app.task_condition = threading.Condition()
    app.shutting_down = False
    app._target_auto_reply_enabled = lambda _name: True
    app._wait_seconds_for_target = lambda _name: wait_seconds
    app._cursor_pos = lambda: (10, 10)
    app._set_status = lambda _text: None
    app._handle_command_if_any = lambda _name, _content: False
    app._user_replied_after_incoming = lambda _name, _msg: False
    return app


def incoming(seq, text):
    return {"sender_id": 27, "type": "文本", "sort_seq": seq, "content": text}


def test_new_message_resets_debounce_and_merges_same_recipient(monkeypatch):
    app = make_app(wait_seconds=2.0)
    times = iter((100.0, 100.01))
    monkeypatch.setattr(app_module.time, "monotonic", lambda: next(times))
    app.on_message("Test Account", incoming(1, "第一句"), None)
    batch = app.pending_batches["Test Account"]
    first_deadline = batch["deadline"]

    app.on_message("Test Account", incoming(2, "第二句"), None)

    assert app.pending_batches["Test Account"] is batch
    assert batch["deadline"] > first_deadline
    assert [text for _, text in batch["items"]] == ["第一句", "第二句"]


def test_message_during_inflight_reply_becomes_serial_followup_task():
    app = make_app(wait_seconds=0.0)
    app.pending_batches["Test Account"] = {
        "items": [(incoming(1, "第一批"), "第一批")],
        "cursor": (10, 10),
        "deadline": time.monotonic() - 1,
        "created_at": time.monotonic() - 2,
    }
    first_started = threading.Event()
    allow_first_to_finish = threading.Event()
    second_started = threading.Event()
    generated = []

    def generate(_target, _msg, content):
        generated.append(content)
        if len(generated) == 1:
            first_started.set()
            assert allow_first_to_finish.wait(timeout=3)
            # The production pipeline keeps this contact in-flight until the
            # generated reply is sent or explicitly discarded.
            app._release_target_task(_target)
        else:
            second_started.set()
            app._release_target_task(_target)

    app._generate = generate
    worker = threading.Thread(target=app._message_worker, daemon=True)
    worker.start()
    assert first_started.wait(timeout=3)

    app.on_message("Test Account", incoming(2, "第二批"), None)
    followup = app.pending_batches["Test Account"]
    assert len(followup["items"]) == 1
    assert "Test Account" in app.inflight_targets

    allow_first_to_finish.set()
    assert second_started.wait(timeout=3)
    assert generated == ["第一批", "第二批"]

    with app.task_condition:
        app.shutting_down = True
        app.task_condition.notify_all()
    worker.join(timeout=3)
    assert not worker.is_alive()


def test_already_answered_messages_are_removed_from_batch():
    app = make_app()
    app._user_replied_after_incoming = lambda _target, msg: msg["sort_seq"] == 1
    items = [(incoming(1, "我已经回过"), "我已经回过"), (incoming(2, "新问题"), "新问题")]

    remaining = app._filter_answered_messages("Test Account", items)

    assert [text for _, text in remaining] == ["新问题"]
    event = app.events.get_nowait()
    assert event[0] == "status"
    assert "你已回复" in event[-1]
