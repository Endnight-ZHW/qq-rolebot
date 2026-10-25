"""Request-local identity evidence for recalling a message; never import fetched chat text."""
from contextvars import ContextVar
from dataclasses import dataclass
import math
import re
import time

from . import tracing


@dataclass(frozen=True)
class RecallTarget:
    scope: str
    persona: str
    message_id: str
    author: str
    epoch: int
    source_id: str = ''
    source_actor: str = ''
    local_id: str = ''


active_recall = ContextVar('active_recall_target', default=None)


def canonical_id(scope, value):
    from .tools import ToolDenied
    value = str(value)
    if ':' in value:
        if not value.startswith(scope+':'):
            raise ToolDenied('目标消息不属于当前会话', code='recall_target_mismatch')
        value = value[len(scope)+1:]
    if not re.fullmatch(r'-?\d{1,20}', value):
        raise ToolDenied('目标消息编号无效，请引用要撤回的原消息', code='recall_target_unavailable')
    return str(int(value))


def local_target(db, scope, persona, message_id):
    from .tools import ToolDenied
    rows = db.rows("SELECT id,persona,user_id,excluded,ts FROM messages WHERE scope=? AND "
                   "(id=? OR CAST(json_extract(data,'$.message_id') AS TEXT)=?)", (scope,scope+':'+message_id,message_id))
    if not rows: return None
    if len(rows) != 1:
        raise ToolDenied('无法唯一确定目标消息及其作者', code='recall_target_mismatch')
    row = rows[0]
    if row['persona'] != persona or row['excluded'] or row['ts'] < time.time()-db.config()['retention_days']*86400:
        raise ToolDenied('目标消息已遗忘、过期或不属于当前角色会话，不能重新取回', code='recall_target_unavailable')
    if not row['user_id'] or not db.get('member',scope+':'+row['user_id'],{}).get('remember',True):
        raise ToolDenied('目标消息的作者不可核验', code='recall_target_unavailable')
    return row


def evidence(db, scope, message_id):
    from .tools import ToolDenied
    value = active_recall.get()
    if value is None or (value.scope,value.message_id) != (scope,canonical_id(scope,message_id)):
        return None
    if value.epoch != db.get('memory_epoch',scope,{'value':0})['value'] or db.get('group',scope,{}).get('persona') != value.persona:
        raise ToolDenied('核验期间会话或记忆状态已变化', code='context_changed')
    if value.local_id:
        row = local_target(db,scope,value.persona,value.message_id)
        if row is None or row['id'] != value.local_id or row['user_id'] != value.author:
            raise ToolDenied('目标消息的来源已变化', code='context_changed')
    return value


async def prepare(tools, scope, persona, source, args, batch=()):
    """Resolve only saved observations, current events, or a real trigger's quoted message."""
    from .tools import ToolDenied
    mid = canonical_id(scope,args['message_id'])
    epoch = tools.db.get('memory_epoch',scope,{'value':0})['value']
    source = source or {}
    if source and (source.get('scope') != scope or source.get('persona') != persona):
        raise ToolDenied('触发消息与当前会话不一致', code='source_mismatch')
    existing = evidence(tools.db,scope,mid)
    if existing and existing.source_id == source.get('id',''):
        return {**args,'message_id':mid}
    row = local_target(tools.db,scope,persona,mid)
    local_id = ''
    if row:
        author,local_id = row['user_id'],row['id']
    else:
        observed = [m for m in batch if m.get('scope')==scope and m.get('persona')==persona and str(m.get('message_id'))==mid]
        if len(observed)==1:
            author = observed[0]['user_id']
        else:
            references = source.get('data',{}).get('replies') or source.get('replies',[])
            refs = {canonical_id(scope,r) for r in references}
            if mid not in refs:
                raise ToolDenied('没有观察到目标消息，也没有本次真实引用可供核验，请引用原消息', code='recall_target_unavailable')
            # A missing observation is not a reason to fetch data for an unauthorized requester.
            from .permissions import recall_grants
            effective = tools.permissions.resolve(scope, source['user_id'])
            access = effective['tool_access']['message_recall']
            if access['mode'] == 'profile' and not access['available']:
                raise ToolDenied(access['reason'], code=access['error_code'])
            if not any(recall_grants(effective)):
                raise ToolDenied('该 QQ 在当前会话没有撤回工具权限',code='user_permission_denied')
            token = tracing.active_trace.set(None)
            try:
                # A mismatched response must not be copied into this group's trace.
                data = await tools.onebot.call('get_msg',{'message_id':mid})
            except Exception as exc:
                raise ToolDenied('QQ 未能返回目标消息的身份信息，尚未执行撤回',code='recall_target_unavailable') from exc
            finally:
                tracing.active_trace.reset(token)
            if not isinstance(data,dict):
                raise ToolDenied('QQ 未返回可核验的消息身份信息',code='recall_target_unavailable')
            sender = data.get('sender') if isinstance(data.get('sender'),dict) else {}
            author = str(data.get('user_id',sender.get('user_id','')))
            stamp = data.get('time')
            if (str(data.get('message_id')) != mid or str(data.get('group_id')) != scope.split(':')[2]
                or data.get('message_type','group') != 'group' or str(data.get('self_id',tools.onebot.self_id)) != tools.onebot.self_id
                or not re.fullmatch(r'\d{5,20}',author) or str(sender.get('user_id',author)) != author):
                raise ToolDenied('QQ 返回的目标消息与当前群或作者身份不匹配，未执行撤回',code='recall_target_mismatch')
            if type(stamp) not in (int,float) or not math.isfinite(stamp) or not time.time()-tools.db.config()['retention_days']*86400 <= stamp <= time.time()+300:
                raise ToolDenied('目标消息时间无法核验或已超过保留范围，未执行撤回',code='recall_target_unavailable')
            if epoch != tools.db.get('memory_epoch',scope,{'value':0})['value']:
                raise ToolDenied('核验期间记忆状态已变化',code='context_changed')
            # Check again after IO so deletion/forgetting cannot be undone by recovery.
            concurrent = local_target(tools.db,scope,persona,mid)
            if concurrent:
                if str(concurrent['user_id']) != author:
                    raise ToolDenied('核验期间目标消息的作者信息发生冲突',code='recall_target_mismatch')
                local_id = concurrent['id']
            tracing.stage(tools.db,'recall_target_verified',{'message_id':mid,'author':author,'via':'qq_quote_lookup'},content=False)
    active_recall.set(RecallTarget(scope,persona,mid,str(author),epoch,source.get('id',''),source.get('user_id',''),local_id))
    return {**args,'message_id':mid}
