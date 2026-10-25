from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .evaluation import replay
from .dashboard import validate_range


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Search(Body):
    scope: str
    persona: str = "default"
    query: str = Field(min_length=1, max_length=1000)


class StyleEdit(Body):
    meaning: str = Field(max_length=500)
    usage: str = Field(max_length=500)
    enabled: bool


class State(Body):
    status: Literal["active", "archived", "unverified"]


class Aliases(Body):
    aliases: list[str] = Field(max_length=20)


def register(api, db, engine):
    ledger = engine.memory.knowledge

    def require(scope, persona=None):
        if not db.get("group", scope) or (persona and not db.get("persona", persona)):
            raise HTTPException(404, "会话或角色不存在")

    def operate(fn):
        try:
            return fn()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    def invalidate(scope):
        db.clear_derived(scope)
        db.put("memory_epoch", scope, {"value": engine.memory.epoch(scope)+1})

    @api.get("/traces")
    async def traces(scope: str = "", limit: int = 40, offset: int = 0, start: float | None = None, end: float | None = None, status: str = '', kind: str = ''):
        validate_range(start, end)
        sql, args = "SELECT data FROM objects WHERE kind='trace'", []
        if scope:
            sql += " AND json_extract(data,'$.scope')=?"; args.append(scope)
        for field, op, value in (('ts','>=',start),('ts','<',end),('status','=',status or None),('kind','=',kind or None)):
            if value is not None:
                sql += f" AND json_extract(data,'$.{field}'){op}?"; args.append(value)
        rows = db.rows(sql+" ORDER BY updated DESC LIMIT ? OFFSET ?", (*args, max(1, min(limit, 100)), max(offset, 0)))
        import json
        return [{k: v for k, v in json.loads(r["data"]).items() if k not in ("context", "stages")} for r in rows]

    @api.get("/traces/{id}")
    async def trace(id: str):
        value = db.get("trace", id)
        if not value:
            raise HTTPException(404, "请求追踪不存在")
        return value

    @api.get("/profiles")
    async def profiles(scope: str, persona: str = "default"):
        require(scope, persona)
        return ledger.profiles(scope, persona)

    @api.put("/profiles/{scope}/{user_id}/aliases")
    async def aliases(scope: str, user_id: str, body: Aliases):
        value = db.get("member", scope+":"+user_id)
        if not value or any(not name.strip() or len(name) > 80 for name in body.aliases):
            raise HTTPException(422, "成员不存在或称呼无效")
        if not value.get("remember", True):
            raise HTTPException(409, "该成员已停止记忆")
        value["aliases"] = list(dict.fromkeys(x.strip() for x in body.aliases))
        db.put("member", value["id"], value)
        return value

    @api.get("/memories/{id}/versions")
    async def memory_versions(id: str, scope: str):
        return ledger.versions(scope, id)

    @api.post("/memories/{id}/restore/{version}")
    async def memory_restore(id: str, version: int, scope: str):
        operate(lambda: ledger.restore(scope, id, version)); invalidate(scope)
        db.audit(scope, "memory_restore", {"id": id, "version": version})
        return {"ok": True}

    @api.put("/memories/{id}/state")
    async def memory_state(id: str, scope: str, body: State):
        operate(lambda: ledger.set_status(scope, id, body.status)); invalidate(scope)
        return {"ok": True}

    @api.post("/memories/{id}/resolve")
    async def memory_resolve(id: str, scope: str):
        operate(lambda: ledger.resolve(scope, id)); invalidate(scope)
        db.audit(scope, "memory_resolve", {"id": id})
        return {"ok": True}

    @api.get("/styles")
    async def styles(scope: str):
        require(scope)
        return ledger.styles(scope)

    @api.put("/styles/{id}")
    async def style_update(id: str, scope: str, body: StyleEdit):
        result = operate(lambda: ledger.edit_style(scope, id, body.meaning, body.usage, body.enabled))
        invalidate(scope)
        return result

    @api.get("/styles/{id}/versions")
    async def style_versions(id: str, scope: str):
        return sorted([s for s in db.all("style_version") if s["scope"] == scope and s["style_id"] == id], key=lambda s: s["version"], reverse=True)

    @api.post("/styles/{id}/restore/{version}")
    async def style_restore(id: str, version: int, scope: str):
        result = operate(lambda: ledger.restore_style(scope, id, version)); invalidate(scope)
        return result

    @api.post("/memory/search")
    async def memory_search(body: Search):
        require(body.scope, body.persona)
        return {"results": db.recall(body.scope, body.persona, body.query), "notice": "当前会话的检索结果与排序依据"}

    @api.get("/memory/jobs")
    async def jobs(scope: str = ""):
        return db.scoped_objects("memory_job", scope or None, limit=100)

    @api.post("/memory/jobs/{id}/retry")
    async def retry(id: str):
        job = db.get("memory_job", id)
        if not job or job["status"] not in ("failed", "retry"):
            raise HTTPException(409, "任务不存在或当前不可重试")
        if job["epoch"] != engine.memory.epoch(job["scope"]):
            raise HTTPException(409, "来源已变更，旧任务不可重试；将由新任务重新整理")
        job.update(status="pending", attempts=0, retry_at=0)
        db.put("memory_job", id, job)
        return {"ok": True}

    @api.post("/evaluation/replay")
    async def replay_samples():
        return replay()
