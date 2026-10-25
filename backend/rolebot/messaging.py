"""Structured outbound messages. Recipients are checked in the bound conversation."""
import base64
import time

from .onebot import ActionUnknown


def reply_plans(executed):
    return [x['result']['delivery'] for x in executed if x.get('status') == 'success'
            and isinstance(x.get('result'), dict) and x['result'].get('prepared') and x['result'].get('delivery')]


def combine_reply_plans(plans):
    """Combine complementary prepared parts, never different recipients or rewrites."""
    if not plans:
        return None
    if len(plans) > 2:
        raise ValueError('本轮最多准备一段正文和一张表情')
    plan = dict(plans[0])
    if len(plans) == 1:
        return plan
    extra = plans[1]
    if any(plan.get(k) != extra.get(k) for k in ('source_id', 'actor')):
        raise ValueError('不能合并不同请求者或不同消息的回复')
    for key in ('text', 'mentions', 'sticker_id'):
        if plan.get(key) and extra.get(key) and plan[key] != extra[key]:
            raise ValueError('不能替换已准备的回复或追加第二张表情')
    adds_image = extra.get('sticker_id') and not plan.get('sticker_id')
    adds_caption = plan.get('sticker_id') and not plan.get('text') and not plan.get('mentions') and (extra.get('text') or extra.get('mentions'))
    if not (adds_image or adds_caption):
        raise ValueError('本轮已准备最终回复')
    for key in ('text', 'mentions', 'sticker_id'):
        plan[key] = plan.get(key) or extra.get(key)
    plan['permission_tools'] = list(dict.fromkeys([plan['permission_tool'], extra['permission_tool']]))
    plan['authorizations'] = [*plan.get('authorizations', []), *extra.get('authorizations', [])]
    return plan


async def recipients(bot, scope, values, actor):
    if values and ':g:' not in scope:
        raise ValueError('真正 @ 只适用于群聊')
    result = list(dict.fromkeys(actor if x == 'self' else x for x in values))
    if len(result) > 3:
        raise ValueError('一次最多 @ 三位群友')
    for qq in result:
        if not qq.isdigit() or not 5 <= len(qq) <= 20:
            raise ValueError('需要明确 QQ，不支持昵称或 @全体')
        info = await bot.call('get_group_member_info', {'group_id': scope.split(':')[2], 'user_id': qq, 'no_cache': True})
        if not isinstance(info, dict) or str(info.get('user_id')) != qq or str(info.get('group_id', scope.split(':')[2])) != scope.split(':')[2]:
            raise ValueError('无法确认目标是当前群成员')
    return result


def sticker_path(settings, item):
    # ID and extension originate in service-created records, never in model paths.
    if not item['id'].isalnum() or item.get('ext', 'jpg') not in ('jpg', 'jpeg', 'png', 'gif', 'webp'):
        raise ValueError('表情文件记录无效')
    return settings.data_dir / 'stickers' / (item['id']+'.'+item.get('ext', 'jpg'))


def available_sticker(db, scope, id):
    item = db.get('sticker', id)
    if not item or not item.get('enabled', True):
        raise ValueError('表情不存在或已禁用')
    return item


def segments(db, settings, scope, text, mentions=(), sticker_id=None):
    result = [{'type': 'at', 'data': {'qq': q}} for q in mentions]
    if text:
        result.append({'type': 'text', 'data': {'text': (' ' if mentions else '')+text}})
    if sticker_id:
        item = available_sticker(db, scope, sticker_id)
        raw = sticker_path(settings, item).read_bytes()
        # NapCat forwards sub_type to QQ's picSubType: 1 is a custom sticker.
        # Keep original pixels/animation; QQ controls the compact sticker display.
        result.append({'type': 'image', 'data': {
            'file': 'base64://'+base64.b64encode(raw).decode(),
            'sub_type': 1, 'summary': '[动画表情]'}})
    if not result:
        raise ValueError('消息内容为空')
    return result


def reserve(db, scope):
    state = db.get('chat_state', scope, {})
    state['sent'] = [t for t in state.get('sent', []) if t > time.time()-60]
    if len(state['sent']) >= 6:
        raise ValueError('聊天发送达到每分钟上限')
    state['sent'].append(time.time())
    db.put('chat_state', scope, state)


async def send_task(engine, task, text, guard):
    db, scope = engine.db, task['scope']
    mentions = await recipients(engine.onebot, scope, task.get('mentions', []), task['creator'])
    await engine.scheduler.authorize_fresh(task)
    guard()  # Recheck cancellation and permission after platform I/O.
    outgoing = segments(db, engine.settings, scope, text, mentions)
    reserve(db, scope)
    result = await engine.onebot.send(scope, outgoing)
    if not result or result.get('message_id') is None:
        raise ActionUnknown('发送结果未知；未自动重试')
    return result


async def find_members(bot, scope, query):
    gid = scope.split(':')[2]
    rows = await bot.call('get_group_member_list', {'group_id': gid, 'no_cache': True})
    if not isinstance(rows, list):
        raise ValueError('未取得成员列表')
    exact, partial = [], []
    for item in rows:
        if str(item.get('group_id', gid)) != gid:
            continue
        names = [str(item.get(k, '')) for k in ('user_id', 'card', 'nickname')]
        value = {k: str(item.get(k, '')) for k in ('user_id', 'card', 'nickname')}
        if query in names: exact.append(value)
        elif any(query.casefold() in n.casefold() for n in names): partial.append(value)
    matches = exact or partial
    return {'members': matches[:20], 'ambiguous': len(matches) != 1, 'total': len(matches)}
