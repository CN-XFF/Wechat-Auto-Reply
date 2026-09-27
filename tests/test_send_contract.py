import json
from pathlib import Path


def test_bridge_has_single_send_action_and_no_send_msg_retry():
    source = (Path(__file__).parents[1] / "wechat_reply" / "wechat_bridge.py").read_text(encoding="utf-8")
    send_body = source.split("def send(self, target_name: str, text: str) -> None:", 1)[1]
    submit_body = source.split("def _submit_once", 1)[1].split("def send", 1)[0]
    config = json.loads((Path(__file__).parents[1] / "config.example.json").read_text(encoding="utf-8"))
    assert "wx.send_msg(" not in send_body
    assert "wx.click_send(" not in send_body
    assert "wx.input_text(" not in send_body
    assert "wx._input.key" not in submit_body
    assert "self._key(0x0D)" in submit_body
    assert "wx._chat_is_open(ui_name)" in send_body
    assert "self._paste_text_win32(wx, text)" in send_body
    assert "self._submit_once(wx" in send_body
    assert "target_username = self.targets[target_name]" in send_body
    assert "self._verify_sent_db(target_username, text, before=mark)" in send_body
    assert "不会重试" in send_body
    assert config["send_submit_method"] == "button"
