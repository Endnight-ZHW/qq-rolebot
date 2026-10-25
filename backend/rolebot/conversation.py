"""Light participation gate and a single role-aware reply/tool runner."""
import time
from .config_schema import runtime_config
from datetime import datetime
from zoneinfo import ZoneInfo

from . import prompts, tracing
import hashlib
from . import delivery, messaging
from .reply_review import ReplyReview
from .context import identity, encode
from .knowledge import is_explicit
from .model import parse_json
from .tool_loop import ToolLoop


class Conversation:
    def __init__(self, engine):
        self.engine = engine


    def __getattr__(self, name):
        return getattr(self.engine, name)


    async def finish(self, scope, persona, batch, group, card, focus, decision, executed, preview, current):
        # A large requested gallery gets time to finish its bounded read batches.
        # This field is assigned by the service, never accepted from model output.
        limits = runtime_config(self.db, scope)
        decision['reply_timeout'] = limits['forward_reply_timeout_seconds'] if preview.get('forward_image_coverage') else limits['reply_timeout_seconds']
        state = self.db.get("chat_state", scope, {"proactive": [], "sent": []})
        selected = decision.get("reply_to_message_id")
        plan = messaging.combine_reply_plans(messaging.reply_plans(executed))
        if plan:
            selected = plan['source_id']
            decision.update(speak=True, reply_to_message_id=selected, delivery_plan=plan)
        target = next((m for m in batch if selected in (m["id"], m["message_id"])), None)
        if not selected and len(batch) == 1:
            target = batch[0]
        addressed = bool(target and self.direct([target], card))
        followup = bool(target and focus and target["user_id"] == focus.get("target") and decision.get("mode") == "followup" and decision.get("continues_dialogue") is True and not any(q != self.onebot.self_id for q in target.get("mentions", [])))
        state["sent"] = [x for x in state.get("sent", []) if x > time.time()-60]
        state["proactive"] = [x for x in state.get("proactive", []) if x > time.time()-3600]
        allowed = addressed or followup or (len(state["proactive"]) < group["proactive_hourly"] and time.time()-state.get("last_proactive", 0) >= group["cooldown"])
        decision_info = {"speak": decision.get("speak") is True and allowed, "target_message_id": target["id"] if target else None,
                         "target_qq": target["user_id"] if target else None, "mode": "direct" if addressed else "followup" if followup else "proactive", "reason": str(decision.get("reason", ""))[:300]}
        tracing.stage(self.db, "decision", decision_info)
        self.db.audit(scope, "decision", {**decision_info, "reason": decision_info["reason"] if all(m["remember"] for m in batch) else "本轮内容不保留"})
        if not target or not allowed or decision.get("speak") is not True or not current():
            return "silent"
        answered = [a for a in state.get("answered", []) if a["ts"] > time.time()-3600]
        if any(a["id"] == target["id"] for a in answered) or time.time()-target["ts"] > decision['reply_timeout']:
            tracing.stage(self.db, "suppressed", {"reason": "目标消息已回复或超过时效"}, content=False)
            return "stale"
        response = {'content': plan['text']} if plan and plan.get('text') else {'content': decision.get('text', '')}
        tracing.stage(self.db,"reply_raw",{"call_id":response.get("_call_id"),"text":response.get("content")})
        reply = (response.get("content") or "").strip()[:2000]
        reply = self.ground_reply(reply, batch, executed, target=target)
        facts = getattr(self, 'capability_facts', None)
        if facts and target['id'] in facts.questions:
            await self.permissions.refresh(scope, [m['user_id'] for m in batch])
            if not current(): return 'cancelled'
            reply = facts.ground(reply, target, executed)
        from .forward_batches import ground_coverage
        reply = ground_coverage(reply, preview.get('forward_image_coverage'))
        tracing.stage(self.db,"reply_prepared",{"text":reply})
        if (not reply and not (plan and plan.get('sticker_id'))) or not current():
            return "cancelled"
        review_text = reply
        if plan and plan.get('sticker_id'):
            sticker = messaging.available_sticker(self.db, scope, plan['sticker_id'])
            review_text += '\n[待发送表情：'+str(sticker.get('name', ''))[:80]+'；'+str(sticker.get('usage', ''))[:160]+']'
        review = ReplyReview(self, scope, target, batch, card, review_text)
        if not await review.check(current):
            return "stale"
        fingerprint = hashlib.sha256("".join(c for c in reply.lower() if c.isalnum()).encode()).hexdigest()
        recent_replies = [r for r in state.get("reply_hashes", []) if r["ts"] > time.time()-120]
        if reply and any(r["hash"] == fingerprint for r in recent_replies):
            tracing.stage(self.db, "suppressed", {"reason": "与两分钟内的已发送回复相同"}, content=False)
            return "duplicate"
        if not current() or time.time()-target["ts"] > decision['reply_timeout']:
            return "stale"
        return await delivery.deliver(self, scope, persona, target, batch, card, reply, preview, executed, decision, current,
                                      proactive=not addressed and not followup, review=review)


    def preview(self, scope, persona, batch, value):
        tracing.stage(self.db, 'context_'+value['stage'], value)
        self.db.put('context_preview', scope+':'+persona, value if all(m['remember'] for m in batch) else
                    {'persona': persona, 'ts': time.time(), 'notice': '本轮含停止记忆的成员，不保存上下文内容'})

    async def run(self, scope, batch):
        from .image_turn import ImageTurn, active_images
        from .forwards import ForwardTurn, active_forwards
        images = ImageTurn(self.engine, scope, self.db.get('group', scope, {})['persona'], batch)
        forwards = ForwardTurn(self.engine, scope, images.persona, images, batch)
        token = active_images.set(images)
        forward_token = active_forwards.set(forwards)
        try:
            return await self._run(scope, batch, images, forwards)
        finally:
            active_forwards.reset(forward_token)
            active_images.reset(token)

    async def _run(self, scope, batch, images, forwards):
        group = self.db.get('group', scope, {})
        persona = group.get('persona', 'default')
        batch = [m for m in batch if m['persona'] == persona and self.permissions.can_respond(scope, m['user_id'])]
        if not batch: return 'cancelled'
        await self.permissions.refresh(scope, [m['user_id'] for m in batch])
        epoch, signature = self.memory.epoch(scope), identity(self.db, scope, persona)
        card = self.db.get('persona', persona, {})
        self.capability_facts = None
        def current():
            return self.current(scope, persona, epoch) and self.audience_current(scope, batch) and identity(self.db, scope, persona) == signature and images.current() and forwards.current() and (self.capability_facts is None or self.capability_facts.current())
        if not current(): return 'cancelled'
        await images.prepare()
        self.stickers.enqueue(scope, persona, batch)
        if not current(): return 'cancelled'
        query = '\n'.join(m['text'] for m in batch)
        focus = self.db.get('followup', scope+':'+persona, {})
        if focus.get('expires', 0) <= time.time(): focus = {}
        runtime = {'time': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(), 'focus': focus,
            'current_ids': [m['id'] for m in batch], 'direct_ids': [m['id'] for m in batch if self.direct([m], card)],
            'tools': self.tools.runtime_status(scope, batch)}
        runtime['recent_creations'] = [self.engine.creations.public(j) for j in self.db.all('creation_job')
            if j['scope']==scope and j['persona']==persona and j['creator'] in {m['user_id'] for m in batch}
            and not j.get('deleted')][-5:]
        from .search_requests import SearchRequests
        searches = SearchRequests(self.db, scope, batch, card.get('aliases', []), runtime['direct_ids'], runtime['tools'])
        if searches.required:
            runtime['search_requests'] = searches.state()
        # Explicit memory writes complete before generating a claim of remembering.
        urgent = [m['id'] for m in batch if m['remember'] and is_explicit(m.get('plain_text', m['text']))]
        memory_results = []
        if urgent:
            job_id = self.memory.enqueue(scope, persona, explicit_ids=urgent)
            if job_id:
                status = await self.memory.process_job(job_id)
                memory_results.append({'name': 'memory_update', 'status': status,
                    'saved_memory_ids': self.db.get('memory_job', job_id, {}).get('memory_ids', []),
                    'notice': prompts.render(self.db, scope, 'memory.result_notice')})
        runtime['memory_results'] = memory_results
        self.memory.enqueue(scope, persona)
        if not current(): return 'cancelled'
        from .attachments import slots
        if not runtime['direct_ids'] and not any(slots(m) for m in batch) and not images.loaded:
            messages, preview = self.memory.build(scope, persona, query, batch, stage='gate', runtime=runtime)
            self.preview(scope, persona, batch, preview)
            response = await self.model.chat(scope, 'decision', messages, json_mode=True, stage='participation.gate')
            try:
                gate = parse_json(response.get('content'))
                valid = all(type(gate.get(k)) is bool for k in ('speak', 'needs_context', 'action_needed'))
            except (ValueError, TypeError):
                gate, valid = {}, False
            # Ambiguous/invalid gates fail open to the full interpreter, not to a send.
            tracing.stage(self.db, 'participation', {'valid': valid, **gate})
            if valid and not any(gate[k] for k in ('speak', 'needs_context', 'action_needed')):
                return 'silent'
        if not current(): return 'cancelled'
        await forwards.prepare(runtime['direct_ids'])
        if not current(): return 'cancelled'
        runtime['forward_records'] = forwards.prefetched
        runtime['_forward_summary_sources'] = forwards.summary_sources()
        if runtime['_forward_summary_sources']:
            from .forward_batches import summarize
            from .media import MediaError
            try:
                runtime['forward_image_coverage'] = await summarize(forwards, runtime['_forward_summary_sources'])
            except MediaError as exc:
                if exc.code == 'context_changed': return 'cancelled'
                raise
            if not current(): return 'cancelled'
        if runtime['_forward_summary_sources']:
            runtime['focus'] = {}
        if len(batch) == 1 and forwards.prefetched and all(
                p.get('status') != 'success' or p.get('result', {}).get('status') != 'read' or
                not p.get('result', {}).get('records') for p in forwards.prefetched):
            # A missing target cannot be replaced with an unrelated group
            # summary, even if a model would otherwise try to be helpful.
            decision = {'speak': True, 'text': prompts.render(self.db, scope, 'forward.unavailable_reply'),
                        'mode': 'direct', 'reply_to_message_id': batch[0]['id'], 'continues_dialogue': False,
                        'expects_followup': False, 'reason': '引用的转发正文未能读取'}
            return await self.finish(scope, persona, batch, group, card, focus, decision, memory_results,
                {'forward_source_ids': [p['source_id'] for p in forwards.prefetched]}, current)
        schemas = self.tools.schemas(scope, bind_source=True)
        await self.permissions.refresh(scope, [m['user_id'] for m in batch])
        if not current(): return 'cancelled'
        runtime['tools'] = self.tools.runtime_status(scope, batch)
        from .capability_facts import CapabilityFacts
        facts = self.capability_facts = CapabilityFacts(self.engine, scope, persona, batch, runtime['direct_ids'])
        await facts.prepare(lambda name, args: self.bound_tool(scope, persona, batch, name, args),
                            allowance=max(0, runtime_config(self.db, scope)['tool_max_calls']-forwards.attempts),
                            history_allowance=max(0, 3-forwards.attempts))
        if not current(): return 'cancelled'
        if facts.questions: runtime['capability_facts'] = list(facts.facts.values())
        runtime['_tool_schema_chars'] = len(encode(schemas))+512
        runtime['_sticker_candidates'] = self.stickers.candidates(scope, persona, batch, query)
        messages, preview = self.memory.build(scope, persona, query, batch, stage='reply', runtime=runtime)
        preview['forward_image_coverage'] = runtime.get('forward_image_coverage', [])
        self.preview(scope, persona, batch, preview)
        await images.flush(messages, preview['budget'], schemas)
        async def execute(name, args):
            blocked = facts.before_tool(name, args) or searches.before_tool(name, args)
            if blocked:
                return blocked
            result = await self.bound_tool(scope, persona, batch, name, args)
            searches.record(name, args, result)
            return result
        async def after_tools():
            await images.flush(messages, preview['budget'], schemas)
            searches.observed = set(searches.attempted)
        def extend_prepared(name, args, executed):
            from .tools import SPECS, ToolDenied
            from pydantic import ValidationError
            plans = messaging.reply_plans(executed)
            if len(plans) != 1:
                return False
            try:
                source, clean = self.tools.bind(scope, batch, args)
                clean = SPECS[name].args.model_validate(clean).model_dump()
                proposed = {'source_id': source['id'], 'actor': source['user_id'], 'permission_tool': name,
                            'text': clean.get('text', ''), 'mentions': clean.get('mentions', []),
                            'sticker_id': clean.get('sticker_id') if name == 'message_send' else clean['id']}
                messaging.combine_reply_plans([plans[0], proposed])
                return True
            except (ValueError, TypeError, KeyError, ToolDenied, ValidationError):
                return False
        response, executed = await ToolLoop(self.model, self.db).run(scope, 'chat', messages, schemas,
            execute, current, stage='conversation.agent', json_mode=True, prepared=True, history_limit=max(0, 3-forwards.attempts-facts.history_calls), input_budget=preview['budget'],
            after_tools=after_tools, completion_guard=searches.finish, initial_tool_calls=forwards.attempts+len(facts.executed),
            extend_prepared=extend_prepared)
        executed = facts.executed+executed
        if not current(): return 'cancelled'
        if all(m.get('remember', True) for m in batch):
            from .working_context import retain_reads
            retain_reads(self.db, scope, persona, executed)
        from .reply_recovery import recover
        decision = await recover(self, scope, batch, card, response, memory_results+executed,
                                 messages, current, preview['budget'], timeout=runtime_config(self.db, scope)['forward_reply_timeout_seconds' if preview.get('forward_image_coverage') else 'reply_timeout_seconds'])
        if decision is None:
            return 'invalid_response' if current() else 'cancelled'
        images.observe(decision)
        preview['image_source_ids'] = sorted({
            d['item']['source_id'] for aid, d in images.loaded.items()
            if aid not in images.references_loaded and not d.get('omitted')} | images.search_sources)
        preview['image_search_refs'] = sorted(images.search_refs)
        preview['forward_source_ids'] = sorted(forwards.sources)
        result = await self.finish(scope, persona, batch, group, card, focus, decision, memory_results+executed, preview, current)
        if result == 'sent' and current():
            images.remember_focus(decision)
        return result
