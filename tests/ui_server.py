"""Ephemeral backend for the browser smoke test; never starts QQ/model workers."""
import io
import json
import os
import tempfile
import time

os.environ.update(ADMIN_PASSWORD='test-password-123456', COOKIE_SECURE='false', PUBLIC_BASE_URL='http://127.0.0.1:5173')

from PIL import Image
from rolebot.api import create_app
from rolebot.call_records import pack
from rolebot.engine import group_defaults
from support import SCOPE, OTHER, USER


def seed_console(app):
    db=app.state.db;now=time.time()
    second=group_defaults(OTHER,'游戏与灵感 · 测试');second.update(enabled=True,interaction_enabled=True)
    db.put('group',OTHER,second)
    private=group_defaults('10000001:p:30000003','小林 · 私聊测试');private.update(enabled=False)
    db.put('group',private['id'],private)
    for i in range(144):
        scope=SCOPE if i%3 else OTHER;ts=now-3*86400+i*1800
        status='http_error' if i%19==0 else 'success';id='ui-call-'+str(i)
        trace_id='ui-trace-'+str(i);duration=600+(i%13)*120
        purpose=['chat','decision','memory'][i%3]
        meta={'id':id,'scope':scope,'ts':ts,'status':status,'duration_ms':duration,'purpose':purpose,'model':'DeepSeek · 测试模型',
              'resource':'chat','provider_id':'deepseek','protocol':'openai-completions','attempt':1,'trace_id':trace_id,'source_ids':[],
              'redacted':False,'images':[],'redactions':[],'expires':now+86400*7}
        request={'model':'test-model','messages':[{'role':'system','content':'你是机器人，以下是隔离 UI 测试数据。'},{'role':'user','content':'今晚一起聊聊最近读过的书吧。'}],'max_tokens':512}
        payload={'request':request,'response':{'choices':[{'message':{'role':'assistant','content':'好呀，最近在读哪一本？','reasoning_content':'仅用于界面验证的接口返回字段。'}}]},'error':{'notice':'模拟接口错误'} if status!='success' else None,'prompts':[]}
        db.execute('INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?)',(id,trace_id,scope,ts,now+86400*7,status,json.dumps(meta),pack(payload)))
        trace={'id':trace_id,'scope':scope,'persona':'default','ts':ts,'kind':'chat','status':'error' if status!='success' else 'sent','duration_ms':duration+400,
               'source_ids':[],'actor_ids':[],'redacted':False,'models':[{'purpose':purpose,'model':'DeepSeek · 测试模型','input_tokens':1200,'cached_tokens':300,'output_tokens':160,'duration_ms':duration,'cost':.0028,'currency':'CNY'}],
               'stages':[{'name':'context','ts':ts,'data':{'estimated_tokens':1200,'budget':16000,'sections':[{'name':'角色与约束','estimated_tokens':400},{'name':'近期消息','estimated_tokens':800}]}},{'name':'sent','ts':ts+1,'data':{'message_id':'synthetic-'+str(i)}}]}
        db.put('trace',trace_id,trace)
        db.execute('INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?,?)',('ui-usage-'+str(i),scope,'test-model',purpose,1200+i*10,300,160+i,.0028+i*.0001,'CNY' if i%5 else 'USD',ts))
    for i,(scope,title,status,seconds) in enumerate([(SCOPE,'晚间喝水提醒','pending',1800),(SCOPE,'整理今天的游戏更新','pending',3600),(OTHER,'每晚阅读摘要','paused',7200),(SCOPE,'昨天的定时摘要','failed',-3600)]):
        item={'id':'ui-task-'+str(i),'scope':scope,'text':title,'status':status,'due':now+seconds,'mode':'message','creator':'admin-console','version':2,'revision':1,'mentions':[],
              'daily_time':None,'interval_seconds':None,'weekdays':[],'created_at':now-86400,'persona':'default','epoch':0,'principal':'admin'}
        db.put('reminder',item['id'],item)
    db.put('task_run','ui-run-failed',{'id':'ui-run-failed','task_id':'ui-task-3','scope':SCOPE,'status':'failed','started_at':now-3600,'finished_at':now-3500,'error':'隔离测试：模拟网络连接失败','trace_id':'ui-trace-114'})
    db.put('member',SCOPE+':30000002',{'id':SCOPE+':30000002','scope':SCOPE,'user_id':'30000002','name':'小林','remember':True})
    app.state.engine.memory.knowledge.manual(SCOPE,'','测试成员喜欢草莓蛋糕。',[USER],False)
    app.state.engine.memory.knowledge.manual(SCOPE,'default','小林提到周末喜欢阅读科幻小说。',['30000002'],False)
    db.put('context_preview',SCOPE+':default',{'ts':now-60,'persona':'default','stage':'reply','estimated_tokens':3420,'budget':16000,'memory_ids':[],'sections':[{'name':'固定规则','estimated_tokens':820},{'name':'稳定历史','estimated_tokens':1800},{'name':'本轮资料','estimated_tokens':800}], 'context':{'消息':[{'speaker':'测试成员','qq':USER,'role':'user','text':'今晚一起聊聊最近读过的书吧。'}]}})
    db.audit(SCOPE,'group',{'enabled':True,'persona':'default','paused':False})


folder = tempfile.TemporaryDirectory(prefix='rolebot-ui-')
app = create_app(folder.name, start_engine=False)
# Synthetic group identities for permission previews; never connect to QQ.
_call = app.state.engine.onebot.call
async def local_identity(action, params=None, **kwargs):
    if action == 'get_group_member_info':
        return {'user_id': params['user_id'], 'group_id': params['group_id'], 'role': 'member'}
    return await _call(action, params, **kwargs)
app.state.engine.onebot.call = local_identity
# A synthetic key keeps settings round-trips initialized. Workers never start here.
app.state.settings.set_key('local-ui-no-network')
db = app.state.db
config = db.config(); config.update(initialized=True, admin_qq=['90000001']); db.put('settings', 'global', config)
group = group_defaults(SCOPE, '自动化测试群')
group.update(enabled=True, interaction_enabled=True, sticker_autocollect=True)
db.put('group', SCOPE, group)
db.put('member', SCOPE+':'+USER, {'id':SCOPE+':'+USER, 'scope':SCOPE, 'user_id':USER, 'name':'测试成员', 'remember':True})
raw=io.BytesIO()
Image.new('RGB',(60,60),'#b7dac6').save(raw,'GIF',save_all=True,append_images=[Image.new('RGB',(60,60),'#e6d6a0')],duration=400,loop=0)
app.state.engine.stickers.register(SCOPE,raw.getvalue(),{'name':'测试动画','tags':'开心','usage':'开心回应'})
seed_console(app)
for i,status in enumerate(('sent','queued','unknown')):
    from rolebot.creation_contract import MIB
    import hashlib
    id=('c'+str(i))*16;folder_path=app.state.engine.creations.folder(id);folder_path.mkdir()
    preview=io.BytesIO();Image.new('RGB',(240,160),'#c9e4ef').save(preview,'PNG');content=preview.getvalue()
    (folder_path/'page-0.png').write_bytes(content)
    source_code='<script>window.creationSourceExecuted=true</script>'
    (folder_path/'inputs.json').write_text(json.dumps({'brief':'隔离制作示例','data':{},'spec':{'code':source_code}}),encoding='utf8')
    db.put('creation_job',id,{'id':id,'scope':SCOPE,'persona':'default','creator':USER,'title':'中文对比图 · '+status,
        'status':status,'format':'png','created_at':time.time(),'expires':time.time()+604800,'private':False,'favorite':False,
        'source_ids':[],'actor_ids':[USER],'asset_ids':[],'model_calls':2,'cost':.03,'currency':'CNY','sent_ids':[],
        'artifacts':[{'id':'art_'+id,'name':'page-0.png','primary':True,'mime':'image/png','width':240,'height':160,'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()}],
        'metrics':{'duration_seconds':2,'peak_memory_bytes':200*MIB}})
app.state.engine.role_images.save('default', raw.getvalue(), '后台合成测试图')

if __name__=='__main__':
    import uvicorn
    try: uvicorn.run(app, host='127.0.0.1', port=18080, access_log=False)
    finally: folder.cleanup()
