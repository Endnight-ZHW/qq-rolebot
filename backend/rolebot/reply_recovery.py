"""One format-only recovery; tool effects are never replayed."""
import time
from .config_schema import runtime_config

from . import messaging, prompts, tracing
from .input_budget import measure, projected
from .model import parse_json


def decision(response, batch):
    if not response or response.get('tool_calls'):
        raise ValueError('缺少最终回复')
    value = parse_json(response.get('content'))
    if type(value.get('speak')) is not bool or not isinstance(value.get('text'), str):
        raise ValueError('回复缺少 speak/text')
    selected = value.get('reply_to_message_id')
    if value['speak'] and (selected not in {m['id'] for m in batch} | {m['message_id'] for m in batch}
                           and not (not selected and len(batch) == 1)):
        raise ValueError('回复目标不属于本轮消息')
    return value


def effects(executed):
    from .tools import SPECS
    # Read-only lookups must not hide a failed mutation, or be reported as one.
    return [x for x in executed if x.get('name') in SPECS and (
        SPECS[x['name']].mutation or x['name'] in
        ('reminder_create', 'reminder_cancel', 'task_create', 'task_update', 'sticker_save'))]


def failure_text(executed):
    actions = effects(executed)
    if any(x.get('status') in ('unknown', 'running', 'error') for x in actions):
        return '刚才操作的结果还没确认，我不会自动重试。请先核对一下实际结果。'
    if any(x.get('status') == 'success' for x in actions):
        return '操作结果已经收到，但我整理回复时出了点问题，不会重复执行。可以让我查询一下当前状态。'
    codes = {x.get('error_code') for x in actions if x.get('status') == 'denied'}
    if codes & {'request_context_unclear', 'request_verification_failed', 'source_mismatch', 'context_changed'}:
        return '刚才的请求还没核对清楚，我这次没执行。请把要做的事、目标和内容说完整一点。'
    if codes & {'user_permission_denied', 'policy_denied', 'user_requests_disabled', 'autonomous_disabled'}:
        return '这次操作被权限或工具设置拦住了，我没有执行，需要管理员核对设置。'
    if actions and all(x.get('status') == 'denied' for x in actions):
        return '这次操作在提交前被拦下了，我没有执行，需要先核对目标和操作条件。'
    return '刚才回复没整理好，我这轮没答上来。麻烦再说一下具体想问什么呀。'


async def recover(engine, scope, batch, card, response, executed, messages, current, budget, *, timeout=None):
    try:
        return decision(response, batch)
    except (ValueError, TypeError):
        tracing.stage(engine.db, 'reply_invalid', {'notice': '回复结构无效，准备一次格式恢复'}, content=False)
    if not current():
        return None
    instruction = prompts.render(engine.db, scope, 'runtime.reply_repair')
    repaired_messages = [*messages, {'role': 'system', 'content': instruction}]
    try:
        if measure({'messages': projected(repaired_messages)})['estimated_input'] > budget:
            raise ValueError('格式恢复超过输入预算')
        repaired = await engine.model.chat(scope, 'chat', repaired_messages, json_mode=True,
                                           max_tokens=8192, stage='conversation.reply_repair')
        if not current():
            return None
        value = decision(repaired, batch)
        if value['speak'] and any(x.get('status') != 'success' for x in effects(executed)):
            value['text'] = failure_text(executed)
        tracing.stage(engine.db, 'reply_recovered', {'method': 'format_only', 'tools_reexecuted': False}, content=False)
        return value
    except Exception as exc:
        tracing.stage(engine.db, 'reply_repair_failed', {'type': type(exc).__name__}, content=False)
    if not current():
        return None
    # A verified prepared reply has a bound target and still uses normal delivery checks.
    plan = messaging.combine_reply_plans(messaging.reply_plans(executed))
    targets = ([m for m in batch if m['id'] == plan['source_id']] if plan else
               [m for m in batch if engine.direct([m], card)])
    if len(targets) != 1 or time.time()-targets[0]['ts'] > (timeout if timeout is not None else runtime_config(engine.db, scope)['reply_timeout_seconds']):
        return None
    target = targets[0]
    text = plan.get('text', '') if plan else failure_text(executed)
    tracing.stage(engine.db, 'reply_fallback', {'target_id': target['id'], 'prepared': bool(plan),
                                              'tools_reexecuted': False}, content=False)
    return {'speak': True, 'reply_to_message_id': target['id'], 'text': text, 'mode': 'direct',
            'continues_dialogue': False, 'expects_followup': False, 'reason': '回复结构恢复失败，反馈本轮已知状态'}
