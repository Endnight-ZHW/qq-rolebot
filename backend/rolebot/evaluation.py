"""Frozen structural regression samples: no live model calls or QQ actions."""
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from .db import Database
from .memory import Memory
from .tools import ToolRunner
from .knowledge import Knowledge


def replay():
    results = []
    with tempfile.TemporaryDirectory(prefix="rolebot-replay-") as folder:
        db = Database(Path(folder)/"samples.sqlite")
        scope, other, actor, another = "10000001:g:20000001", "10000001:g:20000002", "30000001", "30000002"
        try:
            db.put("group", scope, {"id": scope, "persona": "default", "enabled": True})
            expected = db.save_memory(scope, "", "草莓蛋糕的配方是奶油与新鲜草莓", {"confidence": .6})
            db.save_memory(scope, "", "芒果蛋糕的配方是奶油和新鲜芒果", {"confidence": 1})
            found = db.recall(scope, "default", "草莓蛋糕")
            results.append({"name": "全文搜索相关度", "passed": bool(found and found[0]["id"] == expected), "expected": "完整匹配草莓蛋糕的记录优先"})
            db.put("member", scope+":"+actor, {"id": scope+":"+actor, "scope": scope, "user_id": actor, "name": "测试成员甲", "aliases": ["成员甲"], "remember": True})
            favorite = db.save_memory(scope, "", "喜欢喝乌龙茶", {"subject_ids": [actor], "confidence": .6})
            db.save_memory(scope, "", "喜欢喝奶茶", {"subject_ids": [another], "confidence": 1})
            found = db.recall(scope, "default", "成员甲爱喝什么")
            results.append({"name": "成员别名关联", "passed": bool(found and found[0]["id"] == favorite), "expected": "成员甲的别名绑定到真实 QQ"})
            db.add_message("quoted", scope, "default", actor, "user", "约定的地点是蓝桥", {"message_id": "1"})
            for i in range(90):
                db.add_message("filler"+str(i), scope, "default", actor, "user", "闲聊", {"message_id": str(i+10)})
            message = {"id": "current", "message_id": "1000", "scope": scope, "persona": "default", "user_id": actor, "text": "那在哪里？", "replies": ["1"], "mentions": [], "types": ["text"], "ts": time.time(), "remember": True}
            memory = Memory(db, SimpleNamespace())
            new = memory.build(scope, "default", "哪里", [message])[1]
            results.append({"name": "窗口外引用还原", "passed": any("蓝桥" in m.get("text", "") for m in new["context"]["quotes"]), "expected": "从同一角色会话还原引用原文"})
            batch = [message, {**message, "id": "second", "message_id": "1001", "user_id": another}]
            owner, _ = ToolRunner.bind(scope, batch, {"source_message_id": "current", "query": "1+1"})
            results.append({"name": "多人合批工具归属", "passed": owner["user_id"] == actor, "expected": "使用触发消息的作者，而非最后一人"})
            secret = db.save_memory(other, "", "隔壁群的固定秘密", {"pinned": True})
            results.append({"name": "跨群隔离", "passed": all(m["id"] != secret for m in db.recall(scope, "default", "秘密")), "expected": "不得跨群召回"})
            db.invalidate_memory(scope, source_ids=["quoted"])
            new = memory.build(scope, "default", "哪里", [message])[1]
            results.append({"name": "遗忘后引用不可恢复", "passed": "蓝桥" not in str(new) and "无法取得" in str(new), "expected": "原文被遗忘后只能显示缺失状态"})
            ledger = Knowledge(db)
            candidate = {"expression": "读书时间", "meaning": "准备聊天", "usage": "轻松闲聊", "examples": []}
            samples = []
            for i in range(5):
                text = "读书时间啦"
                samples.append({"id": "s"+str(i), "role": "user", "user_id": actor, "text": text, "ts": time.time(), "data": {"plain_text": text, "fragment_id": "a" if i < 3 else "b"}})
                candidate["examples"].append({"source_id": "s"+str(i), "quote": text})
            ledger.learn(scope, [candidate], samples[:4])
            premature = bool(ledger.styles(scope, True))
            ledger.learn(scope, [candidate], samples)
            results.append({"name": "学习样本门槛", "passed": not premature and len(ledger.styles(scope, True)) == 1, "expected": "五条样例、两个片段后启用；四条时不启用"})
        finally:
            db.close()
    return {"kind": "deterministic", "notice": "隔离数据库中的固定行为样例；不调用真实模型或 QQ，不代表语言质量评分", "cases": results,
            "passed": sum(r["passed"] is True for r in results), "total": len(results)}
