from __future__ import annotations

import json
from pathlib import Path

from wechat_reply.engine import CodexReplyEngine
from wechat_reply.wechat_bridge import WeChatBridge


root = Path(__file__).resolve().parent
config = json.loads((root / "config.json").read_text(encoding="utf-8"))
bridge = WeChatBridge(config, root / "runtime")
print("微信数据库连接：正常")
print(f"联系人映射：已加载 {len(bridge.targets)} 个")

# Use synthetic text only; diagnostics must not read or print private conversations.
engine = CodexReplyEngine(root, int(config.get("codex_timeout_seconds", 120)))
decision = engine.decide(
    "测试联系人",
    "使用简短、自然、礼貌的语气回复。",
    "你好，今天方便吗？",
    "测试联系人：你好\n我：在的，怎么啦？",
)
print("Codex 登录调用：正常")
print(json.dumps(decision.__dict__, ensure_ascii=False))
