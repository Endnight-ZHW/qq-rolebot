from . import prompts
"""Independent interpretation of a proposed action; authority stays in the service."""
import hashlib
import json
import re
import time
from dataclasses import dataclass

from .model import parse_json
from . import tracing


@dataclass(frozen=True)
class AutonomousPermit:
    scope: str
    actor: str
    event_id: str
    name: str
    digest: str
    epoch: int
    policy: str
    expires: float


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ToolIntent:
    def __init__(self, tools):
        self.tools = tools
        self.permits = {}

    def state(self, scope, name):
        group = self.tools.db.get("group", scope, {})
        return digest([self.tools.policy(scope, name), self.tools.permissions.config(scope), self.tools.db.config()["admin_qq"], group.get("persona"), group.get("revision")])

    def conversation_context(self, scope, persona, source):
        """Keep speakers and reply targets together; background grants no authority."""
        db = self.tools.db
        actor = source["user_id"]
        if not source.get("remember", True) or not db.get("member", scope+":"+actor, {}).get("remember", True):
            return []
        floor = max(time.time()-600, db.get("context_reset", scope+":"+persona, {}).get("ts", 0),
                    time.time()-db.config()["retention_days"]*86400)
        anchor = db.rows("SELECT seq FROM message_order WHERE message_id=?", (source["id"],))
        boundary = "o.seq<?" if anchor else "m.ts<?"
        cutoff = anchor[0]["seq"] if anchor else source["ts"]
        rows = db.rows("SELECT m.*,o.seq FROM messages m JOIN message_order o ON o.message_id=m.id "
                       "WHERE m.scope=? AND m.persona=? AND m.excluded=0 AND m.ts>? AND "+boundary+
                       " AND NOT EXISTS(SELECT 1 FROM objects u WHERE u.kind='member' AND u.id=m.scope||':'||m.user_id "
                       "AND json_extract(u.data,'$.remember')=0) ORDER BY o.seq DESC LIMIT 80",
                       (scope, persona, floor, cutoff))
        result, length = [], 0
        budget = min(32000, max(0, self.tools.db.config()['context_tokens']-12000))
        for row in rows:
            text = row["text"][:4000]
            data = json.loads(row['data'])
            delivery = db.get('reply_delivery', data.get('delivery_id', ''), {}) if row['role'] == 'assistant' else {}
            target = db.message(scope, delivery['target_id'], persona) if delivery.get('target_id') else None
            item = {"id":row["id"], "qq":row["user_id"], "role":row["role"], "text":text,
                    'timestamp':row['ts'],
                    "mentions":data.get("mentions",[]), "replies":data.get("replies",[]),
                    "reply_to_qq": target['user_id'] if target else None}
            size = len(json.dumps(item, ensure_ascii=False))
            if length+size > budget:
                break
            result.append(item)
            length += size
        return list(reversed(result))

    def request_context(self, scope, persona, source, history=None):
        """Only this actor's user messages can supply earlier authorization."""
        history = self.conversation_context(scope, persona, source) if history is None else history
        result, previous_user = [], None
        for row in history:
            if row['role'] == 'user':
                previous_user = row['qq']
                if row['qq'] == source['user_id']:
                    result.append(row)
            elif row['role'] == 'assistant' and row['qq'] == self.tools.onebot.self_id:
                # For legacy rows with no delivery linkage, retain only a locally
                # adjacent answer; never pair all bot replies with the current actor.
                target = row.get('reply_to_qq') or previous_user
                if target == source['user_id']:
                    result.append(row)
        return result

    async def route(self, scope, persona, batch, source, name, args):
        from .tools import SPECS, ToolDenied
        spec = SPECS[name]
        await self.tools.permissions.refresh(scope, [m['user_id'] for m in batch])
        policy = self.tools.policy(scope, name)
        if name == 'image_search':
            from .search_requests import explicit_search
            card = self.tools.db.get('persona', persona, {})
            if explicit_search(source.get('plain_text', source.get('data', {}).get('plain_text', source['text'])), card.get('aliases', [])):
                if not policy.get('user_enabled', True):
                    raise ToolDenied('后台已关闭用户请求此工具', code='user_requests_disabled')
                self.tools.permissions.check_tool(scope, source['user_id'], name, args)
                return None, 'request'
        # Everyday assistance always respects the triggering user's grants, even
        # when the bot decides to retrieve context without being explicitly asked.
        if not spec.mutation:
            self.tools.permissions.check_tool(scope, source["user_id"], name, args)
            actors = {m["user_id"] for m in batch}
            mixed = any("tool:"+name not in self.tools.permissions.resolve(scope, actor)["allow"] for actor in actors)
            personal_effect = name in ('creation_create','creation_cancel') or (self.tools.db.get('group', scope, {}).get('interaction_enabled') and name in ('task_create', 'task_update', 'reminder_create', 'reminder_cancel'))
            if policy.get("user_enabled", True) and policy.get("autonomous_enabled", True) and not mixed and not personal_effect:
                return None, "shared"
        elif not policy.get("user_enabled", True) and not policy.get("autonomous_enabled", True):
            raise ToolDenied("用户请求和自主使用均已关闭")
        snapshot = self.state(scope, name)
        epoch = self.tools.db.get("memory_epoch", scope, {}).get("value", 0)
        from .request_evidence import RequestEvidence, active_request_evidence
        # Recall already resolves author identity without importing remote text.
        quoted = RequestEvidence(self.tools, scope, persona, source, fetch_missing=name != 'message_recall')
        quote_context = await quoted.collect()
        if not quoted.current() or snapshot != self.state(scope, name):
            raise ToolDenied('补齐引用期间请求或权限已变化，请重新请求', code='context_changed')
        if quote_context:
            active_request_evidence.set(quoted)
        conversation = self.conversation_context(scope, persona, source)
        combined = {row['id']: row for row in conversation}
        combined.update({row['id']: row for row in quote_context})
        conversation = sorted(combined.values(), key=lambda row: row['timestamp'])
        history = self.request_context(scope, persona, source, conversation)
        observations = [{"id": m["id"], "qq": m["user_id"], "text": m["text"][:4000], "mentions": m.get("mentions", []), "replies": m.get('data', {}).get('replies') or m.get("replies", [])} for m in batch]
        system = prompts.render(self.tools.db, scope, 'tool.source_review')
        card = self.tools.db.get('persona', persona, {})
        payload = {"messages": observations, "request_context": history, "conversation_context": conversation,
                   "quoted_context": quote_context, "unavailable_quote_ids": quoted.unavailable,
                   "requester": {"qq": source['user_id'], "bound_name": card.get('relationships', {}).get(source['user_id'], '')},
                   "proposed_source": source["id"], "tool": name, "args": args}
        try:
            from .input_budget import measure
            from .context import hard_budget
            review_messages = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
            if measure({'messages': review_messages})['estimated_input'] > hard_budget(self.tools.db, self.tools.model):
                raise ValueError('来源核对超过输入预算')
            result = await self.tools.model.chat(scope, "decision", review_messages, json_mode=True, max_tokens=8192, stage='tool.source_review')
            verdict = parse_json(result.get("content"))
        except Exception as exc:
            raise ToolDenied("来源核对暂未完成，本次未执行；不能据此断言账号没有权限", code="request_verification_failed") from exc
        tracing.stage(self.tools.db, "tool_source_review", {"tool": name, "mode": str(verdict.get("mode", "uncertain"))[:30],
                      "source_id": source["id"], "request_source_id": str(verdict.get("request_message_id", ""))[:120],
                      "context_count": len(history), "context_request_ids": verdict.get("context_request_ids", [])})
        if snapshot != self.state(scope, name) or epoch != self.tools.db.get("memory_epoch", scope, {}).get("value", 0) or not quoted.current():
            raise ToolDenied("核实时授权或上下文已变化，请重新请求", code="context_changed")
        if verdict.get("mode") == "request":
            if verdict.get("request_message_id") != source["id"] or verdict.get("source_matches") is not True:
                raise ToolDenied("提议绑定的发言人不是实际请求者，不能借用其他成员身份", code="source_mismatch")
            context_ids = verdict.get("context_request_ids", [])
            available = {h["id"] for h in history if h["role"] == "user" and h["qq"] == source["user_id"]}
            current_available = {h["id"] for h in self.request_context(scope, persona, source) if h["role"] == "user" and h["qq"] == source["user_id"]}
            current_available.update(h['id'] for h in quote_context if h['role'] == 'user' and h['qq'] == source['user_id'])
            if not isinstance(context_ids, list) or any(not isinstance(id, str) or id not in available or id not in current_available for id in context_ids):
                raise ToolDenied("无法验证所引用的同一成员请求，请补全操作与目标", code="request_context_unclear")
            if not policy.get("user_enabled", True):
                raise ToolDenied("后台已关闭用户请求此工具", code="user_requests_disabled")
            await self.tools.permissions.check_tool_fresh(scope, source["user_id"], name, args)
            if not quoted.current():
                raise ToolDenied('执行前引用上下文已变化，请重新请求', code='context_changed')
            return None, "request"
        if verdict.get("mode") != "independent":
            raise ToolDenied("这句话与要执行的操作还没有核对清楚，请补充操作或目标；这不表示账号没有权限", code="request_context_unclear")
        if name in ('task_create', 'task_update', 'reminder_create', 'reminder_cancel', 'image_search', 'profile_like', 'member_poke','creation_cancel'):
            raise ToolDenied('该工具需要可确认的用户请求，不能自主执行', code='request_context_unclear')
        if not policy.get("autonomous_enabled", True):
            raise ToolDenied("后台未开放机器人自主使用此工具", code="autonomous_disabled")
        if not spec.mutation:
            await self.tools.permissions.refresh(scope, [m['user_id'] for m in batch])
            if any("tool:"+name not in self.tools.permissions.resolve(scope, m["user_id"])["allow"] for m in batch):
                raise ToolDenied("本批次存在受限用户，不能借用其他成员身份自主取用资料")
            return None, "assistance"
        # Conservative deterministic veto in addition to the independent model
        # review. A chat instruction cannot opt itself into the autonomous route.
        management_words = r"禁言|闭嘴|踢|移出|撤回|公告|群名|名片|头衔|称号|解禁|解除|入群|全员|mute|ban|kick|delete|announcement"
        request_words = r"请|帮|能不能|可以|给|把|让|应该|建议|要求|执行|please|should|can you|do it"
        if any(re.search(management_words, m["text"], re.I) and re.search(request_words, m["text"], re.I) for m in batch):
            raise ToolDenied("消息包含管理请求，不允许改用自主权限执行")
        # A second, role-free review considers only saved backend rules and actual
        # current observations. No persona/web content can grant authority.
        review_system = prompts.render(self.tools.db, scope, 'tool.autonomous_review')
        try:
            review = await self.tools.model.chat(scope, "decision", [{"role": "system", "content": review_system}, {"role": "user", "content": json.dumps({"rules": self.tools.permissions.config(scope)["autonomous_rules"], "group_guidance": self.tools.db.get("group", scope, {}).get("guidance", ""), "messages": observations, "tool": name, "args": args}, ensure_ascii=False)}], json_mode=True, max_tokens=1024, stage='tool.autonomous_review')
            proof = parse_json(review.get("content"))
        except Exception as exc:
            raise ToolDenied("自主管理复核失败，本次未执行") from exc
        evidence = proof.get("evidence_ids", [])
        valid_ids = {m["id"] for m in batch}
        if proof.get("approved") is not True or not isinstance(evidence, list) or not evidence or any(not isinstance(x, str) or x not in valid_ids for x in evidence):
            raise ToolDenied("自主操作缺少可核实的消息依据")
        from .group_actions import target_user
        own_action = name in ("member_card", "message_recall") and target_user(self.tools.db, scope, name, args) == self.tools.onebot.self_id
        if args.get("user_id") and not own_action and args["user_id"] not in {m["user_id"] for m in batch if m["id"] in evidence}:
            raise ToolDenied("自主操作目标与事实来源不一致")
        if name == "message_recall" and not own_action and str(args["message_id"]) not in {m["message_id"] for m in batch if m["id"] in evidence}:
            raise ToolDenied("自主撤回目标与事实来源不一致")
        if snapshot != self.state(scope, name) or epoch != self.tools.db.get("memory_epoch", scope, {}).get("value", 0):
            raise ToolDenied("复核期间授权或上下文已变化")
        permit = AutonomousPermit(scope, source["user_id"], source["id"], name, digest(args), epoch, snapshot, time.time()+30)
        self.permits = {k: v for k, v in self.permits.items() if v.expires > time.time()}
        self.permits[id(permit)] = permit
        self.tools.db.audit(scope, "autonomous_review", {"name": name, "source_ids": evidence, "reason": str(proof.get("reason", ""))[:300], "status": "approved"})
        return permit, "autonomous"

    def check(self, permit, scope, actor, event_id, name, args):
        from .tools import ToolDenied
        if (not isinstance(permit, AutonomousPermit) or self.permits.get(id(permit)) is not permit or permit.expires < time.time()
            or (permit.scope, permit.actor, permit.event_id, permit.name, permit.digest) != (scope, actor, event_id, name, digest(args))
            or permit.policy != self.state(scope, name) or permit.epoch != self.tools.db.get("memory_epoch", scope, {}).get("value", 0)):
            raise ToolDenied("自主任务凭据失效或不属于本次操作")
