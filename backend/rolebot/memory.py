from . import prompts
import json
import time
import asyncio
import hashlib

from .model import parse_json
from .knowledge import Knowledge
from .context import build
from .memory_semantics import extraction_context, view as memory_view
from . import tracing
from . import memory_sources as sources_context


class Memory:
    def __init__(self, db, model):
        self.db, self.model = db, model
        self.knowledge = Knowledge(db)
        self.locks = {}
        from .participation_memory import ParticipationMemory
        self.participation = ParticipationMemory(db)

    def epoch(self, scope):
        return self.db.get("memory_epoch", scope, {"value": 0})["value"]

    def build(self, scope, persona, query, transient=None, stage="reply", runtime=None):
        return build(self.db, self.model, scope, persona, query, transient, stage, runtime)

    def recover(self):
        # Recover active fact jobs; archived jobs are not scheduled.
        for job in self.db.scoped_objects("memory_job", statuses=("running",), limit=None):
            if job["status"] == "running":
                job.update(status="pending", retry_at=0)
                self.db.put("memory_job", job["id"], job)

    def enqueue(self, scope, persona, explicit_ids=()):
        group = self.db.get("group", scope, {})
        if not group.get("enabled") or group.get("paused"):
            return None
        window = None
        if explicit_ids:
            messages = [self.db.message(scope, id, persona) for id in explicit_ids]
            messages = [m for m in messages if m and m["role"] == "user" and not self.db.rows("SELECT 1 FROM memory_processed WHERE message_id=?", (m["id"],))]
        else:
            window, messages = self.participation.next_sources(scope, persona)
        if not messages:
            return None
        # Bound a single model request without skipping the next unprocessed source.
        selected, chars = [], 0
        for m in messages:
            if selected and chars+len(m["text"]) > 14000:
                break
            selected.append(m); chars += len(m["text"])
        source_ids = [m["id"] for m in selected]
        # Keep the stable job-key format so in-flight fact jobs are not duplicated.
        identity = [scope, persona, "extract", source_ids, self.epoch(scope), 0]
        if window: identity.append(window['id'])
        id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
        existing = self.db.get("memory_job", id)
        if existing:
            return id if existing["status"] in ("pending", "retry", "running") else None
        job = {"id": id, "scope": scope, "persona": persona, "kind": "extract", "source_ids": source_ids,
               "through_seq": max((m.get("seq", 0) for m in selected), default=0), "urgent": bool(explicit_ids),
               "epoch": self.epoch(scope), "status": "pending", "attempts": 0, "retry_at": 0, "ts": time.time()}
        if window:
            job.update(learning_session_id=window['id'])
        self.db.put("memory_job", id, job)
        return id

    async def process_job(self, id):
        first = self.db.get("memory_job", id, {})
        lock = self.locks.setdefault(first.get("scope", "")+":"+first.get("persona", ""), asyncio.Lock())
        async with lock:
            job = self.db.get("memory_job", id)
            if not job or job["status"] not in ("pending", "retry") or job.get("retry_at", 0) > time.time():
                return (job or {}).get("status", "missing")
            scope, persona = job["scope"], job["persona"]
            if job.get('kind') != 'extract' or not (job.get('urgent') or job.get('learning_session_id')):
                return 'superseded'
            group = self.db.get("group", scope, {})
            if not group.get("enabled") or group.get("paused"):
                return "paused"
            window = self.db.get('learning_session', job.get('learning_session_id', ''))
            if job.get('learning_session_id') and (not window or window['epoch'] != self.epoch(scope) or group.get('persona') != persona):
                if window:
                    window['status'] = 'cancelled'; self.db.put('learning_session', window['id'], window)
                job.update(status='cancelled'); self.db.put('memory_job', id, job); return 'cancelled'
            sources = [self.db.message(scope, id, persona) for id in job["source_ids"]]
            sources = [m for m in sources if m and self.knowledge.allowed(scope, [m["user_id"]])]
            sources = [m for m in sources if not self.db.rows("SELECT 1 FROM memory_processed WHERE message_id=?", (m["id"],))]
            if not sources and job["epoch"] == self.epoch(scope):
                with self.db.transaction():
                    job.update(status="complete", finished_at=time.time(), memory_ids=[], style_ids=[])
                    self.db.put("memory_job", id, job)
                return "complete"
            if job["epoch"] != self.epoch(scope) or not sources:
                job["status"] = "cancelled"; self.db.put("memory_job", id, job); return "cancelled"
            trace_id, token = tracing.begin(self.db, scope, persona, sources, kind=job["kind"])
            job.update(status="running", attempts=job["attempts"]+1, trace_id=trace_id)
            self.db.put("memory_job", id, job)
            status = "error"
            try:
                auxiliary = sources_context.supporting_context(self.db, scope, persona, sources)
                if window:
                    # Never sweep subsequent silent traffic into a frozen learning batch.
                    auxiliary = [m for m in auxiliary if m['id'] in window['source_ids']]
                # All auxiliary material participates in the same forgetting boundary.
                if auxiliary:
                    trace = self.db.get("trace", trace_id)
                    trace["source_ids"] = sorted({m["id"] for m in sources+auxiliary})
                    trace["actor_ids"] = sorted({m["user_id"] for m in sources+auxiliary})
                    self.db.put("trace", trace_id, trace)
                data = [{"id": m["id"], "qq": m["user_id"], "role": m["role"], "text": m["text"], "sent_at": m["data"].get("sent_at", m["ts"]), "fragment": m["data"].get("fragment_id"),
                         "reply": bool(m["data"].get("replies"))} for m in sources]
                prompt = prompts.render(self.db, scope, 'optimized.memory.extract') + prompts.render(self.db, scope, "optimized.memory.semantics")
                existing = self.db.recall(scope, persona, " ".join(m["text"] for m in sources)[-5000:], limit=24, subject_ids={m["user_id"] for m in sources})
                material = {"messages": data, "known_memories": [{**memory_view(self.db, m, compact=True), "fact_key": m["data"].get("fact_key")} for m in existing]}
                trace = self.db.get("trace", trace_id)
                trace["source_ids"] = sorted(set(trace["source_ids"]) | {id for m in existing for id in m["data"].get("source_ids", [])})
                trace["actor_ids"] = sorted(set(trace["actor_ids"]) | {q for m in existing for k in ("subject_ids","declarant_ids","object_ids") for q in m["data"].get(k,[])})
                self.db.put("trace", trace_id, trace)
                material["supporting_context"] = [sources_context.message_data(m) for m in auxiliary]
                tracing.stage(self.db, "memory_input", {"incremental_ids":[m["id"] for m in sources], "supporting_ids":[m["id"] for m in auxiliary], "kind":job["kind"]})
                material.update(extraction_context(self.db, scope, persona, sources+auxiliary))
                response = await self.model.chat(scope, "memory", [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(material, ensure_ascii=False)}], json_mode=True, stage=job["kind"])
                result = parse_json(response.get("content"))
                if not isinstance(result.get("memories"), list):
                    raise ValueError("记忆整理结果缺少 memories 数组")
                if self.epoch(scope) != job["epoch"] or any(not sources_context.available(self.db, scope, persona, m["id"]) for m in sources+auxiliary):
                    job["status"] = "cancelled"; status = "cancelled"
                elif window and (not self.db.get('learning_session', window['id']) or self.db.get('group', scope, {}).get('persona') != persona):
                    if self.db.get('learning_session', window['id']):
                        window['status'] = 'cancelled'; self.db.put('learning_session', window['id'], window)
                    job['status'] = 'cancelled'; status = 'cancelled'
                elif not (live_group := self.db.get("group", scope, {})).get("enabled") or live_group.get("paused"):
                    job["status"] = "pending"; status = "paused"
                else:
                    with self.db.transaction():
                        changed = self.knowledge.apply(scope, persona, result["memories"], sources, context_messages=auxiliary)
                        learned = self.knowledge.learn(scope, result.get("styles", []), sources, context_messages=auxiliary) if live_group.get("learning_enabled", True) and isinstance(result.get("styles", []), list) else []
                        for m in sources:
                            self.db.execute("INSERT OR IGNORE INTO memory_processed VALUES(?,?)", (m["id"], time.time()))
                        job.update(status="complete", finished_at=time.time(), memory_ids=changed, style_ids=learned)
                        self.db.put("memory_job", id, job)
                    tracing.stage(self.db, "memory_result", {"memory_ids": changed, "style_ids": learned, "source_count": len(sources)})
                    status = "complete"
            except asyncio.CancelledError:
                job.update(status="pending", retry_at=0); status = "cancelled"
                raise
            except Exception as exc:
                job.update(status="retry" if job["attempts"] < 3 else "failed", retry_at=time.time()+15*4**(job["attempts"]-1), error=type(exc).__name__)
                tracing.stage(self.db, "error", {"type": type(exc).__name__, "message": "整理失败，保留游标并按有限重试策略处理"}, content=False)
            finally:
                self.db.put("memory_job", id, job)
                tracing.finish(self.db, trace_id, status, token)
            return job["status"]

    async def background_once(self):
        if not self.model.configured:
            return
        for group in self.db.all("group"):
            if group.get("enabled") and not group.get("paused"):
                self.enqueue(group["id"], group["persona"])
        jobs = sorted([j for j in self.db.scoped_objects("memory_job", statuses=("pending", "retry"), limit=None, oldest=True)
            if j.get('kind') == 'extract' and (j.get('urgent') or j.get('learning_session_id'))], key=lambda j: (not j.get("urgent"), j["ts"]))[:100]
        for job in jobs:
            if job["status"] in ("pending", "retry") and job.get("retry_at", 0) <= time.time():
                if await self.process_job(job["id"]) != "paused":
                    break
