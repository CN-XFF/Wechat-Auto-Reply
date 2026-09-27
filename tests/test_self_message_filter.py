from pathlib import Path


def test_self_sender_ids_are_skipped_to_prevent_reply_loops():
    source = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
    assert "msg.get(\"sender_id\") in {1, 2}" in source
    assert "跳过自己发送的消息" in source
