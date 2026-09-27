import queue
import threading
from concurrent.futures import Future
from pathlib import Path

from reply_core.wechat_bridge import WeChatBridge


def test_prepare_by_search_uses_search_only_and_never_requests_submit():
    bridge = WeChatBridge.__new__(WeChatBridge)
    bridge.config = {"dry_run": False}
    bridge._send_worker_ident = threading.get_ident()
    calls = []

    def fake_send_once(target, text, check, **kwargs):
        calls.append((target, text, check, kwargs))

    bridge._send_once = fake_send_once

    bridge.prepare_by_search("测试账号", "测试内容")

    assert calls == [
        ("测试账号", "测试内容", None, {"contact_search_only": True, "submit": False})
    ]


def test_prepare_message_keeps_list_first_lookup_and_never_requests_submit():
    bridge = WeChatBridge.__new__(WeChatBridge)
    bridge.config = {"dry_run": False}
    bridge._send_worker_ident = threading.get_ident()
    calls = []
    bridge._send_once = lambda target, text, check, **kwargs: calls.append(
        (target, text, check, kwargs)
    )

    bridge.prepare_message("测试账号", "测试内容")

    assert calls == [
        ("测试账号", "测试内容", None, {"contact_search_only": False, "submit": False})
    ]


def test_worker_dispatches_search_draft_task_without_send_submission():
    bridge = WeChatBridge.__new__(WeChatBridge)
    bridge._send_queue = queue.Queue()
    bridge._send_worker_ident = None
    observed = []
    bridge._send_once = lambda target, text, check, **kwargs: observed.append(
        (target, text, check, kwargs)
    )
    future = Future()
    bridge._send_queue.put((future, "测试账号", "测试内容", None, "search_draft"))

    worker = threading.Thread(target=bridge._send_worker_loop, daemon=True)
    worker.start()
    assert future.result(timeout=2) is None
    bridge._send_queue.put(None)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert observed == [
        ("测试账号", "测试内容", None, {"contact_search_only": True, "submit": False})
    ]


def test_search_draft_pastes_text_then_returns_before_submit_once():
    source = (Path(__file__).parents[1] / "reply_core" / "wechat_bridge.py").read_text(
        encoding="utf-8"
    )
    send_once = source.split("def _send_once(", 1)[1].split("def _submit_once", 1)[0]
    paste_index = send_once.index("self._paste_text_win32(wx, text)")
    draft_index = send_once.index("if not submit:", paste_index)
    submit_index = send_once.index("self._submit_once(wx", draft_index)

    assert paste_index < draft_index < submit_index
    assert "return" in send_once[draft_index:submit_index]
    assert "self._remember_wechat_gui(wx)" in send_once[draft_index:submit_index]
