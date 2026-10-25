from .config_schema import number_field, FIELDS
from . import prompts
from .prompt_defaults import DEFAULTS
import ast
import asyncio
import hashlib
import json
import math
import operator
import time
import uuid
from dataclasses import dataclass
from contextvars import ContextVar
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .web_address import public_fetch_url
from .onebot import ActionUnknown, OneBotError
from .tool_access import service_checks, effective_limits, check_mode
from .history import History, HISTORY_TOOLS
from .permissions import Permissions
from .tool_intent import ToolIntent
from .group_actions import protected_target, platform_check, platform_member, verify_change
from .scheduler import TaskCreate, TaskUpdate, READ_TOOLS
from .stickers import StickerSave
from . import messaging
from .social_actions import SOCIAL_TOOLS
from .creations import CreationCreate, CreationById, TOOLS as CREATION_TOOLS

invocation = ContextVar('tool_invocation', default=None)
INTERACTION_TOOLS = {'message_send', 'member_find', 'member_title', 'capabilities', 'task_create', 'task_list', 'task_update', 'sticker_save', 'sticker_search'} | SOCIAL_TOOLS


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Empty(Args):
    pass


class Query(Args):
    query: str = Field(min_length=1, max_length=500)


class HistorySearch(Args):
    query: str = Field(default="", max_length=200, description=DEFAULTS['params.HistorySearch.query']["body"])
    start_at: str | None = Field(default=None, max_length=40, description=DEFAULTS['params.HistorySearch.start_at']["body"])
    end_at: str | None = Field(default=None, max_length=40, description=DEFAULTS['params.HistorySearch.end_at']["body"])
    user_id: str | None = Field(default=None, pattern=r"^\d{5,20}$", description=DEFAULTS['params.HistorySearch.user_id']["body"])
    before_message_id: str | None = Field(default=None, max_length=120, description=DEFAULTS['params.HistorySearch.before_message_id']["body"])
    limit: int = Field(default=8, ge=1, le=12)
    operation: str | None = Field(default=None, max_length=40, description='核查旧操作时填写工具名，如 member_mute；返回可见来源及服务执行记录，不把聊天自述当成功回执。')
    target_user_id: str | None = Field(default=None, pattern=r'^\d{5,20}$', description='可选：旧操作的目标 QQ，与发起者 user_id 区分。')


class HistoryRead(Args):
    message_id: str = Field(min_length=1, max_length=120, description=DEFAULTS['params.HistoryRead.message_id']["body"])
    before: int = Field(default=2, ge=0, le=4)
    after: int = Field(default=2, ge=0, le=4)
    offset: int = Field(default=0, ge=0, le=12000, description=DEFAULTS['params.HistoryRead.offset']["body"])


class URL(Args):
    url: str = Field(min_length=1, max_length=8192)


class ImageRegion(Args):
    image_id: str = Field(pattern=r'^img_[0-9a-f]{32}$')
    box: list[Annotated[int, Field(ge=0, le=1000)]] = Field(min_length=4, max_length=4)

    @model_validator(mode='after')
    def positive_area(self):
        if self.box[0] >= self.box[2] or self.box[1] >= self.box[3]:
            raise ValueError('局部区域须按左、上、右、下填写，并具有正面积')
        return self


class ImageRead(Args):
    image_ids: list[Annotated[str, Field(pattern=r'^img_[0-9a-f]{32}$')]] = Field(min_length=0, max_length=6)
    include_role_references: bool = False
    region: ImageRegion | None = None


class ForwardRead(Args):
    message_id: str = Field(min_length=1, max_length=120)
    card_index: int = Field(default=0, ge=0, le=7)
    path: list[Annotated[int, Field(ge=0, le=1999)]] = Field(default_factory=list, max_length=3)
    offset: int = Field(default=0, ge=0, le=2000000)


class ImageSearch(Args):
    image_id: str = Field(pattern=r'^img_[0-9a-f]{32}$')


class Member(Args):
    user_id: str = Field(pattern=r"^\d{5,20}$")


class Reason(Args):
    reason: str = Field(min_length=1, max_length=500)


class MemberAction(Member, Reason):
    pass


class Mute(MemberAction):
    duration: int = number_field("tool.max_duration", required=True)


class Kick(MemberAction):
    reject_add_request: bool = False


class Toggle(Reason):
    enable: bool


class Card(MemberAction):
    card: str = Field(max_length=60)


class Title(MemberAction):
    title: str = Field(max_length=18)


class ProfileLike(MemberAction):
    times: int | None = number_field('tool.like_max_times', nullable=True)


class Outgoing(Args):
    text: str = Field(default='', max_length=2000)
    mentions: list[str] = Field(default_factory=list, max_length=3)
    sticker_id: str | None = Field(default=None, max_length=80)


class Name(Reason):
    name: str = Field(min_length=1, max_length=60)


class Notice(Reason):
    content: str = Field(min_length=1, max_length=2000)


class Recall(Reason):
    message_id: str = Field(min_length=1, max_length=80)


class Join(Reason):
    request_id: str = Field(min_length=1, max_length=80)
    approve: bool


class Reminder(Reason):
    text: str = Field(min_length=1, max_length=1000)
    due_at: str | None = Field(default=None, description=DEFAULTS['params.Reminder.due_at']["body"])
    delay_seconds: int | None = Field(default=None, ge=5, le=31536000)
    mention_self: bool = False


class ById(Args):
    id: str = Field(min_length=1, max_length=80)


@dataclass
class Spec:
    title: str
    description: str
    args: type[BaseModel]
    mutation: bool = False
    group_only: bool = False


SPECS = {
    'creation_create': Spec('制作图表、动画与三维作品',DEFAULTS['tools.creation_create.description']['body'],CreationCreate,group_only=True),
    'creation_status': Spec('查看作品制作进度',DEFAULTS['tools.creation_status.description']['body'],CreationById,group_only=True),
    'creation_cancel': Spec('取消自己的制作任务',DEFAULTS['tools.creation_cancel.description']['body'],CreationById,group_only=True),
    'profile_like': Spec('主页点赞', DEFAULTS['tools.profile_like.description']['body'], ProfileLike, True, True),
    'member_poke': Spec('群内戳一戳', DEFAULTS['tools.member_poke.description']['body'], MemberAction, True, True),
    **{name: Spec(title, DEFAULTS['tools.'+name+'.description']['body'], args, mutation, True) for name, title, args, mutation in [
        ('message_send', '发送文字与真正 @', Outgoing, False), ('member_find', '查找群成员', Query, False),
        ('member_title', '设置群专属头衔', Title, True), ('capabilities', '查询能力与权限', Empty, False),
        ('task_create', '创建定时任务', TaskCreate, False), ('task_list', '查看定时任务', Empty, False),
        ('task_update', '修改定时任务', TaskUpdate, False), ('sticker_save', '收藏群表情', StickerSave, False),
        ('sticker_search', '搜索共享表情', Query, False)]},
    "group_info": Spec("群资料", DEFAULTS['tools.group_info.description']["body"], Empty, group_only=True),
    "member_info": Spec("成员资料", DEFAULTS['tools.member_info.description']["body"], Member, group_only=True),
    "memory_search": Spec("记忆检索", DEFAULTS['tools.memory_search.description']["body"], Query),
    "history_search": Spec("群聊历史搜索", DEFAULTS['tools.history_search.description']["body"], HistorySearch, group_only=True),
    "history_read": Spec("群聊历史读取", DEFAULTS['tools.history_read.description']["body"], HistoryRead, group_only=True),
    "image_read": Spec("读取图片与角色参考图", DEFAULTS['tools.image_read.description']["body"], ImageRead),
    "forward_read": Spec("读取合并转发聊天记录", DEFAULTS['tools.forward_read.description']["body"], ForwardRead, group_only=True),
    "image_search": Spec("以图搜图", DEFAULTS['tools.image_search.description']["body"], ImageSearch),
    "web_search": Spec("联网搜索", DEFAULTS['tools.web_search.description']["body"], Query),
    "web_fetch": Spec("网页读取", DEFAULTS['tools.web_fetch.description']["body"], URL),
    "calculate": Spec("计算", DEFAULTS['tools.calculate.description']["body"], Query),
    "reminder_create": Spec("创建提醒", DEFAULTS['tools.reminder_create.description']["body"], Reminder),
    "reminder_list": Spec("查看提醒", DEFAULTS['tools.reminder_list.description']["body"], Empty),
    "reminder_cancel": Spec("取消提醒", DEFAULTS['tools.reminder_cancel.description']["body"], ById),
    "sticker_list": Spec("表情包列表", DEFAULTS['tools.sticker_list.description']["body"], Empty),
    "sticker_send": Spec("发送表情包", DEFAULTS['tools.sticker_send.description']["body"], ById),
    "message_recall": Spec("撤回消息", DEFAULTS['tools.message_recall.description']["body"], Recall, True, True),
    "member_mute": Spec("成员禁言", DEFAULTS['tools.member_mute.description']["body"], Mute, True, True),
    "member_unmute": Spec("解除禁言", DEFAULTS['tools.member_unmute.description']["body"], MemberAction, True, True),
    "member_kick": Spec("移出群聊", DEFAULTS['tools.member_kick.description']["body"], Kick, True, True),
    "group_mute": Spec("全员禁言", DEFAULTS['tools.group_mute.description']["body"], Toggle, True, True),
    "member_card": Spec("修改群名片", DEFAULTS['tools.member_card.description']["body"], Card, True, True),
    "group_name": Spec("修改群名称", DEFAULTS['tools.group_name.description']["body"], Name, True, True),
    "group_notice": Spec("发布群公告", DEFAULTS['tools.group_notice.description']["body"], Notice, True, True),
    "join_request": Spec("处理入群申请", DEFAULTS['tools.join_request.description']["body"], Join, True, True),
}


def default_policies():
    return {name: {"enabled": not spec.mutation and name not in HISTORY_TOOLS | CREATION_TOOLS, "max_per_hour": FIELDS["tool.max_per_hour"]["default" if spec.mutation else "read_default"],
                   "max_duration": FIELDS["tool.max_duration"]["default"], "allow_reject_rejoin": False, "protected_ids": [],
                   "user_enabled": True, "autonomous_enabled": name not in SOCIAL_TOOLS | {'image_search'},
                   **({k:FIELDS['tool.'+k]['default'] for k in ('like_max_times','like_daily_limit')} if name == 'profile_like' else {}),
                   **({'poke_cooldown_seconds': FIELDS['tool.poke_cooldown_seconds']['default']} if name == 'member_poke' else {})} for name, spec in SPECS.items()}


def configured_policies(group):
    result = {k: {**p, **group.get('tools', {}).get(k, {})} for k, p in default_policies().items()}
    if 'image_read' not in group.get('tools', {}):
        result['image_read'] = dict(result['history_read'])
    if 'forward_read' not in group.get('tools', {}):
        result['forward_read'] = dict(result['history_read'])
    if 'image_search' not in group.get('tools', {}):
        result['image_search'] = dict(result['web_search'])
    # Legacy switches may inherit; explicit new audience rules never do.
    for name, policy in result.items():
        policy['request_access'] = group.get('tools', {}).get(name, {}).get('request_access')
    for name in SOCIAL_TOOLS | {'image_search'}:
        result[name]['autonomous_enabled'] = False
    return result


def calculator(expression):
    tree = ast.parse(expression[:256], mode="eval")
    if len(list(ast.walk(tree))) > 64:
        raise ValueError("表达式过长")
    operations = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
                  ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
    def visit(node, depth=0):
        if depth > 20:
            raise ValueError("表达式嵌套过深")
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            result = node.value
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            result = visit(node.operand, depth+1) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp) and type(node.op) in operations:
            a, b = visit(node.left, depth+1), visit(node.right, depth+1)
            if isinstance(node.op, ast.Pow) and abs(b) > 10:
                raise ValueError("指数过大")
            result = operations[type(node.op)](a, b)
        else:
            raise ValueError("只支持数字和四则运算、取余、乘方")
        if isinstance(result, complex) or not math.isfinite(result) or abs(result) > 1e18:
            raise ValueError("计算结果超出范围")
        return result
    return visit(tree.body)


class ToolDenied(Exception):
    def __init__(self, message, code="policy_denied"):
        super().__init__(message)
        self.code = code


class ToolRunner:
    def __init__(self, db, settings, onebot, model):
        self.db, self.settings, self.onebot, self.model = db, settings, onebot, model
        self.permissions = Permissions(db, onebot)
        self.intent = ToolIntent(self)

    def runtime_status(self, scope, batch):
        """Current service facts, independent of roleplay and historic refusals."""
        actors = {m["user_id"] for m in batch}
        effective = {actor:self.permissions.resolve(scope,actor) for actor in actors}
        access = {name: {actor: self.permission_status(scope, actor, name, value) for actor, value in effective.items()} for name in SPECS}
        def model_access(name, value):
            compact = {k:value[k] for k in ('mode','available','target_scope','identity_source')}
            if value.get('error_code'): compact.update(error_code=value['error_code'], reason=value['reason'])
            limits = value['limits']
            compact['limits'] = {'max_per_hour': limits['max_per_hour']}
            if name == 'member_mute': compact['limits']['max_duration'] = limits['max_duration']
            if SPECS[name].mutation:
                if limits['protected_ids']: compact['limits']['protected_ids'] = limits['protected_ids']
                if limits['protection_sources']['default']:
                    compact['limits']['default_protected_ids'] = limits['protection_sources']['default']
            return compact
        return {"bot_qq":self.onebot.self_id, "tools":[{"name":name, "enabled":bool(self.policy(scope,name).get("enabled")),
                "user_requests":self.policy(scope,name).get("user_enabled",True), "autonomous":self.policy(scope,name).get("autonomous_enabled",True),
                "authorized_requesters":[actor for actor in sorted(actors) if access[name][actor]['available'] and 'tool:'+name in effective[actor]['allow']],
                'self_only_requesters': sorted(actor for actor in actors if access[name][actor]['available'] and access[name][actor]['target_scope'] == 'self'),
                **({'like_limits': {'per_request': self.policy(scope,name)['like_max_times'], 'per_day': self.policy(scope,name)['like_daily_limit'],
                    'origin': 'bot_configuration', 'shared_across_groups': True, 'unspecified_times': 'fill_remaining_within_per_request_limit', 'automatic_followup': False}} if name == 'profile_like' else {}),
                'requester_access': {actor:model_access(name, access[name][actor]) for actor in sorted(actors)},
                'implemented': True,
                'status': 'disabled' if not self.policy(scope,name).get('enabled') else 'user_requests_disabled' if not self.policy(scope,name).get('user_enabled',True) else 'available' if any(a['available'] for a in access[name].values()) else 'user_not_authorized'}
                for name in SPECS if not SPECS[name].group_only or ":g:" in scope],
                "notice":prompts.render(self.db, scope, 'tool.permission_notice')+' 请求者的 available/status 只描述当前请求许可，不是机器人 QQ 身份或功能是否存在。user_not_authorized 要说当前规则尚未授权这个请求者，不能说我没有管理能力。未查询 bot_role 时不要猜机器人是普通成员。条件玩笑可自然接梗，不必主动解释权限，更不能承诺未来自动处罚。文字和表情的 prepared/success 不是禁言等管理操作成功。'}

    def permission_status(self, scope, actor, name, effective=None):
        effective = effective or self.permissions.resolve(scope, actor)
        access = dict(effective['tool_access'][name])
        policy = self.policy(scope, name)
        checks = service_checks(self, scope, name)
        def check(key, label, ok, reason):
            checks.append({'key':key,'label':label,'passed':bool(ok),'reason':'' if ok else reason})
        blocked = next((x for x in checks if not x['passed']), None)
        check('request','允许用户请求',policy.get('user_enabled',True),'后台已关闭用户请求此工具')
        check('identity_access','身份与授权范围',access['available'],access.get('reason') or '当前成员未授权')
        if blocked:
            access.update(available=False,error_code=blocked['error_code'],reason=blocked['reason'])
        elif not policy.get('user_enabled',True):
            access.update(available=False,error_code='user_requests_disabled',reason='后台已关闭用户请求此工具')
        return {**access,'group_policy':policy,'checks':checks,
                'execution_checks':['实际目标与保护名单','实时调用额度与冷却','QQ 接口权限与执行回执'],
                'limits':effective_limits(self, scope, name, effective)}

    def policy(self, scope, name):
        group = self.db.get("group", scope, {})
        policy = configured_policies(group).get(name, {})
        if name == 'creation_create' and not (getattr(self,'engine',None) and self.engine.creations.configured()):
            policy['enabled'] = False
        if name == 'image_search':
            service = getattr(getattr(self, 'engine', None), 'image_search', None)
            if not service or not service.key():
                policy['enabled'] = False
        if name in INTERACTION_TOOLS and not group.get('interaction_enabled'):
            policy['enabled'] = False
        if not group.get("enabled") or group.get("paused"):
            policy["enabled"] = False
        if group.get("management_paused") and name in SPECS and SPECS[name].mutation:
            policy["enabled"] = False
        return policy

    def schemas(self, scope, bind_source=False):
        result = []
        for name, spec in SPECS.items():
            policy = self.policy(scope, name)
            if policy.get("enabled") and (policy.get("user_enabled", True) or policy.get("autonomous_enabled", True)) and (not spec.group_only or ":g:" in scope):
                schema = spec.args.model_json_schema()
                def compact_schema(value):
                    if isinstance(value, dict):
                        return {k: compact_schema(v) for k, v in value.items() if k != 'title'}
                    if isinstance(value, list):
                        return [compact_schema(v) for v in value]
                    return value
                schema = compact_schema(schema)
                if bind_source:
                    schema["properties"]["source_message_id"] = {"type": "string", "description": prompts.render(self.db, scope, 'tool.source_description')}
                    schema.setdefault("required", []).append("source_message_id")
                description = prompts.render(self.db, scope, "tools."+name+".description")
                if name == 'image_read':
                    description += prompts.render(self.db, scope, 'tools.image_read.region')
                for field_name, field_schema in schema.get("properties", {}).items():
                    key = "params."+spec.args.__name__+"."+field_name
                    if key in DEFAULTS:
                        field_schema["description"] = prompts.render(self.db, scope, key)
                if name == "member_card":
                    description += prompts.render(self.db, scope, "tool.bot_identity", bot_qq=self.onebot.self_id)
                result.append({"type": "function", "function": {"name": name, "description": description, "parameters": schema}})
        return result

    @staticmethod
    def bind(scope, batch, raw_args):
        if not isinstance(raw_args, dict):
            raise ToolDenied("工具参数必须为对象")
        args = dict(raw_args)
        source = args.pop("source_message_id", None)
        matches = [m for m in batch if m["scope"] == scope and source in (m["id"], m["message_id"])] if source else (batch if len(batch) == 1 else [])
        if len(matches) != 1:
            raise ToolDenied("无法确认触发消息；请使用当前批次的 source_message_id，或向群友澄清")
        return matches[0], args

    def check_policy(self, scope, name, args, *, consenting_actor=None, defer_recall=False, skip_target=False, prepared_id=None):
        from .request_evidence import active_request_evidence
        evidence = active_request_evidence.get()
        if evidence is not None and (evidence.scope != scope or not evidence.current()):
            raise ToolDenied('操作前引用来源已变化，本次未执行', code='context_changed')
        policy = self.policy(scope, name)
        blocked = next((x for x in service_checks(self, scope, name) if not x['passed']), None)
        if blocked:
            raise ToolDenied(blocked['reason'], code=blocked['error_code'])
        if SPECS[name].mutation and not skip_target:
            protected_target(self.db, self.onebot, scope, name, args, policy,consenting_actor=consenting_actor,defer_recall=defer_recall)
        if name == "member_mute" and args["duration"] > policy.get("max_duration", 600):
            raise ToolDenied(f"禁言时长超过本群设置的 {policy['max_duration']} 秒上限，本次未执行", code='mute_duration_limit')
        if name == 'profile_like' and args.get('times') is not None and args['times'] > policy.get('like_max_times', 10):
            raise ToolDenied('点赞次数超过后台设置的单次上限，本次未提交', code='like_times_limit')
        if name == "member_kick" and args.get("reject_add_request") and not policy.get("allow_reject_rejoin"):
            raise ToolDenied("后台未允许禁止再次入群，本次未执行", code='rejoin_restriction_disabled')
        count = self.db.rows("SELECT COUNT(*) AS n FROM audit WHERE scope=? AND kind='tool' AND ts>? AND json_extract(data,'$.name')=? AND json_extract(data,'$.status') IN ('success','unknown','running')",
                             (scope, time.time()-3600, name))[0]["n"]
        if prepared_id:
            own = self.db.rows("SELECT COUNT(*) n FROM audit WHERE scope=? AND kind='tool' AND ts>? AND json_extract(data,'$.name')=? AND id=? AND json_extract(data,'$.status')='success'",
                               (scope,time.time()-3600,name,prepared_id))[0]['n']
            count -= min(1, own)
        if count >= policy.get("max_per_hour", 60):
            raise ToolDenied(f"达到此工具每小时 {policy['max_per_hour']} 次的后台上限，本次未执行", code='tool_hourly_limit')

    async def check_delivery(self, scope, plan):
        proofs = plan.get('authorizations') or []
        if not proofs:
            raise ToolDenied('待发送消息缺少服务授权凭据', code='delivery_authorization_invalid')
        def saved_proof(proof):
            rows = self.db.rows("SELECT data FROM audit WHERE scope=? AND kind='tool' AND id=?", (scope,proof['execution_id']))
            saved = json.loads(rows[0]['data']) if len(rows) == 1 else {}
            if (saved.get('status') != 'success' or saved.get('name') != proof['tool']
                or saved.get('actor') != plan['actor'] or saved.get('event_id') != plan['source_id']
                or not (saved.get('result') or {}).get('prepared')):
                raise ToolDenied('待发送消息授权来源不匹配', code='delivery_authorization_invalid')
            return saved
        records = [saved_proof(p) for p in proofs]
        actors = {plan['actor']} | {a for r in records for a in r.get('audience', [])}
        await self.permissions.refresh(scope, actors)
        # Re-read every proof and policy after the final await, including merged plans.
        for proof in proofs:
            saved = saved_proof(proof)
            name, mode = saved['name'], saved['mode']
            actors = set(saved.get('audience', [])) | {plan['actor']}
            self.check_policy(scope, name, {}, prepared_id=proof['execution_id'])
            check_mode(self.policy(scope,name), mode)
            for actor in actors:
                self.permissions.check_tool(scope,actor,name,{})

    async def run(self, scope, persona, actor, event_id, name, raw_args, *, permit=None, mode="request", source=None, audience=()):
        from .message_targets import active_recall
        token = active_recall.set(active_recall.get())
        try:
            return await self._run(scope,persona,actor,event_id,name,raw_args,permit=permit,mode=mode,source=source,audience=audience)
        finally:
            active_recall.reset(token)

    async def _run(self, scope, persona, actor, event_id, name, raw_args, *, permit=None, mode="request", source=None, audience=()):
        from . import tracing
        tracing.stage(self.db,"tool_proposed",{"name":name,"actor":actor,"source_id":event_id,"arguments":raw_args,"mode":mode})
        spec = SPECS.get(name)
        audit = {"name": name, "actor": self.onebot.self_id if permit is not None else actor,
                 "trigger_actor": actor, "persona":persona, "mode": "autonomous" if permit is not None else mode, "event_id": event_id, "status": "denied"}
        effect_key = None
        effect_started = False
        actors = set(audience) | {actor} if mode in ('shared', 'assistance') else {actor}
        audit.update(execution_id=uuid.uuid4().hex, audience=sorted(actors), phase='authorization', submitted=False)
        def authorize(args):
            from .message_targets import evidence
            target = evidence(self.db,scope,args['message_id']) if name == 'message_recall' else None
            consent = actor if (target and permit is None and mode=='request' and target.source_id==event_id and target.source_actor==actor) else None
            self.check_policy(scope, name, args,consenting_actor=consent)
            if self.db.get("group", scope, {}).get("persona") != persona:
                raise ToolDenied("当前角色已变化")
            if permit is not None:
                self.intent.check(permit, scope, actor, event_id, name, args)
            else:
                policy = self.policy(scope, name)
                if mode == 'scheduled_admin':
                    run = self.db.get('task_run', event_id, {})
                    task = self.db.get('reminder', run.get('task_id', ''), {})
                    if (name not in READ_TOOLS or actor != 'admin-console' or run.get('principal') != 'admin'
                        or run.get('scope') != scope or run.get('status') != 'running' or task.get('principal') != 'admin'):
                        raise ToolDenied('后台任务主体校验失败')
                    self.engine.scheduler.authorize(task)
                    check_mode(policy, mode)
                    return
                check_mode(policy, mode)
                if mode not in ("request", "assistance", "shared") or (mode in ("assistance", "shared") and spec.mutation):
                    raise ToolDenied("管理操作需要用户授权或有效的自主任务")
                self.permissions.check_tool(scope, actor, name, args)
                if mode in ('shared', 'assistance'):
                    for other in actors-{actor}:
                        self.permissions.check_tool(scope, other, name, args)
        try:
            if not spec:
                raise ToolDenied("工具不存在")
            args = spec.args.model_validate(raw_args).model_dump()
            if source and (source.get('user_id') != actor or source.get('id') != event_id):
                raise ToolDenied('请求者身份与触发消息不一致', code='source_mismatch')
            if permit is None and mode != 'scheduled_admin':
                await self.permissions.refresh(scope, actors)
            if name == 'message_recall':
                from .message_targets import prepare
                self.check_policy(scope,name,args,skip_target=True)
                resolved_source = source or self.db.message(scope,event_id,persona)
                if resolved_source and (resolved_source.get('user_id') != actor or resolved_source.get('id') != event_id):
                    raise ToolDenied('请求者身份与触发消息不一致',code='source_mismatch')
                args = await prepare(self,scope,persona,resolved_source,args)
            authorize(args)
            tracing.stage(self.db,"tool_validated",{"name":name,"actor":actor,"source_id":event_id,"arguments":args,"mode":mode})
            audit["args"] = args
            if name in HISTORY_TOOLS:
                audit["args"] = {k: v for k, v in args.items() if k != "query"}
                audit["args"]["query_chars"] = len(args.get("query", ""))
            effectful = spec.mutation or name in ("sticker_send", "reminder_create", "reminder_cancel", 'message_send', 'task_create', 'task_update', 'sticker_save','creation_create','creation_cancel')
            if effectful:
                effect_args = {'message_id':args['message_id']} if name=='message_recall' else args
                digest = hashlib.sha256(json.dumps(effect_args, sort_keys=True).encode()).hexdigest()
                effect_key = f"{event_id}:{name}:{digest}"
                existing = self.db.get("tool_effect", effect_key)
                if existing:
                    if permit is not None:
                        self.intent.permits.pop(id(permit), None)
                    return {"status": existing["status"], "result": existing.get("result"), "duplicate": True}
            if spec.mutation:
                audit['phase'] = 'platform_check'
                await platform_check(self.db, self.onebot, scope, name, args)
                # Re-read policy after network waits so revocation takes effect immediately.
                if permit is None:
                    await self.permissions.refresh(scope, actors)
                authorize(args)
            if effect_key:
                self.db.put("tool_effect", effect_key, {"scope": scope, "status": "unknown", "ts": time.time()})
            audit['phase'] = 'invocation'
            effect_started = effectful
            token = invocation.set({'source_id': event_id, 'actor': actor, 'scope': scope, 'name': name, 'mode': mode, 'source':source,'audience':list(actors)})
            try:
                result = await asyncio.wait_for(self.invoke(scope, persona, actor, name, args), 65)
            finally:
                invocation.reset(token)
            if name in ("member_card", "group_name", 'member_title'):
                audit.update(phase='verification', submitted=True)
                result = await verify_change(self.onebot, scope, name, args)
            if not effectful:
                if permit is None and mode != 'scheduled_admin':
                    await self.permissions.refresh(scope, actors)
                authorize(args)
            if name in ('message_send', 'sticker_send'):
                result['delivery']['authorizations'] = [{'tool': name, 'execution_id': audit['execution_id']}]
                audit.update(phase='prepared', submitted=False)
            elif spec.mutation:
                audit['submitted'] = True
            audit["status"], audit["result"] = "success", result
            if name in ('history_search', 'history_read'):
                # Raw historic text is only in the ephemeral result and forgettable request trace.
                audit["result"] = {"source_ids": [m["id"] for m in result["messages"]], "count": len(result["messages"]), "has_more": result.get("has_more", False)}
            elif name == 'image_read':
                audit["result"] = {"images": [{k: r[k] for k in ('image_id', 'status', 'source_id', 'error_code') if k in r}
                                             for r in result.get("images", [])]}
            elif name == 'forward_read':
                audit["result"] = {k: result[k] for k in ('source_id', 'status', 'path', 'offset', 'next_offset', 'has_more', 'error_code') if k in result}
        except ActionUnknown as exc:
            audit["status"], audit["error"] = "unknown", str(exc)
            audit.update(error_code='platform_result_unknown', submitted=None)
        except OneBotError as exc:
            audit.update(status='error', error=str(exc)[:300], submitted=(True if exc.retcode is not None else exc.request_sent) if spec.mutation else False,
                         error_code='platform_rejected' if exc.retcode is not None else 'platform_transport_error')
        except (ValidationError, ToolDenied, ValueError, SyntaxError, ZeroDivisionError) as exc:
            audit["error"] = "工具参数校验失败" if isinstance(exc, ValidationError) else str(exc)[:300]
            audit["error_code"] = getattr(exc, "code", "invalid_arguments")
        except asyncio.CancelledError:
            audit["status"], audit["error"] = "unknown" if effect_started else "error", "服务中断"
            audit.update(error_code='operation_interrupted', submitted=None if effect_started else False)
            self.db.audit(scope, "tool", audit, record_id=audit['execution_id'])
            if permit is not None:
                self.intent.permits.pop(id(permit), None)
            raise
        except Exception as exc:
            audit["status"], audit["error"] = ("unknown" if isinstance(exc, asyncio.TimeoutError) and effect_started else "error"), str(exc)[:300]
            audit['error_code'] = 'operation_timeout' if isinstance(exc, asyncio.TimeoutError) else 'tool_execution_error'
            if effect_started: audit['submitted'] = None
        if effect_key and self.db.get("tool_effect", effect_key):
            self.db.put("tool_effect", effect_key, {"scope": scope, "status": audit["status"], "result": audit.get("result", audit.get("error")), "ts": time.time()})
        if name == 'creation_create':
            clean=raw_args if isinstance(raw_args,dict) else {}
            audit['args']={'brief_chars':len(str(clean.get('brief',''))),'asset_count':len(clean['asset_ids']) if isinstance(clean.get('asset_ids'),list) else 0}
        self.db.audit(scope, "tool", audit, record_id=audit['execution_id'])
        if permit is not None:
            self.intent.permits.pop(id(permit), None)
        return {"status": audit["status"], "result": result if name in HISTORY_TOOLS and audit["status"] == "success" else audit.get("result"), "error": audit.get("error"), "error_code": audit.get("error_code"), 'phase':audit['phase'], 'submitted':audit['submitted']}

    async def invoke(self, scope, persona, actor, name, a):
        gid = scope.split(":", 2)[2]
        source = (invocation.get() or {}).get('source_id', '')
        if name in CREATION_TOOLS:
            context=invocation.get() or {}
            if name=='creation_create':
                message=context.get('source') or self.db.message(scope,source,persona)
                return await self.engine.creations.create(scope,persona,actor,a,message,context.get('mode','request'),context.get('audience',[]))
            if name=='creation_status':
                return self.engine.creations.public(self.engine.creations.get(a['job_id'],scope,actor))
            return self.engine.creations.cancel(a['job_id'],scope,actor)
        if name in SOCIAL_TOOLS:
            from .social_actions import execute
            return await execute(self, scope, persona, actor, source, name, a, (invocation.get() or {}).get('mode'))
        if name == 'member_find':
            return await messaging.find_members(self.onebot, scope, a['query'])
        if name == 'capabilities':
            value = self.runtime_status(scope, [{'user_id': actor}])
            me = await platform_member(self.onebot, gid, self.onebot.self_id)
            try:
                await self.onebot.call('nc_get_packet_status', {})
                packet = True
            except Exception: packet = False
            value['platform'] = {'bot_role': me.get('role', 'unknown'), 'packet_available': packet,
                                 'member_title': 'available' if me.get('role') == 'owner' and packet else 'requires_group_owner' if me.get('role') != 'owner' else 'packet_unavailable'}
            return value
        if name in ('message_send', 'sticker_send'):
            mentions = await messaging.recipients(self.onebot, scope, a.get('mentions', []), actor)
            sticker = a.get('sticker_id') if name == 'message_send' else a['id']
            if sticker: messaging.available_sticker(self.db, scope, sticker)
            if not a.get('text') and not mentions and not sticker: raise ValueError('消息内容为空')
            return {'prepared': True, 'sent': False, 'delivery': {'text': a.get('text', ''), 'mentions': mentions,
                    'sticker_id': sticker, 'source_id': source, 'actor': actor, 'permission_tool': name}}
        if name == 'task_create':
            return await self.engine.scheduler.create(scope, persona, actor, a, source)
        if name == 'task_list':
            return [x for x in self.db.all('reminder') if x['scope'] == scope and x['creator'] == actor][-100:]
        if name == 'task_update':
            return self.engine.scheduler.update(scope, actor, a)
        if name == 'sticker_save':
            return await self.engine.stickers.save(scope, persona, **a)
        if name == 'sticker_search':
            return self.engine.stickers.list(scope, a['query'])
        if name == "memory_search":
            from .memory_semantics import view
            return [view(self.db, x) for x in self.db.recall(scope, persona, a["query"])]
        if name == "history_search":
            return History(self.db, scope, persona).search(**a)
        if name == "history_read":
            return History(self.db, scope, persona).read(**a)
        if name == "forward_read":
            from .forwards import active_forwards
            turn = active_forwards.get()
            if not turn or turn.scope != scope or turn.persona != persona:
                raise ToolDenied('转发读取仅限当前会话执行器')
            return await turn.read(**a, actor=actor, mode=(invocation.get() or {}).get('mode', 'request'))
        if name == "image_read":
            from .image_turn import active_images
            turn = active_images.get()
            if not turn or turn.scope != scope or turn.persona != persona:
                raise ToolDenied('图片读取仅限当前会话执行器')
            mode = (invocation.get() or {}).get('mode', 'request')
            return await turn.read_tool(**a, actor=None if mode == 'scheduled_admin' else actor, mode=mode)
        if name == "image_search":
            from .image_search_tool import search
            return await search(self.engine, scope, persona, actor, source, a['image_id'],
                                (invocation.get() or {}).get('mode', 'request'))
        if name == "calculate":
            return calculator(a["query"])
        if name == "web_search":
            return await self.model.search(scope, a["query"])
        if name == "web_fetch":
            await public_fetch_url(a["url"])
            return await self.model.fetch(scope, a["url"])
        if name == "group_info":
            return await self.onebot.call("get_group_info", {"group_id": gid})
        if name == "member_info":
            info = await self.onebot.call("get_group_member_info", {"group_id": gid, "user_id": a["user_id"]})
            if self.permissions.profiles(scope).get(name) and (not isinstance(info, dict) or str(info.get('user_id', '')) != a['user_id'] or str(info.get('group_id', gid)) != gid):
                raise ToolDenied('无法确认目标属于当前群', code='platform_identity_unavailable')
            return info
        if name == "reminder_create":
            return await self.engine.scheduler.create(scope, persona, actor,
                {'text': a['text'], 'due_at': a['due_at'], 'delay_seconds': a['delay_seconds'],
                 'mentions': ['self'] if a['mention_self'] else [], 'reason': a['reason']}, source, creation_tool='reminder_create')
        if name == "reminder_list":
            return [x for x in self.db.all("reminder") if x["scope"] == scope and x["creator"] == actor and x["status"] == "pending"]
        if name == "reminder_cancel":
            r = self.db.get("reminder", a["id"])
            if not r or r["scope"] != scope or r["creator"] != actor or r["status"] != "pending":
                raise ToolDenied("无法取消其他用户、其他会话或已执行的提醒")
            r["status"] = "cancelled"; self.db.put("reminder", r["id"], r)
            return {"cancelled": True}
        if name == "sticker_list":
            return [{"id": x["id"], "name": x["name"], "tags": x.get("tags", "")} for x in self.db.all("sticker") if x.get('enabled', True)]
        if name == "message_recall":
            from .message_targets import evidence
            if not evidence(self.db,scope,a['message_id']):
                raise ToolDenied('目标消息身份尚未核验',code='recall_target_unavailable')
            return await self.onebot.call("delete_msg", {"message_id": a["message_id"]}, mutation=True)
        if name == "join_request":
            request = self.db.get("join_request", a["request_id"])
            if not request or request["scope"] != scope or request["status"] != "pending":
                raise ToolDenied("入群申请不存在、已处理或不属于当前群")
            result = await self.onebot.call("set_group_add_request", {"flag": request["flag"], "sub_type": request["sub_type"], "approve": a["approve"], "reason": a["reason"]}, mutation=True)
            request["status"] = "approved" if a["approve"] else "rejected"
            self.db.put("join_request", request["id"], request)
            return result
        mapping = {
            "member_mute": ("set_group_ban", {"user_id": a.get("user_id"), "duration": a.get("duration")}),
            "member_unmute": ("set_group_ban", {"user_id": a.get("user_id"), "duration": 0}),
            "member_kick": ("set_group_kick", {"user_id": a.get("user_id"), "reject_add_request": a.get("reject_add_request", False)}),
            "group_mute": ("set_group_whole_ban", {"enable": a.get("enable")}),
            "member_card": ("set_group_card", {"user_id": a.get("user_id"), "card": a.get("card")}),
            'member_title': ('set_group_special_title', {'user_id': a.get('user_id'), 'special_title': a.get('title')}),
            "group_name": ("set_group_name", {"group_name": a.get("name")}),
            "group_notice": ("_send_group_notice", {"content": a.get("content")}),
        }
        action, params = mapping[name]
        return await self.onebot.call(action, {"group_id": gid, **params}, mutation=True)
