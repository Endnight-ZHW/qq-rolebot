"""Request diagnostics, source links and frozen prompt versions."""
import time
from contextvars import ContextVar

from .db import uid

active_trace = ContextVar("rolebot_trace", default=None)


def begin(db, scope, persona, messages, kind="chat"):
    from .prompts import active_snapshot, snapshot
    id = uid()
    private = any(not m.get("remember", True) for m in messages)
    value = {"id": id, "scope": scope, "persona": persona, "kind": kind, "ts": time.time(),
             "parent_id": active_trace.get(),
             "source_ids": [m["id"] for m in messages], "actor_ids": sorted({m["user_id"] for m in messages}),
             "redacted": private, "stages": [], "models": [], "status": "running"}
    db.put("trace", id, value)
    return id, (active_trace.set(id), active_snapshot.set(snapshot(db, scope)))


def stage(db, name, data, content=True):
    id = active_trace.get()
    stage_for(db, id, name, data, content)


def stage_for(db, id, name, data, content=True):
    row = db.get("trace", id) if id else None
    if not row:
        return
    if hasattr(db,"scrub_log"):data=db.scrub_log(data)
    row["stages"].append({"name": name, "ts": time.time(), "data": {"notice": "内容已隐藏"} if content and row.get("redacted") else data})
    if name in ("context", "context_after_memory", "context_reply") and not row.get("redacted"):
        row["context"] = data
    db.put("trace", id, row)


def model_usage(db, data):
    id = active_trace.get()
    row = db.get("trace", id) if id else None
    if row:
        row["models"].append(data)
        db.put("trace", id, row)


def finish(db, id, status, token):
    from .prompts import active_snapshot
    row = db.get("trace", id)
    if row:
        row.update(status=status, duration_ms=round((time.time()-row["ts"])*1000))
        db.put("trace", id, row)
    active_trace.reset(token[0])
    active_snapshot.reset(token[1])
