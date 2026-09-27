from __future__ import annotations

import json
from pathlib import Path

from reply_core.engine import CodexReplyEngine
from reply_core.wechat_bridge import WeChatBridge

root = Path(__file__).resolve().parent
config = json.loads((root / "config.json").read_text(encoding="utf-8"))
bridge = WeChatBridge(config, root / "runtime")
print("微信数据库：正常")
print("目标会话：已唯一锁定（" + "、".join(bridge.targets) + "）")
print("近期文本上下文：可读取" if bridge.recent_context("联系人示例", 3, 300) else "近期文本上下文：为空")
engine = CodexReplyEngine(root, int(config.get("codex_timeout_seconds", 120)))
decision = engine.decide("联系人示例", config["targets"][0]["profile"], "今天晚饭吃什么呀？", "联系人示例: 今天有点饿\n我: 那我们想想吃什么")
print("Codex 登录调用：正常")
print(json.dumps(decision.__dict__, ensure_ascii=False))
