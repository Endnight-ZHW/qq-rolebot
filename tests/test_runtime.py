import asyncio
import json
import time

from starlette.websockets import WebSocketDisconnect
import pytest

from rolebot.db import Database
from rolebot.engine import group_defaults
from rolebot.media import public_url
from rolebot.onebot import ActionUnknown, OneBot, normalize
from rolebot.reply_recovery import recover, failure_text
from rolebot.tools import calculator

from support import SECOND, OTHER, SCOPE, USER, auth, event, grant_management, message, reply_decision, source, participation


def test_release_manifest_excludes_credentials_and_runtime_data(tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'ops'))
    from remote import managed_files
    for name in ['backend/rolebot/app.py','frontend/dist/assets/app.js','frontend/dist/index.html','data/secrets.json','artifacts/private.py']:
        file=tmp_path/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_text('fixture')
    actual=[p.relative_to(tmp_path).as_posix() for p in managed_files(tmp_path)]
    assert actual==['backend/rolebot/app.py','frontend/dist/assets/app.js','frontend/dist/index.html']


def test_release_stage_does_not_upload_ingress_changes(tmp_path,monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
    import release
    for name in ('frontend/dist/index.html','compose.yaml','ops/Caddyfile'):
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('candidate')
    monkeypatch.setattr(release,'WORKSPACE',tmp_path)
    reads=[]
    def read_only(client,password,command):
        reads.append(command)
        assert 'read_bytes()' in command
        return json.dumps({'compose.yaml':'different','ops/Caddyfile':'different'})
    def forbidden_upload(*a,**kw):raise AssertionError('Production upload occurred before ingress approval')
    monkeypatch.setattr(release,'checked',read_only);monkeypatch.setattr(release,'upload',forbidden_upload)
    with pytest.raises(ValueError,match='Ingress configuration changed'):release.stage(None,'')
    assert len(reads)==1 and not (tmp_path/'artifacts/release.json').exists()


def test_retired_manifest_only_includes_removed_managed_sources():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'ops'))
    from release import retired_sources
    previous = {'file_hashes': {'backend/old.py': 'old', 'backend/active.py': 'active',
        'frontend/dist/assets/old-abcdefgh.js': 'asset', 'data/secrets.json': 'private'}}
    assert retired_sources(previous, {'backend/active.py'}) == [('backend/old.py', 'old')]


def test_static_cleanup_keeps_current_previous_and_unmanaged_files(tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'ops'))
    from release import static_cleanup_script
    assets = tmp_path/'frontend/dist/assets';assets.mkdir(parents=True)
    for name in ('active-12345678.js', 'previous-abcdefgh.css', 'stale-87654321.js', 'custom.js'):
        (assets/name).write_text('content')
    (tmp_path/'data').mkdir(); (tmp_path/'data/secrets.json').write_text('private')
    retained = {'frontend/dist/assets/active-12345678.js', 'frontend/dist/assets/previous-abcdefgh.css'}
    exec(static_cleanup_script(tmp_path, retained), {})
    assert {p.name for p in assets.iterdir()} == {'active-12345678.js','previous-abcdefgh.css','custom.js'}
    assert (tmp_path/'data/secrets.json').read_text() == 'private'
    with pytest.raises(AssertionError):
        exec(static_cleanup_script(tmp_path, {'frontend/dist/../data/secrets.json'}), {})


@pytest.mark.parametrize('action', ['deploy', 'rollback', 'failed_deploy'])
def test_release_and_rollback_preserve_current_data(tmp_path, monkeypatch, action):
    import shlex
    import tarfile
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'ops'))
    import release
    root = tmp_path/'server'; backup = root/'upgrade-backups/current'
    backup.mkdir(parents=True)
    assets = root/'frontend/dist'; assets.mkdir(parents=True)
    (assets/'index.html').write_text('previous frontend')
    with tarfile.open(backup/'source.tar.gz', 'w:gz') as archive:
        archive.add(assets, arcname='frontend/dist')
    (assets/'index.html').write_text('candidate frontend')
    data = root/'data'; data.mkdir()
    db = Database(data/'app.sqlite')
    db.put('group', SCOPE, {'enabled': True, 'tools': {'profile_like': {'request_access': None}}})
    db.add_message('new-chat', SCOPE, 'default', USER, 'user', 'keep new chat')
    db.close()
    (data/'secrets.json').write_text('private')
    (data/'original.source-v2').write_bytes(b'original image')
    before = {p.name: p.read_bytes() for p in data.iterdir()}
    monkeypatch.setattr(release, 'ROOT', str(root))
    monkeypatch.setattr(release, 'STATE', tmp_path/'release.json')
    monkeypatch.setattr(release, 'managed_files', lambda *a: [])
    monkeypatch.setattr(release, 'upload', lambda *a, **kw: {'exit_code': int(action == 'failed_deploy')})
    commands = []
    def checked(client, password, command, timeout=120):
        commands.append(command)
        if command.startswith('python3 -c '):
            exec(shlex.split(command)[2], {})
        return 'sha256:candidate' if command.startswith('docker image inspect') else ''
    monkeypatch.setattr(release, 'checked', checked)
    state = {'candidate': 'candidate', 'previous': 'previous', 'tested': 'sha256:candidate', 'backup': str(backup)}
    if action == 'failed_deploy':
        with pytest.raises(RuntimeError): release.deploy(None, None, state)
    elif action == 'deploy':
        release.deploy(None, None, state)
    else:
        release.rollback(None, None, state)
    assert {p.name: p.read_bytes() for p in data.iterdir()} == before
    assert state['status'] == ('deployed' if action == 'deploy' else 'rolled_back')
    assert (assets/'index.html').read_text() == ('candidate frontend' if action == 'deploy' else 'previous frontend')
    assert any('docker compose up' in command for command in commands)


@pytest.mark.parametrize('failed', [False, True])
def test_release_uses_recorded_selective_rollback(tmp_path, monkeypatch, failed):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'ops'))
    import release
    monkeypatch.setattr(release, 'STATE', tmp_path/'release.json')
    state = {'status': 'deployed', 'rollback_command': 'sudo python3 /backup/rollback.py'}
    def checked(client, password, command):
        assert command == 'python3 /backup/rollback.py'
        if failed: raise RuntimeError('rollback failed')
    monkeypatch.setattr(release, 'checked', checked)
    if failed:
        with pytest.raises(RuntimeError): release.rollback(None, None, state)
        assert state['status'] == 'deployed' and not release.STATE.exists()
    else:
        release.rollback(None, None, state)
        assert json.loads(release.STATE.read_text())['status'] == 'rolled_back'


def test_memory_scope_and_persona_isolation(db):
    db.save_memory(SCOPE,'','喜欢草莓',{'pinned':True})
    db.save_memory(SCOPE,'default','助手一起去过海边',{'pinned':True})
    db.save_memory(SCOPE,'another','另一个角色的秘密',{'pinned':True})
    db.save_memory(OTHER,'default','其他群秘密',{'pinned':True})
    found={x['text'] for x in db.recall(SCOPE,'default','喜欢什么')}
    assert found=={'喜欢草莓','助手一起去过海边'}
    assert not db.recall('10000001:p:'+USER,'default','秘密')

def test_chinese_full_text_search(db):
    db.save_memory(SCOPE,'','测试成员乙喜欢草莓蛋糕',{'confidence':.9})
    assert db.recall(SCOPE,'default','草莓')[0]['text']=='测试成员乙喜欢草莓蛋糕'

def test_forget_clears_source_summary_search_and_epoch(db):
    db.add_message('source',SCOPE,'default',USER,'user','秘密地点')
    a=db.save_memory(SCOPE,'','秘密地点',{'source_ids':['source'],'subject_ids':[USER]})
    db.save_memory(SCOPE,'default','去过秘密地点',{'source_ids':['source']})
    db.put('summary',SCOPE+':default',{'text':'秘密地点'})
    db.put('context_preview',SCOPE+':default',{'text':'秘密地点'})
    db.invalidate_memory(SCOPE,memory_id=a)
    assert not db.memories(SCOPE)
    assert not db.recent(SCOPE,'default')
    assert not db.get('summary',SCOPE+':default')
    assert not db.get('context_preview',SCOPE+':default')
    assert db.get('memory_epoch',SCOPE)['value']==1
    assert not db.rows('SELECT id FROM memory_fts')

def test_backup_has_consistent_data(db,tmp_path):
    db.save_memory(SCOPE,'','备份里的记忆',{'pinned':True})
    copy=Database(db.backup(tmp_path/'backups'))
    assert copy.memories(SCOPE)[0]['text']=='备份里的记忆'
    copy.close()

async def test_management_is_denied_until_authorized(runtime):
    result=await runtime.tools.run(SCOPE,'default',USER,'e1','member_mute',{'user_id':USER,'duration':60,'reason':'测试'})
    assert result['status']=='denied'
    assert not runtime.bot.calls

async def test_user_management_requires_explicit_qq_grant(runtime):
    runtime.group['tools']['member_mute']['enabled']=True;runtime.db.put('group',SCOPE,runtime.group)
    denied=await runtime.tools.run(SCOPE,'default',USER,'e1','member_mute',{'user_id':USER,'duration':60,'reason':'测试'})
    assert denied['status']=='denied' and not runtime.bot.calls
    grant_management(runtime)
    result=await runtime.tools.run(SCOPE,'default',USER,'e1','member_mute',{'user_id':USER,'duration':60,'reason':'基于当前群授权'})
    assert result['status']=='success'
    assert runtime.bot.calls[-1]==('set_group_ban',{'group_id':'20000001','user_id':USER,'duration':60},True)

async def test_bounds_protected_accounts_and_unknown_tools(runtime):
    grant_management(runtime)
    runtime.group['tools']['member_mute']['enabled']=True;runtime.db.put('group',SCOPE,runtime.group)
    for name,args in [('member_mute',{'user_id':USER,'duration':601,'reason':'x'}),('member_mute',{'user_id':'90000001','duration':60,'reason':'x'}),('shell',{'command':'whoami'}),('member_mute',{'user_id':USER,'duration':60,'reason':'x','group_id':'999'})]:
        assert (await runtime.tools.run(SCOPE,'default',USER,'e',name,args))['status']=='denied'
    assert not runtime.bot.calls

async def test_revocation_after_permission_lookup(runtime):
    grant_management(runtime)
    runtime.group['tools']['member_mute']['enabled']=True;runtime.db.put('group',SCOPE,runtime.group)
    original=runtime.bot.call
    async def call(*a,**kw):
        value=await original(*a,**kw)
        runtime.group['tools']['member_mute']['enabled']=False;runtime.db.put('group',SCOPE,runtime.group)
        return value
    runtime.bot.call=call
    result=await runtime.tools.run(SCOPE,'default',USER,'e','member_mute',{'user_id':USER,'duration':60,'reason':'x'})
    assert result['status']=='denied'
    assert not any(c[0]=='set_group_ban' for c in runtime.bot.calls)

async def test_uncertain_mutation_is_not_retried(runtime):
    grant_management(runtime)
    runtime.group['tools']['member_mute']['enabled']=True;runtime.db.put('group',SCOPE,runtime.group);runtime.bot.unknown=True
    args={'user_id':USER,'duration':60,'reason':'x'}
    a=await runtime.tools.run(SCOPE,'default',USER,'e','member_mute',args)
    b=await runtime.tools.run(SCOPE,'default',USER,'e','member_mute',args)
    assert a['status']=='unknown' and b['duplicate']
    assert len([c for c in runtime.bot.calls if c[0]=='set_group_ban'])==1

async def test_cross_scope_recall_denied(runtime):
    runtime.group['tools']['message_recall']['enabled']=True;runtime.db.put('group',SCOPE,runtime.group)
    runtime.db.add_message('outside',OTHER,'default',USER,'user','x',{'message_id':'123'})
    result=await runtime.tools.run(SCOPE,'default',USER,'e','message_recall',{'message_id':'123','reason':'x'})
    assert result['status']=='denied'
    assert not any(x[0]=='delete_msg' for x in runtime.bot.calls)

def test_calculator_has_no_code_execution():
    assert calculator('(2+3)*4')==20
    for value in ['__import__("os").system("x")','10**999','[1][0]','True+1']:
        with pytest.raises((ValueError,SyntaxError)): calculator(value)

async def test_private_url_rejected():
    for url in ['http://127.0.0.1/a','http://169.254.169.254/','file:///etc/passwd','https://user:pass@example.com']:
        with pytest.raises(ValueError): await public_url(url)

async def test_duplicate_and_disabled_groups_are_not_processed(runtime):
    await runtime.engine.receive(event(scope=OTHER))
    assert not runtime.db.rows('SELECT * FROM messages')
    runtime.model.configured=False
    await runtime.engine.receive(event());await runtime.engine.receive(event())
    assert len(runtime.db.recent(SCOPE,'default'))==1
    assert not runtime.model.calls

async def test_stopped_memory_member_is_not_stored(runtime):
    runtime.model.configured=False
    runtime.db.put('member',SCOPE+':'+USER,{'id':SCOPE+':'+USER,'scope':SCOPE,'user_id':USER,'remember':False})
    await runtime.engine.receive(event())
    assert not runtime.db.rows('SELECT * FROM messages')

async def test_thinking_tool_chain_is_preserved_but_not_sent(runtime):
    m = message(runtime)
    runtime.model.script=[
      {'role':'assistant','content':None,'reasoning_content':'private thinking','tool_calls':[{'id':'call1','type':'function','function':{'name':'calculate','arguments':'{"query":"2+2"}'}}]},
      {**reply_decision(m, text='是 4。'), 'reasoning_content':'private final'}]
    await runtime.engine.turn(SCOPE,[m])
    assert runtime.model.calls[1][2][-2]['reasoning_content']=='private thinking'
    assert runtime.model.calls[1][2][-1]['tool_call_id']=='call1'
    assert runtime.bot.sent[0][1][0]['data']['text']=='是 4。'
    assert 'private' not in json.dumps(runtime.bot.sent)

async def test_persona_switch_during_request_discards_old_reply(runtime):
    async def switch(*args,**kwargs):
        runtime.group['persona']='another';runtime.db.put('group',SCOPE,runtime.group)
        return {'role':'assistant','content':'{"speak":true,"intent":"reply"}'}
    runtime.model.chat=switch
    await runtime.engine.turn(SCOPE,[message(runtime)])
    assert not runtime.bot.sent

async def test_proactive_quota_and_silence(runtime):
    runtime.group['proactive_hourly']=0;runtime.db.put('group',SCOPE,runtime.group)
    runtime.model.script=[{'role':'assistant','content':'{"speak":true,"intent":"join in"}'}]
    await runtime.engine.turn(SCOPE,[message(runtime,'大家在聊天')])
    assert not runtime.bot.sent

async def test_onebot_timeout_and_reconnect_cancel_pending():
    bot=OneBot()
    class Socket:
        async def send_json(self,data): self.data=data
        async def close(self,**kw): pass
    first=Socket();await bot.connect(first,'10000001')
    with pytest.raises(ActionUnknown): await bot.call('set_group_ban',{},mutation=True,timeout=.01)
    pending=asyncio.create_task(bot.call('send_group_msg',{},mutation=True))
    await asyncio.sleep(.01)
    second=Socket();await bot.connect(second,'10000001')
    with pytest.raises(ActionUnknown): await pending
    await bot.disconnect(first)
    assert bot.socket is second

def test_login_csrf_origin_and_secret_redaction(client):
    assert client.get('/api/v1/settings').status_code==401
    headers=auth(client)
    assert client.put('/api/v1/settings',json={'admin_qq':['90000001']}).status_code==403
    assert client.put('/api/v1/settings',headers={**headers,'origin':'https://evil.example'},json={}).status_code==403
    r=client.put('/api/v1/settings',headers=headers,json={'admin_qq':['90000001'],'api_key':'secret-test-key'})
    assert r.status_code==200 and r.json()['initialized']
    assert 'secret-test-key' not in client.get('/api/v1/settings').text
    assert 'secret-test-key' not in client.get('/api/v1/audit').text
    assert client.put('/api/v1/settings',headers=headers,json={'context_tokens':-1}).status_code==422

def test_management_defaults_and_role_version_restore(client):
    h=auth(client)
    client.put('/api/v1/settings',headers=h,json={'admin_qq':['90000001'],'api_key':'x'})
    assert client.put('/api/v1/groups/'+SCOPE,headers=h,json={'name':'测试群','enabled':True}).status_code==200
    g=client.get('/api/v1/groups').json()[0]
    assert not g['tools']['member_kick']['enabled']
    p=client.get('/api/v1/personas').json()[0];p.pop('id');p.pop('version');p['name']='新名字'
    assert client.put('/api/v1/personas/default',headers=h,json=p).json()['version']==2
    restored=client.post('/api/v1/personas/default/restore/1',headers=h).json()
    assert restored['name']=='机器人' and restored['version']==3

def test_websocket_requires_token_and_account_header(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/onebot/v11/ws',headers={'x-self-id':'10000001','authorization':'Bearer wrong'}): pass
    token=client.app.state.settings.secret['onebot_token']
    with client.websocket_connect('/onebot/v11/ws',headers={'x-self-id':'10000001','authorization':'Bearer '+token}) as ws:
        ws.send_json({'post_type':'meta_event','self_id':'10000001','meta_event_type':'heartbeat'})
        assert client.app.state.onebot.online

def test_login_rate_limit(client):
    for _ in range(10):
        assert client.post('/api/v1/auth/login',json={'username':'admin','password':'bad'}).status_code==401
    assert client.post('/api/v1/auth/login',json={'username':'admin','password':'bad'}).status_code==429


@pytest.mark.parametrize('remember,seconds',[(None,12*3600),(False,12*3600),(True,30*24*3600)])
def test_login_lifetime_cookie_flags_and_server_expiry(client,remember,seconds):
    import hashlib
    from http.cookies import SimpleCookie
    client.base_url='https://testserver'
    client.app.state.settings.secure_cookie=True
    body={'username':'admin','password':'test-password-123456'}
    if remember is not None:body['remember_me']=remember
    start=time.time();r=client.post('/api/v1/auth/login',json=body)
    assert r.status_code==200
    cookie=SimpleCookie(r.headers['set-cookie'])['rolebot_session']
    assert int(cookie['max-age'])==seconds and cookie['httponly'] and cookie['secure']
    assert cookie['samesite']=='strict' and cookie['path']=='/' and cookie['domain']==''
    assert r.headers['cache-control']=='no-store'
    token=hashlib.sha256(cookie.value.encode()).hexdigest();db=client.app.state.db
    row=db.rows('SELECT * FROM sessions WHERE token=?',(token,))[0]
    assert start+seconds<=row['expires']<=time.time()+seconds
    assert row['csrf']==r.json()['csrf'] and row['token']!=cookie.value
    assert client.get('/api/v1/auth/me').status_code==200
    db.execute('UPDATE sessions SET expires=? WHERE token=?',(time.time()-1,token))
    assert client.get('/api/v1/auth/me').status_code==401  # Still has a live browser cookie.


def test_remembered_login_survives_restart_and_logout_revokes_other_copy(client):
    from fastapi.testclient import TestClient
    from rolebot.api import create_app
    r=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-123456','remember_me':True})
    token=client.cookies.get('rolebot_session')
    restarted=create_app(client.app.state.settings.data_dir,start_engine=False)
    with TestClient(restarted) as other:
        other.cookies.set('rolebot_session',token)
        assert other.get('/api/v1/auth/me').status_code==200
        result=other.post('/api/v1/auth/logout',json={},headers={'X-CSRF-Token':r.json()['csrf']})
        assert result.status_code==200 and 'Max-Age=0' in result.headers['set-cookie']
    assert client.get('/api/v1/auth/me').status_code==401


def test_public_and_explicit_legacy_login_origins(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from rolebot.api import create_app
    monkeypatch.setenv('ADMIN_PASSWORD','test-password-123456')
    monkeypatch.setenv('PUBLIC_BASE_URL','https://panel.example.com')
    monkeypatch.setenv('ADMIN_ADDITIONAL_ORIGINS','https://192.0.2.10:8443')
    app=create_app(tmp_path/'origins',start_engine=False)
    body={'username':'admin','password':'test-password-123456'}
    with TestClient(app,base_url='https://panel.example.com') as c:
        for origin in ('https://panel.example.com','https://192.0.2.10:8443'):
            r=c.post('/api/v1/auth/login',json=body,headers={'Origin':origin});assert r.status_code==200
            assert c.post('/api/v1/auth/logout',json={},headers={'Origin':origin,'X-CSRF-Token':r.json()['csrf']}).status_code==200
        for origin in ('https://napcat.example.com','https://panel.example.com.evil.test','http://panel.example.com','https://panel.example.com:444','null','https://panel.example.com/extra'):
            assert c.post('/api/v1/auth/login',json=body,headers={'Origin':origin,'X-Forwarded-Host':'panel.example.com'}).status_code==403
    monkeypatch.setenv('ADMIN_ADDITIONAL_ORIGINS','https://*.114600.xyz')
    with pytest.raises(ValueError,match='通配符'):create_app(tmp_path/'invalid-origin',start_engine=False)


@pytest.mark.parametrize('remember',['true','false',1,2592000,None])
def test_login_rejects_invalid_remember_flag(client,remember):
    r=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-123456','remember_me':remember})
    assert r.status_code==422 and not client.app.state.db.rows('SELECT * FROM sessions')

async def test_same_command_event_does_not_send_twice(runtime):
    e=event('/bot status',user='90000001')
    await runtime.engine.receive(e);await runtime.engine.receive(e)
    assert len(runtime.bot.sent)==1

async def test_three_group_replay_deduplicates_1000_events(runtime):
    runtime.model.configured=False
    scopes=[SCOPE,OTHER,'10000001:g:20000003']
    for s in scopes:
        g=group_defaults(s);g['enabled']=True;runtime.db.put('group',s,g)
    for i in range(1000):
        e=event('合成消息 '+str(i),id=i,scope=scopes[i%3])
        await runtime.engine.receive(e)
        if i%10==0: await runtime.engine.receive(e)
    assert runtime.db.rows('SELECT COUNT(*) AS n FROM messages')[0]['n']==1000
    assert not runtime.model.calls
    for s in scopes:
        assert all(x['scope']==s for x in runtime.db.recent(s,'default'))

def test_settings_cannot_change_tool_authority_or_invalid_prices(client):
    h=auth(client)
    assert client.put('/api/v1/settings',headers=h,json={'onebot_token':'injected'}).status_code==422
    assert client.put('/api/v1/settings',headers=h,json={'prices':{'input':0}}).status_code==422
    assert client.put('/api/v1/settings',headers=h,json={'admin_qq':['not-a-qq']}).status_code==422

def test_unauthenticated_upload_is_rejected_before_image_processing(client):
    response=client.post('/api/v1/stickers',files={'file':('x.jpg',b'not an image','image/jpeg')})
    assert response.status_code==401

async def test_opted_out_reply_keeps_only_quote_metadata(runtime):
    m=normalize(event('机器人，临时告诉你一件事'));m.update(persona='default',remember=False)
    runtime.model.script=[reply_decision(m, text='这件事只在本轮对话里处理。')]
    await runtime.engine.turn(SCOPE,[m])
    assert not runtime.db.recent(SCOPE,'default')
    stored=runtime.db.rows('SELECT * FROM messages')
    assert len(stored)==1 and stored[0]['text']=='' and stored[0]['excluded']==1
    reply=normalize(event('收到',id=2));reply['replies']=['501']
    assert runtime.engine.direct([reply],runtime.db.get('persona','default'))

def test_nested_transactions_rollback_all_writes(db):
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.put("test", "a", {"value": 1})
            db.save_memory(SCOPE, "", "应回滚")
            raise RuntimeError()
    assert not db.get("test", "a") and not db.memories(SCOPE)

def test_message_sequence_does_not_reset_when_retention_deletes_all(db):
    db.add_message("a", SCOPE, "default", USER, "user", "a")
    first = db.message_batch(SCOPE, "default")[0]["seq"]
    db.execute("DELETE FROM messages")
    db.add_message("b", SCOPE, "default", USER, "user", "b")
    assert db.message_batch(SCOPE, "default", first)[0]["id"] == "b"

def test_normalization_retains_metadata_and_marks_large_inputs():
    e = event("x"*13000);e["time"] = time.time()-5
    e["message"].insert(0, {"type": "reply", "data": {"id": "42"}})
    e["message"].append({"type": "at", "data": {"qq": SECOND}})
    m = normalize(e)
    assert m["truncated"] and len(m["text"]) == 12000
    assert m["replies"] == ["42"] and m["mentions"] == [SECOND] and m["sent_at"] == e["time"]

@pytest.mark.parametrize("continuation,actor,expect",[(True,USER,True),(False,USER,False),(True,SECOND,False)])
async def test_followup_requires_semantic_confirmation_and_matching_actor(runtime,continuation,actor,expect):
    r=runtime;r.group["proactive_hourly"]=0;r.db.put("group",SCOPE,r.group)
    r.db.put("followup",SCOPE+":default",{"target":USER,"topic":"游戏","expires":time.time()+120})
    m=source(r,"然后呢？",1,actor=actor)
    r.model.script=[participation(), reply_decision(m,mode="followup",continues_dialogue=continuation, text="接着往前走")]
    await r.engine.turn(SCOPE,[m])
    assert bool(r.bot.sent)==expect

async def test_final_review_cancels_an_outdated_reply(runtime):
    r=runtime;m=source(r,"机器人，帮我解释这个",1)
    async def chat(scope,purpose,messages,**kwargs):
        if purpose=="chat":
            new=source(r,"不用了，我已经明白",2);r.engine.recent_events[SCOPE]=[m,new]
            return reply_decision(m, text="准备发送的解释")
        if messages[0]["content"].startswith("复核准备发送"):return {"content":"{\"send\":false,\"reason\":\"对方已取消\"}"}
        return reply_decision(m)
    r.model.chat=chat
    await r.engine.turn(SCOPE,[m]);assert not r.bot.sent
    assert r.db.all("trace")[0]["status"]=="stale"

async def test_repeated_reply_is_suppressed(runtime):
    r=runtime
    for i in range(2):
        m=source(r,"机器人你好",i)
        r.model.script=[reply_decision(m, text="你好")]
        await r.engine.turn(SCOPE,[m])
    assert len(r.bot.sent)==1
    assert any(t["status"]=="duplicate" for t in r.db.all("trace"))

async def test_opt_out_trace_and_derived_context_do_not_persist_text(runtime):
    r=runtime;m=normalize(event("机器人，临时秘密内容"));m.update(persona="default",remember=False)
    r.model.script=[reply_decision(m, text="临时秘密答复")]
    await r.engine.turn(SCOPE,[m])
    assert "临时秘密" not in str(r.db.all("trace"))
    assert not r.db.recent(SCOPE,"default")
    assert r.db.all("trace")[0]["redacted"]


async def test_plain_reply_recovers_once_without_tools(runtime):
    r = runtime
    m = source(r, '机器人你好', 1)
    r.model.script = [{'content': '你好呀'}, reply_decision(m, '你好呀')]
    await r.engine.turn(SCOPE, [m])
    assert len(r.bot.sent) == 1
    assert len(r.model.calls) == 2
    call = r.model.calls[-1]
    assert call[3]['stage'] == 'conversation.reply_repair'
    assert call[3]['max_tokens'] == 8192 and not call[3].get('tools')
    assert r.db.all('trace')[0]['status'] == 'sent'


async def test_two_invalid_responses_send_one_fallback(runtime):
    r = runtime
    m = source(r, '机器人你好', 1)
    r.model.script = [{'content': '你好'}, {'content': '依然不是JSON'}]
    await r.engine.turn(SCOPE, [m])
    assert len(r.model.calls) == 2 and len(r.bot.sent) == 1
    assert '没整理好' in r.bot.sent[0][1][0]['data']['text']
    assert not r.bot.calls


async def test_repair_cannot_repeat_or_invent_tool_success(runtime):
    r = runtime
    m = source(r, '机器人同意了', 1)
    r.model.script = [reply_decision(m, '已经设置成功了')]
    effects = [{'name':'member_title','status':'denied','error_code':'request_verification_failed'},
               {'name':'member_find','status':'success','result':{'members':[]}}]
    result = await recover(r.engine,SCOPE,[m],r.db.get('persona','default'),{'content':'普通文字'},
                           effects,[{'role':'assistant','content':'普通文字'}],lambda:True,128000)
    assert '没执行' in result['text'] and '权限' not in result['text']
    assert len(r.model.calls) == 1 and not r.bot.calls and not r.bot.sent


@pytest.mark.parametrize('failure', ['exception','tool_call','invalid_target'])
async def test_repair_failure_has_bounded_fallback(runtime, failure):
    r = runtime
    m = source(r,'机器人你好',1)
    if failure == 'exception':
        async def failed(*args,**kwargs):raise RuntimeError('provider unavailable')
        r.model.chat = failed
    elif failure == 'tool_call':
        r.model.script = [{'tool_calls':[{'function':{'name':'member_title'}}]}]
    else:
        r.model.script = [{'content':json.dumps({'speak':True,'text':'错发','reply_to_message_id':'other-group'})}]
    result = await recover(r.engine,SCOPE,[m],r.db.get('persona','default'),None,[],[],lambda:True,128000)
    assert result['reply_to_message_id'] == m['id'] and '没整理好' in result['text']
    assert not r.bot.calls


async def test_recovery_obeys_revocation_and_ambiguous_targets(runtime):
    r = runtime
    a, b = source(r,'机器人你好',1), source(r,'机器人你好',2,actor=SECOND)
    r.model.script = [{'content':'invalid'}]
    assert await recover(r.engine,SCOPE,[a,b],r.db.get('persona','default'),None,[],[],lambda:True,128000) is None
    calls = len(r.model.calls)
    assert await recover(r.engine,SCOPE,[a],r.db.get('persona','default'),None,[],[],lambda:False,128000) is None
    assert len(r.model.calls) == calls


async def test_prepared_reply_survives_invalid_final_format(runtime):
    r = runtime
    r.group['interaction_enabled']=True;r.db.put('group',SCOPE,r.group)
    m=source(r,'机器人你好',1)
    r.model.script=[{'role':'assistant','tool_calls':[{'id':'c1','function':{'name':'message_send','arguments':json.dumps({'text':'已准备好的台词','source_message_id':m['id']})}}]},
                    {'content':'invalid'},{'content':'still invalid'}]
    await r.engine.turn(SCOPE,[m])
    assert len(r.bot.sent)==1 and r.bot.sent[0][1][0]['data']['text']=='已准备好的台词'
    assert len([x for x in r.db.rows("SELECT data FROM audit WHERE kind='tool'") if json.loads(x['data']).get('name')=='message_send'])==1


@pytest.mark.parametrize('status,word',[('denied','没执行'),('unknown','不会自动重试'),('error','还没确认'),('success','不会重复执行')])
def test_fallback_keeps_operation_outcomes_distinct(status,word):
    value=failure_text([{'name':'member_title','status':status,'error_code':'request_context_unclear'},
                        {'name':'member_find','status':'success'}])
    assert word in value
def test_new_database_has_no_persona_content_or_account_bindings(tmp_path):
    from rolebot.db import Database
    db=Database(tmp_path/'fresh.sqlite')
    try:
        card=db.get('persona','default')
        assert card['name']=='机器人'
        assert card['prompt']==card['description']==''
        assert card['aliases']==[] and card['relationships']=={}
        assert db.config()['admin_qq']==[]
        assert db.all('group')==[] and db.all('creation_asset')==[]
    finally:db.close()


def test_blank_persona_can_be_saved_without_inventing_content(authed_client):
    c=authed_client
    data={'name':'新配置','description':'','prompt':'','aliases':[],'relationships':{}}
    response=c.put('/api/v1/personas/default',json=data)
    assert response.status_code==200,response.text
    assert response.json()['prompt']=='' and response.json()['relationships']=={}
    data['name']=''
    assert c.put('/api/v1/personas/default',json=data).status_code==422
