import copy
from rolebot.config_schema import SAFE_INTEGER
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import ast
import base64
import json
import sqlite3
import time

import httpx
import pytest

from rolebot import prompts, tracing
from rolebot.call_records import Records, pack
from rolebot.engine import group_defaults
from rolebot.model import Model, ModelError
from rolebot.prompts import DEFAULTS, PromptStore, render, render_body

from support import OTHER, SCOPE, USER, auth, configure_model, fact, message, reply_decision, source


def test_all_templates_validate_and_required_variables_are_literal_only(db):
    for key,spec in DEFAULTS.items():
        import re
        values={name:'{{not_evaluated}}' for name in re.findall(r'\{\{(\w+)\}\}',spec['body'])}
        assert isinstance(render_body(key,spec['body'],values),str)
    assert render_body('optimized.system.base','{"nested":{"ok":true}}',{})=='{"nested":{"ok":true}}'
    with pytest.raises(ValueError):render_body('optimized.system.base','{{secret}}',{})
    with pytest.raises(ValueError):render_body('tool.bot_identity','删去身份变量',{})

def test_prompt_drafts_publish_scope_override_and_frozen_turn(db):
    store=PromptStore(db);key='optimized.system.base'
    original=render(db,SCOPE,key)
    store.draft('',key,'全局 v1',0)
    assert render(db,SCOPE,key)==original
    store.publish('',key,0)
    id,token=tracing.begin(db,SCOPE,'default',[])
    try:
        assert render(db,SCOPE,key)=='全局 v1'
        store.publish('',key,1,'全局 v2')
        store.publish(SCOPE,key,0,'群覆盖 v1')
        assert render(db,SCOPE,key)=='全局 v1'
        child,child_token=tracing.begin(db,SCOPE,'default',[],kind='extract')
        try:assert render(db,SCOPE,key)=='全局 v1'
        finally:tracing.finish(db,child,'complete',child_token)
    finally:tracing.finish(db,id,'complete',token)
    assert render(db,SCOPE,key)=='群覆盖 v1' and render(db,'other',key)=='全局 v2'
    store.restore(SCOPE,key,0,1)
    assert render(db,SCOPE,key)==original and len(store.versions(SCOPE,key))==2
    with pytest.raises(ValueError):store.publish('',key,0,'stale edit')

def test_stale_draft_hash_cannot_publish_another_edit(db):
    store=PromptStore(db);draft=store.draft('','optimized.system.base','已核对草稿',0)
    store.draft('','optimized.system.base','另一窗口的草稿',0)
    with pytest.raises(ValueError):store.publish('','optimized.system.base',0,draft_hash=draft['sha256'])

async def test_keys_are_redacted_from_process_logs_as_well_as_payloads(model):
    id,token=tracing.begin(model.db,SCOPE,'default',[])
    try:
        tracing.stage(model.db,'context',{'text':'test-only-provider-key'})
        model.db.audit(SCOPE,'test',{'value':'test-only-provider-key'})
        PromptStore(model.db).publish('','runtime.agent',0,'test-only-provider-key')
        # Snapshot remains pinned; explicitly exercise recording an external prompt.
        from rolebot.prompts import note
        note(model.db,{'key':'test','body':'test-only-provider-key','sha256':'hash'})
    finally:tracing.finish(model.db,id,'complete',token)
    assert 'test-only-provider-key' not in str(model.db.get('trace',id))
    assert 'test-only-provider-key' not in str(model.db.rows('SELECT data FROM audit'))

def test_tool_help_is_editable_but_schema_and_permission_are_fixed(runtime):
    r=runtime;store=PromptStore(r.db)
    before=r.tools.schemas(SCOPE,True)
    store.publish(SCOPE,'tools.calculate.description',0,'说明已编辑')
    after=r.tools.schemas(SCOPE,True)
    a=next(t for t in after if t['function']['name']=='calculate')
    b=next(t for t in before if t['function']['name']=='calculate')
    assert a['function']['description']=='说明已编辑'
    assert a['function']['parameters']==b['function']['parameters']
    assert not any(t['function']['name']=='member_kick' for t in after)

async def test_exact_wire_payload_response_reasoning_and_prompt_snapshot(model):
    configure_model(model,protocol='openai-completions')
    outgoing=[]
    response={'id':'provider-id','choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'结果'+('长'*13000),'reasoning_content':'接口实际返回的思考'}}],'usage':{'prompt_tokens':37,'completion_tokens':49}}
    def handle(req):outgoing.append(json.loads(req.content));return httpx.Response(200,json=response,headers={'x-request-id':'rid'})
    model.client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    id,token=tracing.begin(model.db,SCOPE,'default',[{'id':'s','user_id':USER}])
    try:
        result=await model.chat(SCOPE,'chat',[{'role':'system','content':render(model.db,SCOPE,'optimized.system.base')},{'role':'user','content':'你好'}],stage='runtime.agent')
    finally:tracing.finish(model.db,id,'complete',token)
    record=model.records.detail(result['_call_id'])
    assert record['request']==outgoing[0] and record['response']==response
    assert record['response_headers']['x-request-id']=='rid'
    assert record['prompts'][0]['key']=='optimized.system.base' and record['stage']=='runtime.agent'
    assert 'test-only-provider-key' not in json.dumps(record)
    assert record['expires']-record['ts']==7*86400

async def test_native_protocol_records_signatures_system_and_json_suffix(model):
    configure_model(model)
    raw=[];blocks=[{'type':'thinking','thinking':'原生思考','signature':'opaque-sig'},{'type':'tool_use','id':'t1','name':'calculate','input':{'query':'1+1'}}]
    def handle(req):raw.append(json.loads(req.content));return httpx.Response(200,json={'content':blocks,'usage':{}})
    model.client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    result=await model.chat(SCOPE,'decision',[{'role':'system','content':'系统文本'},{'role':'user','content':'请求'}],json_mode=True)
    row=model.records.detail(result['_call_id'])
    assert row['request']==raw[0] and '有效 JSON' in row['request']['system']
    assert row['response']['content']==blocks
    assert row['prompts'][0]['key']=='protocol.json_instruction'

async def test_retry_attempts_and_non_json_errors_are_all_recorded(model,monkeypatch):
    count=0
    async def immediate(_):pass
    monkeypatch.setattr('rolebot.model.asyncio.sleep',immediate)
    def handle(req):
        nonlocal count
        count+=1
        if count==1:return httpx.Response(429,json={'error':{'message':'rate limited'}},headers={'retry-after':'1'})
        if count==2:raise httpx.ReadTimeout('timeout',request=req)
        return httpx.Response(502,text='<html>upstream error</html>')
    model.client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    with pytest.raises(ModelError):await model.chat(SCOPE,'chat',[{'role':'user','content':'测试'}])
    rows=model.records.listing(SCOPE)[::-1]
    assert [r['status'] for r in rows]==['http_error','network_error','http_error']
    assert [r['attempt'] for r in rows]==[1,2,3] and len({r['operation_id'] for r in rows})==1
    assert model.records.detail(rows[-1]['id'])['response']['raw_text']=='<html>upstream error</html>'

async def test_local_rejection_is_marked_not_sent(model):
    cfg=model.catalog.get();cfg['models'][0]['enabled']=False;model.db.put('inference','global',cfg)
    with pytest.raises(ModelError):await model.chat(SCOPE,'chat',[{'role':'user','content':'尚未发送'}])
    row=model.records.detail(model.records.listing(SCOPE)[0]['id'])
    assert row['status']=='not_sent' and row['request'] is None
    assert row['input']['messages'][0]['content']=='尚未发送'

async def test_opt_out_and_forgetting_in_flight_never_reinstall_payload(model):
    async def handle(req):
        model.db.invalidate_memory(SCOPE,user_id=USER)
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'不得重存','reasoning_content':'秘密'}}]})
    model.client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    id,token=tracing.begin(model.db,SCOPE,'default',[{'id':'s','user_id':USER}])
    try:await model.chat(SCOPE,'chat',[{'role':'user','content':'待遗忘内容'}])
    finally:tracing.finish(model.db,id,'complete',token)
    row=model.records.detail(model.records.listing(SCOPE)[0]['id'])
    assert row['redacted'] and 'request' not in row and 'response' not in row
    id,token=tracing.begin(model.db,SCOPE,'default',[{'id':'s2','user_id':USER,'remember':False}])
    try:await model.chat(SCOPE,'chat',[{'role':'user','content':'停止记忆成员'}])
    finally:tracing.finish(model.db,id,'complete',token)
    assert all('request' not in model.records.detail(r['id']) for r in model.records.listing(SCOPE))

def test_images_expire_separately_and_cleanup_preserves_business_data(db):
    records=Records(db);provider={'id':'test','protocol':'openai-completions'}
    image='data:image/png;base64,'+base64.b64encode(b'actual-image-bytes').decode()
    id=records.start(SCOPE,'chat','chat',provider,{'messages':[{'content':[{'type':'image_url','image_url':{'url':image}}]}]})
    records.finish(id,'success',response={'reasoning_content':'思考保留七天'})
    row=records.detail(id)
    assert records.expand(row['request'])['messages'][0]['content'][0]['image_url']['url']==image
    assert 'actual-image-bytes' not in str(row)
    db.execute('UPDATE call_images SET expires=?',(time.time()-1,));records.cleanup()
    row=records.detail(id)
    assert row['images'][0]['status']=='expired' and row['response']['reasoning_content']=='思考保留七天'
    with pytest.raises(ValueError):records.expand(row['request'])
    db.save_memory(SCOPE,'default','需要长期保留的记忆')
    db.execute('UPDATE model_calls SET expires=?',(time.time()-1,));records.cleanup()
    assert records.detail(id) is None and db.memories(SCOPE)

def test_invalid_image_and_literal_marker_round_trip_without_fake_attachments(db):
    records=Records(db)
    payload={'messages':[{'content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,NOT-BASE64!'}}]}],'literal':{'__record_image__':{'unexpected':True}}}
    id=records.start(SCOPE,'chat','chat',{'id':'t','protocol':'openai-completions'},payload)
    assert records.expand(records.detail(id)['request'])==payload

def test_secret_redaction_is_explicit_and_images_forget_atomically(runtime):
    r=runtime;r.settings.set_key('sk-private-test-secret')
    records=Records(r.db,r.settings)
    id=records.start(SCOPE,'chat','chat',{'id':'t','protocol':'openai-completions'},{'messages':[{'content':'sk-private-test-secret'}],'api_key':'sk-private-test-secret'})
    records.finish(id,'success',response={'content':'sk-private-test-secret'})
    row=records.detail(id)
    assert 'sk-private-test-secret' not in str(row) and row['redactions']
    r.db.invalidate_memory(SCOPE,user_id=USER)
    assert records.detail(id)['redacted']

def test_restart_marks_unknown_call_without_replay(db):
    trace,token=tracing.begin(db,SCOPE,'default',[])
    records=Records(db);id=records.start(SCOPE,'chat','chat',{'id':'t','protocol':'openai-completions'},{'model':'test'})
    tracing.finish(db,trace,'running',token)
    records.recover()
    assert records.detail(id)['status']=='interrupted'
    assert db.get('trace',trace)['status']=='interrupted'
    assert len(records.listing(SCOPE))==1

def test_daily_backups_do_not_extend_diagnostic_retention(db,tmp_path):
    records=Records(db);id=records.start(SCOPE,'chat','chat',{'id':'t','protocol':'openai-completions'},{'model':'test'})
    db.save_memory(SCOPE,'default','保留业务记忆')
    copied=sqlite3.connect(db.backup(tmp_path/'backup'))
    assert copied.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0]==0
    assert copied.execute('SELECT COUNT(*) FROM memories').fetchone()[0]==1
    assert records.detail(id) is not None
    copied.close()

def test_prompt_api_auth_csrf_drafts_preview_versions_and_validation(client):
    assert client.get('/api/v1/prompts').status_code==401
    headers=auth(client)
    rows=client.get('/api/v1/prompts').json();assert len(rows)>=66
    assert client.put('/api/v1/prompts/optimized.system.base/draft',json={'body':'草稿','base_version':0}).status_code==403
    draft=client.put('/api/v1/prompts/optimized.system.base/draft',headers=headers,json={'body':'草稿','base_version':0});assert draft.status_code==200
    assert client.post('/api/v1/prompts/optimized.system.base/preview',headers=headers,json={'body':'{"a":{"b":1}}'}).json()['model_called'] is False
    assert client.post('/api/v1/prompts/optimized.system.base/publish',headers=headers,json={'base_version':0,'draft_hash':draft.json()['sha256']}).json()['version']==1
    assert client.post('/api/v1/prompts/optimized.system.base/restore/0',headers=headers,json={'base_version':1}).json()['version']==2
    assert [v['version'] for v in client.get('/api/v1/prompts/optimized.system.base/versions').json()]==[0,2,1]
    assert client.post('/api/v1/prompts/optimized.system.base/preview',headers=headers,json={'body':'{{unknown}}'}).status_code==409
    assert client.put('/api/v1/model-calls/settings',headers=headers,json={'days':7,'image_hours':24}).status_code==200
    assert client.put('/api/v1/settings',headers=headers,json={'model_log_days':-1}).status_code==422

def test_call_details_export_and_safe_replay_never_run_qq_or_save_memories(client):
    headers=auth(client);app=client.app;model=app.state.model;app.state.settings.set_key('test-key-only')
    provider=model.catalog.get()['providers'][0];model_id=model.catalog.resolve('chat')['model']['model_id']
    payload={'model':model_id,'messages':[{'role':'user','content':'模拟一次管理工具'}],'max_tokens':256,'stream':False,'tools':[{'type':'function','function':{'name':'member_kick','parameters':{'type':'object'}}}]}
    id=model.records.start(SCOPE,'chat','chat',provider,payload)
    model.records.finish(id,'success',response={'choices':[{'message':{'role':'assistant','content':'原始响应'}}]})
    count=0
    def handle(req):
        nonlocal count
        count+=1
        if count==1:message={'role':'assistant','content':None,'reasoning_content':'模拟工具提议','tool_calls':[{'id':'tool1','type':'function','function':{'name':'member_kick','arguments':json.dumps({'user_id':USER})}}]}
        else:
            submitted=json.loads(req.content);assert any(m.get('role')=='tool' and 'simulated' in m['content'] for m in submitted['messages'])
            message={'role':'assistant','content':'模拟完成','reasoning_content':'已查看模拟结果'}
        return httpx.Response(200,json={'choices':[{'message':message,'finish_reason':'stop'}],'usage':{}})
    model.client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    async def forbidden(*a,**kw):raise AssertionError('No QQ access is allowed during replay')
    app.state.onebot.call=forbidden
    r=client.post('/api/v1/model-calls/'+id+'/replay',headers=headers,json={'rounds':2,'mock_results':{'member_kick':{'ok':True}}})
    assert r.status_code==200,r.text
    assert r.json()['tools_simulated'] and r.json()['qq_messages_sent']==0 and len(r.json()['call_ids'])==2
    assert not app.state.db.memories(SCOPE) and model.records.detail(id)['response']['choices'][0]['message']['content']=='原始响应'
    assert client.get('/api/v1/model-calls/'+id+'/export').json()['request']==payload
    app.state.db.invalidate_memory(SCOPE,user_id=USER)
    assert client.post('/api/v1/model-calls/'+id+'/replay',headers=headers,json={}).status_code==409

def test_replay_stops_when_provider_is_disabled_mid_chain(client):
    headers=auth(client);model=client.app.state.model;client.app.state.settings.set_key('test-key-only')
    provider=model.catalog.get()['providers'][0]
    payload={'model':model.catalog.resolve('chat')['model']['model_id'],'messages':[{'role':'user','content':'模拟'}],'max_tokens':256}
    id=model.records.start(SCOPE,'chat','chat',provider,payload);model.records.finish(id,'success',response={})
    count=0
    def handle(req):
        nonlocal count
        count+=1;cfg=model.catalog.get();cfg['providers'][0]['enabled']=False;model.db.put('inference','global',cfg)
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'id':'x','function':{'name':'calculate','arguments':'{}'}}]}}]})
    model.client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    response=client.post('/api/v1/model-calls/'+id+'/replay',headers=headers,json={'rounds':2})
    assert response.status_code==409 and count==1

async def test_end_to_end_trace_keeps_reasoning_in_console_only_and_memory_link(runtime):
    r=runtime;r.settings.set_key('test-model-key');model=Model(r.db,r.settings);await model.client.aclose()
    r.engine.model=model;r.engine.memory.model=model;r.tools.model=model
    m=source(r,'机器人你好',1)
    def handle(req):
        body=json.loads(req.content)
        if body.get('response_format'):
            message={**reply_decision(m, text='你好呀'),'reasoning_content':'chat-thought'}
        else:message={'role':'assistant','content':'你好呀','reasoning_content':'chat-thought'}
        return httpx.Response(200,json={'choices':[{'message':message}],'usage':{}})
    model.client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        await r.engine.turn(SCOPE,[m]);assert r.bot.sent
        assert 'thought' not in str(r.bot.sent)
        rows=model.records.listing(SCOPE);assert len(rows)==1
        assert all(model.records.detail(row['id'])['prompts'] for row in rows)
        id,token=tracing.begin(r.db,SCOPE,'default',[m],kind='extract')
        try:
            result=await model.chat(SCOPE,'memory',[{'role':'user','content':'用于测试关联'}])
            memory_id=r.engine.memory.knowledge.apply(SCOPE,'default',[fact(m,'已核对的表达')],[{**m,'role':'user'}])[0]
        finally:tracing.finish(r.db,id,'complete',token)
        formation=r.engine.memory.knowledge.row(SCOPE,memory_id)['data']['formation']
        assert formation['trace_id']==id and formation['call_id']==result['_call_id']
    finally:await model.close()

def test_system_and_user_instruction_literals_are_not_hidden_in_call_sites():
    # Runtime data/JSON serialization is allowed; direct instruction strings in
    # message dictionaries must go through the editable catalog.
    root=Path(__file__).resolve().parents[1]/'backend/rolebot'
    offenders=[]
    for name in ['engine','memory','context','vision','delivery','tool_intent','api','model']:
        tree=ast.parse((root/(name+'.py')).read_text('utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node,ast.Dict):
                for key,value in zip(node.keys,node.values):
                    if isinstance(key,ast.Constant) and key.value=='content' and isinstance(value,ast.Constant) and isinstance(value.value,str) and len(value.value)>12 and any('\u4e00'<=c<='\u9fff' for c in value.value):offenders.append((name,node.lineno))
    assert not offenders,offenders

async def test_engine_uses_role_in_one_call_without_tool_boilerplate(runtime):
    r=runtime;m=source(r,"机器人，下午好",1)
    r.model.script=[reply_decision(m, text="下午好呀～")]
    await r.engine.turn(SCOPE,[m])
    assert len(r.bot.sent)==1
    assert len(r.model.calls)==1 and "合成测试" in r.model.calls[0][2][0]["content"]
    assert "本轮没有实际工具调用" not in str(r.model.calls[0][2])
    assert r.db.get("context_preview",SCOPE+":default")["stage"]=="reply"

def test_scope_version_and_turn_snapshot_can_rollback(runtime):
    r=runtime;store=prompts.PromptStore(r.db)
    store.publish(SCOPE,"runtime.agent",0,"群专用新版")
    assert prompts.render(r.db,SCOPE,"runtime.agent")=="群专用新版"
    assert prompts.render(r.db,OTHER,"runtime.agent")!= "群专用新版"
    id,token=tracing.begin(r.db,SCOPE,"default",[])
    try:
        store.publish(SCOPE,"runtime.agent",1,"下一版本")
        assert prompts.render(r.db,SCOPE,"runtime.agent")=="群专用新版"
    finally:tracing.finish(r.db,id,"complete",token)
    assert prompts.render(r.db,SCOPE,"runtime.agent")=="下一版本"
    store.restore(SCOPE,"runtime.agent",1,2)
    assert prompts.render(r.db,SCOPE,"runtime.agent")=="群专用新版"


def seed(client, now):
    db = client.app.state.db
    for scope in (SCOPE, OTHER): db.put('group', scope, group_defaults(scope, scope))
    def call(id, scope, status, ts, ms=10, expires=None):
        meta = {'id': id, 'scope': scope, 'ts': ts, 'status': status, 'duration_ms': ms,
                'purpose': 'chat', 'model': 'test-model', 'trace_id': 'trace-'+id}
        db.execute('INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?)',
                   (id, 'trace-'+id, scope, ts, expires or now+86400, status, json.dumps(meta), pack({'request': {'private': 'not projected'}})))
    return db, call


def test_dashboard_attempt_outcomes_percentile_and_currency(authed_client, monkeypatch):
    now = time.time()
    monkeypatch.setattr('rolebot.dashboard.time.time', lambda: now)
    db, call = seed(authed_client, now)
    for i in range(20): call('s'+str(i), SCOPE, 'success', now-100+i, (i+1)*100)
    call('bad', SCOPE, 'network_error', now-60, 2100)
    for status in ('running', 'cancelled', 'interrupted', 'not_sent'): call(status, SCOPE, status, now-50, 999999)
    call('other', OTHER, 'http_error', now-30)
    call('expired', SCOPE, 'success', now-40, 1, now-1)
    # End is exclusive and start is inclusive.
    call('start', SCOPE, 'http_error', now-200, 2200)
    call('end', SCOPE, 'http_error', now)
    for id, scope, currency, incoming, cached, out, cost in [('u1', SCOPE, 'USD', 100, 25, 20, .02), ('u2', SCOPE, 'CNY', 60, 10, 30, .1), ('u3', OTHER, 'USD', 900, 0, 900, 9)]:
        db.execute('INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?,?)', (id,scope,'m','chat',incoming,cached,out,cost,currency,now-30))
    result = authed_client.get('/api/v1/dashboard', params={'scope': SCOPE, 'start': now-200, 'end': now}).json()
    m = result['metrics']
    assert m['calls']==26 and m['success']==20 and m['failed']==2
    assert m['success_rate']==pytest.approx(20/22)
    assert m['p95_ms']==2100
    assert m['excluded']=={'running':1, 'cancelled':1, 'interrupted':1, 'not_sent':1}
    assert (m['input_tokens'],m['cached_tokens'],m['output_tokens'])==(160,35,50)
    assert m['costs']==[{'currency':'CNY','cost':.1},{'currency':'USD','cost':.02}]
    assert [g['id'] for g in result['groups']]==[SCOPE]
    assert all(a['scope']==SCOPE for a in result['alerts'])
    assert 'private' not in str(result)
    assert sum(s['calls'] or 0 for s in result['series'])==26


def test_dashboard_empty_missing_and_invalid_ranges(authed_client):
    data = authed_client.get('/api/v1/dashboard').json()
    assert data['metrics']['success_rate'] is None
    assert data['metrics']['p95_ms'] is None
    assert data['range']['partial'] and all(s['calls'] is None for s in data['series'])
    assert authed_client.get('/api/v1/dashboard',params={'scope':'missing'}).status_code==404
    for start,end in [(1,1),(2,1),(0,32*86400),('nan',10),(0,'inf')]:
        assert authed_client.get('/api/v1/dashboard',params={'start':start,'end':end}).status_code==422


def test_daily_buckets_and_usage_filters_use_shanghai(authed_client, monkeypatch):
    midnight = datetime(2026,10,19,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    now = midnight+10
    monkeypatch.setattr('rolebot.dashboard.time.time',lambda:now)
    db, call = seed(authed_client,now)
    call('before',SCOPE,'success',midnight-1)
    call('after',SCOPE,'success',midnight)
    for id,ts in [('u1',midnight-1),('u2',midnight),('u3',now)]:
        db.execute('INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?,?)',(id,SCOPE,'m','chat',100,25,5,.01,'USD',ts))
    data=authed_client.get('/api/v1/dashboard',params={'start':midnight-7*86400,'end':now}).json()
    assert data['range']['bucket']=='day'
    bins={s['label']:s for s in data['series']}
    assert bins['10-18']['calls']==1 and bins['10-19']['calls']==1
    assert bins['10-17']['calls'] is None
    usage=authed_client.get('/api/v1/usage',params={'scope':SCOPE,'start':midnight,'end':now}).json()
    assert len(usage['rows'])==1 and usage['rows'][0]['calls']==1
    assert usage['daily'][0]['day']=='2026-10-19'
    assert authed_client.get('/api/v1/usage').json()['rows'][0]['calls']==3


def test_filtered_lists_match_dashboard_and_preserve_contract(authed_client):
    now=time.time();db,call=seed(authed_client,now)
    for i,scope,status in [('1',SCOPE,'success'),('2',SCOPE,'http_error'),('3',OTHER,'error')]:
        call(i,scope,status,now-60)
        db.put('trace','t'+i,{'id':'t'+i,'scope':scope,'kind':'chat','status':'error' if status!='success' else 'sent','ts':now-60,'context':{},'stages':[]})
        db.execute('INSERT INTO audit VALUES(?,?,?,?,?)',('a'+i,scope,'tool',json.dumps({'status':status}),now-60))
    args={'scope':SCOPE,'start':now-120,'end':now}
    listed=authed_client.get('/api/v1/model-calls',params={**args,'status':'failure'}).json()
    assert [r['id'] for r in listed]==['2']
    assert len(authed_client.get('/api/v1/model-calls',params={**args,'model':'test-model','purpose':'chat'}).json())==2
    traces=authed_client.get('/api/v1/traces',params={**args,'status':'error','kind':'chat'}).json()
    assert [r['id'] for r in traces]==['t2'] and 'stages' not in traces[0]
    logs=authed_client.get('/api/v1/audit',params={**args,'status':'http_error','kind':'tool'}).json()
    assert [r['id'] for r in logs]==['a2']
    assert isinstance(authed_client.get('/api/v1/model-calls').json(),list)
    assert isinstance(authed_client.get('/api/v1/traces').json(),list)


def test_dashboard_requires_authentication(client):
    assert client.get('/api/v1/dashboard').status_code==401


def test_dashboard_pending_tasks_and_failed_runs_are_scoped(authed_client):
    now=time.time();db,_=seed(authed_client,now)
    for i,scope in [('1',SCOPE),('2',OTHER)]:
        db.put('reminder',i,{'id':i,'scope':scope,'text':'提醒','status':'pending','due':now+60,'creator':'private-field'})
        db.put('task_run','r'+i,{'id':'r'+i,'task_id':i,'scope':scope,'status':'unknown','started_at':now-30,'trace_id':'t'+i,'error':'private-field'})
    data=authed_client.get('/api/v1/dashboard',params={'scope':SCOPE}).json()
    assert [t['id'] for t in data['tasks']]==['1']
    assert data['alerts'][0]['trace_id']=='t1'
    assert 'private-field' not in str(data)


def console_group(c):
    db = c.app.state.db
    db.put('group',SCOPE,group_defaults(SCOPE))
    return db



def test_schema_and_expanded_configuration_roundtrip(authed_client):
    c=authed_client; db=console_group(c)
    schema=c.get('/api/v1/config-schema').json()['fields']
    assert schema['group.followup_seconds']['maximum'] > 900
    settings={'gate_max_messages':800,'working_initial_messages':600,'working_max_messages':900,
              'context_tokens':3000000,'image_max_count':128,'image_input_budget':128*4096,'image_request_mib':128,
              'media_hours':1000,'model_log_image_hours':1000,'retention_days':5000}
    response=c.put('/api/v1/settings',json=settings)
    assert response.status_code==200,response.text
    assert all(c.get('/api/v1/settings').json()[k]==v for k,v in settings.items())
    url='/api/v1/groups/'+SCOPE
    changes={'followup_seconds':900,'proactive_hourly':120,'segment_max_parts':5,'segment_threshold':1000,
             'segment_interval_min':7,'segment_interval_max':10,'send_per_minute':45,'tool_max_rounds':8,'tool_max_calls':20}
    response=c.patch(url,json={**changes,'expected_revision':1})
    assert response.status_code==200,response.text
    assert all(db.get('group',SCOPE)[k]==v for k,v in changes.items())
    # Legacy clients omit the new fields without resetting them.
    assert c.put(url,json={'name':'兼容旧客户端'}).status_code==200
    assert db.get('group',SCOPE)['tool_max_calls']==20
    assert c.put('/api/v1/sticker-library',json={'sticker_limit':4000,'sticker_max_mib':8}).status_code==200



@pytest.mark.parametrize('key,value',[('image_max_count',129),('image_request_mib',129),('gate_max_messages',True),
                                    ('gate_max_messages',1.5),('gate_max_messages',SAFE_INTEGER+1),('gate_max_messages',10**400),('image_input_budget',None)])
def test_numeric_resource_boundaries(authed_client,key,value):
    c=authed_client; before=c.get('/api/v1/settings').json()
    assert c.put('/api/v1/settings',json={key:value}).status_code==422
    assert c.get('/api/v1/settings').json()==before



def test_access_atomicity_revisions_and_legacy_writers(authed_client):
    c=authed_client; db=console_group(c); url='/api/v1/groups/'+SCOPE+'/access'
    original=c.get(url).json(); body=copy.deepcopy(original)
    body['interaction_enabled']=True
    body['tools']['member_title'].update(enabled=True,max_per_hour=600,request_access={'member':'self','admin':'all'})
    body['permissions']['members'][USER]={'roles':[],'allow':[],'deny':['tool:member_kick']}
    result=c.put(url,json=body)
    assert result.status_code==200,result.text
    value=result.json(); assert value['group_revision']==2 and value['permissions']['revision']==1
    assert c.put(url,json=body).status_code==409
    assert c.get(url).json()==value
    # A legacy account write invalidates the access half only. No group change may leak.
    assert c.put('/api/v1/permissions/'+SCOPE,json=value['permissions']).status_code==200
    before_group=db.get('group',SCOPE)
    value['interaction_enabled']=False
    assert c.put(url,json=value).status_code==409
    assert db.get('group',SCOPE)==before_group
    latest=c.get(url).json(); latest['tools']['web_search']['request_access']={'member':'self','admin':'all'}
    before=c.get(url).json()
    assert c.put(url,json=latest).status_code==422
    assert c.get(url).json()==before



def test_access_rolls_back_when_second_write_fails(authed_client,monkeypatch):
    c=authed_client; db=console_group(c); url='/api/v1/groups/'+SCOPE+'/access'
    before=c.get(url).json(); body=copy.deepcopy(before);body['permissions']['autonomous_rules']='新的规则'
    original=db.put
    def broken(kind,key,value):
        if kind=='group': raise RuntimeError('injected write failure')
        return original(kind,key,value)
    monkeypatch.setattr(db,'put',broken)
    with pytest.raises(RuntimeError):c.put(url,json=body)
    assert c.get(url).json()==before



def test_patch_does_not_overwrite_other_fields(authed_client):
    c=authed_client;db=console_group(c);url='/api/v1/groups/'+SCOPE
    first=c.patch(url,json={'expected_revision':1,'guidance':'先保存的内容'}).json()
    assert c.patch(url,json={'expected_revision':1,'paused':True}).status_code==409
    result=c.patch(url,json={'expected_revision':first['revision'],'paused':True}).json()
    assert result['guidance']=='先保存的内容' and result['paused'] is True
    before=db.get('group',SCOPE)
    assert c.patch(url,json={'expected_revision':result['revision'],'segment_interval_min':9}).status_code==422
    assert db.get('group',SCOPE)==before



def test_settings_retention_transaction_and_conflicts(authed_client):
    c=authed_client;db=console_group(c); before=c.get('/api/v1/settings').json()
    result=c.put('/api/v1/settings?expected_revision='+str(before.get('revision',0)),json={
        'model_log_days':6000,'model_log_image_hours':600,'working_max_messages':700})
    assert result.status_code==200,result.text
    assert c.get('/api/v1/model-calls/settings').json()=={'days':6000,'image_hours':600}
    assert c.put('/api/v1/settings?expected_revision='+str(before.get('revision',0)),json={'model_log_days':7}).status_code==409
    stored=db.config()
    assert c.put('/api/v1/settings',json={'working_initial_messages':900,'model_log_days':7}).status_code==422
    assert db.config()==stored
