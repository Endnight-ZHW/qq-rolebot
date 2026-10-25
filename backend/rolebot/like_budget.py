"""Cumulative, cross-group like budget. A receipt is not a verified profile count."""
import json
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo


def day_at(stamp):
    return datetime.fromtimestamp(stamp, ZoneInfo('Asia/Shanghai')).date().isoformat()


def rejection_reason(message):
    """Only known platform refusals prove that no likes were applied."""
    if not isinstance(message, str): return None
    if any(x in message for x in ('权限设置', '对方设置', '禁止点赞', '不允许点赞', '不是好友')):
        return 'restricted'
    if '点赞' in message and any(x in message for x in ('今日', '今天', '每日', '当天')) and any(x in message for x in ('上限', '次数已用完', '额度已用完')):
        return 'quota'
    return None


def receipts(db, source_id, bot_id, target, day, after=None):
    """Match real send_like requests and their own echoes, not newest traces.

    Extract/replay traces and unrelated responses are never execution evidence.
    Project just the relevant stages so sticker payloads are not loaded here.
    """
    scope = source_id.rsplit(':', 1)[0]
    if not scope.startswith(bot_id+':g:'): return None
    requests = db.rows("SELECT t.id,s.value AS request FROM objects t,json_each(t.data,'$.stages') s "
        "WHERE t.kind='trace' AND json_extract(t.data,'$.kind')='chat' AND json_extract(t.data,'$.scope')=? "
        "AND coalesce(json_extract(t.data,'$.redacted'),0)=0 AND EXISTS "
        "(SELECT 1 FROM json_each(t.data,'$.source_ids') WHERE value=?) "
        "AND json_extract(s.value,'$.name')='onebot_request' AND json_extract(s.value,'$.data.action')='send_like' "
        "AND CAST(json_extract(s.value,'$.data.params.user_id') AS TEXT)=?", (scope,source_id,target))
    outcomes = {}; response_at = 0
    for record in requests:
        request = json.loads(record['request']); stamp = request.get('ts')
        if stamp is not None and day_at(stamp) != day: continue
        if after is not None and (stamp is None or stamp < after): continue
        data = request['data']; echo = data.get('echo'); amount = data.get('params', {}).get('times')
        if not echo or type(amount) is not int or not 1 <= amount <= 10: continue
        responses = db.rows("SELECT s.value AS response FROM objects t,json_each(t.data,'$.stages') s "
            "WHERE t.kind='trace' AND t.id=? AND json_extract(s.value,'$.name')='onebot_response' "
            "AND json_extract(s.value,'$.data.echo')=?", (record['id'],echo))
        results = set()
        for entry in responses:
            stage = json.loads(entry['response'])
            response = stage.get('data', {}).get('response') or {}
            if response.get('echo', echo) != echo: continue
            response_at = max(response_at, stage.get('ts', 0))
            if response.get('status') == 'ok' and response.get('retcode', 0) == 0: results.add('accepted')
            elif response.get('status') == 'failed':
                results.add(rejection_reason(response.get('wording') or response.get('message')) or 'unknown')
            else: results.add('unknown')
        outcome = next(iter(results)) if len(results) == 1 else 'unknown'
        previous = outcomes.get(echo)
        outcomes[echo] = (amount, outcome) if previous is None or previous == (amount,outcome) else (max(amount,previous[0]),'unknown')
    if not outcomes: return None
    return {'submitted_count':sum(amount for amount, result in outcomes.values() if result == 'accepted'),
        'unknown':any(result == 'unknown' for amount, result in outcomes.values()),
        'platform_daily_block':any(result == 'quota' for amount, result in outcomes.values()),
        'receipt_count':len(outcomes), 'response_at':response_at}


def load(db, key, bot_id, target, now):
    old = db.get('social_attempt', key, {})
    if not old:
        return {'version': 2, 'day': day_at(now), 'submitted_count': 0}
    if old.get('version') != 2:
        from .tools import ToolDenied
        raise ToolDenied('点赞记录格式不受支持，本次未提交；请先核查恢复的数据版本', code='like_state_unavailable')
    if old.get('day') != day_at(now):
        pending = old.get('pending')
        if pending and now-pending.get('ts', 0) < 120:
            return old  # A request spanning midnight must finish first.
        if old.get('unknown') or pending:
            proof = receipts(db, old.get('source_id', ''), bot_id, target, old['day'], after=old.get('attempted_at', now))
            if proof and not proof['unknown'] and day_at(proof['response_at']) == day_at(now):
                return {'version':2, 'day':day_at(now), 'submitted_count':proof['submitted_count'],
                    'platform_daily_block':proof['platform_daily_block'], 'last_outcome':'recovered_receipt',
                    'retry_after':proof['response_at']+30 if not proof['submitted_count'] else 0}
            if old.get('unknown') and now-old.get('attempted_at', 0) < 120:
                return old
        return {'version': 2, 'day': day_at(now), 'submitted_count': 0}
    # A delayed QQ receipt can settle uncertainty without another QQ request.
    stale = old.get('pending') and now-old['pending'].get('ts', 0) >= 120
    if old.get('unknown') or stale:
        proof = receipts(db, old.get('source_id', ''), bot_id, target, old['day'], after=old.get('attempted_at', now))
        if proof and not proof['unknown']:
            old['submitted_count'] += proof['submitted_count']
            old.update(unknown=False, platform_daily_block=proof['platform_daily_block'], last_outcome='recovered_receipt')
            if not proof['submitted_count']:
                old['retry_after'] = max(old.get('retry_after', 0), proof['response_at']+30)
            old.pop('pending', None)
    return old


def reserve(db, bot_id, scope, actor, source_id, target, requested, policy):
    from .tools import ToolDenied
    now = time.time(); key = f'{bot_id}:profile_like:{target}'
    limit = policy.get('like_daily_limit', 10)
    with db.transaction():
        row = load(db, key, bot_id, target, now)
        if row.get('unknown') or row.get('pending') and now-row['pending'].get('ts', 0) >= 120:
            raise ToolDenied('上次点赞结果尚未确认，本次没有补发；这不是 QQ 规定一天只能请求一次', code='like_result_unknown')
        if row.get('pending'):
            raise ToolDenied('这个 QQ 的点赞请求正在处理，本次没有重复提交', code='like_request_in_progress')
        if row.get('platform_daily_block'):
            raise ToolDenied('QQ 已明确返回该目标今日点赞额度用尽，本次没有重新提交', code='like_platform_daily_limit')
        if row.get('retry_after', 0) > now:
            raise ToolDenied('刚才的点赞被拒绝，稍等片刻后可明确请求重试；不会自动补赞', code='like_retry_cooldown')
        remaining = max(0, limit-row['submitted_count'])
        if not remaining:
            raise ToolDenied(f"今天累计提交了 {row['submitted_count']} 赞，已到后台设置的 {limit} 赞上限，本次未请求 QQ；不是 QQ 规定一天只能点一次", code='like_daily_limit')
        amount = min(policy.get('like_max_times', 10), remaining, requested if requested is not None else policy.get('like_max_times', 10))
        token = uuid.uuid4().hex
        row.update(id=key, scope=scope, actor=actor, source_id=source_id,
            pending={'id': token, 'amount': amount, 'ts': now}, attempted_at=now)
        db.put('social_attempt', key, row)
    return {'key': key, 'id': token, 'amount': amount, 'daily_limit': limit, 'requested_times': requested}


def settle(db, reservation, outcome):
    now = time.time()
    with db.transaction():
        row = db.get('social_attempt', reservation['key'], {})
        if row.get('pending', {}).get('id') != reservation['id']:
            raise RuntimeError('Like reservation changed while in flight')
        if row['day'] != day_at(now):
            row.update(day=day_at(now), submitted_count=0)
        row.pop('pending')
        if outcome == 'accepted': row['submitted_count'] += reservation['amount']
        elif outcome == 'unknown': row['unknown'] = True
        else:
            row['retry_after'] = now+30
            if outcome == 'quota': row['platform_daily_block'] = True
        row['last_outcome'] = outcome
        db.put('social_attempt', reservation['key'], row)
    return {'submitted_times': reservation['amount'], 'daily_submitted': row['submitted_count'],
        'daily_limit': reservation['daily_limit'], 'remaining_today': max(0, reservation['daily_limit']-row['submitted_count']),
        'limit_origin': 'bot_configuration', 'automatic_followup': False}
