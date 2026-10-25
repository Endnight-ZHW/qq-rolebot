"""Configuration metadata, validation boundaries and compatible runtime defaults."""
import math
from pydantic import Field

SAFE_INTEGER = 2147483647
FIELDS = {}


def define(scope, key, label, default, minimum, recommended, *, maximum=None, unit='', kind='integer', reason='', depends=None):
    FIELDS[f'{scope}.{key}'] = dict(key=key, scope=scope, label=label, default=default,
        type=kind, minimum=minimum, maximum=maximum if maximum is not None else SAFE_INTEGER,
        recommended_max= recommended, unit=unit, hard_reason=reason or '数值需在可安全计算的范围内',
        dependencies=depends or [])


for key, label, default, minimum, recommended, unit in [
    ('context_tokens','上下文总预算',256000,4096,1000000,'token'),
    ('gate_max_messages','轻量判定消息条数',80,1,500,'条'),
    ('gate_context_budget','轻量判定软预算',32000,4096,1000000,'token'),
    ('working_initial_messages','工作上下文初始消息条数',40,1,500,'条'),
    ('working_max_messages','工作上下文消息条数上限',80,1,500,'条'),
    ('working_context_budget','工作上下文软预算',32000,4096,1000000,'token'),
    ('learning_idle_seconds','停止参与后等待时间',600,60,86400,'秒'),
    ('learning_max_seconds','持续参与整理间隔',7200,600,86400,'秒'),
    ('image_input_budget','视觉输入预算',49152,4096,49152,'token'),
    ('retention_days','聊天数据保留',30,1,3650,'天'),
    ('media_hours','聊天媒体保留',24,1,168,'小时'),
    ('model_log_days','模型调用记录保留',7,1,3650,'天'),
    ('model_log_image_hours','模型调用图片保留',24,1,168,'小时'),
]: define('global',key,label,default,minimum,recommended,unit=unit)
define('global','image_max_count','单次请求图片上限',12,1,12,maximum=128,unit='张',reason='单次图像处理资源上限为 128 张')
define('global','image_request_mib','模型请求体上限',16,2,32,maximum=128,unit='MiB',reason='单次传输资源上限为 128 MiB')

for key,label,default,minimum,recommended,unit in [
    ('cooldown','主动发言最短间隔',60,10,86400,'秒'),
    ('proactive_hourly','每小时主动发言上限',10,0,60,'次'),
    ('followup_seconds','连续跟聊窗口',120,30,300,'秒'),
    ('segment_threshold','开始分段的字数',80,40,500,'字'),
    ('segment_max_parts','每轮最多几条',3,2,3,'条'),
    ('send_per_minute','每分钟发送消息上限',20,1,20,'条'),
    ('send_wait_seconds','发送限频最长等待',65,0,65,'秒'),
    ('reply_timeout_seconds','普通回复有效时限',120,1,120,'秒'),
    ('forward_reply_timeout_seconds','合并转发多图回复有效时限',300,1,300,'秒'),
]: define('group',key,label,default,minimum,recommended,unit=unit)
define('group','listening_probability','旁听判定概率',1,0,100,maximum=100,unit='%',kind='number',reason='概率范围为 0–100%')
define('group','segment_interval_min','分段最短间隔',1,.5,5,unit='秒',kind='number')
define('group','segment_interval_max','分段最长间隔',2.5,.5,5,unit='秒',kind='number',depends=['segment_interval_min'])
define('group','tool_max_rounds','单轮模型交互轮数',4,2,4,maximum=32,unit='轮',reason='执行资源上限为 32 轮，最后一轮收束回答')
define('group','tool_max_calls','单轮工具调用次数',6,1,6,maximum=256,unit='次',reason='单轮执行资源上限为 256 次')
for key,label,default,minimum,recommended,unit in [
    ('max_per_hour','每小时最多调用',10,1,300,'次'),
    ('like_max_times','单次点赞上限',10,1,10,'赞'),
    ('like_daily_limit','每日累计点赞上限',10,1,10,'赞'),
    ('poke_cooldown_seconds','同一目标戳一戳冷却',60,10,86400,'秒'),
]: define('tool',key,label,default,minimum,recommended,unit=unit)
define('tool','max_duration','单次禁言最多',600,1,2592000,maximum=2592000,unit='秒',reason='当前群管理接口兼容上限为 30 天')
define('library','sticker_limit','共享收藏上限',100,1,1000,unit='张')
define('library','sticker_max_mib','共享单张大小',5,1,8,maximum=8,unit='MiB',reason='媒体解码与上传资源边界为单张 8 MiB')
define('model','context_window','上下文容量',1000000,4096,2000000,unit='token')
define('model','max_tokens','最大输出容量',256000,128,256000,unit='token',depends=['context_window'])
define('route','max_tokens','输出上限',65536,128,256000,unit='token',depends=['model.max_tokens'])
FIELDS['tool.max_per_hour']['read_default'] = 60
FIELDS['global.working_initial_messages']['dependencies'] = ['working_max_messages']
FIELDS['global.learning_idle_seconds']['dependencies'] = ['learning_max_seconds']


def number_field(name, *, required=False, nullable=False):
    spec = FIELDS[name]
    args = dict(ge=spec['minimum'], le=spec['maximum'], strict=True)
    if spec['type'] == 'number': args['allow_inf_nan'] = False
    return Field(default=... if required else None if nullable else spec['default'], **args)


def validate_values(scope, values):
    for name, value in values.items():
        spec = FIELDS.get(f'{scope}.{name}')
        if not spec: continue
        valid_type = type(value) is int if spec['type'] == 'integer' else type(value) in (int, float)
        if not valid_type or not spec['minimum'] <= value <= spec['maximum'] or not math.isfinite(value):
            raise ValueError(f"{spec['label']} 应为 {spec['minimum']} 至 {spec['maximum']} 范围内的{'整数' if spec['type'] == 'integer' else '数值'}")


GLOBAL_NUMBERS = {v['key']:v['default'] for v in FIELDS.values() if v['scope']=='global'}
GROUP_NUMBERS = {v['key']:v['default'] for v in FIELDS.values() if v['scope']=='group'}

RUNTIME_KEYS = ('send_per_minute','send_wait_seconds','reply_timeout_seconds','forward_reply_timeout_seconds','tool_max_rounds','tool_max_calls')
RUNTIME_DEFAULTS = {k:FIELDS['group.'+k]['default'] for k in RUNTIME_KEYS}


def runtime_config(db, scope):
    group = db.get('group', scope, {})
    return {k:group.get(k, value) for k,value in RUNTIME_DEFAULTS.items()}


def save_global(db, patch):
    """The two legacy settings endpoints share retention updates and one transaction."""
    validate_values('global', patch)
    with db.transaction():
        old = db.config()
        value = {**old, **patch, 'revision':old.get('revision',0)+1}
        if value['working_initial_messages'] > value['working_max_messages']:
            raise ValueError('初始消息条数不能超过工作上下文条数上限')
        if value['learning_idle_seconds'] > value['learning_max_seconds']:
            raise ValueError('停止参与等待时间不能超过持续参与整理间隔')
        db.put('settings','global',value)
        if 'model_log_days' in patch:
            db.execute('UPDATE model_calls SET expires=ts+?', (value['model_log_days']*86400,))
        if 'model_log_image_hours' in patch:
            db.execute('UPDATE call_images SET expires=MIN(expires,(SELECT ts FROM model_calls WHERE id=call_images.call_id)+?)', (value['model_log_image_hours']*3600,))
        db.audit('', 'settings', {'changed':list(patch)})
    return value
