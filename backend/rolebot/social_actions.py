"""Explicit, scoped QQ social actions with cross-group attempt limits."""
import time
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from .onebot import OneBotError, ActionUnknown

SOCIAL_TOOLS = {'profile_like', 'member_poke'}


def reserve(db, bot_id, scope, actor, source_id, name, target, policy):
    """Reserve BEFORE I/O. Unknown/failed attempts cannot become retry storms."""
    from .tools import ToolDenied
    now = time.time()
    day = datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai')).date().isoformat()
    key = f'{bot_id}:{name}:{target}'
    with db.transaction():
        old = db.get('social_attempt', key, {})
        if name == 'member_poke' and old.get('blocked_until', 0) > now:
            raise ToolDenied('这个 QQ 的戳一戳还在冷却中，本次未发送', code='poke_cooldown')
        db.put('social_attempt', key, {'id': key, 'scope': scope, 'actor': actor, 'source_id': source_id,
            'day': day, 'attempted_at': now, 'blocked_until': now+policy.get('poke_cooldown_seconds', 60)})


async def execute(tools, scope, persona, actor, source_id, name, args, mode):
    from .tools import ToolDenied
    if mode != 'request':
        raise ToolDenied('点赞和戳一戳需要可核实的用户请求，不自主发送', code='autonomous_disabled')
    await tools.permissions.check_tool_fresh(scope, actor, name, args)
    tools.check_policy(scope, name, args)
    if tools.db.get('group', scope, {}).get('persona') != persona:
        raise ToolDenied('当前角色已变化', code='context_changed')
    from . import like_budget
    reservation = None
    if name == 'profile_like':
        previous = tools.db.get('social_attempt', f"{tools.onebot.self_id}:profile_like:{args['user_id']}", {})
        if previous.get('source_id') == source_id and previous.get('day') == like_budget.day_at(time.time()):
            raise ToolDenied('这条消息的点赞请求已经处理过，本次没有重复提交；需要重试请发一条新请求', code='like_request_already_processed')
        reservation = like_budget.reserve(tools.db, tools.onebot.self_id, scope, actor, source_id,
            args['user_id'], args.get('times'), tools.policy(scope, name))
    else:
        reserve(tools.db, tools.onebot.self_id, scope, actor, source_id, name, args['user_id'], tools.policy(scope, name))
    params = ({'user_id': args['user_id'], 'times': reservation['amount']} if reservation else
              {'group_id': scope.split(':')[2], 'user_id': args['user_id']})
    try:
        await tools.onebot.call('send_like' if name == 'profile_like' else 'group_poke', params, mutation=True)
    except (ActionUnknown, asyncio.CancelledError):
        if reservation: like_budget.settle(tools.db, reservation, 'unknown')
        raise  # Keep an unknown outcome; never retry another endpoint.
    except OneBotError as exc:
        if reservation:
            reason = like_budget.rejection_reason(exc.upstream_message)
            if not reason and exc.request_sent is not False:
                like_budget.settle(tools.db, reservation, 'unknown')
                raise ActionUnknown('QQ 返回了无法核实实际赞数的错误，本次停止补发') from None
            like_budget.settle(tools.db, reservation, reason or 'not_sent')
            if reason == 'restricted':
                raise ToolDenied('QQ 明确返回对方的点赞权限不允许，本次未点赞；调整对方设置后可重新明确请求，不会自动补赞', code='like_target_restricted') from None
            if reason == 'quota':
                raise ToolDenied('QQ 明确返回对该目标今日点赞额度用尽，本次未点赞，不会自动补赞', code='like_platform_daily_limit') from None
        raise ToolDenied('QQ 接口没有接受这次互动请求，未自动重试；'+str(exc)[:150], code='social_platform_rejected') from None
    receipt = like_budget.settle(tools.db, reservation, 'accepted') if reservation else {}
    return {'submitted': True, 'user_id': args['user_id'], 'action': name,
            **({'requested_times': reservation['amount'], **receipt} if reservation else {}),
            'verification': 'platform_acknowledged',
            'notice': 'QQ 接口已接受请求；未独立核验主页赞数或客户端动画，不能声称已核实显示结果。每日赞数上限来自后台配置，不能编成 QQ 一天只能请求一次，也不会稍后或明天自动补赞。' if reservation else
                      'QQ 接口已接受请求；未独立核验客户端动画，不能声称已核实显示结果。'}
