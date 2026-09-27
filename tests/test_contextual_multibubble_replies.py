import sys
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "wechatauto-replica"))
sys.path.insert(0, str(ROOT))

from app import Application, split_reply_messages  # noqa: E402
from wechat_reply.wechat_bridge import WeChatBridge  # noqa: E402


def test_reply_lines_are_separate_bubbles_and_capped_at_three():
    assert split_reply_messages("好呀\n我一会儿看看") == ["好呀", "我一会儿看看"]
    assert split_reply_messages("第一条\n第二条\n第三条\n第四条") == [
        "第一条", "第二条", "第三条 第四条"
    ]
    assert split_reply_messages("\n  只有一条  \n") == ["只有一条"]


def test_reply_parts_send_separately_in_order_and_keep_partial_retry_safe():
    sent = []

    class Bridge:
        def send_with_pre_submit_check(self, target, text, check):
            assert check() == ""
            sent.append((target, text))

    app = Application.__new__(Application)
    app.send_lock = threading.Lock()
    app.bridge = Bridge()
    app._send_reply_messages("Contact A", "先说一句\n再补一句", lambda: "")
    assert sent == [("Contact A", "先说一句"), ("Contact A", "再补一句")]

    class FailingBridge:
        def __init__(self):
            self.calls = []

        def send(self, target, text):
            self.calls.append((target, text))
            if len(self.calls) == 2:
                raise RuntimeError("发送结果待核实")

    failing = FailingBridge()
    app.bridge = failing
    try:
        app._send_reply_messages("Contact A", "第一句\n第二句\n第三句")
    except RuntimeError as exc:
        assert exc.remaining_reply_text == "第二句\n第三句"
        assert exc.sent_reply_parts == ("第一句",)
    else:
        raise AssertionError("expected an uncertain send failure")


def test_recent_context_marks_both_self_sender_ids_as_user_messages():
    bridge = WeChatBridge.__new__(WeChatBridge)
    bridge.targets = {"Contact A": "contact-id"}
    bridge.db = SimpleNamespace(
        get_messages=lambda _username, limit: [
            {"type": "文本", "sender_id": 0, "content": "对方的新话"},
            {"type": "文本", "sender_id": 1, "content": "我后来补充"},
            {"type": "文本", "sender_id": 2, "content": "我先说的话"},
        ]
    )

    assert bridge.recent_context("Contact A", 6, 1200) == (
        "我: 我先说的话\n我: 我后来补充\nContact A: 对方的新话"
    )


def test_prompt_uses_prior_user_messages_for_style_and_allows_optional_bubbles():
    source = (ROOT / "wechat_reply" / "engine.py").read_text(encoding="utf-8")
    assert "必须结合上下文理解指代、前后话题和已经回答过的内容" in source
    assert "上下文里“我:”的实际用词" in source
    assert "最多三条短消息" in source
    assert "默认回复一条" in source
