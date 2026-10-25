"""Resolve one authorized chat attachment before uploading it to Lens."""
import base64

from .attachments import register, visible
from .image_search import LensError
from .image_turn import active_images
from .media import MediaError
from .tools import ToolDenied


async def search(engine, scope, persona, actor, source_id, aid, mode):
    turn = active_images.get()
    if not turn or (turn.scope, turn.persona) != (scope, persona) or mode != 'request':
        raise ToolDenied('以图搜图需要当前成员的明确请求，不支持自主或定时调用', code='request_context_unclear')
    source = next((m for m in turn.batch if m['id'] == source_id and m['user_id'] == actor), None)
    if source is None:
        raise ToolDenied('无法确认搜图请求者', code='source_mismatch')
    if turn.search_attempted:
        raise ToolDenied('本轮已经尝试过搜图，不重复请求；请使用已有结果或说明未能完成', code='search_limit')
    service = engine.image_search
    transient = next((m for m in turn.batch if any(x['id'] == aid for x in register(engine.db, scope, persona, m))), None)
    try:
        item = (next(x for x in register(engine.db, scope, persona, transient) if x['id'] == aid)
                if transient else engine.attachments.item(scope, persona, aid))
    except MediaError as exc:
        raise ToolDenied(str(exc), code=exc.code) from None
    current_ids = {m['id'] for m in turn.batch}
    quoted = {scope+':'+str(mid) for mid in source.get('replies', source.get('data', {}).get('replies', []))}
    historical = item['source_id'] not in current_ids | quoted
    try:
        credentials = service.credentials()
    except LensError as exc:
        raise ToolDenied(exc.message, code=exc.code) from None

    def guard():
        if not engine.current(scope, persona, turn.epoch) or not engine.audience_current(scope, turn.batch):
            raise ToolDenied('会话或请求者授权已变化，搜图已停止', code='context_changed')
        if credentials != (service.key(), service.revision()):
            raise ToolDenied('搜图密钥已变更，本次结果不再使用', code='context_changed')
        policy = engine.tools.policy(scope, 'image_search')
        if not policy.get('enabled') or not policy.get('user_enabled', True):
            raise ToolDenied('以图搜图已关闭', code='user_requests_disabled')
        engine.permissions.check_tool(scope, actor, 'image_search', {})
        if historical:
            policy = engine.tools.policy(scope, 'image_read')
            if not policy.get('enabled') or not policy.get('user_enabled', True):
                raise ToolDenied('没有开放这张历史图片的读取权限', code='user_permission_denied')
            engine.permissions.check_tool(scope, actor, 'image_read', {})
        if not (transient and not transient.get('remember', True)) and not visible(engine.db, scope, persona, item['source_id']):
            raise ToolDenied('图片来源已遗忘、过期或停止记忆', code='source_unavailable')

    async def refresh_guard():
        await engine.permissions.refresh(scope, [actor])
        guard()

    await refresh_guard()
    turn.search_attempted = True  # Timeouts, cancellations and failures do not retry this turn.
    turn.search_checks.append(guard)
    try:
        item, block = await engine.attachments.read(scope, persona, aid, turn.epoch, transient)
        guard()
        attached = await turn.add(aid, transient, prepared=(item, block))
        raw = base64.b64decode(block['image_url']['url'].split(',', 1)[1], validate=True)
        result = await service.search(raw, context={
            'scope': scope, 'persona': persona, 'epoch': turn.epoch,
            'remember': all(m.get('remember', True) for m in turn.batch),
            'source_ids': sorted(current_ids | {item['source_id']}),
            'actor_ids': sorted({actor, item['sender']} | {m['user_id'] for m in turn.batch}),
        }, guard=guard, refresh_guard=refresh_guard)
    except (LensError, MediaError) as exc:
        raise ToolDenied(str(exc), code=exc.code) from None
    guard()
    turn.search_sources.add(item['source_id'])
    if aid in turn.loaded and aid in turn.pending:
        turn.loaded[aid]['label'].update(search_for_source=source_id,
            notice='这是本轮搜图所用的原图；请结合工具检索结果回答对应请求，仍按本轮要求返回 JSON。')
    from .image_evidence import link
    ref = link(engine.db, item, source, result) if all(m.get('remember', True) for m in turn.batch) else None
    if ref:
        turn.search_refs.add(ref)
    return {'image_id': aid, 'source_id': item['source_id'],
            'image_for_reply': attached['status'],
            **{key: result[key] for key in ('provider', 'status', 'cached', 'search_requests_this_run',
                'matches', 'overview', 'overview_status', 'overview_notice', 'notice', 'error_code', 'error')
               if key in result},
            'notice': '以图检索候选；匹配类型见 kind，来源见 url。'}
