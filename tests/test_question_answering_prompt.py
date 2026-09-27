import json
from pathlib import Path


def test_prompt_allows_reasoned_answers_for_questions():
    source = (Path(__file__).parents[1] / "wechat_reply" / "engine.py").read_text(encoding="utf-8")
    assert "认真提问要先想出有用答案再压缩表达" in source
    assert "不能用“嗯/我看看”敷衍" in source
    assert "不编造事实" in source


def test_weather_questions_get_supplemental_lookup_instead_of_short_filler():
    root = Path(__file__).parents[1]
    source = (root / "wechat_reply" / "engine.py").read_text(encoding="utf-8")
    config = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
    assert "wttr.in" in source
    assert config["weather_lookup_enabled"] is False
    assert config["default_weather_location"] == ""
    assert "天气问题要直接告诉对方天气和简单建议，不要只回“我看看/等会”" in source or "天气补充信息可用时直接回答天气和建议" in source


def test_model_and_token_settings_are_explicit_and_compact():
    root = Path(__file__).parents[1]
    config = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
    source = (root / "wechat_reply" / "engine.py").read_text(encoding="utf-8")
    assert config["codex_model"] == "gpt-6-luna"
    assert config["context_messages"] == 30
    assert config["context_char_limit"] == 1200
    assert config["max_incoming_chars"] == 1200
    assert '"--model", self.model_name' in source
    assert 'model_reasoning_effort="{self.reasoning_effort}"' in source
