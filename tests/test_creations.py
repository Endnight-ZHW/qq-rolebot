from support import tool_reply
import asyncio
import hashlib
import io
import json
import time
from pathlib import Path

from PIL import Image
import pytest

from rolebot.creations import CreationConfig, parse_creation_code
from rolebot.creation_contract import MIB, output_names, validate_spec
from rolebot.providers import ModelConfigWrite
from rolebot.onebot import ActionUnknown
from support import SCOPE, OTHER, USER, SECOND, source, auth


SPEC={'runtime':'python','format':'png','width':200,'height':100,'duration':0,'fps':1,'pages':1,
      'code':'def render(t,width,height,data,assets,page): pass','caption':'对比图做好啦。'}


def test_empty_asset_library_is_a_supported_default(tmp_path,monkeypatch):
    from rolebot import art_assets
    monkeypatch.setattr(art_assets,'ROOT',tmp_path/'absent-pack')
    assert art_assets.catalog('default')['assets']==[]
    with pytest.raises(ValueError,match='不存在'):art_assets.read('art:missing')


async def test_creation_succeeds_without_any_builtin_assets(creative,tmp_path,monkeypatch):
    from rolebot import art_assets
    monkeypatch.setattr(art_assets,'ROOT',tmp_path/'no-assets')
    job,_=await create(creative)
    await creative.service.run(job['id'])
    assert creative.service.get(job['id'])['status']=='sent'
    assert len(creative.bot.sent)==2


def test_character_lookup_uses_current_names_and_escapes_regex(creative):
    r=creative;card=r.db.get('persona','default')
    card.update(name='R(1)+',aliases=['示例助理'])
    r.db.put('persona','default',card)
    lib=r.service.art_library
    assert lib.character_mode('做图','示例助理，请对比这两个产品',persona='default')=='auto'
    assert lib.character_mode('画R(1)+的头像','',persona='default')=='current'
    assert lib.character_mode('画机器人','',persona='default')=='auto'
    assert lib.character_mode('不要R(1)+，只要图表','',persona='default')=='none'
    from rolebot.persona_names import strip_address
    assert strip_address('示例助理，取消制作',r.db,'default')=='取消制作'


class Renderer:
    configured=True
    def __init__(self):self.jobs={};self.cancelled=[];self.after_download=None
    async def health(self):return {'ready':True,'can_start':True}
    async def submit(self,id,spec,data,assets,timeout):
        files={}
        for name in output_names(spec):
            buf=io.BytesIO();Image.new('RGB',(spec['width'],spec['height']),'blue').save(buf,'PNG')
            raw=buf.getvalue()
            files[name]={'name':name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
                         'mime':'image/png','width':spec['width'],'height':spec['height'],'raw':raw}
        self.jobs[id]=files
    async def status(self,id):
        return {'state':'complete','artifacts':[{k:v for k,v in item.items() if k!='raw'} for item in self.jobs[id].values()],
                'peak_memory_bytes':100*MIB,'duration_seconds':1}
    async def download(self,id,item,target):
        target.write_bytes(self.jobs[id][item['name']]['raw'])
        if self.after_download:self.after_download()
    async def cancel(self,id):self.cancelled.append(id)


@pytest.fixture
def creative(runtime,sample_art):
    r=runtime;r.service=r.engine.creations;r.service.renderer=Renderer()
    r.db.put('creation_config','global',CreationConfig(enabled=True).model_dump())
    for name in ('creation_create','creation_status','creation_cancel'):
        r.group['tools'][name].update(enabled=True,request_access={'member':'all','admin':'all'})
    r.db.put('group',SCOPE,r.group)
    r.model.script=[{'role':'assistant','content':json.dumps(SPEC)},
                    {'role':'assistant','content':'{"approved":true,"relevant":true,"reason":"文字清楚"}'}]
    return r


async def create(r,id=1,actor=USER,**args):
    message=source(r,'机器人，做一张产品对比图',id,actor)
    result=await r.service.create(SCOPE,'default',actor,{'brief':'显示已核对的数据','data':{'rows':[{'name':'甲','value':10}]},**args},message)
    return r.service.get(result['job_id']),message


async def test_creation_returns_queue_then_sends_text_and_picture_separately(creative):
    r=creative;job,_=await create(r)
    assert job['status']=='queued' and not r.bot.sent and not r.model.calls
    await r.service.run(job['id'])
    live=r.service.get(job['id'])
    assert live['status']=='sent' and live['model_calls']==2 and len(r.bot.sent)==2
    assert [s['type'] for s in r.bot.sent[0][1]]==['reply','text']
    assert [s['type'] for s in r.bot.sent[1][1]]==['image']
    assert r.bot.sent[1][1][0]['data']['sub_type']==0
    assert all(call[1]=='creation' for call in r.model.calls)
    assert r.service.renderer.cancelled
    assert r.db.get('group',SCOPE)['tools']==r.group['tools']


async def test_builtin_art_assets_keep_alpha_and_reach_creation_model(creative):
    import base64
    from rolebot import art_assets
    r=creative;job,_=await create(r,asset_ids=['art:grid','art:character_front'])
    payload=r.service.payload(job)
    assert [a['id'] for a in payload['asset_manifest']]==['art:grid','art:character_front']
    raw=base64.b64decode(payload['assets']['asset_1.png'])
    with Image.open(io.BytesIO(raw)) as im:assert im.getchannel('A').getextrema()==(0,255)
    await r.service.run(job['id'])
    content=r.model.calls[0][2][-1]['content']
    request=json.loads(content[0]['text'])
    assert sum(x['type']=='image_url' for x in content)==2
    assert request['asset_manifest']==payload['asset_manifest'] and request['palette']==art_assets.catalog()['palette']
    assert r.service.get(job['id'])['status']=='sent'
    child,_=await create(r,2,parent_job_id=job['id'])
    assert r.service.payload(child)['builtin_assets']==payload['builtin_assets']
    assert r.service.payload(child)['asset_manifest']==payload['asset_manifest']


async def test_art_asset_change_stops_queued_job_and_unknown_ids_are_rejected(creative,monkeypatch):
    from rolebot import art_assets
    r=creative;job,_=await create(r,asset_ids=['art:triangle'])
    monkeypatch.setattr(r.service.art_library,'current',lambda ref,persona:False)
    await r.service.run(job['id'])
    assert not r.model.calls and not r.bot.sent and not r.service.renderer.jobs
    for name in ('art:../secrets','art:missing'):
        with pytest.raises(ValueError,match='不存在'):await create(r,2,asset_ids=[name])


def test_synthetic_art_library_is_readable_but_not_packaged(sample_art):
    import sys
    from rolebot import art_assets
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
    from remote import managed_files
    root=Path(__file__).resolve().parents[1]
    shipped={p.relative_to(root).as_posix() for p in managed_files(root)}
    book=art_assets.manifest();assert book['origin']=='synthetic-test'
    for row in book['assets']:
        item,raw=art_assets.read(row['id'],'default')
        assert len(raw)<1024*1024
        with Image.open(io.BytesIO(raw)) as im:assert im.size==(item['width'],item['height'])
        assert 'backend/rolebot/art_library/'+row['file'] not in shipped
    assert 'backend/rolebot/art_library/manifest.json' not in shipped


def test_character_art_cannot_be_used_by_other_personas(sample_art):
    from rolebot import art_assets
    from xml.etree import ElementTree as ET
    chars=[a for a in art_assets.catalog('default')['assets'] if a['kind']=='character']
    assert len(chars)==2
    assert not any(a['kind']=='character' for a in art_assets.catalog('other')['assets'])
    for char in chars:
        item,raw=art_assets.read(char['id'],'default')
        with pytest.raises(ValueError,match='当前角色'):art_assets.read(char['id'],'other')
        assert art_assets.current(item,'default') and not art_assets.current(item,'other')
        with Image.open(io.BytesIO(raw)) as im:assert im.getchannel('A').getextrema()==(0,255)
        root=ET.fromstring((art_assets.ROOT/'sources'/item['source']).read_text('utf8'))
        assert not any(e.tag.rsplit('}',1)[-1] in ('image','script','foreignObject') for e in root.iter())
        assert {'shape'}<={e.get('id') for e in root.iter()}


async def test_character_request_selects_lying_svg_without_chat_asset_ids(creative):
    r=creative;text='做一个助手抱着小鱼干躺在床上的场景';message=source(r,text,21)
    result=await r.service.create(SCOPE,'default',USER,{'brief':text},message)
    job=r.service.get(result['job_id']);payload=r.service.payload(job)
    assert 'art:character_horizontal' in job['asset_ids']
    assert payload['character_required'] and 'source_0.svg' in payload['assets']
    assert payload['asset_manifest'][0]['vector_file']=='source_0.svg'
    r.model.script[1]['content']='{"approved":true,"character_consistent":true,"relevant":true}'
    await r.service.run(job['id']);assert r.service.get(job['id'])['status']=='sent'
    generation=r.model.calls[0][2][-1]['content'];request=json.loads(generation[0]['text'])
    assert request['asset_manifest'][0]['kind']=='character'
    check=r.model.calls[1][2][-1]['content']
    assert sum(b['type']=='image_url' for b in check)==2  # Character reference, then actual render.


async def test_asset_lookup_is_on_demand_and_never_creates_a_job(creative):
    r=creative;m=source(r,'看看躺卧助手素材',23)
    result=await r.service.create(SCOPE,'default',USER,{'brief':'侧躺 抱鱼','operation':'list_assets'},m)
    assert result['status']=='assets' and any(a['id']=='art:character_horizontal' for a in result['assets'])
    assert not r.db.all('creation_job') and not r.model.calls and not r.bot.sent


@pytest.mark.parametrize('text,mode,expected',[
    ('[@QQ:10000001] 助手制作一个表格对比呀','none',False),
    ('[@QQ:10000001] 助手帮我对比一下两款产品','auto',False),
    ('小助手，帮我画一张参数表','auto',False),
    ('画一个助手抱着小鱼干躺在床上的场景','auto',True),
    ('对比表右上角放一个招手助手','current',True),
    ('对比表右上角放一个招手助手','none',False),
])
def test_character_subject_is_not_the_bot_addressee(creative,text,mode,expected):
    selected,needs=creative.service.art_library.select('default','制作作品',text,character=mode)
    assert needs is expected
    assert bool(selected) is expected


async def test_character_none_survives_revision_and_reaches_both_models(creative):
    r=creative;parent,_=await create(r,asset_ids=['art:character_front','art:dark_background'])
    await r.service.run(parent['id'])
    child,_=await create(r,2,parent_job_id=parent['id'],character='none',asset_ids=['art:character_front'])
    payload=r.service.payload(child)
    assert payload['asset_ids']==['art:dark_background'] and payload['character']=='none' and not payload['character_required']
    assert set(payload['assets'])=={'asset_0.png','source_0.svg'}
    assert payload['asset_manifest'][0]['file']=='asset_0.png'
    assert [a['id'] for a in payload['builtin_assets']]==['art:dark_background']
    r.model.calls.clear()
    r.model.script=[{'role':'assistant','content':json.dumps(SPEC)},
                    {'role':'assistant','content':'{"approved":true,"relevant":true,"unwanted_character":false}'}]
    await r.service.run(child['id'])
    assert r.service.get(child['id'])['status']=='sent'
    for call in r.model.calls:
        content=call[2][-1]['content'];data=json.loads(content[0]['text'] if isinstance(content,list) else content)
        assert data['character']=='none'


async def test_maker_cannot_load_character_when_none_and_review_repairs_its_own_mascot(creative):
    r=creative;job,_=await create(r,character='none')
    r.model.script=[tool_reply('art_read', {'ids':['art:character_front']}, id='read'),
        {'role':'assistant','content':json.dumps(SPEC)},
        {'role':'assistant','content':'{"approved":true,"relevant":true,"unwanted_character":true,"reason":"右上角有人物头像"}'},
        {'role':'assistant','content':json.dumps(SPEC)},
        {'role':'assistant','content':'{"approved":true,"relevant":true,"unwanted_character":false}'}]
    await r.service.run(job['id'])
    assert r.service.get(job['id'])['status']=='sent' and len(r.model.calls)==5
    assert not r.service.payload(job)['assets']
    assert '不读取人物素材' in r.model.calls[1][2][-1]['content']
    assert not any(a['kind']=='character' for a in r.service.art_library.search('default','助手',include_characters=False))


async def test_maker_loads_extra_assets_only_after_explicit_read(creative):
    r=creative;job,_=await create(r)
    assert not r.service.payload(job)['assets']
    def call(id,name,args):return tool_reply(name, args, id=id)
    r.model.script=[call('search','art_search',{'query':'夜晚星空'}),call('read','art_read',{'ids':['art:dark_background']}),
                    {'role':'assistant','content':json.dumps(SPEC)},{'role':'assistant','content':'{"approved":true,"relevant":true}'}]
    await r.service.run(job['id'])
    assert r.service.get(job['id'])['status']=='sent' and len(r.model.calls)==4
    assert r.service.payload(job)['asset_ids']==['art:dark_background']
    assert not any(isinstance(m.get('content'),list) and any(b['type']=='image_url' for b in m['content']) for m in r.model.calls[0][2])
    assert any(isinstance(m.get('content'),list) and any(b['type']=='image_url' for b in m['content']) for m in r.model.calls[2][2])


async def test_disabled_or_deleted_asset_stops_pending_work_and_does_not_reappear(creative):
    r=creative;lib=r.service.art_library;job,_=await create(r,asset_ids=['art:character_front'])
    row=lib.edit('art:character_front',{'enabled':False},0)
    await r.service.run(job['id']);assert not r.model.calls and not r.bot.sent
    assert not any(a['id']==row['id'] for a in lib.listing('default'))
    with pytest.raises(RuntimeError):lib.edit(row['id'],{'enabled':True},0)
    removed=lib.delete(row['id'],row['revision'])
    from rolebot.art_library_store import ArtLibrary
    again=ArtLibrary(r.service)
    assert not any(a['id']==row['id'] for a in again.listing('default'))
    again.edit(row['id'],{'deleted':False,'enabled':True},removed['revision'])
    assert again.item(row['id'],'default')['enabled']


@pytest.mark.parametrize('svg',[
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><script>alert(1)</script></svg>',
    '<svg viewBox="0 0 100 100"><use href="https://example.com/a.svg#x"/></svg>',
    '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///secret">]><svg viewBox="0 0 100 100"/>',
    '<svg viewBox="0 0 100 100"><path onload="alert(1)"/></svg>',
    '<svg viewBox="0 0 100 100"><style>@import "http://example.com";</style></svg>',
])
def test_uploaded_svg_rejects_active_or_external_content(svg):
    from rolebot.art_library_store import svg_source
    with pytest.raises(ValueError):svg_source(svg.encode())


def test_art_management_upload_scope_conflict_delete_and_source_download(authed_client,sample_art):
    c=authed_client;buf=io.BytesIO();Image.new('RGBA',(120,90),(30,100,180,100)).save(buf,'PNG')
    response=c.post('/api/v1/creation-assets',files={'file':('demo.png',buf.getvalue(),'image/png')},
        data={'label':'上传测试','kind':'character','persona':'default','usage':'坐姿参考'})
    assert response.status_code==200,response.text
    row=response.json();id=row['id']
    assert c.get('/api/v1/creation-assets/'+id+'/file').content==buf.getvalue()
    assert c.put('/api/v1/creation-assets/'+id,json={'expected_revision':0,'enabled':False}).status_code==409
    changed=c.put('/api/v1/creation-assets/'+id,json={'expected_revision':row['revision'],'enabled':False})
    assert changed.status_code==200 and not changed.json()['enabled']
    service=c.app.state.engine.creations
    with pytest.raises(ValueError):service.art_library.read(id,'default')
    assert c.get('/api/v1/creation-assets/art:character_front/file?source=true').headers['content-type'].startswith('image/svg+xml')
    assert c.delete('/api/v1/creation-assets/'+id+'?expected_revision='+str(changed.json()['revision'])).status_code==200
    assert not service.art_library.path(row).exists()
    assert c.get('/api/v1/creation-assets/'+id+'/file').status_code==400


def test_svg_upload_renders_preview_and_only_exposes_source_as_download(authed_client):
    c=authed_client;service=c.app.state.engine.creations;service.renderer=Renderer()
    raw=b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><g id="body"><circle cx="50" cy="50" r="30" fill="red"/></g></svg>'
    response=c.post('/api/v1/creation-assets',files={'file':('safe.svg',raw,'image/svg+xml')},data={'label':'矢量测试','kind':'prop'})
    assert response.status_code==200,response.text
    row=response.json();assert row['layers']==['body'] and not service.asset_preview_busy and service.renderer.cancelled
    assert c.get('/api/v1/creation-assets/'+row['id']+'/file').headers['content-type']=='image/png'
    downloaded=c.get('/api/v1/creation-assets/'+row['id']+'/file?source=true')
    assert downloaded.content==raw and 'attachment' in downloaded.headers['content-disposition']
    assert 'sandbox' in downloaded.headers['content-security-policy']


async def test_character_consistency_failure_cannot_be_approved_by_missing_flag(creative):
    r=creative;m=source(r,'画助手抱鱼',31)
    created=await r.service.create(SCOPE,'default',USER,{'brief':'助手抱鱼'},m)
    r.model.script=[{'role':'assistant','content':json.dumps(SPEC)},{'role':'assistant','content':'{"approved":true,"relevant":true}'}]*3
    await r.service.run(created['job_id'])
    assert r.service.get(created['job_id'])['status']=='failed'
    assert not any(s['type']=='image' for _,segments in r.bot.sent for s in segments)


@pytest.mark.parametrize('approved',[True,False])
async def test_failed_validation_has_three_repairs_and_a_separate_hard_call_cap(creative,approved):
    r=creative;job,_=await create(r)
    r.model.script=[
        {'role':'assistant','content':json.dumps(SPEC)},
        {'role':'assistant','content':'{"approved":false,"reason":"标题被裁切"}'},
        {'role':'assistant','content':json.dumps(SPEC)},
        {'role':'assistant','content':'{"approved":false,"reason":"图例仍然遮挡正文"}'},
        {'role':'assistant','content':json.dumps(SPEC)},
        {'role':'assistant','content':'{"approved":false,"reason":"图例仍需调整"}'},
        {'role':'assistant','content':json.dumps(SPEC)},
        {'role':'assistant','content':json.dumps({'approved':approved,'relevant':True,'reason':'检查结果'})}]
    await r.service.run(job['id'])
    assert r.service.get(job['id'])['status']==('sent' if approved else 'failed')
    assert len(r.model.calls)==8
    live=r.service.get(job['id']);live['model_calls']=16
    with pytest.raises(ValueError,match='十六次'):await r.service.call_model(live,'creation.code',[])
    assert len(r.model.calls)==8


@pytest.mark.parametrize('fenced',[False,True])
def test_creation_code_accepts_literal_whitespace_without_rewriting_source(fenced):
    code='def render(t,width,height,data,assets,page):\n\treturn "line\\nwith quotes"\n'
    data={**SPEC,'code':code}
    raw=json.dumps(data).replace('\\n','\n',1).replace('\\t','\t',1)
    if fenced:raw='```json\n'+raw+'\n```'
    assert parse_creation_code(raw)==data


@pytest.mark.parametrize('raw',['{"code":"unfinished','{"code":"bad\x00value"}','{"code":"ok",}','[]'])
def test_creation_code_does_not_guess_incomplete_or_invalid_json(raw):
    with pytest.raises(ValueError):parse_creation_code(raw)


async def test_visual_feedback_and_preview_survive_following_format_error(creative):
    r=creative;job,_=await create(r)
    r.model.script=[{'role':'assistant','content':json.dumps(SPEC)},
        {'role':'assistant','content':'{"approved":false,"reason":"核心动作需要更清楚"}'},
        {'role':'assistant','content':'{"code":"unfinished'},
        {'role':'assistant','content':json.dumps(SPEC)},
        {'role':'assistant','content':'{"approved":true,"relevant":true}'}]
    await r.service.run(job['id'])
    assert r.service.get(job['id'])['status']=='sent' and len(r.model.calls)==5
    for index in (2,3):
        content=r.model.calls[index][2][-1]['content']
        assert isinstance(content,list) and any(block['type']=='image_url' for block in content)
        request=json.loads(content[0]['text'])
        assert request['current_spec']=={k:v for k,v in SPEC.items() if k!='code'}
        assert any(m.get('role')=='assistant' and m.get('content')==json.dumps(SPEC) for m in r.model.calls[index][2][:-1])
        assert request['feedback_history'][0]['stage']=='checking'
        assert json.loads(request['feedback_history'][0]['reason'])['reason']=='核心动作需要更清楚'
        assert len(request['feedback_history'])==(1 if index==2 else 2)


async def test_raw_newline_code_does_not_spend_repair_budget(creative):
    r=creative;job,_=await create(r)
    spec={**SPEC,'code':'def render(*args):\n    pass'}
    r.model.script=[{'role':'assistant','content':json.dumps(spec).replace('\\n','\n')},
                    {'role':'assistant','content':'{"approved":true,"relevant":true}'}]
    await r.service.run(job['id'])
    assert r.service.get(job['id'])['status']=='sent' and len(r.model.calls)==2


@pytest.mark.parametrize('change',['permissions','group','persona','forget'])
async def test_revocation_before_execution_never_renders_or_sends(creative,change):
    r=creative;job,message=await create(r)
    if change=='permissions':
        r.group['tools']['creation_create']['request_access']['member']='disabled';r.db.put('group',SCOPE,r.group)
    elif change=='group':r.group['paused']=True;r.db.put('group',SCOPE,r.group)
    elif change=='persona':
        card=r.db.get('persona','default');card['version']+=1;r.db.put('persona','default',card)
    else:r.db.invalidate_memory(SCOPE,source_ids=[message['id']])
    if change!='forget':await r.service.run(job['id'])
    assert not r.service.renderer.jobs and not r.model.calls and not r.bot.sent
    if change=='forget':assert not r.service.folder(job['id']).exists()


async def test_forgetting_during_output_download_cannot_resurrect_data(creative):
    r=creative;job,message=await create(r)
    r.service.renderer.after_download=lambda:r.db.invalidate_memory(SCOPE,source_ids=[message['id']])
    await r.service.run(job['id'])
    live=r.service.get(job['id'])
    assert live['deleted'] and not live['artifacts'] and not r.bot.sent
    assert not r.service.folder(job['id']).exists()


async def test_private_job_keeps_no_source_files_and_cleans_outputs(creative):
    r=creative;message=source(r,'PRIVATE SENTINEL',1)
    message['remember']=False;r.db.execute('DELETE FROM messages WHERE id=?',(message['id'],))
    r.db.put('member',SCOPE+':'+USER,{'remember':False})
    result=await r.service.create(SCOPE,'default',USER,{'brief':'PRIVATE SENTINEL'},message)
    id=result['job_id']
    assert not r.service.folder(id).exists()
    await r.service.run(id)
    assert not r.service.folder(id).exists() and not r.service.private and not r.service.private_files
    assert 'PRIVATE SENTINEL' not in json.dumps(r.db.all('creation_job'))
    assert all(t['redacted'] for t in r.db.all('trace'))


async def test_ownership_queue_limit_and_cancellation(creative):
    r=creative;job,_=await create(r)
    with pytest.raises(ValueError,match='未完成'):await create(r,2)
    with pytest.raises(ValueError):r.service.cancel(job['id'],OTHER,USER)
    with pytest.raises(ValueError):r.service.cancel(job['id'],SCOPE,SECOND)
    r.service.cancel(job['id'],SCOPE,USER)
    assert r.service.get(job['id'])['status']=='cancelled'
    for i in range(r.service.config()['queue_limit']):await create(r,i+3,str(30000002+i))
    with pytest.raises(ValueError,match='队列'):await create(r,18,'30000018')


async def test_unknown_send_is_journaled_without_retry(creative):
    r=creative;job,_=await create(r);r.bot.unknown=True
    await r.service.run(job['id'])
    assert r.service.get(job['id'])['status']=='unknown' and len(r.bot.sent)==1
    r.service.recover()
    assert r.service.get(job['id'])['status']=='unknown' and len(r.bot.sent)==1


async def test_recovery_only_preserves_queued_and_ready_jobs(creative):
    r=creative;job,_=await create(r)
    for status,expected in [('generating','interrupted'),('rendering','interrupted'),('sending','unknown'),('queued','queued')]:
        job['status']=status;r.db.put('creation_job',job['id'],job)
        r.service.recover()
        assert r.service.get(job['id'])['status']==expected


async def test_expiry_and_favorites_respect_retention_and_forgetting(creative):
    r=creative;job,message=await create(r);await r.service.run(job['id'])
    r.service.favorite(job['id'],True)
    live=r.service.get(job['id']);live['expires']=time.time()-1;r.service.save(live)
    r.service.cleanup();assert r.service.folder(job['id']).exists()
    r.service.favorite(job['id'],False);r.service.cleanup()
    assert not r.service.folder(job['id']).exists()
    assert r.service.get(job['id'])['cleanup_reason']=='作品已到保存期限'


@pytest.mark.parametrize('patch',[{'width':5000},{'width':4096,'height':4096},{'format':'mp4','duration':21},
    {'format':'gif','duration':8,'fps':13},{'runtime':'shell'},{'code':''},{'pages':4},{'unexpected':1}])
def test_render_contract_rejects_unbounded_or_unknown_operations(patch):
    with pytest.raises(ValueError):validate_spec({**SPEC,**patch})


def test_creation_model_route_is_optional_and_legacy_writes_preserve_it(model):
    cfg=model.catalog.get()
    assert 'creation' not in cfg['routes']
    assert model.catalog.resolve('creation')['route']['reasoning_effort']=='high'
    assert model.catalog.resolve('creation')['route']['max_tokens']==65536
    cfg['routes']['creation']={'model':cfg['routes']['chat']['model'],'reasoning_effort':'high','max_tokens':4096}
    model.catalog.save(ModelConfigWrite.model_validate(cfg))
    del cfg['routes']['creation']
    model.catalog.save(ModelConfigWrite.model_validate(cfg))
    assert model.catalog.get()['routes']['creation']['max_tokens']==4096
    assert model.catalog.ready()


def test_creation_admin_api_auth_and_config_conflict(client):
    assert client.get('/api/v1/creations').status_code==401
    client.headers.update(auth(client))
    value=client.get('/api/v1/creations/settings').json()['config']
    assert not value['enabled']
    value.update(context_tokens=256000,request_hourly=24,proactive_hourly=4,storage_mib=10240,favorites_mib=4096,
                 queue_limit=8,queue_seconds=600,run_seconds=600,proactive_cooldown=300)
    assert client.put('/api/v1/creations/settings?expected_revision=0',json=value).status_code==200
    assert client.put('/api/v1/creations/settings?expected_revision=0',json=value).status_code==409
    saved=client.get('/api/v1/creations/settings').json()['config']
    assert all(saved[k]==v for k,v in value.items() if k!='revision')
    saved['context_tokens']=300000
    assert client.put('/api/v1/creations/settings?expected_revision=1',json=saved).status_code==200
    del saved['context_tokens']
    assert client.put('/api/v1/creations/settings?expected_revision=2',json=saved).status_code==200
    assert client.get('/api/v1/creations/settings').json()['config']['context_tokens']==300000
    assert client.get('/internal/creation-media/missing').status_code==404


async def test_explicit_cancellation_does_not_follow_quoted_or_other_users_text(creative):
    r=creative;job,_=await create(r)
    assert not r.service.cancel_from_message(source(r,'他说“不要做了”是什么意思',2))
    assert not r.service.cancel_from_message(source(r,'取消制作',3,SECOND))
    assert r.service.cancel_from_message(source(r,'机器人，取消制作',4))==[job['id']]
    assert r.service.get(job['id'])['status']=='cancelled'


async def test_forgetting_also_removes_preview_files_and_tokens(creative):
    from rolebot.attachments import register
    r=creative;job,message=await create(r);await r.service.run(job['id'])
    job=r.service.get(job['id']);artifact=job['artifacts'][0]
    mid=SCOPE+':'+job['sent_ids'][-1];sent=r.db.message(SCOPE,mid,'default')
    item=register(r.db,SCOPE,'default',sent)[0]
    for path in r.engine.attachments.paths(item['id']):path.write_bytes(b'cached copy')
    url=r.service.media_url(job,artifact)
    r.db.invalidate_memory(SCOPE,source_ids=[message['id']])
    assert not r.service.tokens and not r.db.get('attachment',item['id'])
    assert not any(p.exists() for p in r.engine.attachments.paths(item['id']))


async def test_autonomous_obsolete_topic_does_not_send_but_explicit_request_survives_chat(creative):
    r=creative;message=source(r,'用一张图解释会更清楚',1)
    result=await r.service.create(SCOPE,'default',USER,{'brief':'制作说明图'},message,'assistance')
    r.model.script[-1]={'role':'assistant','content':'{"approved":true,"relevant":false}'}
    await r.service.run(result['job_id'])
    assert r.service.get(result['job_id'])['status']=='cancelled' and not r.bot.sent
    r.model.script=[{'role':'assistant','content':json.dumps(SPEC)},{'role':'assistant','content':'{"approved":true,"relevant":false}'}]
    job,_=await create(r,3);r.engine.recent_events[SCOPE]=[source(r,'换个话题聊游戏',4,SECOND)]
    await r.service.run(job['id']);assert r.service.get(job['id'])['status']=='sent'


async def test_permissions_rechecked_after_wait_before_next_packet(creative):
    r=creative;job,_=await create(r)
    async def sleep(_):
        r.group['tools']['creation_create']['enabled']=False;r.db.put('group',SCOPE,r.group)
    r.engine.delivery_sleep=sleep
    await r.service.run(job['id'])
    assert len(r.bot.sent)==1 and r.service.get(job['id'])['status']=='failed'


async def test_storage_and_favorite_limits_do_not_delete_kept_work(creative,monkeypatch):
    r=creative;job,_=await create(r);await r.service.run(job['id'])
    original=r.service.usage
    cfg=r.service.config()
    monkeypatch.setattr(r.service,'usage',lambda:{**original(),'stored_bytes':cfg['storage_mib']*MIB,'favorite_bytes':cfg['favorites_mib']*MIB})
    with pytest.raises(ValueError,match='空间已满'):await create(r,2)
    with pytest.raises(ValueError,match='收藏空间'):r.service.favorite(job['id'],True)
    assert r.service.folder(job['id']).exists()


async def test_missing_source_and_renderer_busy_never_repay(creative):
    r=creative;job,_=await create(r)
    (r.service.folder(job['id'])/'inputs.json').unlink()
    await r.service.run(job['id'])
    assert r.service.get(job['id'])['deleted'] and not r.model.calls


async def test_queue_expiration_is_notified_once_without_generation(creative):
    r=creative;job,_=await create(r);job['queue_until']=0;r.service.save(job)
    await r.service.tick();await asyncio.gather(*r.engine.tasks)
    await r.service.tick()
    assert r.service.get(job['id'])['status']=='failed' and not r.model.calls and len(r.bot.sent)==1


async def test_request_hourly_quota_is_independent_of_autonomous_quota(creative):
    r=creative;cfg=r.service.config();cfg.update(request_hourly=1,proactive_hourly=1);r.db.put('creation_config','global',cfg)
    job,_=await create(r);r.service.cancel(job['id'])
    with pytest.raises(ValueError,match='额度'):await create(r,2)
    message=source(r,'解释这组数据',3)
    result=await r.service.create(SCOPE,'default',USER,{'brief':'制作说明图'},message,'assistance')
    assert result['status']=='queued'


async def test_creation_transport_does_not_repeat_paid_requests(model):
    import httpx
    from rolebot.model import ModelError
    calls=[]
    def timeout(request):
        assert request.extensions['timeout']['read']==300
        assert request.extensions['timeout']['connect']==10
        calls.append(request);raise httpx.ReadTimeout('unknown provider outcome')
    model.client=httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    with pytest.raises(ModelError) as error:await model.chat(SCOPE,'creation',[{'role':'user','content':'画图'}])
    assert error.value.code=='model_timeout'
    assert len(calls)==1


async def test_creation_timeout_does_not_change_concurrent_chat(model):
    import httpx
    seen={}
    async def response(request):
        name=json.loads(request.content)['messages'][0]['content']
        seen[name]=request.extensions['timeout']['read']
        await asyncio.sleep(0)
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'{}'}}]})
    model.client=httpx.AsyncClient(transport=httpx.MockTransport(response),timeout=httpx.Timeout(65,connect=10))
    await asyncio.gather(*(model.chat(SCOPE,p,[{'role':'user','content':p}]) for p in ('creation','chat')))
    assert seen=={'creation':300,'chat':65}


async def test_creation_context_budget_is_independent_and_reserves_output(model):
    import httpx
    from rolebot.model import ModelError
    cfg=model.db.config();cfg.update(context_tokens=128000,gate_context_budget=128000,working_context_budget=128000)
    model.db.put('settings','global',cfg);model.db.put('creation_config','global',{'context_tokens':256000})
    seen=[]
    def response(request):
        seen.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'{}'}}]})
    model.client=httpx.AsyncClient(transport=httpx.MockTransport(response))
    await model.chat(SCOPE,'creation',[{'role':'user','content':'x'*150000}])
    assert len(seen)==1 and model.db.config()==cfg
    with pytest.raises(ModelError) as error:
        await model.chat(SCOPE,'creation',[{'role':'user','content':'x'*230000}])
    assert error.value.code=='creation_context_budget' and len(seen)==1
    catalog=model.catalog.get();entry=next(m for m in catalog['models'] if m['id']==catalog['routes']['chat']['model'])
    entry['context_window']=40000;model.db.put('inference','global',catalog)
    with pytest.raises(ModelError,match='模型上下文容量'):
        await model.chat(SCOPE,'creation',[{'role':'user','content':'x'*20000}])
    assert len(seen)==1


@pytest.mark.parametrize('recover',[True,False])
async def test_truncated_code_uses_bounded_repair_and_specific_failure(creative,recover):
    r=creative;job,_=await create(r)
    truncated={'role':'assistant','content':'{"runtime":"python","code":"','_finish_reason':'length'}
    good=list(r.model.script)
    r.model.script=[truncated,*good] if recover else [truncated]*4
    await r.service.run(job['id']);live=r.service.get(job['id'])
    assert live['status']==('sent' if recover else 'failed') and live['model_calls']==(3 if recover else 4)
    if not recover:
        assert live['error_code']=='model_output_limit' and not r.service.renderer.jobs
        assert '截断' in r.bot.sent[0][1][0]['data']['text']


@pytest.mark.parametrize('code,word',[('model_timeout','超时'),('model_network','连接断了')])
async def test_creation_network_failure_has_specific_feedback_and_no_retry(creative,monkeypatch,code,word):
    from rolebot.model import ModelError
    r=creative;job,_=await create(r);calls=[]
    async def failed(*args,**kwargs):calls.append(args);raise ModelError('网络故障',code=code)
    monkeypatch.setattr(r.model,'chat',failed)
    await r.service.run(job['id']);await r.service.tick()
    live=r.service.get(job['id'])
    assert live['error_code']==code and live['error_stage']=='generating'
    assert len(calls)==1 and not r.service.renderer.jobs and len(r.bot.sent)==1
    text=r.bot.sent[0][1][0]['data']['text']
    assert word in text and '简化' not in text and '额度' not in text


async def test_creation_total_deadline_cancels_stalled_render_and_notifies(creative,monkeypatch):
    r=creative;job,_=await create(r);cfg=r.service.config()
    monkeypatch.setattr(r.service,'config',lambda:{**cfg,'run_seconds':0.05})
    async def pending(id):return {'state':'running'}
    monkeypatch.setattr(r.service.renderer,'status',pending)
    await r.service.run(job['id'])
    live=r.service.get(job['id'])
    assert live['error_code']=='creation_timeout' and live['error_stage']=='rendering'
    assert r.service.renderer.cancelled and len(r.bot.sent)==1
    assert '等待时限' in r.bot.sent[0][1][0]['data']['text']


def test_existing_creation_budgets_remain_explicit_until_changed():
    old=CreationConfig(run_seconds=180,queue_seconds=180,queue_limit=3,storage_mib=5120,
                       favorites_mib=2048,request_hourly=12,proactive_hourly=2,proactive_cooldown=600)
    assert old.run_seconds==180 and old.queue_limit==3
    assert CreationConfig().run_seconds==600


def test_feature_rollback_preserves_other_permissions_and_new_messages(creative):
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
    from creation_release import snapshot,configure
    r=creative;before=snapshot(r.db.conn,[SCOPE]);configure(r.db.conn,before)
    group=r.db.get('group',SCOPE);group['guidance']='后来修改的说明';group['tools']['member_title']['enabled']=True
    r.db.put('group',SCOPE,group);message=source(r,'不能被回退删掉的新聊天',50)
    configure(r.db.conn,before,restore=True)
    live=r.db.get('group',SCOPE)
    assert live['guidance']=='后来修改的说明' and live['tools']['member_title']['enabled']
    assert r.db.message(SCOPE,message['id'],'default')['text']=='不能被回退删掉的新聊天'
    assert {k:live['tools'][k] for k in before['groups'][SCOPE]}==before['groups'][SCOPE]


async def test_shared_sticker_forgetting_cleans_cross_group_creation_and_caption(creative):
    r=creative;buf=io.BytesIO();Image.new('RGB',(64,64),'pink').save(buf,'PNG')
    origin=OTHER+':77'
    r.db.add_message(origin,OTHER,'default',SECOND,'user','跨群素材来源',{})
    sticker=r.engine.stickers.register(OTHER,buf.getvalue(),{'name':'共享素材'},[origin],[SECOND])
    job,_=await create(r,asset_ids=['sticker:'+sticker['id']]);await r.service.run(job['id'])
    live=r.service.get(job['id']);assert live['status']=='sent'
    r.db.invalidate_memory(OTHER,source_ids=[origin])
    live=r.service.get(job['id'])
    assert live['deleted'] and 'caption' not in live and not r.service.folder(job['id']).exists()
    assert not r.db.message(SCOPE,SCOPE+':'+live['sent_ids'][0],'default')


def test_media_tokens_expire_and_deleted_artifacts_are_unavailable(client,monkeypatch):
    service=client.app.state.engine.creations;db=client.app.state.db;id='a'*32
    folder=service.folder(id);folder.mkdir();buf=io.BytesIO();Image.new('RGB',(64,64)).save(buf,'PNG')
    raw=buf.getvalue();(folder/'page-0.png').write_bytes(raw)
    artifact={'id':'art_'+'b'*32,'name':'page-0.png','mime':'image/png','bytes':len(raw)}
    job={'id':id,'scope':SCOPE,'status':'sending','private':False,'artifacts':[artifact],'source_ids':[],'asset_ids':[],'actor_ids':[USER]}
    from unittest.mock import AsyncMock
    db.put('creation_job',id,job);monkeypatch.setattr(service,'authorize_fresh',AsyncMock())
    url=service.media_url(job,artifact);path='/internal/creation-media/'+url.rsplit('/',1)[-1]
    response=client.get(path)
    assert response.status_code==200 and response.content==raw and response.headers['cache-control']=='no-store'
    assert client.get('/api/v1/creations/'+id+'/artifacts/'+artifact['id']).status_code==401
    service.tokens[url.rsplit('/',1)[-1]]['expires']=0
    assert client.get(path).status_code==404
    path=service.media_url(job,artifact).split(':8080')[1];service.purge(job,'删除')
    assert client.get(path).status_code==404


async def test_cancelling_inflight_send_preserves_unknown_outcome(creative):
    r=creative;job,_=await create(r);entered=asyncio.Event()
    async def send(scope,segments):
        r.bot.sent.append((scope,segments));entered.set();await asyncio.Event().wait()
    r.bot.send=send;task=asyncio.create_task(r.service.run(job['id']));r.service.active=(job['id'],task)
    await asyncio.wait_for(entered.wait(),5)
    r.service.cancel(job['id'],SCOPE,USER)
    await asyncio.gather(task,return_exceptions=True)
    assert r.service.get(job['id'])['status']=='unknown' and len(r.bot.sent)==1


async def test_bound_tool_queues_real_request_without_waiting_for_renderer(creative):
    r=creative;message=source(r,'机器人，帮我做一张对比图',100)
    r.model.script=[{'role':'assistant','content':json.dumps({'mode':'request','source_matches':True,
        'request_message_id':message['id'],'context_request_ids':[]})}]
    result=await r.engine.bound_tool(SCOPE,'default',[message],'creation_create',{'brief':'制作对比图'})
    assert result['status']=='success' and result['result']['status']=='queued'
    job=r.service.get(result['result']['job_id'])
    assert job['creator']==USER and job['source_ids']==[message['id']]
    assert not r.bot.sent and not r.service.renderer.jobs and len(r.model.calls)==1


@pytest.mark.parametrize('enabled',[True,False])
async def test_comparison_assistance_obeys_autonomous_switch(creative,enabled):
    r=creative;r.group['tools']['creation_create']['autonomous_enabled']=enabled;r.db.put('group',SCOPE,r.group)
    message=source(r,'助手帮我对比一下甲乙的价格、功耗和内存',102)
    r.model.script=[{'role':'assistant','content':'{"mode":"independent","source_matches":false,"request_message_id":"","context_request_ids":[]}'}]
    result=await r.engine.bound_tool(SCOPE,'default',[message],'creation_create',{'brief':'按已核实参数绘制对比表','character':'none'})
    jobs=r.db.all('creation_job')
    if enabled:
        assert result['status']=='success' and len(jobs)==1 and jobs[0]['autonomous']
    else:
        assert result['status']!='success' and not jobs


async def test_media_http_request_refreshes_identity_and_rejects_demotion(client,monkeypatch):
    from types import SimpleNamespace
    from rolebot.engine import group_defaults
    from rolebot.permissions import _identities
    from support import FakeModel
    e=client.app.state.engine;db=e.db;s=e.creations;s.renderer=Renderer();e.model=FakeModel()
    group=group_defaults(SCOPE);group['enabled']=True
    group['tools']['creation_create'].update(enabled=True,request_access={'member':'disabled','admin':'all'})
    db.put('group',SCOPE,group);db.put('creation_config','global',CreationConfig(enabled=True).model_dump())
    role=['admin'];calls=[]
    async def lookup(action,params=None,**kwargs):
        calls.append(action);return {'group_id':params['group_id'],'user_id':params['user_id'],'role':role[0]}
    monkeypatch.setattr(e.onebot,'call',lookup)
    m=source(SimpleNamespace(db=db),'画一张图',101)
    result=await s.create(SCOPE,'default',USER,{'brief':'画图'},m)
    job=s.get(result['job_id']);buf=io.BytesIO();Image.new('RGB',(64,64)).save(buf,'PNG')
    (s.folder(job['id'])/'page-0.png').write_bytes(buf.getvalue())
    artifact={'id':'art_'+'d'*32,'name':'page-0.png','mime':'image/png','bytes':len(buf.getvalue())}
    job.update(status='sending',artifacts=[artifact]);s.save(job)
    path=s.media_url(job,artifact).split(':8080')[1]
    token=_identities.set(None);calls.clear()
    try:
        assert client.get(path).status_code==200
        assert calls==['get_group_member_info']
        role[0]='member'
        assert client.get(path).status_code==404
        assert len(calls)==2
    finally:_identities.reset(token)


@pytest.mark.parametrize('available,expected',[(1791,False),(1792,True),(2304,True),(3040,True),(8192,True)])
def test_renderer_adapts_memory_and_preserves_chat_reserve(available,expected):
    import importlib.util
    import sys
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root/'backend/rolebot'))
    try:
        spec=importlib.util.spec_from_file_location('creation_broker_test',root/'rendering/broker.py')
        broker=importlib.util.module_from_spec(spec);spec.loader.exec_module(broker)
        envelope=broker.resource_envelope(available*MIB)
    finally:sys.path.pop(0)
    assert envelope['can_start'] is expected
    assert envelope['memory_mib']<=2048 and envelope['memory_mib']+768<=available


def test_draft_patch_is_exact_bounded_and_checks_python_without_executing():
    from rolebot.creation_workflow import resolve_draft
    spec,design=resolve_draft({'patches':[{'old':'pass','new':'return None'}],
                              'design':{'layers':['subject','caption']}},SPEC)
    assert spec['code'].endswith('return None') and design['layers'][0]=='subject'
    assert SPEC['code'].endswith('pass')
    with pytest.raises(ValueError,match='唯一'):resolve_draft({'patches':[{'old':'missing','new':'x'}]},SPEC)
    with pytest.raises(ValueError,match='语法'):resolve_draft({**SPEC,'code':'def render('})
    with pytest.raises(ValueError):resolve_draft({'patches':[{'old':'pass','new':'x'}]})


async def test_asset_reads_do_not_consume_the_reserved_repair_and_review(creative):
    r=creative;job,_=await create(r)
    patch={'patches':[{'old':'pass','new':'return None'}]}
    r.model.script=[tool_reply('art_search',{'query':'夜晚'},id='a'),
        tool_reply('art_read',{'ids':['art:dark_background']},id='b'),
        {'role':'assistant','content':json.dumps(SPEC)},
        {'role':'assistant','content':json.dumps({'approved':False,'observations':['文字靠边'],'reason':'留白不足','suggestions':['增大边距']})},
        {'role':'assistant','content':json.dumps(patch)},
        {'role':'assistant','content':'{"approved":true,"relevant":true}'}]
    await r.service.run(job['id']);live=r.service.get(job['id'])
    assert live['status']=='sent' and live['model_calls']==6
    assert live['call_counts']=={'creation.code':4,'creation.review':2}
    assert r.service.payload(live)['spec']['code'].endswith('return None')
    assert len(r.service.payload(live)['reviews'])==2
    tool_results=[m for m in r.model.calls[2][2] if m['role']=='tool']
    assert 'svg_source' in tool_results[-1]['content']


async def test_resource_wait_reason_reaches_ui_and_failure_without_model_call(creative):
    r=creative;job,_=await create(r)
    async def low():return {'ready':True,'can_start':False,'reason':'宿主可用内存不足'}
    r.service.renderer.health=low
    await r.service.tick();job=r.service.get(job['id'])
    assert r.service.public(job)['waiting_reason']=='宿主可用内存不足' and not r.model.calls
    job['queue_until']=0;r.service.save(job)
    await r.service.tick();await asyncio.gather(*r.engine.tasks)
    assert '内存' in r.service.get(job['id'])['error'] and not r.model.calls


@pytest.mark.parametrize('explicit',[False,True])
async def test_worker_interruption_does_not_overwrite_real_cancellation(creative,explicit):
    r=creative;job,_=await create(r);started=asyncio.Event()
    async def slow(*args,**kwargs):started.set();await asyncio.Event().wait()
    r.model.chat=slow
    task=asyncio.create_task(r.service.run(job['id']));r.service.active=(job['id'],task)
    await started.wait()
    if explicit:r.service.cancel(job['id'],SCOPE,USER)
    else:task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    live=r.service.get(job['id'])
    assert live['status']==('cancelled' if explicit else 'interrupted')
    assert live['error']==('已取消' if explicit else '制作进程已中断，未自动重复付费')
    assert not r.bot.sent


@pytest.mark.parametrize('matrix,det',[
    ([[1,.5,0],[.5,1,0],[0,.5,1]],.75),
    ([[0,-1],[1,0]],1),
    ([[1,2],[2,4]],0),
    ([[-1,0,0],[0,2,0],[0,0,3]],-6),
])
def test_transform_facts_derive_determinant_instead_of_trusting_copied_text(matrix,det):
    from rolebot.creation_workflow import matrix_facts
    facts=matrix_facts({'matrix':matrix,'determinant':'incorrect supplied label'})
    assert facts['determinant']==det and facts['volume_scale']==abs(det)
    assert matrix_facts({'矩阵':'A = '+json.dumps(matrix)})['determinant']==det
    assert matrix_facts({'matrix':'__import__("os").system("invalid")'}) is None
    assert matrix_facts({'matrix':[[1,2],[3]]}) is None



def test_visual_context_keeps_recent_proofs_without_mutating_history():
    from rolebot.creation_workflow import visual_history
    history=[{'role':'user','content':[{'type':'image_url','image_url':{'url':str(n)}}]} for n in range(20)]
    history.insert(1,{'role':'assistant','content':'original source','reasoning_content':'native tool reasoning','_route':'stable'})
    result=visual_history(history,8)
    images=[b['image_url']['url'] for m in result if isinstance(m.get('content'),list) for b in m['content'] if b['type']=='image_url']
    assert images==list(map(str,range(12,20)))
    assert result[1]==history[1] and history[0]['content'][0]['type']=='image_url'



async def test_revision_can_patch_the_retained_parent_source(creative):
    r=creative;parent,_=await create(r);await r.service.run(parent['id'])
    child,_=await create(r,2,parent_job_id=parent['id'])
    r.model.script=[{'role':'assistant','content':json.dumps({'patches':[{'old':'pass','new':'return None'}]})},
                    {'role':'assistant','content':'{"approved":true,"relevant":true}'}]
    await r.service.run(child['id']);live=r.service.get(child['id'])
    assert live['status']=='sent' and live['model_calls']==2
    assert r.service.payload(live)['spec']['code'].endswith('return None')
    assert r.service.payload(r.service.get(parent['id']))['spec']['code'].endswith('pass')



@pytest.mark.parametrize('recovers',[True,False])
async def test_malformed_review_retries_review_without_redrawing(creative,recovers):
    r=creative;job,_=await create(r)
    truncated={'role':'assistant','content':'','_finish_reason':'length'}
    r.model.script=[{'role':'assistant','content':json.dumps(SPEC)},truncated,
        {'role':'assistant','content':'{"approved":true,"relevant":true}'} if recovers else truncated]
    await r.service.run(job['id']);live=r.service.get(job['id'])
    assert live['status']==('sent' if recovers else 'failed')
    assert live['call_counts']=={'creation.code':1,'creation.review':2}
    assert len(r.service.renderer.jobs)==1 and all(c[3]['max_tokens']==65536 for c in r.model.calls)
    if not recovers:assert live['error_code']=='review_response_invalid'


async def test_tool_reasoning_is_kept_then_completed_draft_reasoning_is_not_recounted(creative):
    r=creative;job,_=await create(r)
    lookup=tool_reply('art_search',{'query':'光'},id='lookup');lookup['reasoning_content']='tool reasoning must survive'
    r.model.script=[lookup,{'role':'assistant','content':json.dumps(SPEC),'reasoning_content':'long completed reasoning'*10000},
        {'role':'assistant','content':'{"approved":false,"reason":"文字裁切"}'},
        {'role':'assistant','content':json.dumps({'patches':[{'old':'pass','new':'return None'}]})},
        {'role':'assistant','content':'{"approved":true,"relevant":true}'}]
    await r.service.run(job['id'])
    assert r.service.get(job['id'])['status']=='sent'
    assert any(m.get('reasoning_content')=='tool reasoning must survive' for m in r.model.calls[1][2])
    assert 'tools' not in r.model.calls[3][3]
    assert not any('reasoning_content' in m for m in r.model.calls[3][2])



async def test_reviewer_can_inspect_owned_pixels_without_reading_paths(creative):
    r=creative;job,_=await create(r)
    r.model.script=[{'role':'assistant','content':json.dumps(SPEC)},
        tool_reply('creation_inspect',{'name':'page-0.png','box':[0,0,1000,1000]},id='inspect'),
        {'role':'assistant','content':'{"approved":true,"relevant":true}'}]
    await r.service.run(job['id']);live=r.service.get(job['id'])
    assert live['status']=='sent' and live['call_counts']['creation.review']==2
    with pytest.raises(ValueError):r.service.inspect_artifact(live,{'name':'../../secret','box':[0,0,1000,1000]})
    with pytest.raises(ValueError):r.service.inspect_artifact(live,{'name':'page-0.png','box':[0,0,1000,1000],'frames':[1]})
    assert any(b.get('type')=='image_url' for m in r.model.calls[-1][2] if isinstance(m.get('content'),list) for b in m['content'])


async def test_complex_artwork_plans_once_and_shares_verified_matrix_with_review(creative):
    r=creative;message=source(r,'制作三维矩阵说明图',555)
    job_id=(await r.service.create(SCOPE,'default',USER,{'brief':'三维矩阵说明图','data':{'matrix':[[1,.5,0],[.5,1,0],[0,.5,1]],'determinant':.5}},message))['job_id']
    r.model.script=[{'role':'assistant','content':'{"composition":"坐标系与变换后的立方体"}'},*r.model.script]
    await r.service.run(job_id);live=r.service.get(job_id)
    assert live['status']=='sent' and live['call_counts']=={'creation.plan':1,'creation.code':1,'creation.review':1}
    assert r.service.payload(live)['math_checks']['determinant']==.75
    for index in (1,2):
        content=r.model.calls[index][2][-1]['content'];data=json.loads(content[0]['text'] if isinstance(content,list) else content)
        assert data['math_checks']['determinant']==.75



@pytest.mark.parametrize('protocol,ready',[(None,False),(1,False),(2,True)])
async def test_renderer_protocol_mismatch_blocks_work_before_paid_generation(monkeypatch,protocol,ready):
    from rolebot.creation_client import RenderClient
    client=RenderClient();monkeypatch.setattr(RenderClient,'configured',property(lambda self:True))
    async def request(*args):return {'ready':True,'can_start':True,'protocol':protocol}
    client.request=request;health=await client.health()
    assert health['can_start'] is ready
    if not ready:assert '版本不匹配' in health['reason']



@pytest.mark.parametrize('origin,expected',[(None,'system'),('administrator','administrator')])
async def test_system_cancel_does_not_claim_to_be_an_administrator(creative,origin,expected):
    r=creative;job,_=await create(r)
    cancelled=r.service.cancel(job['id'],origin=origin)
    assert cancelled['cancel_source']==expected
    assert r.service.get(job['id'])['error']=='已取消'
