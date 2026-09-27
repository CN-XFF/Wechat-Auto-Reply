from reply_core.risk import detect_risks


def test_normal_chat_has_no_risk():
    assert detect_risks("晚饭吃什么") == []


def test_breakup_and_money_are_detected():
    assert "breakup" in detect_risks("我们分手吧")
    assert "money" in detect_risks("转我 200 块")


def test_self_harm_is_detected():
    assert "self_harm" in detect_risks("我不想活了")


def test_account_and_location_are_private():
    assert "privacy" in detect_risks("我要用一下你的港区账号")
    assert "privacy" in detect_risks("给我打个微信电话，我要定位")


def test_extreme_relationship_and_violence_are_sensitive():
    assert "breakup" in detect_risks("我不想你离开我")
    assert "self_harm" in detect_risks("孤单会死掉")
    assert "third_party_sensitive" in detect_risks("把别人炖成汤")
