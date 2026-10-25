"""Read-only console projections. Never load/decompress model request payloads."""
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException

ZONE = ZoneInfo('Asia/Shanghai')
OUTCOMES = ('success', 'http_error', 'network_error', 'invalid_response', 'error')
FAILURES = OUTCOMES[1:]


def validate_range(start=None, end=None, maximum=None):
    if any(v is not None and (not math.isfinite(v) or v < 0) for v in (start, end)):
        raise HTTPException(422, '时间必须为有效的 Unix 秒数')
    if start is not None and end is not None:
        if start >= end or (maximum and end-start > maximum):
            raise HTTPException(422, '时间范围无效或超出支持范围')


def usage_summary(db, scope='', start=None, end=None):
    validate_range(start, end)
    where, args = ['1=1'], []
    for field, op, value in (('scope', '=', scope or None), ('ts', '>=', start), ('ts', '<', end)):
        if value is not None:
            where.append(f'{field}{op}?'); args.append(value)
    clause = ' WHERE ' + ' AND '.join(where)
    rows = db.rows('SELECT scope,model,purpose,currency,COUNT(*) AS calls,SUM(input_tokens) AS input_tokens,'
                   'SUM(cached_tokens) AS cached_tokens,SUM(output_tokens) AS output_tokens,SUM(cost) AS cost '
                   'FROM usage' + clause + ' GROUP BY scope,model,purpose,currency', args)
    # Retain the historic no-argument contract; explicit ranges return all calendar days present.
    daily = db.rows("SELECT date(ts,'unixepoch','+8 hours') AS day,currency,SUM(cost) AS cost,COUNT(*) AS calls "
                    'FROM usage' + clause + ' GROUP BY day,currency ORDER BY day DESC' +
                    (' LIMIT 30' if start is None and end is None else ''), args)
    stages = []
    if db.rows("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_details'"):
        detail_clause = clause.replace('scope', 'u.scope').replace('ts', 'u.ts')
        stages = db.rows("SELECT COALESCE(d.stage,'legacy.unknown') AS stage,u.model,u.currency,COUNT(*) AS calls,"
            "SUM(CASE WHEN d.input_known=1 THEN u.input_tokens END) AS input_tokens,"
            "SUM(CASE WHEN d.cache_known=1 THEN u.cached_tokens END) AS cached_tokens,"
            "SUM(d.cache_miss_tokens) AS cache_miss_tokens,"
            "SUM(CASE WHEN d.output_known=1 THEN u.output_tokens END) AS output_tokens,"
            "SUM(COALESCE(d.input_known,0)=0 OR COALESCE(d.cache_known,0)=0 OR COALESCE(d.output_known,0)=0) AS unknown_calls,"
            "SUM(CASE WHEN d.input_known=1 AND d.cache_known=1 THEN u.input_tokens END) AS cache_measured_input,"
            "SUM(u.cost) AS cost FROM usage u LEFT JOIN usage_details d ON d.usage_id=u.id"+
            detail_clause+" GROUP BY 1,u.model,u.currency", args)
        for row in stages:
            row['cache_hit_ratio'] = row['cached_tokens']/row['cache_measured_input'] if row['cache_measured_input'] else None
    return {'rows': rows, 'daily': daily, 'by_stage': stages, 'note': '按后台配置费率估算；以供应商账单为准。阶段指标缺失标为未知；旧记录不补造。未设置金额上限。'}


def dashboard(db, health, scope='', start=None, end=None):
    now = time.time()
    end = now if end is None else end
    start = end-86400 if start is None else start
    validate_range(start, end, 31*86400)
    if scope and not db.get('group', scope):
        raise HTTPException(404, '会话不存在')
    where, args = 'ts>=? AND ts<? AND expires>?', [start, end, now]
    if scope: where += ' AND scope=?'; args.append(scope)
    first = db.rows('SELECT MIN(ts) AS ts FROM model_calls WHERE expires>?', (now,))[0]['ts']
    coverage = max(now-db.config().get('model_log_days', 7)*86400, first if first is not None else now)
    statuses = {r['status']: r['n'] for r in db.rows('SELECT status,COUNT(*) AS n FROM model_calls WHERE '+where+' GROUP BY status', args)}
    success = statuses.get('success', 0)
    failed = sum(statuses.get(s, 0) for s in FAILURES)
    completed = success+failed
    duration_where = where + " AND status IN ('success','http_error','network_error','invalid_response','error') AND json_type(meta,'$.duration_ms') IN ('integer','real') AND json_extract(meta,'$.duration_ms')>=0"
    duration_count = db.rows('SELECT COUNT(*) AS n FROM model_calls WHERE '+duration_where, args)[0]['n']
    p95 = None
    if duration_count:
        p95 = db.rows("SELECT json_extract(meta,'$.duration_ms') AS ms FROM model_calls WHERE "+duration_where+
                      " ORDER BY json_extract(meta,'$.duration_ms') LIMIT 1 OFFSET ?", (*args, math.ceil(duration_count*.95)-1))[0]['ms']
    bucket = 3600 if end-start <= 2*86400 else 86400
    bucket_sql = f'(CAST((ts+28800)/{bucket} AS INTEGER)*{bucket}-28800)'
    bins = {r['ts']: r for r in db.rows('SELECT '+bucket_sql+" AS ts,COUNT(*) AS calls,SUM(status='success') AS success,"
            "SUM(status IN ('http_error','network_error','invalid_response','error')) AS failed FROM model_calls WHERE "+where+' GROUP BY 1', args)}
    series = []
    cursor = math.floor((start+28800)/bucket)*bucket-28800
    while cursor < end:
        row = bins.get(cursor, {'calls': 0, 'success': 0, 'failed': 0})
        unavailable = min(cursor+bucket, end) <= coverage
        series.append({'ts': cursor, 'label': datetime.fromtimestamp(cursor, ZONE).strftime('%m-%d %H:%M' if bucket==3600 else '%m-%d'),
                       **{k: None if unavailable else row[k] for k in ('calls', 'success', 'failed')},
                       'partial': cursor < coverage})
        cursor += bucket
    usage = usage_summary(db, scope, start, end)['rows']
    totals = {k: sum(r[k] or 0 for r in usage) for k in ('input_tokens', 'cached_tokens', 'output_tokens')}
    costs, purposes = {}, {}
    for r in usage:
        costs[r['currency']] = costs.get(r['currency'], 0)+(r['cost'] or 0)
        item = purposes.setdefault(r['purpose'], {'purpose': r['purpose'], 'calls': 0, 'input_tokens': 0, 'cached_tokens': 0, 'output_tokens': 0, 'costs': {}})
        for k in ('calls', 'input_tokens', 'cached_tokens', 'output_tokens'): item[k] += r[k] or 0
        item['costs'][r['currency']] = item['costs'].get(r['currency'], 0)+(r['cost'] or 0)
    def money_map(values): return [{'currency': c, 'cost': v} for c, v in sorted(values.items())]
    for p in purposes.values(): p['costs'] = money_map(p['costs'])
    group_counts = {r['scope']: r for r in db.rows("SELECT scope,COUNT(*) AS calls,SUM(status IN ('http_error','network_error','invalid_response','error')) AS failures FROM model_calls WHERE "+where+' GROUP BY scope', args)}
    groups = [{k: v for k, v in g.items() if k in ('id', 'name', 'enabled', 'paused', 'management_paused', 'persona')} |
              {'calls': group_counts.get(g['id'], {}).get('calls', 0), 'failures': group_counts.get(g['id'], {}).get('failures', 0)}
              for g in db.all('group') if not scope or g['id']==scope]
    alerts = [{'id': r['id'], 'scope': r['scope'], 'type': 'call', 'status': r['status'], 'ts': r['ts'],
               'title': (r['model'] or r['purpose'] or '接口调用'), 'trace_id': r['trace_id']} for r in db.rows(
        "SELECT id,scope,status,ts,trace_id,json_extract(meta,'$.model') AS model,json_extract(meta,'$.purpose') AS purpose FROM model_calls WHERE "+where+
        " AND status IN ('http_error','network_error','invalid_response','error','interrupted','not_sent') ORDER BY ts DESC LIMIT 8", args)]
    for kind, alert_type in (('task_run', 'task'), ('memory_job', 'memory_job')):
        rows = db.scoped_objects(kind, scope or None, statuses=('failed', 'unknown', 'missed', 'retry'), limit=20)
        for r in rows:
            ts = r.get('finished_at') or r.get('started_at') or r.get('updated_at') or r.get('created_at') or r.get('ts', 0)
            if start <= ts < end:
                alerts.append({'id': r.get('task_id', r['id']), 'scope': r.get('scope', ''), 'type': alert_type, 'status': r['status'], 'ts': ts,
                               'title': '任务执行' if alert_type=='task' else '记忆整理', 'trace_id': r.get('trace_id')})
    tasks = sorted(db.scoped_objects('reminder', scope or None, statuses=('pending', 'running', 'sending'), limit=None), key=lambda r:r.get('due', 0))[:6]
    return {'generated_at': now, 'range': {'start': start, 'end': end, 'bucket': 'hour' if bucket==3600 else 'day', 'coverage_start': coverage, 'partial': start < coverage},
            'health': health, 'metrics': {'calls': sum(statuses.values()), 'success': success, 'failed': failed,
            'excluded': {k: v for k, v in statuses.items() if k not in OUTCOMES}, 'success_rate': success/completed if completed else None,
            'p95_ms': p95, **totals, 'costs': money_map(costs)}, 'series': series, 'purposes': list(purposes.values()), 'groups': groups,
            'alerts': sorted(alerts, key=lambda a:a['ts'], reverse=True)[:8],
            'tasks': [{k:v for k,v in r.items() if k in ('id','scope','text','due','status')} for r in tasks]}
