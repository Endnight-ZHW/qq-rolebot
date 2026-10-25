"""Durable, occurrence-based jobs. Unknown sends are never replayed."""
import asyncio
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import tracing
from .db import uid
from .messaging import recipients, send_task
from .onebot import ActionUnknown

ZONE = ZoneInfo('Asia/Shanghai')
READ_TOOLS = frozenset({'calculate', 'web_search', 'web_fetch', 'memory_search', 'history_search', 'history_read', 'image_read', 'group_info', 'member_info', 'member_find', 'sticker_search', 'sticker_list'})


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str = Field(min_length=1, max_length=2000)
    mode: str = Field(default='message', pattern='^(message|model)$')
    delay_seconds: int | None = Field(default=None, ge=5, le=31536000)
    due_at: str | None = Field(default=None, max_length=50)
    daily_time: str | None = Field(default=None, pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    interval_seconds: int | None = Field(default=None, ge=60, le=31536000)
    mentions: list[str] = Field(default_factory=list, max_length=3)
    reason: str = Field(default='', max_length=500)

    @model_validator(mode='after')
    def timing(self):
        if sum(x is not None for x in (self.delay_seconds, self.due_at, self.daily_time)) != 1:
            raise ValueError('请选择相对时间、指定时间、每天时刻中的一种')
        if self.daily_time and self.interval_seconds:
            raise ValueError('每天时刻与固定间隔不能同时指定')
        if any(x < 0 or x > 6 for x in self.weekdays) or (self.weekdays and not self.daily_time):
            raise ValueError('星期仅用于每天时刻，周一为0、周日为6')
        return self


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(min_length=1, max_length=80)
    action: str = Field(pattern='^(edit|pause|resume|cancel)$')
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    delay_seconds: int | None = Field(default=None, ge=5, le=31536000)
    daily_time: str | None = Field(default=None, pattern=r'^([01]\d|2[0-3]):[0-5]\d$')


def daily_after(value, after):
    hour, minute = map(int, value['daily_time'].split(':'))
    local = datetime.fromtimestamp(after, ZONE)
    for day in range(8):
        due = (local+timedelta(days=day)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due.timestamp() > after and (not value.get('weekdays') or due.weekday() in value['weekdays']):
            return due.timestamp()
    raise ValueError('无可执行日期')


def next_due(value, after):
    if value.get('daily_time'):
        return daily_after(value, after)
    interval = value.get('interval_seconds')
    if interval:
        old = value['due']
        return old+max(1, int((after-old)//interval)+1)*interval
    return None


class Scheduler:
    def __init__(self, engine):
        self.engine, self.db = engine, engine.db
        self.active = set()

    def recover(self):
        for task in self.db.all('reminder'):
            if task.get('version') == 2 and task.get('status') in ('running', 'sending'):
                task.update(status='unknown', error='服务重启前执行状态不明，未重试')
                self.db.put('reminder', task['id'], task)
        for run in self.db.all('task_run'):
            if run.get('status') == 'running':
                run.update(status='unknown', error='服务中断，未重试', finished_at=time.time())
                self.db.put('task_run', run['id'], run)

    async def create(self, scope, persona, actor, data, source_id='', principal='qq', creation_tool='task_create'):
        value = TaskCreate.model_validate(data).model_dump()
        if not self.db.get('group', scope, {}).get('interaction_enabled'):
            raise ValueError('本群尚未开启互动扩展')
        epoch = self.engine.memory.epoch(scope)
        value['mentions'] = await recipients(self.engine.onebot, scope, value['mentions'], actor)
        if epoch != self.engine.memory.epoch(scope):
            raise ValueError('资料已遗忘，请重新确认任务')
        rows = self.db.rows('SELECT ts FROM messages WHERE id=? AND scope=? AND excluded=0', (source_id, scope))
        anchor = rows[0]['ts'] if rows else time.time()
        now = time.time()
        if value['daily_time']:
            due = daily_after(value, now)
        elif value['delay_seconds'] is not None:
            due = anchor+value['delay_seconds']
        else:
            dt = datetime.fromisoformat(value['due_at'].replace('Z', '+00:00'))
            due = (dt if dt.tzinfo else dt.replace(tzinfo=ZONE)).timestamp()
            if due <= now: raise ValueError('指定时间已过去，请重新确认执行时间')
        if due < now-600 or due > now+366*86400:
            raise ValueError('执行时间应为未来一年内，已过期请求请重新指定')
        if sum(x['scope'] == scope and x['status'] in ('pending', 'paused', 'running') for x in self.db.all('reminder')) >= 100:
            raise ValueError('本群待执行任务已满')
        value.update(id=uid(), version=2, scope=scope, persona=persona, creator=actor, principal=principal,
                     creation_tool=creation_tool, source_ids=[source_id] if source_id else [], due=due,
                     status='pending', revision=1, created_at=now)
        await self.authorize_fresh(value, check_stored=False)
        self.db.put('reminder', value['id'], value)
        self.db.audit(scope, 'task_created', {'id': value['id'], 'creator': actor, 'mode': value['mode'], 'due': due})
        return value

    def authorize(self, task, check_stored=True):
        g = self.db.get('group', task['scope'], {})
        if not g.get('enabled') or g.get('paused') or not g.get('interaction_enabled') or g.get('persona') != task['persona']:
            raise ValueError('群、角色或互动开关已经变化')
        tool = task.get('creation_tool', 'task_create')
        p = self.engine.tools.policy(task['scope'], tool)
        if not p.get('enabled') or not p.get('user_enabled', True):
            raise ValueError('任务工具已关闭')
        if task.get('principal', 'qq') == 'qq':
            self.engine.permissions.check_tool(task['scope'], task['creator'], tool, {})
        elif task.get('principal') != 'admin' or task['creator'] != 'admin-console':
            raise ValueError('任务主体无效')
        if check_stored:
            current = self.db.get('reminder', task['id'])
            if not current or current['status'] not in ('pending', 'running') or current.get('revision') != task.get('revision'):
                raise ValueError('任务已被修改、暂停或取消')

    async def authorize_fresh(self, task, check_stored=True):
        if task.get('principal', 'qq') == 'qq':
            await self.engine.permissions.refresh(task['scope'], [task['creator']])
        self.authorize(task, check_stored=check_stored)

    def update(self, scope, actor, value, admin=False):
        data = TaskUpdate.model_validate(value).model_dump()
        task = self.db.get('reminder', data['id'])
        if not task or task['scope'] != scope or (not admin and task['creator'] != actor):
            raise ValueError('只能管理当前群自己创建的任务')
        if task['status'] == 'sending':
            raise ValueError('任务正在发送，请先核对发送结果')
        if data['action'] == 'resume':
            if task['status'] != 'paused':
                raise ValueError('只有暂停任务可以恢复；未知结果需核对后新建任务')
            task['status'] = 'pending'
            if task['due'] < time.time():
                task['due'] = next_due(task, time.time()) or time.time()+5
        elif data['action'] in ('pause', 'cancel'):
            if data['action'] == 'pause' and task['status'] not in ('pending', 'running'):
                raise ValueError('仅可暂停尚未执行的任务')
            task['status'] = 'paused' if data['action'] == 'pause' else 'cancelled'
        else:
            if task['status'] not in ('pending', 'paused'):
                raise ValueError('仅可修改待执行或暂停任务')
            if data['text'] is not None: task['text'] = data['text']
            if data['delay_seconds'] is not None:
                task.update(due=time.time()+data['delay_seconds'], daily_time=None, weekdays=[])
            if data['daily_time'] is not None:
                task.update(daily_time=data['daily_time'], interval_seconds=None)
                task['due'] = daily_after(task, time.time())
        task['revision'] = task.get('revision', 0)+1
        self.db.put('reminder', task['id'], task)
        self.db.audit(scope, 'task_updated', {'id': task['id'], 'actor': actor, 'action': data['action']})
        return task

    async def tick(self):
        due = sorted((x for x in self.db.all('reminder') if x.get('version') == 2 and x['status'] == 'pending' and x['due'] <= time.time()), key=lambda x: (x['due'], x.get('created_at', 0)))
        for task in due:
            if task['scope'] in self.active:
                continue
            group = self.db.get('group', task['scope'], {})
            if not self.engine.onebot.online or not group.get('enabled') or group.get('paused') or not group.get('interaction_enabled'):
                continue
            self.active.add(task['scope'])
            self.engine.spawn(self.execute(task))

    async def generate(self, task, run_id):
        from .image_turn import ImageTurn, active_images
        images = ImageTurn(self.engine, task['scope'], task['persona'])
        token = active_images.set(images)
        try:
            return await self._generate(task, run_id, images)
        finally:
            active_images.reset(token)

    async def _generate(self, task, run_id, images):
        from .tool_loop import ToolLoop
        await self.authorize_fresh(task)
        messages, preview = self.engine.memory.build(task['scope'], task['persona'], task['text'], stage='task',
            runtime={'task': task['text'], 'creator': task['creator'], 'time': datetime.now(ZONE).isoformat()})
        tracing.stage(self.db, 'context_task', preview)
        tools = self.engine.tools
        schemas = [s for s in tools.schemas(task['scope']) if s['function']['name'] in READ_TOOLS]
        if task['principal'] == 'qq':
            allowed = tools.permissions.resolve(task['scope'], task['creator'])['allow']
            schemas = [s for s in schemas if 'tool:'+s['function']['name'] in allowed]
        def current():
            self.authorize(task)
            return images.current()
        async def execute(name, args):
            await self.authorize_fresh(task)
            if name not in READ_TOOLS:
                return {'status': 'denied', 'error': 'Scheduled tasks permit read tools only'}
            try:
                return await tools.run(task['scope'], task['persona'], task['creator'], run_id, name, args,
                    mode='scheduled_admin' if task['principal'] == 'admin' else 'request')
            except (ValueError, TypeError):
                return {'status': 'denied', 'error': 'Invalid tool arguments'}
        response, _ = await ToolLoop(self.engine.model, self.db).run(task['scope'], 'chat', messages, schemas, execute, current, stage='task.execute', history_limit=3, input_budget=preview['budget'],
            after_tools=lambda: images.flush(messages, preview['budget'], schemas))
        if response is None:
            raise ValueError('Task did not produce a final response')
        return (response.get('content') or '').strip()[:2000]


    async def execute(self, task):
        run_id = task['id']+':'+str(int(task['due']*1000))
        if self.db.get('task_run', run_id):
            task.update(status='unknown', error='该次执行已有记录，未重复发送')
            self.db.put('reminder', task['id'], task)
            self.active.discard(task['scope'])
            return
        trace_id = token = None
        run = {'id': run_id, 'task_id': task['id'], 'scope': task['scope'], 'creator': task['creator'], 'principal': task['principal'], 'due': task['due'], 'status': 'running', 'started_at': time.time()}
        try:
            await self.authorize_fresh(task)
            self.db.put('task_run', run_id, run)
            if time.time()-task['due'] > 600:
                run['status'] = 'missed'
            else:
                task['status'] = 'running'; self.db.put('reminder', task['id'], task)
                private = not self.db.get('member', task['scope']+':'+task['creator'], {}).get('remember', True)
                trace_id, token = tracing.begin(self.db, task['scope'], task['persona'], [{'id': x, 'user_id': task['creator'], 'remember': not private} for x in task.get('source_ids', [])], kind='scheduled_task')
                run['trace_id'] = trace_id
                tracing.stage(self.db, 'task_start', {'task_id': task['id'], 'text': task['text'], 'due': task['due']})
                text = task['text'] if task['mode'] == 'message' else await asyncio.wait_for(self.generate(task, run_id), 180)
                if not text: raise ValueError('任务未生成正文')
                await self.authorize_fresh(task)
                result = await send_task(self.engine, task, text, lambda: self.authorize(task))
                run.update(status='sent', message_id=str(result['message_id']))
                tracing.stage(self.db, 'task_sent', {'message_id': run['message_id'], 'text': text, 'mentions': task.get('mentions', [])})
                private = private or not self.db.get('reminder', task['id'])
                self.db.add_message(task['scope']+':'+run['message_id'], task['scope'], task['persona'], self.engine.onebot.self_id, 'assistant', '' if private else text,
                                    {'message_id': run['message_id'], 'task_id': task['id'], 'source_ids': task.get('source_ids', []), 'subject_ids': [task['creator']], 'mentions': task.get('mentions', [])}, excluded=private)
        except asyncio.CancelledError:
            run.update(status='unknown', error='服务中断，未重试')
            raise
        except ActionUnknown:
            run.update(status='unknown', error='发送结果未知，未重试')
        except Exception as exc:
            run.update(status='failed', error=str(exc)[:200])
        finally:
            run['finished_at'] = time.time()
            if run['status'] == 'running': run['status'] = 'unknown'
            current = self.db.get('reminder', task['id'])
            if current: self.db.put('task_run', run_id, run)
            if current and current.get('revision') == task.get('revision'):
                due = next_due(task, time.time()) if run['status'] in ('sent', 'missed') else None
                current.update(status='pending' if due else run['status'], last_run_id=run_id)
                if due: current['due'] = due
                self.db.put('reminder', task['id'], current)
            self.db.audit(task['scope'], 'task_run', {k: v for k, v in run.items() if k not in ('text', 'error')})
            if token: tracing.finish(self.db, trace_id, run['status'], token)
            self.active.discard(task['scope'])
