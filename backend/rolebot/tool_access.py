"""Shared service gates and effective limits for execution, previews and model facts."""


def service_checks(tools, scope, name):
    from .tools import SPECS, INTERACTION_TOOLS, configured_policies
    group = tools.db.get('group', scope, {})
    policy = configured_policies(group)[name]
    checks = []
    def check(key, label, passed, code, reason):
        checks.append({'key': key, 'label': label, 'passed': bool(passed),
                       'error_code': None if passed else code, 'reason': '' if passed else reason})
    check('conversation', '会话状态', group.get('enabled') and not group.get('paused'),
          'conversation_paused', '会话未启用或已暂停')
    check('group', '会话类型', not SPECS[name].group_only or ':g:' in scope,
          'group_required', '此工具仅适用于群聊')
    check('management', '管理工具总控', not (group.get('management_paused') and SPECS[name].mutation),
          'management_paused', '管理工具已紧急停用')
    check('interaction', '互动扩展', name not in INTERACTION_TOOLS or group.get('interaction_enabled'),
          'interaction_disabled', '互动扩展未开启')
    check('tool', '工具开关', policy.get('enabled'), 'tool_disabled', '工具开关未开启')
    engine = getattr(tools, 'engine', None)
    service = getattr(engine, 'image_search', None)
    configured = (bool(service and service.key()) if name == 'image_search' else
                  bool(engine and engine.creations.configured()) if name == 'creation_create' else True)
    check('service', '服务配置', configured, 'service_unavailable',
          '搜图服务尚未配置' if name == 'image_search' else '制作服务尚未就绪')
    return checks


def effective_limits(tools, scope, name, effective):
    from .tools import configured_policies
    configured = configured_policies(tools.db.get('group', scope, {}))[name]
    prefix = 'profile_' if effective['tool_access'][name]['mode'] == 'profile' else ''
    requester_limit = effective[prefix+'max_duration']
    account_protected = effective[prefix+'protected_ids']
    implicit = sorted(set(tools.db.config()['admin_qq']) | {tools.onebot.self_id}) if name in (
        'member_mute', 'member_kick', 'message_recall') else []
    return {'max_per_hour': configured['max_per_hour'],
            'max_duration': min(configured['max_duration'], requester_limit) if requester_limit is not None else configured['max_duration'],
            'duration_sources': {'tool': configured['max_duration'], 'requester': requester_limit},
            'protected_ids': sorted(set(configured.get('protected_ids', [])) | set(account_protected)),
            'protection_sources': {'tool': configured.get('protected_ids', []), 'requester': account_protected,
                                   'default': [x for x in implicit if x]},
            'protection_notice': ('默认保护的本人撤回例外仍需真实作者与本人请求核验；' if name == 'message_recall' else '')+'QQ 身份等级限制在执行时另行检查'}


def check_mode(policy, mode):
    from .tools import ToolDenied
    if mode not in ('request', 'assistance', 'shared', 'scheduled_admin', 'autonomous'):
        raise ToolDenied('调用方式无效', code='invalid_call_mode')
    if mode in ('request', 'shared', 'scheduled_admin') and not policy.get('user_enabled', True):
        raise ToolDenied('后台已关闭用户请求此工具', code='user_requests_disabled')
    if mode in ('assistance', 'shared', 'autonomous') and not policy.get('autonomous_enabled', True):
        raise ToolDenied('后台已关闭自主使用此工具', code='autonomous_disabled')
