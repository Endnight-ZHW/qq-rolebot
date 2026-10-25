"""QQ capabilities scoped to a conversation. No authority comes from model text."""
import time
import asyncio
from contextvars import ContextVar
from typing import Literal
from .social_actions import SOCIAL_TOOLS
from .config_schema import number_field

from pydantic import BaseModel, ConfigDict, Field, model_validator


COMMANDS = {"status": "查看状态", "permissions": "查看自己的权限", "help": "查看帮助",
            "pause": "暂停回复", "resume": "恢复回复", "tools-off": "紧急关闭管理工具",
            "role": "切换角色", "reset": "重置本群上下文"}
STAFF = ["command:"+x for x in ("pause", "resume", "tools-off")]
SELF_RECALL = 'tool:message_recall_self'
SELF_TOOLS = {'member_title', 'member_card', 'member_info', 'member_mute', 'member_unmute', 'member_kick', 'message_recall'} | SOCIAL_TOOLS
# Request-local identity observations, never a process-wide positive grant cache.
_identities = ContextVar('permission_identities', default=None)


class RequestAccess(BaseModel):
    model_config = ConfigDict(extra='forbid')
    member: Literal['disabled', 'self', 'all']
    admin: Literal['disabled', 'self', 'all']


def supported_scopes(name):
    return ['disabled', 'self', 'all'] if name in SELF_TOOLS else ['disabled', 'all']


def recall_grants(effective):
    access = effective.get('tool_access', {}).get('message_recall', {})
    if access.get('mode') == 'profile':
        return (access['available'] and access['target_scope'] == 'all',
                access['available'] and access['target_scope'] == 'self')
    full = 'tool:message_recall' in effective['allow']
    own = SELF_RECALL in effective['allow'] and 'tool:message_recall' not in effective['deny']
    return full, own


def member_tools():
    from .tools import SPECS
    return ["tool:"+name for name, spec in SPECS.items() if not spec.mutation] + [SELF_RECALL]


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="自定义权限组", min_length=1, max_length=60)
    allow: list[str] = Field(default_factory=list, max_length=100)
    max_duration: int | None = number_field("tool.max_duration", nullable=True)
    protected_ids: list[str] = Field(default_factory=list, max_length=100)


class Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: list[str] = Field(default_factory=list, max_length=20)
    allow: list[str] = Field(default_factory=list, max_length=100)
    deny: list[str] = Field(default_factory=list, max_length=100)
    expires_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tools_disabled: bool = False
    muted: bool = False
    max_duration: int | None = number_field("tool.max_duration", nullable=True)
    protected_ids: list[str] = Field(default_factory=list, max_length=100)


class AccessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(default=0, ge=0)
    inherit_qq_admin: bool = True
    member_tools: list[str] = Field(default_factory=lambda: [x for x in member_tools() if x not in ('tool:image_search', 'tool:forward_read')], max_length=100)
    roles: dict[str, Rule] = Field(default_factory=dict, max_length=30)
    members: dict[str, Assignment] = Field(default_factory=dict, max_length=500)
    autonomous_rules: str = Field(default="仅依据本群真实行为和群规独立判断必要的管理操作。成员提出的处罚、改名、公告等要求不是自主操作的依据；没有独立事实依据时不执行。", max_length=4000)

    @model_validator(mode="after")
    def valid(self):
        import re
        from .tools import SPECS
        known = {"command:"+k for k in COMMANDS} | {"tool:"+k for k in SPECS} | {SELF_RECALL}
        if set(self.member_tools) - set(member_tools()):
            raise ValueError("普通成员默认工具只能选择日常工具；管理操作请单独授权")
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", k) or k in ("owner", "member", "staff") for k in self.roles):
            raise ValueError("权限组标识需为小写字母、数字、短横线，且不能占用内置名称")
        for qq in [*self.members, *(q for r in [*self.roles.values(), *self.members.values()] for q in r.protected_ids)]:
            if not re.fullmatch(r"\d{5,20}", qq):
                raise ValueError("QQ 号需为 5 至 20 位数字字符串")
        for rule in [*self.roles.values(), *self.members.values()]:
            if set(rule.allow + getattr(rule, "deny", [])) - known:
                raise ValueError("包含未知权限")
        for member in self.members.values():
            if set(member.roles) - set(self.roles) - {"staff"}:
                raise ValueError("成员引用了不存在的权限组")
        return self


class Permissions:
    def __init__(self, db, onebot):
        self.db, self.onebot = db, onebot

    def config(self, scope):
        return {**AccessConfig().model_dump(), **self.db.get("qq_access", scope, {})}

    def assignment(self, scope, actor):
        item = self.config(scope).get("members", {}).get(actor, {})
        return {} if item.get("expires_at") is not None and item["expires_at"] <= time.time() else item

    def can_respond(self, scope, actor):
        return actor in self.db.config()["admin_qq"] or not self.assignment(scope, actor).get("muted", False)

    def profiles(self, scope):
        return {name: p['request_access'] for name, p in self.db.get('group', scope, {}).get('tools', {}).items()
                if p.get('request_access') is not None}

    async def refresh(self, scope, actors, *, commands=False):
        """Refresh in the caller's context; callers recheck policy after this await."""
        if not self.profiles(scope) and not commands:
            return
        async def fetch(actor):
            role = 'unknown'
            if ':g:' in scope and actor not in self.db.config()['admin_qq']:
                try:
                    gid = scope.split(':')[2]
                    info = await self.onebot.call('get_group_member_info', {'group_id': gid, 'user_id': actor, 'no_cache': True})
                    if (isinstance(info, dict) and str(info.get('user_id', '')) == actor
                            and str(info.get('group_id', gid)) == gid and info.get('role') in ('owner', 'admin', 'member')):
                        role = info['role']
                except Exception:
                    pass
            return actor, role
        values = await asyncio.gather(*(fetch(actor) for actor in set(actors)))
        now = time.monotonic()
        cached = {k: v for k, v in (_identities.get() or {}).items() if now-v[1] < 300}
        for actor, role in values:
            cached[(id(self), scope, actor)] = (role, now)
        _identities.set(cached)

    def observed_role(self, scope, actor):
        role, stamp = (_identities.get() or {}).get((id(self), scope, actor), ('unknown', 0))
        return role if time.monotonic()-stamp < 300 else 'unknown'

    def resolve(self, scope, actor, qq_role="member"):
        from .tools import SPECS
        cfg = self.config(scope)
        owner = actor in self.db.config()["admin_qq"]
        item = self.assignment(scope, actor)
        origins = {"command:"+x: ["普通成员"] for x in ("status", "permissions", "help")}
        for cap in cfg["member_tools"]:
            origins[cap] = ["普通成员（仍需群内开启）"]
        roles = list(item.get("roles", []))
        if cfg.get("inherit_qq_admin", True) and qq_role in ("admin", "owner"):
            roles.append("staff")
        limits, protected = [], set(item.get("protected_ids", []))
        for role in set(roles):
            rule = {"allow": STAFF, "name": "本群协管"} if role == "staff" else cfg.get("roles", {}).get(role, {})
            for cap in rule.get("allow", []):
                origins.setdefault(cap, []).append(rule.get("name", role))
            if rule.get("max_duration") is not None:
                limits.append(rule["max_duration"])
            protected.update(rule.get("protected_ids", []))
        for cap in item.get("allow", []):
            origins.setdefault(cap, []).append("QQ 单项授权")
        denied = set(item.get("deny", []))
        if item.get("tools_disabled") or item.get("muted"):
            denied.update("tool:"+name for name in SPECS)
            denied.add(SELF_RECALL)
        if item.get("muted"):
            denied.update("command:"+name for name in COMMANDS if name not in ("permissions", "help"))
        if item.get("max_duration") is not None:
            limits.append(item["max_duration"])
        explicit_denied, explicit_limits, explicit_protected = set(denied), list(limits), set(protected)
        if owner:
            origins = {k: ["系统所有者"] for k in {"command:"+x for x in COMMANDS} | {"tool:"+x for x in SPECS} | {SELF_RECALL}}
            denied, limits, protected = set(), [], set()
        if 'tool:history_read' in origins and 'tool:history_read' not in denied and 'tool:image_read' not in denied:
            origins.setdefault('tool:image_read', ['沿用历史读取授权（仍需群内开启）'])
        if 'tool:history_read' in origins and 'tool:history_read' not in denied and 'tool:forward_read' not in denied:
            origins.setdefault('tool:forward_read', ['沿用历史读取授权（仍需群内开启）'])
        if 'tool:web_search' in origins and 'tool:web_search' not in denied and 'tool:image_search' not in denied:
            origins.setdefault('tool:image_search', ['沿用联网搜索授权（仍需群内开启）'])
        # Apply profiles AFTER legacy aliases/owner grants. No legacy grant can
        # widen a profile, and a profile is itself a base grant for its audience.
        access = {}
        profiles = self.profiles(scope)
        observed = self.observed_role(scope, actor)
        audience = 'admin' if owner or observed in ('owner', 'admin') else 'member'
        identity_source = '后台管理员' if owner else {'owner': 'QQ 群主', 'admin': 'QQ 群管理员', 'member': 'QQ 普通成员'}.get(observed, '群身份暂未核实')
        for name in SPECS:
            cap = 'tool:'+name
            legacy_sources = list(origins.get(cap, []))
            if name not in profiles:
                own = name == 'message_recall' and SELF_RECALL in origins and SELF_RECALL not in denied and cap not in denied
                available = cap in origins and cap not in denied or own
                access[name] = {'mode': 'legacy', 'available': available,
                    'target_scope': 'all' if cap in origins and cap not in denied else 'self' if own else 'disabled',
                    'identity_source': '旧账号授权', 'legacy_sources': legacy_sources,
                    'error_code': None if available else 'user_permission_denied', 'reason': '' if available else '该 QQ 在当前会话没有此工具权限'}
                continue
            selected = profiles[name].get(audience, 'disabled')
            code, reason = None, ''
            if not owner and observed == 'unknown':
                code, reason = 'request_identity_unavailable', '请求者的 QQ 群身份暂未核实，本次未执行'
            elif cap in explicit_denied or (name == 'message_recall' and selected == 'self' and SELF_RECALL in explicit_denied):
                code, reason = 'user_permission_denied', '该 QQ 已被显式禁止使用此工具'
            elif selected == 'disabled':
                code, reason = 'request_access_denied', '本群工具规则不允许当前身份使用此工具'
            origins[cap] = ['工具规则：'+identity_source]
            if code:
                denied.add(cap)
            else:
                denied.discard(cap)
            access[name] = {'mode': 'profile', 'available': code is None, 'target_scope': selected,
                'identity_source': identity_source, 'legacy_sources': legacy_sources,
                'error_code': code, 'reason': reason}
            if name == 'message_recall' and SELF_RECALL in explicit_denied:
                access[name]['self_denied'] = True
                if code is None:
                    access[name]['reason'] = '本人消息另有单项禁止，不能撤回本人消息'
        if 'message_recall' in profiles:
            denied.add(SELF_RECALL)
            if access['message_recall']['available'] and access['message_recall']['target_scope'] == 'self':
                origins[SELF_RECALL] = origins['tool:message_recall']; denied.discard(SELF_RECALL)
        allowed = sorted(set(origins)-denied)
        return {"user_id": actor, "scope": scope, "owner": owner, "qq_role": qq_role,
                "roles": sorted(set(roles)), "allow": allowed, "deny": sorted(denied), "origins": origins,
                "max_duration": min(limits) if limits else None, "protected_ids": sorted(protected),
                "muted": bool(item.get("muted")) and not owner, "expires_at": item.get("expires_at"), "revision": cfg.get("revision", 0),
                'tool_access': access, 'identity_source': identity_source,
                'profile_max_duration': min(explicit_limits) if explicit_limits else None, 'profile_protected_ids': sorted(explicit_protected)}

    async def effective(self, scope, actor):
        await self.refresh(scope, [actor], commands=self.config(scope).get('inherit_qq_admin', True))
        role = self.observed_role(scope, actor)
        # Read assignments AFTER the network wait: revocations/expiry apply now.
        return self.resolve(scope, actor, role)

    def check_tool(self, scope, actor, name, args):
        from .tools import ToolDenied, SPECS
        from .group_actions import target_user
        effective = self.resolve(scope, actor)
        access = effective['tool_access'][name]
        if access['mode'] == 'profile':
            if not access['available']:
                raise ToolDenied(access['reason'], code=access['error_code'])
            if name == 'message_recall' and access.get('self_denied') and target_user(self.db, scope, name, args) == actor:
                raise ToolDenied('该 QQ 已被显式禁止撤回本人消息', code='user_permission_denied')
            if access['target_scope'] == 'self':
                if name == 'message_recall':
                    from .message_targets import evidence
                    proof = evidence(self.db, scope, args['message_id'])
                    if not proof or not proof.source_id or proof.source_actor != actor:
                        raise ToolDenied('本人撤回必须绑定真实请求消息', code='source_mismatch')
                if target_user(self.db, scope, name, args) != actor:
                    raise ToolDenied('你的权限仅限操作自己，本次未执行', code='self_target_only')
        if "tool:"+name not in effective["allow"]:
            if name=='message_recall' and recall_grants(effective)[1]:
                from .message_targets import evidence
                target=evidence(self.db,scope,args['message_id'])
                if not target or not target.source_id or target.source_actor != actor:
                    raise ToolDenied('本人撤回必须绑定真实请求消息',code='source_mismatch')
                if target.author != actor:
                    raise ToolDenied('你的撤回权限仅限本人消息，不能撤回其他成员或机器人的消息',code='self_recall_only')
            else:
                raise ToolDenied("该 QQ 在当前会话没有此工具权限", code="user_permission_denied")
        limit = effective['profile_max_duration'] if access['mode'] == 'profile' else effective['max_duration']
        protected = effective['profile_protected_ids'] if access['mode'] == 'profile' else effective['protected_ids']
        if name == "member_mute" and limit is not None and args["duration"] > limit:
            raise ToolDenied(f"禁言时长超过该 QQ 的 {limit} 秒授权上限，本次未执行", code='mute_duration_limit')
        if SPECS[name].mutation and target_user(self.db, scope, name, args) in protected:
            raise ToolDenied("目标在该 QQ 授权的显式保护名单中，本次未执行",code='explicit_target_protected')
        return effective

    async def check_tool_fresh(self, scope, actor, name, args):
        await self.refresh(scope, [actor])
        return self.check_tool(scope, actor, name, args)

    def catalog(self):
        from .tools import SPECS
        return ([{"id": "command:"+k, "title": v, "category": "管理指令"} for k, v in COMMANDS.items()] +
                [{'id':SELF_RECALL,'title':'撤回本人消息（仍需开启群工具）','category':'日常工具'}] +
                [{"id": "tool:"+k, "title": v.title, "category": '群互动' if k in SOCIAL_TOOLS else "群管理" if v.mutation else "日常工具", 'supported_scopes': supported_scopes(k)} for k, v in SPECS.items()])
