from datetime import datetime
import asyncio
import io
import json
import time
import zipfile

from PIL import Image
import pytest

from rolebot import delivery
from rolebot.messaging import available_sticker, find_members, recipients, sticker_path
from rolebot.scheduler import TaskCreate, ZONE, next_due

from support import OTHER, SCOPE, USER, auth, message


@pytest.fixture
def live(runtime):
    runtime.group.update(interaction_enabled=True)
    runtime.db.put('group', SCOPE, runtime.group)
    return runtime

def image_bytes(animated=False):
    out = io.BytesIO()
    a = Image.new('RGB', (20, 20), 'red')
    if animated: a.save(out, 'GIF', save_all=True, append_images=[Image.new('RGB', (20,20),'blue')], duration=100, loop=0)
    else: a.save(out, 'PNG')
    return out.getvalue()

async def test_real_mentions_resolve_self_and_reject_invalid(live):
    assert await recipients(live.bot, SCOPE, ['self', USER], USER) == [USER]
    for invalid in ['all', '测试成员乙', '123']:
        with pytest.raises(ValueError): await recipients(live.bot, SCOPE, [invalid], USER)
    with pytest.raises(ValueError): await recipients(live.bot, '10000001:p:'+USER, [USER], USER)

async def test_member_lookup_does_not_guess_duplicate_names(live):
    async def call(*args, **kwargs):
        return [{'user_id':'123456','card':'朋友'}, {'user_id':'234567','nickname':'朋友'}, {'user_id':'345678','card':'朋友的朋友'}]
    live.bot.call=call
    result=await find_members(live.bot,SCOPE,'朋友')
    assert result['ambiguous'] and result['total']==2

async def test_prepared_reply_sends_at_once_and_uses_existing_delivery(live):
    m=message(live)
    result=await live.tools.run(SCOPE,'default',USER,m['id'],'message_send',{'text':'来一起聊聊呀','mentions':['self']})
    assert result['status']=='success' and result['result']['sent'] is False and not live.bot.sent
    plan=result['result']['delivery']
    assert plan['actor']==USER and plan['mentions']==[USER]
    status=await delivery.deliver(live.engine,SCOPE,'default',m,[m],{},plan['text'],{},[],{'delivery_plan':plan},lambda:True,False)
    assert status=='sent' and len(live.bot.sent)==1
    assert live.bot.sent[0][1][0]=={'type':'at','data':{'qq':USER}}

async def test_revoke_after_preparation_prevents_send(live):
    m=message(live)
    result=await live.tools.run(SCOPE,'default',USER,m['id'],'message_send',{'text':'hello','mentions':['self']})
    live.group['tools']['message_send']['enabled']=False;live.db.put('group',SCOPE,live.group)
    with pytest.raises(Exception):
        await delivery.deliver(live.engine,SCOPE,'default',m,[m],{},'hello',{},[],{'delivery_plan':result['result']['delivery']},lambda:True,False)
    assert not live.bot.sent

async def test_source_identity_cannot_be_selected_in_args(live):
    result=await live.tools.run(SCOPE,'default',USER,'msg','task_create',{'text':'hi','delay_seconds':60,'creator':'90000001'})
    assert result['status']=='denied' and not live.db.all('reminder')

async def test_relative_reminder_bound_to_actual_creator(live):
    m=message(live)
    result=await live.tools.run(SCOPE,'default',USER,m['id'],'reminder_create',{'text':'喝水','delay_seconds':60,'mention_self':True,'reason':'本人要求'})
    assert result['status']=='success',result
    task=result['result']
    assert task['creator']==USER and task['mentions']==[USER]
    assert abs(task['due']-live.db.message(SCOPE,m['id'],'default')['ts']-60)<.1
    task['due']=time.time()-1;live.db.put('reminder',task['id'],task)
    await live.engine.scheduler.execute(task)
    assert live.db.get('reminder',task['id'])['status']=='sent'
    assert live.bot.sent[0][1][0]['type']=='at'
    await live.engine.scheduler.tick();await asyncio.sleep(0)
    assert len(live.bot.sent)==1

async def test_unknown_send_does_not_repeat_or_resume(live):
    task=await live.engine.scheduler.create(SCOPE,'default',USER,{'text':'hi','delay_seconds':5})
    live.bot.unknown=True
    await live.engine.scheduler.execute(task)
    assert live.db.get('reminder',task['id'])['status']=='unknown'
    with pytest.raises(ValueError): live.engine.scheduler.update(SCOPE,USER,{'id':task['id'],'action':'pause'})
    await live.engine.scheduler.tick();await asyncio.sleep(0)
    assert len(live.bot.sent)==1

async def test_task_permissions_rechecked_and_cross_creator_denied(live):
    task=await live.engine.scheduler.create(SCOPE,'default',USER,{'text':'hello','delay_seconds':5})
    with pytest.raises(ValueError): live.engine.scheduler.update(SCOPE,'77777777',{'id':task['id'],'action':'cancel'})
    live.group['tools']['task_create']['enabled']=False;live.db.put('group',SCOPE,live.group)
    await live.engine.scheduler.execute(task)
    assert not live.bot.sent and live.db.get('reminder',task['id'])['status']=='failed'

def test_repeat_time_is_shanghai_and_skips_missed_intervals():
    t=datetime(2026,10,19,20,1,tzinfo=ZONE).timestamp()
    assert datetime.fromtimestamp(next_due({'daily_time':'20:00'},t),ZONE).isoformat()=='2026-10-20T20:00:00+08:00'
    assert next_due({'due':100,'interval_seconds':60},1000)==1060
    with pytest.raises(ValueError): TaskCreate(text='x',delay_seconds=60,due_at='2026-10-20')

async def test_scheduled_model_cannot_manage_or_create_more_tasks(live):
    task=await live.engine.scheduler.create(SCOPE,'default',USER,{'mode':'model','text':'查询','delay_seconds':5})
    live.model.script=[{'role':'assistant','content':None,'tool_calls':[
        {'id':'a','type':'function','function':{'name':'task_create','arguments':'{"text":"loop","delay_seconds":5}'}},
        {'id':'b','type':'function','function':{'name':'member_mute','arguments':'{"user_id":"30000001","duration":60,"reason":"test"}'}}]},
        {'role':'assistant','content':'这类定时操作不受支持'}]
    await live.engine.scheduler.execute(task)
    assert len(live.db.all('reminder'))==1 and not any(c[0]=='set_group_ban' for c in live.bot.calls)
    assert len(live.bot.sent)==1
    for call in live.model.calls:
        assert all(x['function']['name'] not in ('task_create','member_mute','message_send') for x in call[3].get('tools') or [])

async def test_console_query_task_uses_admin_principal(live):
    task=await live.engine.scheduler.create(SCOPE,'default','admin-console',{'mode':'model','text':'计算','delay_seconds':5},principal='admin')
    live.model.script=[{'role':'assistant','content':None,'tool_calls':[{'id':'c','type':'function','function':{'name':'calculate','arguments':'{"query":"2+3"}'}}]}, {'role':'assistant','content':'5'}]
    await live.engine.scheduler.execute(task)
    results=[json.loads(m['content']) for c in live.model.calls for m in c[2] if m['role']=='tool']
    assert any(r['status']=='success' and r['result']==5 for r in results)
    assert live.db.get('reminder',task['id'])['status']=='sent'

async def test_title_needs_qq_owner_and_verified_readback(live):
    live.group['tools']['member_title']['enabled']=True;live.db.put('group',SCOPE,live.group)
    result=await live.tools.run(SCOPE,'default','90000001','one','member_title',{'user_id':USER,'title':'测试','reason':'测试'})
    assert result['error_code']=='platform_owner_required'
    assert not any(c[0]=='set_group_special_title' for c in live.bot.calls)
    async def call(action,params,**kwargs):
        if action=='get_group_member_info':return {'user_id':params['user_id'],'group_id':SCOPE.split(':')[2],'role':'owner','title':'测试'}
        return {}
    live.bot.call=call
    result=await live.tools.run(SCOPE,'default','90000001','two','member_title',{'user_id':USER,'title':'测试','reason':'测试'})
    assert result['status']=='success' and result['result']=={'verified':True,'field':'title','value':'测试'}

async def test_title_eventual_readback_never_repeats_mutation(live,monkeypatch):
    from rolebot import group_actions
    live.group['tools']['member_title']['enabled']=True;live.db.put('group',SCOPE,live.group)
    mutations=[];reads=[]
    async def call(action,params,**kw):
        if action=='set_group_special_title':mutations.append(action);return None
        if action=='get_group_member_info':
            reads.append(action)
            return {'user_id':params['user_id'],'role':'owner','title':'新头衔' if len(reads)>=5 else ''}
        return {}
    async def sleep(*a):pass
    monkeypatch.setattr(group_actions.asyncio,'sleep',sleep);live.bot.call=call
    result=await live.tools.run(SCOPE,'default','90000001','eventual','member_title',{'user_id':USER,'title':'新头衔','reason':'测试'})
    assert result['status']=='success' and len(mutations)==1 and len(reads)==5

def test_animated_sticker_shared_dedup_and_disable(live):
    raw=image_bytes(True); item=live.engine.stickers.register(SCOPE,raw,{'name':'开心','tags':'高兴','usage':'庆祝'})
    assert item['animated'] and item['ext']=='gif'
    assert sticker_path(live.settings,item).read_bytes()==raw
    assert live.engine.stickers.register(SCOPE,raw,{})['id']==item['id']
    assert available_sticker(live.db,OTHER,item['id'])['id']==item['id']
    assert live.engine.stickers.register(OTHER,raw,{})['id']==item['id']
    assert live.engine.stickers.list(SCOPE,'庆祝')[0]['id']==item['id']
    item['enabled']=False;live.db.put('sticker',item['id'],item)
    assert not live.engine.stickers.register(SCOPE,raw,{})['enabled']
    assert not live.engine.stickers.list(SCOPE)
    assert not live.engine.stickers.list(OTHER)
    with pytest.raises(ValueError): available_sticker(live.db,OTHER,item['id'])


def test_shared_sticker_capacity_and_dedup_across_groups(live):
    # Old per-group capacity fields must not split or override the shared budget.
    live.group.update(sticker_limit=900, sticker_max_mib=8)
    live.db.put('group',SCOPE,live.group)
    library=live.engine.stickers
    assert library.config()=={'sticker_limit':100,'sticker_max_mib':5}
    live.db.put('sticker_library','global',{'sticker_limit':1,'sticker_max_mib':1})
    item=library.register(SCOPE,image_bytes(),{'name':'共享'})
    assert library.register(OTHER,image_bytes(),{})['id']==item['id']
    with pytest.raises(ValueError,match='共享表情库已达上限'):
        library.register(OTHER,image_bytes(True),{})
    assert len(live.db.all('sticker'))==1
    assert available_sticker(live.db,OTHER,item['id'])['name']=='共享'
    with pytest.raises(ValueError,match='大小限制'):
        library.register(SCOPE,image_bytes()+b'x'*(1024*1024),{})


def test_shared_sticker_registration_serializes_cross_group_duplicates(live):
    from concurrent.futures import ThreadPoolExecutor
    raw=image_bytes(True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids=list(pool.map(lambda scope: live.engine.stickers.register(scope,raw,{})['id'],[SCOPE,OTHER]*8))
    assert len(set(ids))==1 and len(live.db.all('sticker'))==1
    assert len(list((live.settings.data_dir/'stickers').iterdir()))==1


def test_legacy_sticker_shares_without_rewriting_file_or_identifier(live):
    from rolebot.stickers import Stickers
    item=live.engine.stickers.register(SCOPE,image_bytes(True),{'name':'原有动画','tags':'开心'})
    item.pop('origins');live.db.put('sticker',item['id'],item)
    library=Stickers(live.engine)  # Restart preserves the original record as well.
    assert library.list(OTHER,'开心')[0]['id']==item['id']
    assert live.db.get('sticker',item['id'])==item
    assert sticker_path(live.settings,item).read_bytes()==image_bytes(True)
    live.db.invalidate_memory(OTHER,user_id=USER)
    assert live.db.get('sticker',item['id'])


def test_shared_sticker_provenance_forgetting_is_scoped(live):
    item=live.engine.stickers.register(SCOPE,image_bytes(),{'name':'共享'},[SCOPE+':1'],['11111111'])
    shared=live.engine.stickers.register(OTHER,image_bytes(),{},[OTHER+':2'],[USER])
    assert shared['id']==item['id']
    assert shared['source_ids']==[SCOPE+':1'] and shared['actor_ids']==['11111111']
    assert {o['scope'] for o in shared['origins']}=={SCOPE,OTHER}
    live.db.invalidate_memory(SCOPE,user_id=USER)  # Same QQ in an unrelated source scope.
    assert live.db.get('sticker',item['id'])
    unrelated=live.engine.stickers.register(SCOPE,image_bytes(True),{'name':'其他'})
    live.db.invalidate_memory(OTHER,source_ids=[OTHER+':2'])
    assert not live.db.get('sticker',item['id']) and not sticker_path(live.settings,item).exists()
    assert live.db.get('sticker',unrelated['id'])


async def test_shared_search_list_and_send_preserve_permissions_and_original(live):
    import base64
    item=live.engine.stickers.register(OTHER,image_bytes(True),{'name':'开心动画','tags':'庆祝'},[OTHER+':9'],['11111111'])
    m=message(live)
    for name,args in [('sticker_search',{'query':'庆祝'}),('sticker_list',{})]:
        result=await live.tools.run(SCOPE,'default',USER,m['id']+name,name,args)
        assert result['status']=='success' and result['result'][0]['id']==item['id']
        assert not {'scope','source_ids','actor_ids','origins'} & result['result'][0].keys()
    result=await live.tools.run(SCOPE,'default',USER,m['id'],'sticker_send',{'id':item['id']})
    assert result['status']=='success' and not live.bot.sent
    plan=result['result']['delivery']
    status=await delivery.deliver(live.engine,SCOPE,'default',m,[m],{},'',{},[],{'delivery_plan':plan},lambda:True,False)
    assert status=='sent'
    assert live.bot.sent[0][1][0]['data']['file']=='base64://'+base64.b64encode(image_bytes(True)).decode()
    live.group['tools']['sticker_send']['enabled']=False;live.db.put('group',SCOPE,live.group)
    denied=await live.tools.run(SCOPE,'default',USER,'disabled','sticker_send',{'id':item['id']})
    assert denied['status']=='denied' and len(live.bot.sent)==1
    live.group['interaction_enabled']=False;live.db.put('group',SCOPE,live.group)
    denied=await live.tools.run(SCOPE,'default',USER,'off','sticker_search',{'query':'庆祝'})
    assert denied['status']=='denied'


async def test_shared_sticker_disable_between_prepare_and_delivery(live):
    item=live.engine.stickers.register(OTHER,image_bytes(),{'name':'撤销'})
    m=message(live)
    result=await live.tools.run(SCOPE,'default',USER,m['id'],'message_send',{'text':'表情','sticker_id':item['id']})
    assert result['status']=='success'
    item['enabled']=False;live.db.put('sticker',item['id'],item)
    with pytest.raises(ValueError,match='已禁用'):
        await delivery.deliver(live.engine,SCOPE,'default',m,[m],{},'表情',{},[],{'delivery_plan':result['result']['delivery']},lambda:True,False)
    assert not live.bot.sent


async def test_shared_library_does_not_allow_collecting_other_group_history(live):
    m=message(live)
    live.db.put('group',OTHER,{**live.group,'id':OTHER})
    with pytest.raises(ValueError,match='遗忘、过期或停止记忆'):
        await live.engine.stickers.save(OTHER,'default',m['id'])
    assert not live.model.calls and not live.bot.calls

async def test_forget_deletes_registered_assets_and_tasks(live):
    m=message(live)
    item=live.engine.stickers.register(SCOPE,image_bytes(),{'name':'x'},[m['id']],[USER])
    task=await live.engine.scheduler.create(SCOPE,'default',USER,{'text':'secret','delay_seconds':5},m['id'])
    live.db.invalidate_memory(SCOPE,user_id=USER)
    assert not live.db.get('reminder',task['id']) and not live.db.get('sticker',item['id'])
    assert not sticker_path(live.settings,item).exists()

def test_asset_backup_contains_registered_animation(live,tmp_path):
    raw=image_bytes(True);item=live.engine.stickers.register(SCOPE,raw,{'name':'backup'})
    path=live.db.backup(tmp_path/'backups')
    with zipfile.ZipFile(path.with_suffix('.stickers.zip')) as archive:
        assert archive.read(item['id']+'.gif')==raw

async def test_sticker_classifier_rejects_screenshots_and_optout(live,monkeypatch):
    import rolebot.stickers as module
    m=message(live)
    live.db.patch_message_data(SCOPE,m['id'],{'image_count':1,'types':['image']})
    async def refresh(*a):return ['https://example.invalid/test']
    async def download(*a):return image_bytes()
    live.engine.vision.refresh=refresh;monkeypatch.setattr(module,'download',download)
    live.model.script=[{'content':json.dumps({'is_sticker':True,'contains_photo':False,'contains_private_info':False,'is_screenshot':True})}]
    result=await live.engine.stickers.save(SCOPE,'default',m['id'])
    assert result['saved'] is False and not live.db.all('sticker')
    live.db.put('member',SCOPE+':'+USER,{'remember':False})
    with pytest.raises(ValueError): await live.engine.stickers.save(SCOPE,'default',m['id'])
    assert len(live.model.calls)==1

def test_new_features_disabled_by_default(runtime):
    assert not runtime.tools.policy(SCOPE,'message_send')['enabled']
    assert not runtime.tools.policy(SCOPE,'task_create')['enabled']
    assert not runtime.tools.policy(SCOPE,'member_title')['enabled']

async def test_multiplayer_task_requires_matching_actual_requester(live):
    a=message(live,id=1);b=message(live,id=2);b['user_id']='40000001'
    live.model.script=[{'content':json.dumps({'mode':'request','request_message_id':b['id'],'source_matches':False})}]
    result=await live.engine.bound_tool(SCOPE,'default',[a,b],'task_create',{'source_message_id':a['id'],'text':'喝水','delay_seconds':60,'mentions':['self']})
    assert result['error_code']=='source_mismatch' and not live.db.all('reminder')

async def test_cancel_during_model_generation_prevents_send(live):
    task=await live.engine.scheduler.create(SCOPE,'default',USER,{'text':'消息','delay_seconds':5,'mode':'model'})
    async def chat(*a,**kw):
        live.engine.scheduler.update(SCOPE,USER,{'id':task['id'],'action':'cancel'})
        return {'role':'assistant','content':'不应发送'}
    live.model.chat=chat
    await live.engine.scheduler.execute(task)
    assert not live.bot.sent and live.db.get('reminder',task['id'])['status']=='cancelled'

async def test_restart_preserves_pending_and_does_not_replay_running(live):
    task=await live.engine.scheduler.create(SCOPE,'default',USER,{'text':'消息','delay_seconds':60})
    live.engine.scheduler.recover();assert live.db.get('reminder',task['id'])['status']=='pending'
    task['status']='running';live.db.put('reminder',task['id'],task)
    live.engine.scheduler.recover();assert live.db.get('reminder',task['id'])['status']=='unknown'

async def test_due_tasks_order_by_deadline_not_creation_order(live):
    first=await live.engine.scheduler.create(SCOPE,'default',USER,{'text':'先到期','delay_seconds':60})
    second=await live.engine.scheduler.create(SCOPE,'default',USER,{'text':'后到期','delay_seconds':120})
    for offset,t in [(2,first),(1,second)]:
        t['due']=time.time()-offset;live.db.put('reminder',t['id'],t)
    await live.engine.scheduler.tick()
    await asyncio.gather(*live.engine.tasks)
    assert live.bot.sent[0][1][0]['data']['text']=='先到期'
    assert live.db.get('reminder',second['id'])['status']=='pending'
    await live.engine.scheduler.tick()
    await asyncio.gather(*live.engine.tasks)
    assert live.bot.sent[1][1][0]['data']['text']=='后到期'

async def test_background_sticker_job_skips_forgotten_source(live):
    m=message(live);m['images']=['not-a-fetchable-url']
    live.group['sticker_autocollect']=True;live.db.put('group',SCOPE,live.group)
    live.engine.stickers.enqueue(SCOPE,'default',[m]);job=live.db.all('sticker_job')[0]
    live.db.invalidate_memory(SCOPE,user_id=USER)
    await live.engine.stickers.process(job)
    assert not live.model.calls and not live.db.all('sticker')

def test_task_api_and_original_animation(client):
    h=auth(client)
    client.put('/api/v1/settings',headers=h,json={'admin_qq':['90000001'],'api_key':'test'})
    client.put('/api/v1/groups/'+SCOPE,headers=h,json={'enabled':True,})
    assert client.put('/api/v1/groups/'+SCOPE+'/interaction',headers=h,json={'interaction_enabled':True}).status_code==200
    body={'scope':SCOPE,'text':'API任务','delay_seconds':60}
    assert client.post('/api/v1/tasks',json=body).status_code==403
    response=client.post('/api/v1/tasks',headers=h,json=body);assert response.status_code==200,response.text
    t=response.json();assert t['principal']=='admin' and t['creator']=='admin-console'
    for action,status in [('pause','paused'),('resume','pending'),('cancel','cancelled')]:
        assert client.put('/api/v1/tasks/'+t['id'],headers=h,json={'id':t['id'],'action':action}).json()['status']==status
    raw=image_bytes(True)
    response=client.post('/api/v1/stickers?scope='+SCOPE,headers=h,files={'file':('test.gif',raw,'image/gif')})
    assert response.status_code==200,response.text
    s=response.json();assert s['animated']
    image=client.get('/api/v1/stickers/'+s['id']+'/image');assert image.content==raw and image.headers['content-type']=='image/gif'
    assert client.get('/api/v1/stickers?scope='+OTHER).json()[0]['id']==s['id']
    assert client.put('/api/v1/stickers/'+s['id'],headers=h,json={'name':'测试','enabled':False}).json()['enabled'] is False


def test_shared_library_api_global_settings_and_crud(client):
    h=auth(client)
    client.put('/api/v1/settings',headers=h,json={'admin_qq':['90000001'],'api_key':'test'})
    for scope in (SCOPE,OTHER):
        assert client.put('/api/v1/groups/'+scope,headers=h,json={'enabled':True,'sticker_limit':750}).status_code==200
    assert client.get('/api/v1/sticker-library').json()=={'sticker_limit':100,'sticker_max_mib':5}
    assert client.put('/api/v1/sticker-library',json={'sticker_limit':1,'sticker_max_mib':1}).status_code==403
    for limit,size in [(0,5),(2147483648,5),(100,0),(100,9)]:
        assert client.put('/api/v1/sticker-library',headers=h,json={'sticker_limit':limit,'sticker_max_mib':size}).status_code==422
    config={'sticker_limit':2,'sticker_max_mib':1}
    assert client.put('/api/v1/sticker-library',headers=h,json=config).json()==config
    raw=image_bytes(True)
    upload=lambda scope: client.post('/api/v1/stickers?scope='+scope,headers=h,files={'file':('shared.gif',raw,'image/gif')})
    item=upload(SCOPE).json()
    assert upload(OTHER).json()['id']==item['id']
    assert len(client.get('/api/v1/stickers?scope='+OTHER).json())==1
    assert client.put('/api/v1/stickers/'+item['id'],headers=h,json={'name':'全群更新','enabled':False}).status_code==200
    assert client.get('/api/v1/stickers?scope='+SCOPE).json()[0]['name']=='全群更新'
    assert upload(OTHER).json()['enabled'] is False
    # Saving unrelated group switches neither resets shared capacity nor the auto flag.
    client.put('/api/v1/groups/'+SCOPE+'/interaction',headers=h,json={'sticker_autocollect':True})
    group=client.put('/api/v1/groups/'+SCOPE+'/interaction',headers=h,json={'interaction_enabled':True}).json()
    assert group['sticker_autocollect'] and group['sticker_limit']==750
    assert client.get('/api/v1/sticker-library').json()==config
    assert client.delete('/api/v1/stickers/'+item['id'],headers=h).status_code==200
    assert client.get('/api/v1/stickers?scope='+OTHER).json()==[]
    assert client.get('/api/v1/stickers/'+item['id']+'/image').status_code==404
