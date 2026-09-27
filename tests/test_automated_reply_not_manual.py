import queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "wechatauto-replica"))
sys.path.insert(0, str(ROOT))

from app import Application  # noqa: E402
from reply_core.wechat_bridge import WeChatBridge  # noqa: E402


def test_previous_automated_reply_does_not_cancel_second_queued_message():
    rows = [
        {
            "sort_seq": 15,
            "local_id": 150,
            "sender_id": 2,
            "type": "文本",
            "content": "第一条自动回复",
        }
    ]
    bridge = WeChatBridge.__new__(WeChatBridge)
    bridge.db = SimpleNamespace(get_messages=lambda _username, limit: rows[:limit])
    bridge.targets = {"测试账号": "wxid_example_009"}
    bridge._automated_sent_lock = threading.Lock()
    bridge._automated_sent_ids = {}
    bridge._automated_sent_id_limit = 5000

    # Incoming message #2 (seq 14) is followed by the application's reply to #1 (seq 15).
    assert bridge._verify_sent_db("wxid_example_009", "第一条自动回复", before={(14, 140)})

    app = Application.__new__(Application)
    app.config = {"cancel_if_user_replied": True}
    app.bridge = bridge
    app.events = queue.Queue()
    second_msg = {"sort_seq": 14, "content": "第二条消息"}

    remaining = app._filter_answered_messages("测试账号", [(second_msg, "第二条消息")])

    assert remaining == [(second_msg, "第二条消息")]
    assert app.events.empty()


def test_manual_reply_after_second_message_still_cancels_it():
    rows = [
        {
            "sort_seq": 15,
            "local_id": 150,
            "sender_id": 2,
            "type": "文本",
            "content": "第一条自动回复",
        },
        {
            "sort_seq": 16,
            "local_id": 160,
            "sender_id": 1,
            "type": "文本",
            "content": "我自己手动回复",
        },
    ]
    bridge = WeChatBridge.__new__(WeChatBridge)
    bridge.db = SimpleNamespace(get_messages=lambda _username, limit: rows[:limit])
    bridge.targets = {"测试账号": "wxid_example_009"}
    bridge._automated_sent_lock = threading.Lock()
    bridge._automated_sent_ids = {}
    bridge._automated_sent_id_limit = 5000
    bridge._remember_automated_sent("wxid_example_009", (15, 150))

    app = Application.__new__(Application)
    app.config = {"cancel_if_user_replied": True}
    app.bridge = bridge
    app.events = queue.Queue()
    second_msg = {"sort_seq": 14, "content": "第二条消息"}

    remaining = app._filter_answered_messages("测试账号", [(second_msg, "第二条消息")])

    assert remaining == []
    event = app.events.get_nowait()
    assert event[0] == "status"
    assert "你已回复" in event[-1]


def test_manual_voice_reply_cancels_only_the_matching_contact():
    messages_by_username = {
        "wxid_example_005": [
            {
                "sort_seq": 15,
                "local_id": 150,
                "sender_id": 1,
                "type": "语音",
                "content": "[语音]",
            }
        ],
        "wxid_example_101": [],
    }
    queried = []
    bridge = WeChatBridge.__new__(WeChatBridge)
    bridge.db = SimpleNamespace(
        get_messages=lambda username, limit: (
            queried.append(username) or messages_by_username[username][:limit]
        )
    )
    bridge.targets = {"联系人示例": "wxid_example_005", "测试账号": "wxid_example_101"}
    bridge._automated_sent_lock = threading.Lock()
    bridge._automated_sent_ids = {}
    bridge._automated_sent_id_limit = 5000

    assert bridge.has_self_reply_after("联系人示例", 14) is True
    assert bridge.has_self_reply_after("测试账号", 14) is False
    assert queried == ["wxid_example_005", "wxid_example_101"]
