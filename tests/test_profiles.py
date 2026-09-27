import json
from pathlib import Path


def test_contact_styles_are_separate_and_private_content_is_excluded():
    config = json.loads((Path(__file__).parents[1] / "config.json").read_text(encoding="utf-8"))
    profiles = {target["name"]: target["profile"] for target in config["targets"]}
    assert "站在用户本人视角" in profiles["联系人示例"]
    assert "短句连发" in profiles["联系人示例"]
    assert "我会改" in profiles["联系人示例"]
    assert "宝宝" in profiles["联系人示例"]
    assert "禁止模仿成人/私密表达" in profiles["联系人示例"]
    assert "游戏搭子" in profiles["联系人A"]
    assert "来、等会、开了" in profiles["联系人A"]
    assert "绝不使用宝宝" in profiles["联系人A"]
    assert "嗯呢等情侣/哄人语气" in profiles["联系人A"]
    assert "666" in profiles["联系人A"]
    assert "联系人B" in profiles
    assert "熟的同学/朋友" in profiles["联系人B"]
    assert "啊这" in profiles["联系人B"]
    assert "账号密码、定位" in profiles["联系人B"]
    assert "测试账号" in profiles
    assert "测试小号" in profiles["测试账号"]
    assert "测试正常" in profiles["测试账号"]
    assert "不要模仿历史中的极端占有" in profiles["测试账号"]
