"""Shared writes for the consolidated console and compatible legacy endpoints."""
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from .permissions import AccessConfig, supported_scopes
from .tools import SPECS, SOCIAL_TOOLS, configured_policies
from .config_schema import RUNTIME_DEFAULTS


def check_revision(actual, expected, label):
    if expected is not None and actual != expected:
        raise HTTPException(409, f'{label}已在其他页面修改；请核对服务器版本后重新保存')


def policy_update(group, changes, validate_qq):
    if set(changes)-set(SPECS): raise HTTPException(422, '未知工具')
    for name, policy in changes.items():
        validate_qq(policy.protected_ids)
        if policy.request_access and any(v not in supported_scopes(name) for v in policy.request_access.model_dump().values()):
            raise HTTPException(422, '此工具没有可校验的个人目标，不支持仅限自己')
    policies = {k:{**p, **(changes[k].model_dump(exclude_unset=True) if k in changes else {})}
                for k,p in configured_policies(group).items()}
    for name in SOCIAL_TOOLS | {'image_search'}: policies[name]['autonomous_enabled'] = False
    return policies


def write_permissions(db, scope, body):
    old = db.get('qq_access', scope, {})
    check_revision(old.get('revision',0), body.revision, '账号权限')
    value = {**body.model_dump(), 'revision':body.revision+1}
    db.put('qq_access',scope,value)
    db.audit(scope,'qq_permissions',{'revision':value['revision'],'members':list(value['members']),
                                    'roles':list(value['roles']),'actor':'web:admin'})
    return value


def register(api, db, tools, onebot, Group, Policy, validate_qq):
    class GroupPatch(Group):
        expected_revision: int = Field(ge=0, strict=True)

    class AccessWrite(BaseModel):
        model_config = ConfigDict(extra='forbid')
        group_revision: int = Field(ge=0, strict=True)
        interaction_enabled: bool
        tools: dict[str, Policy]
        permissions: AccessConfig

    def group_for(scope):
        group = db.get('group',scope)
        if not group: raise HTTPException(404,'会话不存在')
        return group

    def snapshot(scope):
        group = group_for(scope)
        return {'group_revision':group.get('revision',0), 'interaction_enabled':group.get('interaction_enabled',False),
                'tools':configured_policies(group), 'permissions':tools.permissions.config(scope)}

    @api.get('/groups/{scope}/access')
    async def access(scope: str):
        return snapshot(scope)

    @api.put('/groups/{scope}/access')
    async def save_access(scope: str, body: AccessWrite):
        with db.transaction():
            group = group_for(scope)
            check_revision(group.get('revision',0),body.group_revision,'会话配置')
            policies = policy_update(group,body.tools,validate_qq)
            write_permissions(db,scope,body.permissions)
            group.update(tools=policies,interaction_enabled=body.interaction_enabled,revision=body.group_revision+1)
            db.put('group',scope,group)
            db.audit(scope,'tool_policy',{'revision':group['revision'],'policies':{k:v.model_dump(exclude_unset=True) for k,v in body.tools.items()}})
        return snapshot(scope)

    @api.patch('/groups/{scope}')
    async def patch_group(scope: str, body: GroupPatch):
        with db.transaction():
            group = group_for(scope)
            if onebot.self_id and scope.split(':')[0] != onebot.self_id:
                raise HTTPException(422,'会话不属于当前机器人')
            check_revision(group.get('revision',0),body.expected_revision,'会话配置')
            patch = body.model_dump(exclude_unset=True,exclude={'expected_revision'})
            merged = {**group, **patch}
            try:
                validated = Group.model_validate({k:v for k,v in merged.items() if k in Group.model_fields})
            except ValidationError as exc:
                raise HTTPException(422,'；'.join(e['msg'] for e in exc.errors())) from exc
            if not db.get('persona',merged['persona']): raise HTTPException(422,'角色不存在')
            if patch.get('enabled') and not db.config()['initialized']:
                raise HTTPException(422,'请先配置模型密钥和管理员 QQ 号')
            merged['revision'] = group.get('revision',0)+1
            db.put('group',scope,merged)
            db.audit(scope,'group',{'changes':patch,'revision':merged['revision']})
        return {**validated.model_dump(), **merged}
