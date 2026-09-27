from __future__ import annotations

import re

RISK_PATTERNS: dict[str, tuple[str, ...]] = {
    "breakup": ("分手", "分开吧", "到此为止", "不想继续", "别联系", "拉黑", "删除好友", "结束关系", "离开我"),
    "argument": ("吵架", "生气", "失望", "你骗我", "讨厌你", "滚", "烦死", "凭什么", "不想理你", "会疯"),
    "money": ("转账", "红包", "借钱", "还钱", "欠钱", "收款", "付款"),
    "privacy": ("密码", "账号", "验证码", "身份证", "银行卡", "住址", "定位", "隐私", "聊天记录"),
    "meeting_promise": ("保证", "发誓", "一定会", "答应你", "几点见", "订票", "预订", "作业帮我", "帮我写"),
    "health_emergency": ("医院", "急救", "报警", "昏倒", "流血", "吃药", "怀孕"),
    "self_harm": ("自杀", "不想活", "活不下去", "伤害自己", "割腕", "跳楼", "死掉", "会死"),
    "third_party_sensitive": ("别告诉", "第三者", "出轨", "前任", "举报", "起诉", "关起来", "逃不掉", "杀人", "炖成汤"),
}

MONEY_RE = re.compile(r"(?:\d+(?:\.\d+)?\s*(?:元|块|块钱)|(?:给|转|还|借)我?\s*\d+)")


def detect_risks(*texts: str) -> list[str]:
    combined = "\n".join(t for t in texts if t)
    found = {category for category, words in RISK_PATTERNS.items() if any(word in combined for word in words)}
    if MONEY_RE.search(combined):
        found.add("money")
    return sorted(found)
