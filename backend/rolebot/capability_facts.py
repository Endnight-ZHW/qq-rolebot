"""Turn-local capability answers and auditable past actions; no new authority."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import tracing

LABELS = {
    'member_mute': ('禁言', r'禁言'), 'member_unmute': ('解禁', r'解禁|解除禁言'),
    'message_recall': ('撤回消息', r'撤回'), 'member_kick': ('移出群聊', r'踢人|踢出|移出群'),
    'member_title': ('改群头衔', r'头衔'), 'member_card': ('改群名片', r'名片|群昵称'),
    'group_name': ('改群名', r'群名(?!片)'), 'group_mute': ('全员禁言', r'全员禁言'),
    'profile_like': ('主页点赞', r'点赞|主页赞|补赞'), 'member_poke': ('戳一戳', r'戳一戳|戳人|戳过'),
    'web_search': ('联网搜索', r'联网|上网|搜索'), 'image_search': ('以图搜图', r'搜图|以图搜'),
}


def inquiry(text):
    """Only unquoted questions about the bot; mixed action requests stay normal."""
    text = re.sub(r'\[回复消息:[^\]]*\]|\[CQ:[^\]]*\]', '', text)
    text = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"|`[^`]*`', '', text)
    if re.search(r'假如|如果|假设|他说|她说|转述|角色扮演|(?:帮|给|替)(?:我|他|她)|试试|试一下|再来|重做|\d+\s*(?:秒|分钟|小时|赞)', text):
        return None
    names = [name for name, (_, pattern) in LABELS.items() if re.search(pattern, text)]
    if 'member_unmute' in names or 'group_mute' in names:
        names = [name for name in names if name != 'member_mute']
    if 'image_search' in names:
        names = [name for name in names if name != 'web_search']
    if not names:
        return None
    subjectless = re.sub(r'^\s*(?:\[@QQ:\d+\]\s*)?(?:(?:机器人|助手)[，,：:\s]*)?', '', text)
    if ('你' not in text and not re.search(r'(?:机器人|助手)(?:会|能|可以|有)', text)
        and not re.match(r'(?:请问[，,\s]*)?(?:有|会|能|可以|是不是|有没有)', subjectless)):
        return None
    past = bool(re.search(r'(?:看见|看到|明明|记得).{0,12}你|你.{0,10}(?:刚才|之前|早上|昨天|上次|以前)|(?:刚才|之前|早上|昨天|上次|以前).{0,10}你', text))
    # "Can you mute this person?" is an action request, not a capability demo.
    if not past and re.search(r'把|(?:禁言|解禁|撤回|踢出|点赞|戳).{0,6}(?:一下|这个|那个|他|她|本人|自己)|(?:给|替).{0,20}(?:赞|禁言|戳)|(?:现在|请).{0,12}(?:禁言|解禁|撤回|踢|点|戳)', text):
        return None
    question = bool(re.search(r'功能|能力|本事|会不会|有没有|是不是有|(?:有|没).{0,10}权限|(?:你|机器人|助手).{0,8}(?:会|能|可以).{0,16}[吗么？?]', text))
    return {'kind': 'history' if past else 'capability', 'tools': names[:3], 'text': text} if past or question else None


def capability_text(fact):
    label = LABELS[fact['name']][0]
    access = fact['access']
    prefix = f'有{label}这个功能呀。'
    if not access['available']:
        if access.get('error_code') == 'request_identity_unavailable':
            return prefix+'不过你当前的群身份还没核实到，不能先说你没权限。'
        if access.get('error_code') == 'tool_disabled':
            return prefix+'只是这个群现在没开放，我暂时用不了。'
        if access.get('error_code') in ('conversation_paused','management_paused','interaction_disabled','service_unavailable','user_requests_disabled'):
            return prefix+access['reason']+'，不代表我没有这个功能。'
        return prefix+'不过当前规则还没允许我按你的请求使用，不能绕过授权。'
    if fact.get('platform_role') == 'member' and fact['name'] in ('member_mute','member_unmute','member_kick','group_mute'):
        return prefix+'不过我当前的 QQ 群身份不是管理员，现在执行不了这项管理操作。'
    if access['target_scope'] == 'self':
        identity = '我在 QQ 群里是群主，' if fact.get('platform_role') == 'owner' else ''
        return prefix+identity+'按这个群的规则，你可以让我操作你自己，不能借这个去操作别人。'
    return prefix+'我可以按群里的授权来用，具体对象还得符合 QQ 的限制，不能乱来。'


def matching_records(records, question):
    exact = [r for r in records if r.get('target_qq') and r['target_qq'] in question]
    matches = exact or [r for r in records if r.get('target_name') and r['target_name'] in question]
    if not matches and re.search(r'(?:禁言|解禁|撤回|点赞|戳|改名)(?:过|了)?(?:人|群友|成员|别人)?(?:[吗呢呀了哦吧？?，。]|$)', question):
        return ([r for r in records if r['status'] == 'success'] or records)[:1]
    return matches if len({r['target_qq'] for r in matches}) <= 1 else []


def history_text(fact, question):
    lookup = fact.get('history', {})
    if lookup.get('status') != 'success':
        return '我现在没能核查那次操作的记录，不能凭印象说没做过。'
    records = (lookup.get('result') or {}).get('operations', [])
    matching = matching_records(records, question)
    if not matching:
        return '我查了保留的操作记录，还没对上你说的那一次，不能断言没发生过。'
    record = matching[0]
    target = record['target_name'] or record['target_qq']
    label = LABELS[fact['name']][0]
    when = record['time'][5:16].replace('T', ' ')
    if record['status'] == 'success':
        duration = record.get('parameters', {}).get('duration')
        detail = f'，时长 {duration} 秒' if duration is not None else ''
        return f'查到了，{when} 我对{target}提交过{label}{detail}，QQ 接口返回成功，这条有执行回执。'
    if record['status'] == 'denied':
        return f'查到了，{when} 对{target}的{label}在提交前被拒绝，那次没有执行。'
    if record['status'] == 'unknown':
        return f'查到了，{when} 对{target}的{label}结果还不确定，不能说成功或没做过，也不能直接重做。'
    return f'查到了，{when} 对{target}的{label}记录是报错，不能把它说成成功。'


class CapabilityFacts:
    def __init__(self, engine, scope, persona, batch, direct_ids):
        self.engine, self.scope, self.persona, self.batch = engine, scope, persona, batch
        self.questions = {m['id']: {**question, 'actor': m['user_id']} for m in batch
            if m['id'] in direct_ids and (question := inquiry(m.get('plain_text', m.get('data', {}).get('plain_text', m['text']))))}
        self.executed, self.facts, self.sources, self.audits = [], {}, set(), {}
        self.signatures = {}

    def access(self, actor, name):
        value = self.engine.tools.permission_status(self.scope, actor, name)
        return {key: value.get(key) for key in ('available', 'target_scope', 'identity_source', 'error_code', 'reason')}

    async def prepare(self, execute, allowance=3, history_allowance=3):
        from .tools import SPECS
        for mid, question in self.questions.items():
            values = []
            platform_role = None
            if question['kind'] == 'capability' and len(self.executed) < allowance and self.access(question['actor'], 'capabilities')['available']:
                self.signatures[(question['actor'], 'capabilities')] = self.access(question['actor'], 'capabilities')
                checked = await execute('capabilities', {'source_message_id': mid})
                self.executed.append({'name': 'capabilities', **checked})
                if checked.get('status') == 'success':
                    platform_role = (checked.get('result') or {}).get('platform', {}).get('bot_role')
            for name in question['tools']:
                fact = {'name': name, 'implemented': True, 'access': self.access(question['actor'], name),
                        'platform_role': platform_role,
                        'target_and_platform_checks': '实际目标、保护名单及 QQ 条件在操作时核验，回答功能问题不执行操作'}
                if fact['access']['target_scope'] == 'self':
                    fact.update(self_target_qq=question['actor'], self_means='仅限当前请求者本人，不是机器人；对群友解释时说“你自己”')
                if question['kind'] == 'history' and SPECS[name].mutation:
                    self.signatures[(question['actor'], 'history_search')] = self.access(question['actor'], 'history_search')
                    if len(self.executed) < allowance and self.history_calls < history_allowance:
                        query = {'operation': name, 'limit': 8, 'source_message_id': mid}
                        today = datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=0, minute=0, second=0, microsecond=0)
                        if '前天' in question['text'] or '昨天' in question['text']:
                            day = today-timedelta(days=2 if '前天' in question['text'] else 1)
                            query.update(start_at=day.isoformat(), end_at=(day+timedelta(days=1)).isoformat())
                        elif '今天' in question['text'] or '早上' in question['text']:
                            query['start_at'] = today.isoformat()
                            if '早上' in question['text']: query['end_at'] = (today+timedelta(hours=12)).isoformat()
                        result = await execute('history_search', query)
                        self.executed.append({'name': 'history_search', **result})
                        if result.get('status') == 'success':
                            for row in result['result'].get('messages', []): self.sources.add(row['id'])
                            for row in result['result'].get('operations', []):
                                saved = self.engine.db.rows('SELECT data FROM audit WHERE id=? AND scope=?', (row['audit_id'], self.scope))
                                if saved: self.audits[row['audit_id']] = saved[0]['data']
                        fact['history'] = result
                    else:
                        fact['history'] = {'status': 'unavailable', 'reason': '本轮历史读取额度已用尽，不能断言没发生过'}
                values.append(fact)
                self.signatures[(question['actor'], name)] = fact['access']
            self.facts[mid] = {'source_message_id': mid, 'kind': question['kind'], 'tools': values}

    @property
    def history_calls(self):
        return sum(x['name'] == 'history_search' for x in self.executed)

    def current(self):
        from .history import History
        if any(self.access(actor, name) != value for (actor, name), value in self.signatures.items()): return False
        try:
            history = History(self.engine.db, self.scope, self.persona)
            for source in self.sources: history.message(source)
        except ValueError: return False
        for aid, saved in self.audits.items():
            row = self.engine.db.rows('SELECT data FROM audit WHERE id=? AND scope=?', (aid, self.scope))
            if not row or row[0]['data'] != saved: return False
        for value in self.facts.values():
            for fact in value['tools']:
                for record in (fact.get('history', {}).get('result') or {}).get('operations', []):
                    if self.engine.db.get('member', self.scope+':'+record['target_qq'], {}).get('remember') is False: return False
        return True

    def before_tool(self, name, args):
        from .tools import SPECS, ToolDenied
        if not self.questions or name not in SPECS or not SPECS[name].mutation: return None
        try: source, _ = self.engine.tools.bind(self.scope, self.batch, args)
        except (ToolDenied, ValueError, TypeError): return None
        if source['id'] in self.questions and name in SPECS and SPECS[name].mutation:
            return {'status': 'denied', 'error_code': 'capability_question_is_not_action',
                    'error': '当前只是在询问能力或核查旧操作，不是新的执行请求；不能真的操作来证明能力。'}
        return None

    def ground(self, reply, target, executed):
        mid = target['id']
        if mid not in self.facts: return reply
        values = self.facts[mid]['tools']; question = self.questions[mid]
        platform = next((x['result'].get('platform', {}) for x in reversed(executed)
            if x.get('name') == 'capabilities' and x.get('status') == 'success' and isinstance(x.get('result'), dict)), {})
        values = [{**fact, 'platform_role': platform.get('bot_role', fact.get('platform_role'))} for fact in values]
        # Quoting/retracting an earlier mistake is not another denial.
        claims = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"', '', reply)
        claims = re.sub(r'(?:不是|并非)[^，,。！？\n]{0,12}(?:没(?:有)?(?:这|那)?(?:个)?(?:本事|能力|功能)|不会)', '', claims)
        denied = re.search(r'没(?:有)?(?:这|那|这种|那个)?(?:个)?(?:本事|能力|功能)|哪会|后台.{0,8}摸不到|摸不到.{0,8}后台|我.{0,8}(?:不会|没法|不能)(?!乱|随便|擅自|滥用).{0,8}(?:禁言|点赞|戳|撤回|搜图)|没(?:有)?[^。！？]{0,12}(?:权限|授权)', claims)
        denial_of_past = re.search(r'没按过|没动过|没动手|从没|从未|没(?:有)?(?:真|实际|真的)(?:做|执行|提交|操作|禁言|撤回|点|戳)|没(?:有)?(?:做|执行|提交|操作|禁言|撤回|点|戳)过|不是(?:我|我)(?:做|干|提交|操作|禁|点|戳)|真没|(?:那句|前面|上次|之前).{0,20}(?:嘴快|瞎说|乱说|编的)', claims)
        receipt_denial = re.search(r'没(?:有|收到|读到|查到)?[^。！？]{0,8}(?:回执|执行记录)', claims)
        fixed = None
        if question['kind'] == 'history':
            # Missing evidence cannot prove either outcome. Non-success outcomes
            # use their exact recorded status, including the no-retry boundary.
            for fact in values:
                if 'history' not in fact: continue
                records = (fact['history'].get('result') or {}).get('operations', [])
                matching = matching_records(records, question['text'])
                if not matching or matching[0]['status'] != 'success' or denial_of_past or denied or receipt_denial:
                    fixed = history_text(fact, question['text']); break
        elif denied:
            fixed = capability_text(values[0])
        elif question['kind'] == 'capability':
            unlimited = re.search(r'(?<!不)(?<!没)能.{0,6}(?:随便|任何人|谁都)|(?:谁都|任何人).{0,4}(?:能|可以)', reply)
            for fact in values:
                wrong_bot_rank = re.search(r'(?:我|我|机器人|群里)[^。！？]{0,20}(?:缺|没|还得等|需要|得先)[^。！？]{0,12}(?:管理身份|管理权限|群主身份)|等群里有[^。！？]{0,8}管理身份', claims)
                if fact.get('platform_role') == 'owner' and wrong_bot_rank:
                    fixed = capability_text(fact); break
                if fact.get('platform_role') == 'member' and fact['name'] in ('member_mute','member_unmute','member_kick','group_mute') and not re.search(r'执行不了|用不了|无法执行|不能执行|没法执行|没有管理权限', reply):
                    fixed = capability_text(fact); break
                if fact['access']['target_scope'] == 'self' and re.search(r'(?:我|我|我)[^。！？]{0,10}(?:只|仅)[^你。！？]{0,10}(?:自己|本人)', reply):
                    fixed = capability_text(fact); break
                positive = re.search(r'(?:我|我|当然).{0,4}(?:能|可以).{0,10}'+LABELS[fact['name']][1], reply)
                qualified = re.search(r'没开|未开|没授权|未授权|不能|不可以|核实|限制|只.{0,4}自己|需要|得先', reply)
                if unlimited or positive and not qualified and (not fact['access']['available'] or fact.get('platform_role') == 'member' and fact['name'] in ('member_mute','member_unmute','member_kick','group_mute')):
                    fixed = capability_text(fact); break
        if fixed:
            tracing.stage(self.engine.db, 'capability_reply_corrected', {'source_id': mid, 'kind': question['kind']}, content=False)
            return fixed
        return reply
