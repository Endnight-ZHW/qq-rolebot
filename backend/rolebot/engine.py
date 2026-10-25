import asyncio
import hashlib
import logging
import random
import time
from collections import deque

from .db import uid
from .config_schema import GROUP_NUMBERS
from .memory import Memory
from .onebot import normalize, scope_of
from .tools import default_policies
from .context import ContextTooLarge
from . import tracing, delivery
from .vision import Vision
from .tool_feedback import ground_reply
from .scheduler import Scheduler
from .stickers import Stickers
from .incoming_filter import IncomingFilter, WINDOW_SECONDS

log = logging.getLogger("rolebot")


def group_defaults(scope, name=""):
    return {"id": scope, "name": name or scope.split(":")[-1], "enabled": False, "paused": False,
            "management_paused": False, "persona": "default", "guidance": "",
            "cooldown": 60, "proactive_hourly": 10, "listening_probability": 1.0, "tools": default_policies(), "revision": 1,
            "learning_enabled": True, "followup_seconds": 120,
            'interaction_enabled': False, 'sticker_autocollect': False, 'sticker_limit': 100, 'sticker_max_mib': 5, **delivery.DEFAULTS, **GROUP_NUMBERS}


class Engine:
    def __init__(self, db, settings, onebot, model, media, tools):
        self.db, self.settings, self.onebot, self.model, self.media, self.tools = db, settings, onebot, model, media, tools
        db.asset_root = settings.data_dir
        self.memory = Memory(db, model)
        onebot.trace_db = db
        self.permissions = tools.permissions
        self.vision = Vision(self)
        from .attachments import Attachments
        self.attachments = Attachments(self)
        from .role_images import RoleImages
        self.role_images = RoleImages(self)
        self.scheduler = Scheduler(self)
        self.stickers = Stickers(self)
        from .creations import Creations
        self.creations = Creations(self)
        tools.engine = self
        self.queues, self.workers, self.tasks = {}, {}, set()
        self.started = time.time()
        self.stopping = False
        self.delivery_sleep = asyncio.sleep
        self.recent_events = {}
        self.visible_messages = {}
        self.incoming_filter = IncomingFilter()
        self.participation_random = random.random

    def observe_visible(self, event):
        """Count chat events without retaining their content (including commands/opt-outs)."""
        if event.get("post_type") not in ("message", "message_sent") or event.get("message_id") is None:
            return
        scope = scope_of(event)
        if ":g:" not in scope or not self.db.get("group", scope, {}).get("enabled"):
            return
        id = scope+":"+str(event["message_id"])
        recent = self.visible_messages.setdefault(scope, deque(maxlen=128))
        if any(item[0] == id for item in recent) or self.db.rows("SELECT 1 FROM seen WHERE id=?", (id,)):
            return
        recent.append((id, time.time()))

    def should_quote(self, scope, target, batch):
        """Quote only when at least four distinct messages followed the target."""
        if ":g:" not in scope or not target["message_id"].lstrip("-").isdigit():
            return False
        later = set()
        anchor = self.db.rows("SELECT seq FROM message_order WHERE message_id=?", (target["id"],))
        if anchor:
            rows = self.db.rows("SELECT m.id FROM messages m JOIN message_order o ON o.message_id=m.id WHERE m.scope=? AND o.seq>? ORDER BY o.seq LIMIT 4", (scope, anchor[0]["seq"]))
        else:
            rows = self.db.rows("SELECT id FROM messages WHERE scope=? AND ts>? AND id<>? ORDER BY ts LIMIT 4", (scope, target["ts"], target["id"]))
        later.update(row["id"] for row in rows)
        for messages in (batch, self.recent_events.get(scope, [])):
            index = next((i for i, m in enumerate(messages) if m["id"] == target["id"]), None)
            candidates = messages[index+1:] if index is not None else [m for m in messages if m["ts"] > target["ts"]]
            later.update(m["id"] for m in candidates if m["scope"] == scope)
        visible = list(self.visible_messages.get(scope, []))
        index = next((i for i, item in enumerate(visible) if item[0] == target["id"]), None)
        later.update(id for id, ts in (visible[index+1:] if index is not None else visible) if index is not None or ts > target["ts"])
        later.discard(target["id"])
        return len(later) > 3

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        def finish(t):
            self.tasks.discard(t)
            if not t.cancelled() and t.exception():
                log.error("background_task_failed: %s", type(t.exception()).__name__)
        task.add_done_callback(finish)
        return task

    async def start(self):
        if hasattr(self.model, "records"):
            self.model.records.recover()
            self.model.records.cleanup()
        self.memory.recover()
        delivery.recover(self.db)
        self.scheduler.recover()
        self.creations.recover()
        self.spawn(self.maintenance())
        self.spawn(self.task_worker())
        self.spawn(self.memory_worker())
        self.spawn(self.creation_worker())

    async def task_worker(self):
        while True:
            try:
                await self.scheduler.tick()
                self.stickers.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.db.audit('', 'error', {'stage': 'task_worker', 'type': type(exc).__name__})
            await asyncio.sleep(1)

    async def creation_worker(self):
        while True:
            try:
                await self.creations.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.db.audit('', 'error', {'stage':'creation_worker','type':type(exc).__name__})
            await asyncio.sleep(1)

    async def memory_worker(self):
        while True:
            try:
                await self.memory.background_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.db.audit("", "error", {"stage": "memory_worker", "type": type(exc).__name__})
            await asyncio.sleep(15)

    async def stop(self):
        self.stopping = True
        tasks = list(self.tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def receive(self, event):
        if str(event.get("self_id", "")) != self.onebot.self_id:
            return
        self.observe_visible(event)
        if event.get("post_type") == "request" and event.get("request_type") == "group":
            scope = scope_of(event)
            group = self.db.get("group", scope, {})
            if not group.get("enabled"):
                return
            request_id = hashlib.sha256((scope+str(event.get("flag", ""))).encode()).hexdigest()[:32]
            if self.db.get("join_request", request_id):
                return
            self.db.put("join_request", request_id, {"id": request_id, "scope": scope, "flag": event.get("flag"), "sub_type": event.get("sub_type", "add"), "status": "pending", "user_id": str(event.get("user_id", "")), "comment": str(event.get("comment", ""))[:2000]})
            event = {**event, "post_type": "message", "message_type": "group", "message_id": "join-"+request_id,
                     "message": [{"type": "text", "data": {"text": f"[待审入群申请 request_id={request_id}] {str(event.get('comment',''))[:2000]}"}}]}
        if event.get("post_type") != "message":
            return
        m = normalize(event)
        if m["user_id"] == self.onebot.self_id or m["event"].get("message_sent_type"):
            return
        if m["text"].strip().split(maxsplit=1)[:1] == ["/bot"]:
            if not self.db.mark_seen(m["id"]):
                return
            await self.command(m)
            return
        scope = m["scope"]
        group = self.db.get("group", scope)
        if not group and ":p:" in scope and self.db.config().get("private_enabled"):
            group = group_defaults(scope, m["name"])
            group["enabled"] = True
            self.db.put("group", scope, group)
        if not group or not group.get("enabled") or not self.db.config().get("initialized"):
            return
        if not self.db.mark_seen(m["id"]):
            return
        m["persona"] = group["persona"]
        m["memory_epoch"] = self.memory.epoch(scope)
        focus = self.db.get('followup', scope+':'+m['persona'], {})
        pending_answer = focus.get('source_id') if focus.get('target') == m['user_id'] and focus.get('expires', 0) > time.time() else None
        generation = (m['persona'], m['memory_epoch'], group.get('revision', 0),
                      self.permissions.config(scope).get('revision', 0), pending_answer)
        ignored, input_key = self.incoming_filter.check(m, self.onebot.self_id, generation)
        if ignored:
            self.db.audit(scope, 'message_filter', {'status': 'ignored', 'reason': ignored,
                          'source_id': m['id'], 'actor': m['user_id'], 'window_seconds': WINDOW_SECONDS})
            return
        member_key = scope + ":" + m["user_id"]
        member = self.db.get("member", member_key, {"id": member_key, "scope": scope, "user_id": m["user_id"], "remember": True})
        member["name"] = m["name"]; member["last_seen"] = time.time()
        self.db.put("member", member_key, member)
        m["remember"] = member.get("remember", True)
        self.creations.cancel_from_message(m)
        previous = self.recent_events.get(scope, [])
        fragment = self.db.get("fragment", scope+":"+m["persona"], {})
        if time.time()-fragment.get("last", 0) > 120:
            fragment = {"id": uid()}
        fragment["last"] = time.time()
        self.db.put("fragment", scope+":"+m["persona"], fragment)
        m["data"] = {"name": m["name"], "message_id": m["message_id"], "mentions": m["mentions"], "replies": m["replies"],
                     "forwards": m["forwards"],
                     "sent_at": m["sent_at"], "received_at": m["ts"], "types": m["types"], "plain_text": m["plain_text"],
                     "truncated": m["truncated"], "original_chars": m["original_chars"], "fragment_id": fragment["id"], "image_count": len(m["images"])}
        self.recent_events[scope] = [x for x in previous if time.time()-x["ts"] < 180][-39:] + [m]
        if m["remember"]:
            self.db.add_message(m["id"], scope, m["persona"], m["user_id"], "user", m["text"], m["data"])
            from .attachments import register
            register(self.db, scope, m["persona"], m)
        if group.get("paused") or not self.model.configured or not self.permissions.can_respond(scope, m["user_id"]):
            return
        if scope not in self.queues:
            self.queues[scope] = asyncio.Queue(maxsize=200)
            self.workers[scope] = self.spawn(self.worker(scope))
        try:
            self.queues[scope].put_nowait(m)
            self.incoming_filter.remember(input_key)
        except asyncio.QueueFull:
            self.db.audit(scope, "queue", {"status": "dropped", "reason": "消息队列已满"})

    async def worker(self, scope):
        queue = self.queues[scope]
        while True:
            first = await queue.get()
            batch = [first]
            await asyncio.sleep(1.2)
            while not queue.empty() and len(batch) < 20:
                batch.append(queue.get_nowait())
            batch = [m for m in batch if time.time()-m["ts"] < 120]
            if not batch:
                continue
            try:
                # Per-group workers serialize turns; Model.limit bounds global LLM
                # requests. Sending pauses must not occupy another group's slot.
                await self.turn(scope, batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.db.audit(scope, "error", {"stage": "turn", "type": type(exc).__name__, "message": str(exc)[:200]})

    def current(self, scope, persona, epoch):
        group = self.db.get("group", scope, {})
        return group.get("enabled") and not group.get("paused") and group.get("persona") == persona and self.memory.epoch(scope) == epoch

    def direct(self, batch, card):
        for m in batch:
            if ":p:" in m["scope"] or self.onebot.self_id in m["mentions"]:
                return True
            if any(alias and alias in m["text"] for alias in card.get("aliases", [])):
                return True
            for reply in m["replies"]:
                rows = self.db.rows("SELECT id FROM messages WHERE scope=? AND role='assistant' AND json_extract(data,'$.message_id')=?", (m["scope"], reply))
                if rows:
                    return True
        return False

    async def turn(self, scope, batch):
        epoch = self.memory.epoch(scope)
        batch = [m for m in batch if self.permissions.can_respond(scope, m["user_id"]) and m.get("memory_epoch", epoch) == epoch and (not m.get("remember", True) or self.db.message(scope, m["id"], m["persona"]))]
        for m in batch:
            m["permission_revision"] = self.permissions.config(scope).get("revision", 0)
        if not batch:
            return
        persona = self.db.get("group", scope, {}).get("persona", "default")
        id, token = tracing.begin(self.db, scope, persona, batch)
        status = "complete"
        try:
            status = await self.chat_turn(scope, batch) or "complete"
        except asyncio.CancelledError:
            status = "cancelled"; raise
        except Exception as exc:
            status = "error"
            tracing.stage(self.db, "error", {"type": type(exc).__name__, "message": str(exc)[:200]}, content=True)
            raise
        finally:
            tracing.finish(self.db, id, status, token)


    async def bound_tool(self, scope, persona, batch, name, arguments):
        from .message_targets import active_recall
        from .request_evidence import active_request_evidence
        token = active_recall.set(None)
        request_token = active_request_evidence.set(None)
        try:
            return await self._bound_tool(scope,persona,batch,name,arguments)
        finally:
            active_recall.reset(token)
            active_request_evidence.reset(request_token)

    async def _bound_tool(self, scope, persona, batch, name, arguments):
        from .tools import ToolDenied, SPECS
        from pydantic import ValidationError
        try:
            source, clean = self.tools.bind(scope, batch, arguments)
            if name not in SPECS:
                raise ToolDenied("工具不存在")
            if not self.audience_current(scope, batch):
                raise ToolDenied("本轮账号权限已变化")
            clean = SPECS[name].args.model_validate(clean).model_dump()
            await self.permissions.refresh(scope, [m['user_id'] for m in batch])
            if name == 'message_recall':
                from .message_targets import prepare
                # Enable/rate checks precede any read-only recovery; target checks
                # follow the identity resolution and independent source review.
                self.tools.check_policy(scope,name,clean,skip_target=True)
                clean = await prepare(self.tools,scope,persona,source,clean,batch)
                if not self.audience_current(scope,batch):
                    raise ToolDenied('核验期间请求者权限已变化',code='context_changed')
            self.tools.check_policy(scope, name, clean, defer_recall=True)
            permit, mode = await self.tools.intent.route(scope, persona, batch, source, name, clean)
            if not self.audience_current(scope, batch) or not self.current(scope, persona, self.memory.epoch(scope)):
                if permit is not None:
                    self.tools.intent.permits.pop(id(permit), None)
                raise ToolDenied("复核期间会话状态或账号权限已变化")
        except (ToolDenied, ValidationError) as exc:
            value = {"status": "denied", "error": str(exc), "error_code": getattr(exc, "code", "invalid_arguments"), 'phase':'authorization', 'submitted':False}
            self.db.audit(scope, "tool", {"name": name, **value, "actor": locals().get("source", {}).get("user_id", ""), "event_id": locals().get("source", {}).get("id", "")})
            tracing.stage(self.db, "tool", {"name": name, **value})
            return value
        result = await self.tools.run(scope, persona, source["user_id"], source["id"], name, clean, permit=permit, mode=mode, source=source, audience=[m['user_id'] for m in batch])
        tracing.stage(self.db, "tool", {"name": name, "actor": source["user_id"], "source_id": source["id"], "arguments": clean, **result})
        return result

    def audience_current(self, scope, batch):
        revision = self.permissions.config(scope).get("revision", 0)
        return all(self.permissions.can_respond(scope, m["user_id"]) and m.get("permission_revision", revision) == revision for m in batch)

    def ground_reply(self, reply, batch, executed, target=None):
        runtime = self.tools.runtime_status(batch[0]["scope"], batch) if batch else None
        text, reason = ground_reply(reply, batch, executed, runtime, target=target)
        if reason:
            tracing.stage(self.db, "tool_reply_corrected", {"reason": reason}, content=False)
        return text

    def reply_provenance(self, scope, persona, preview, batch, executed=()):
        context = preview.get("context", {})
        sources = {m["id"] for m in batch}
        users = {m["user_id"] for m in batch}
        memory_ids = set(preview.get("memory_ids", []))
        for message in context.get("消息", []):
            sources.add(message["id"])
            users.add(message.get("qq", ""))
            for quote in message.get("replies", []):
                if quote.get("id"):
                    sources.add(quote["id"])
        for memory in context.get("相关记忆", []):
            sources.update(memory.get("source_ids", []))
        for quote in context.get('quotes', [])+context.get('retrieved_history', []):
            if quote.get('id'): sources.add(quote['id'])
            if quote.get('qq'): users.add(quote['qq'])
        for tool in executed:
            if tool.get("name") == "memory_search" and tool.get("status") == "success":
                for memory in tool.get("result") or []:
                    memory_ids.add(memory["id"])
                    sources.update(memory.get("source_ids", []))
                    for key in ("subject_ids", "declarant_ids", "object_ids"):
                        users.update(memory.get(key, []))
            if tool.get("name") in ("history_search", "history_read") and tool.get("status") == "success":
                for message in (tool.get("result") or {}).get("messages", []):
                    sources.add(message["id"])
                    users.add(message["qq"])
                for record in (tool.get('result') or {}).get('operations', []):
                    sources.add(record['source_id'])
                    users.update([record.get('actor_qq', ''), record.get('target_qq', '')])
        sources.update(preview.get("image_source_ids", []))
        sources.update(preview.get("forward_source_ids", []))
        for memory in self.db.memories(scope, persona):
            if memory["id"] in preview.get("memory_ids", []):
                users.update(memory["data"].get("subject_ids", []))
                users.update(memory["data"].get("declarant_ids", []))
                users.update(memory["data"].get("object_ids", []))
        return {"source_ids": sorted(sources), "subject_ids": sorted(users-{self.onebot.self_id, ""}), "memory_ids": sorted(memory_ids),
                **({'image_search_refs': preview['image_search_refs']} if preview.get('image_search_refs') else {})}


    async def chat_turn(self, scope, batch):
        from .conversation import Conversation
        group = self.db.get('group', scope, {})
        persona = group.get('persona', 'default')
        batch = [m for m in batch if m['persona'] == persona and self.permissions.can_respond(scope, m['user_id'])]
        if not batch or not self.current(scope, persona, self.memory.epoch(scope)) or not self.audience_current(scope, batch):
            return 'cancelled'
        card = self.db.get('persona', persona, {})
        if ':g:' in scope and not self.direct(batch, card) and not any(m.get('event', {}).get('request_type') for m in batch):
            probability = group.get('listening_probability', 1.0)
            draw = self.participation_random() if 0 < probability < 100 else None
            selected = probability >= 100 or (probability > 0 and draw < probability / 100)
            tracing.stage(self.db, 'participation.sample', {'probability_percent': probability,
                          'draw': draw, 'selected': selected}, content=False)
            if not selected:
                return 'silent'
        runner = Conversation(self)
        try:
            return await runner.run(scope, batch)
        except ContextTooLarge as exc:
            tracing.stage(self.db, 'context_limit', {'message': str(exc)}, content=False)
            group = self.db.get('group', scope, {})
            card = self.db.get('persona', group.get('persona', ''), {})
            rate = self.db.get('chat_state', scope, {})
            sent = [t for t in rate.get('sent', []) if t > time.time()-60]
            if group.get('enabled') and not group.get('paused') and self.audience_current(scope, batch) and self.direct(batch, card) and len(sent) < 6:
                await self.onebot.send(scope, [{'type':'text','data':{'text':'这次的内容超过当前上下文预算啦，请分开发送或在后台提高预算，。'}}])
                rate['sent'] = sent+[time.time()]
                self.db.put('chat_state', scope, rate)
            return 'context_limit'

    async def command(self, m):
        group=self.db.get("group",m["scope"],{})
        if not group.get("enabled"):
            return await self._command(m)
        source={**m,"remember":self.db.get("member",m["scope"]+":"+m["user_id"],{}).get("remember",True)}
        id,token=tracing.begin(self.db,m["scope"],group.get("persona",""),[source],kind="command")
        status="complete"
        try:
            tracing.stage(self.db,"command",{"source_id":m["id"],"actor":m["user_id"],"text":m["text"],"model_called":False})
            return await self._command(m)
        except BaseException:status="error";raise
        finally:tracing.finish(self.db,id,status,token)

    async def _command(self, m):
        scope, actor = m["scope"], m["user_id"]
        group = self.db.get("group", scope, {})
        is_owner = actor in self.db.config()["admin_qq"]
        if not group.get("enabled") and not is_owner:
            return
        parts = m["text"].strip().split(maxsplit=2)
        cmd = parts[1] if len(parts) > 1 else "help"
        from .permissions import COMMANDS
        if cmd not in COMMANDS:
            cmd = "help"
        effective = await self.permissions.effective(scope, actor)
        group = self.db.get("group", scope, {})
        if not group.get("enabled") and not is_owner:
            return
        if "command:"+cmd not in effective["allow"]:
            self.db.audit(scope, "command", {"actor": actor, "command": cmd, "status": "denied", "reason": "该 QQ 无此指令权限"})
            if self.permissions.can_respond(scope, actor):
                await self.onebot.send(scope, [{"type": "text", "data": {"text": "你在本群还没有这项权限，可以用 /bot permissions 查看。"}}])
            return
        reply = "可用指令：" + "、".join("/bot "+name for name in COMMANDS if "command:"+name in effective["allow"])
        if cmd == "permissions":
            labels = {x["id"]: x["title"] for x in self.permissions.catalog()}
            reply = "你在当前会话的权限：" + "、".join(labels.get(k, k) for k in effective["allow"])
            reply += "。工具仍受本群开关、参数范围和 QQ 实际权限限制。"
        if cmd == "status":
            reply = f"机器人在线；模型{'已配置' if self.model.configured else '待配置'}；当前角色 {group.get('persona','未启用')}；{'已暂停' if group.get('paused') else '运行中'}。"
        elif cmd in ("pause", "resume", "tools-off", "role", "reset") and group:
            if cmd in ("pause", "resume"):
                group["paused"] = cmd == "pause"; reply = "已暂停当前会话。" if group["paused"] else "已恢复当前会话。"
            elif cmd == "tools-off":
                group["management_paused"] = True; reply = "当前群的管理工具已全部紧急停用，请在后台恢复。"
            elif cmd == "role":
                target = parts[2] if len(parts) > 2 else ""
                if self.db.get("persona", target):
                    group["persona"] = target; reply = "已切换角色，原角色记忆已保留。"
                else:
                    reply = "可用角色：" + "、".join(x["id"]+"（"+x["name"]+"）" for x in self.db.all("persona"))
            elif cmd == "reset":
                self.db.put("context_reset", scope+":"+group["persona"], {"ts": time.time()})
                self.db.delete("summary", scope+":"+group["persona"])
                self.db.put("memory_epoch", scope, {"value": self.memory.epoch(scope)+1})
                reply = "当前上下文已重置，长期记忆保留。"
            group["revision"] = group.get("revision", 0)+1
            self.db.put("group", scope, group)
            self.db.audit(scope, "command", {"actor": actor, "command": cmd, "status": "success"})
        await self.onebot.send(scope, [{"type": "text", "data": {"text": reply}}])

    async def maintenance(self):
        last_cleanup = 0
        while True:
            try:
                if time.time()-last_cleanup > 3600:
                    cfg = self.db.config()
                    self.db.execute("DELETE FROM messages WHERE ts<?", (time.time()-cfg["retention_days"]*86400,))
                    self.db.execute("DELETE FROM seen WHERE ts<?", (time.time()-7*86400,))
                    self.db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
                    self.db.execute("DELETE FROM memory_processed WHERE message_id NOT IN (SELECT id FROM messages)")
                    self.db.execute("DELETE FROM objects WHERE kind='trace' AND updated<?", (time.time()-cfg["model_log_days"]*86400,))
                    self.db.execute("DELETE FROM objects WHERE kind='summary_backup' AND updated<?", (time.time()-7*86400,))
                    if hasattr(self.model, "records"): self.model.records.cleanup()
                    self.db.execute("DELETE FROM objects WHERE kind='reply_delivery' AND updated<?", (time.time()-cfg["retention_days"]*86400,))
                    self.db.execute("DELETE FROM objects WHERE kind='memory_job' AND json_extract(data,'$.status') IN ('complete','cancelled') AND updated<?", (time.time()-cfg["retention_days"]*86400,))
                    for p in (*self.media.root.glob("*.jpg"), *self.media.root.glob("*.source-v2")):
                        if p.stat().st_mtime < time.time()-cfg["media_hours"]*3600:
                            p.unlink()
                    self.db.backup(self.settings.data_dir / "backups")
                    self.stickers.cleanup()
                    self.attachments.cleanup()
                    self.db.execute("DELETE FROM objects WHERE kind IN ('sticker_checked','sticker_job','task_run') AND updated<?", (time.time()-cfg['retention_days']*86400,))
                    last_cleanup = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.db.audit("", "error", {"stage": "maintenance", "type": type(exc).__name__})
            await asyncio.sleep(15)
