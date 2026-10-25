"""Source-bound asynchronous artwork jobs; generated code never runs in this process."""
import asyncio
import base64
import copy
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import prompts, tracing
from .attachments import visible
from .creation_client import RenderClient
from .creation_contract import MIB, JOB_ID, OUTPUT_LIMITS, output_names, validate_data, validate_spec
from .db import uid
from .model import parse_json

TOOLS={'creation_create','creation_status','creation_cancel'}
ACTIVE={'queued','generating','rendering','checking','ready','sending'}
RUNNING={'generating','rendering','checking'}
RESERVATION=96*MIB
MAX_MODEL_CALLS=16
MAX_REPAIRS=3


class CreationOutputLimit(ValueError):
    code='model_output_limit'


class CreationReviewError(ValueError):
    code='review_response_invalid'


def parse_creation_code(text):
    """Recover literal whitespace inside JSON strings without guessing code."""
    try:return parse_json(text)
    except json.JSONDecodeError as exc:
        if not exc.msg.startswith('Invalid control character'):raise
        # Generated source occasionally includes an actual newline/tab in its
        # JSON string. Escape only these characters; never invent quotes, close
        # truncated JSON, evaluate code, or relax ordinary tool JSON parsing.
        quoted=False;escaped=False;fixed=[]
        for char in text:
            if quoted and not escaped and char in '\n\r\t':
                fixed.append({'\n':'\\n','\r':'\\r','\t':'\\t'}[char]);continue
            fixed.append(char)
            if escaped:escaped=False
            elif quoted and char=='\\':escaped=True
            elif char=='"':quoted=not quoted
        return parse_json(''.join(fixed))


class CreationCreate(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    brief: str = Field(min_length=1,max_length=4000)
    format: Literal['auto','png','gif','mp4'] = 'auto'
    asset_ids: list[str] = Field(default_factory=list,max_length=8)
    character: Literal['auto','current','none'] = 'auto'
    operation: Literal['create','list_assets'] = 'create'
    source_message_ids: list[str] = Field(default_factory=list,max_length=8)
    data: dict = Field(default_factory=dict)
    parent_job_id: str | None = Field(default=None,pattern=r'^[0-9a-f]{32}$')

    @model_validator(mode='after')
    def bounded(self):
        validate_data(self.data)
        if any(len(x)>120 for x in self.asset_ids+self.source_message_ids):raise ValueError('来源编号过长')
        return self


class CreationById(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    job_id: str = Field(pattern=r'^[0-9a-f]{32}$')


class CreationConfig(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    revision: int = Field(default=0,ge=0)
    enabled: bool = False
    context_tokens: int = Field(default=256000,ge=4096,le=1000000)
    retention_days: int = Field(default=7,ge=1,le=30)
    storage_mib: int = Field(default=10240,ge=192,le=10240)
    favorites_mib: int = Field(default=4096,ge=0,le=4096)
    queue_limit: int = Field(default=8,ge=1,le=8)
    queue_seconds: int = Field(default=600,ge=30,le=900)
    run_seconds: int = Field(default=600,ge=30,le=900)
    request_hourly: int = Field(default=24,ge=1,le=120)
    proactive_hourly: int = Field(default=4,ge=0,le=12)
    proactive_cooldown: int = Field(default=300,ge=60,le=86400)

    @model_validator(mode='after')
    def quotas(self):
        if self.favorites_mib>self.storage_mib:raise ValueError('收藏配额不能超过总配额')
        return self


class Creations:
    def __init__(self,engine):
        self.engine,self.db=engine,engine.db
        self.root=engine.settings.data_dir/'creations';self.root.mkdir(exist_ok=True)
        self.renderer=RenderClient()
        self.private={}  # Opt-outs: inputs never enter SQLite or durable storage.
        self.private_files={}
        self.active=None;self.last_scope=None;self.admission=asyncio.Lock()
        self.tokens={};self.last_cleanup=0
        from .art_library_store import ArtLibrary
        self.art_library=ArtLibrary(self);self.asset_preview_busy=False
        self.db.creation_invalidator=self.invalidate

    def config(self):
        return CreationConfig.model_validate(self.db.get('creation_config','global',{})).model_dump()

    def configured(self):
        if not self.config()['enabled'] or not self.renderer.configured:return False
        if hasattr(self.engine.model,'catalog'):
            try:
                route=self.engine.model.catalog.resolve('creation')
                return bool(route['key'] and route['model']['enabled'] and route['provider']['enabled']
                            and 'image' in route['model']['input_modalities'])
            except (KeyError,StopIteration):return False
        return True

    def folder(self,id):
        if not JOB_ID.fullmatch(id):raise ValueError('作品编号无效')
        path=self.root/id
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):raise ValueError('作品路径无效')
        return path

    def save(self,job):
        live=self.db.get('creation_job',job['id'],{})
        if job.get('deleted') or live.get('deleted') or (live.get('status')=='cancelled' and job['status']!='cancelled'):return
        if live.get('cancel_requested') and not job.get('cancel_requested'):return
        self.db.put('creation_job',job['id'],job)

    def payload(self,job):
        return self.private[job['id']] if job['private'] else json.loads((self.folder(job['id'])/'inputs.json').read_text('utf8'))

    def persist_payload(self,job,payload):
        if self.db.get('creation_job',job['id'],{}).get('deleted'):raise ValueError('制作资料已清理')
        if job['private']:self.private[job['id']]=payload;return
        folder=self.folder(job['id']);folder.mkdir(exist_ok=True,mode=0o700)
        raw=json.dumps(payload,ensure_ascii=False)
        if len(raw.encode())>40*MIB:raise ValueError('制作输入过大')
        path=folder/'inputs.json';tmp=folder/'inputs.tmp';tmp.write_text(raw,encoding='utf8');tmp.replace(path)

    def stored_bytes(self,job):
        folder=self.folder(job['id'])
        return sum(p.stat().st_size for p in folder.iterdir() if p.is_file() and not p.is_symlink()) if folder.exists() else 0

    def usage(self):
        jobs=self.db.all('creation_job')
        return {'stored_bytes':sum(self.stored_bytes(j) for j in jobs),
                'favorite_bytes':sum(self.stored_bytes(j) for j in jobs if j.get('favorite')),
                'reserved_bytes':RESERVATION*sum(j['status'] in ACTIVE for j in jobs),
                'queued':sum(j['status']=='queued' for j in jobs),'active':self.active is not None,
                'hard_limits':{'memory_mib':2048,'cpus':2,'pids':128,'work_mib':256,'concurrency':1}}

    def get(self,id,scope=None,actor=None):
        job=self.db.get('creation_job',id)
        if not job or (scope and job['scope']!=scope) or (actor and job['creator']!=actor):
            raise ValueError('没有可访问的制作任务')
        return job

    def public(self,job,detail=False):
        keys=('id','scope','persona','creator','title','status','format','created_at','started_at','finished_at',
              'expires','favorite','error','error_code','error_stage','model_calls','cost','currency','usage','metrics',
              'source_ids','asset_ids','artifacts','sent_ids','private','cleanup_reason','trace_id','parent_job_id',
              'waiting_reason','waiting_since','call_counts','repairs','cancel_source')
        result={k:job[k] for k in keys if k in job}
        if detail and not job['private'] and self.folder(job['id']).exists():
            data=self.payload(job);result['source']={k:data.get(k) for k in ('brief','data','evidence','spec','design','reviews','math_checks','plan')}
        return result

    def authorize(self,job):
        from .tools import ToolDenied
        g=self.db.get('group',job['scope'],{})
        if not self.config()['enabled'] or not g.get('enabled') or g.get('paused') or g.get('persona')!=job['persona']:
            raise ToolDenied('群、角色或制作开关已变化')
        if self.db.get('persona',job['persona'],{}).get('version')!=job['persona_version']:
            raise ToolDenied('角色设定已变化')
        live=self.db.get('creation_job',job['id'])
        if not live or live.get('deleted') or live.get('cancel_requested') or live['status']=='cancelled':
            raise ToolDenied('制作任务已取消或删除')
        if not job.get('favorite') and job['expires']<=time.time():raise ToolDenied('作品已到保存期限')
        if job['status'] in ACTIVE and self.engine.memory.epoch(job['scope'])!=job['epoch']:
            raise ToolDenied('制作来源已变化')
        policy=self.engine.tools.policy(job['scope'],'creation_create')
        if not policy.get('enabled') or not policy.get('autonomous_enabled' if job['autonomous'] else 'user_enabled',True):
            raise ToolDenied('制作权限或使用方式已关闭')
        for actor in job.get('authorization_actors',[job['creator']]):
            self.engine.permissions.check_tool(job['scope'],actor,'creation_create',{})
        if not job['private']:
            if any(not visible(self.db,job['scope'],job['persona'],id) for id in job['source_ids']):
                raise ToolDenied('制作资料已遗忘、过期或停止记忆')
        elif job['id'] not in self.private:
            raise ToolDenied('临时制作资料已释放')
        payload=self.payload(job)
        if any(not self.art_library.current(ref,job['persona']) for ref in payload.get('builtin_assets',[])):
            raise ToolDenied('美术素材已停用、删除或变更')
        for ref in payload.get('references',[]):
            if not self.engine.role_images.current(ref):raise ToolDenied('角色素材已替换或停用')
        for sticker in payload.get('stickers',[]):
            live=self.db.get('sticker',sticker['id'],{})
            if not live.get('enabled',True) or live.get('sha256')!=sticker['sha256']:
                raise ToolDenied('表情素材已撤销')

    async def authorize_fresh(self,job):
        await self.engine.permissions.refresh(job['scope'],job.get('authorization_actors',[job['creator']]))
        self.authorize(job)

    async def create(self,scope,persona,actor,args,source,mode='request',audience=()):
        if not self.configured():raise ValueError('制作环境尚未启用或不可用')
        if not source or source['user_id']!=actor or source['scope']!=scope:raise ValueError('制作请求来源不匹配')
        args=CreationCreate.model_validate(args).model_dump();cfg=self.config();now=time.time()
        if args['operation']=='list_assets':
            group=self.db.get('group',scope,{})
            if not group.get('enabled') or group.get('paused') or group.get('persona')!=persona:raise ValueError('当前群或角色不可用')
            await self.engine.permissions.refresh(scope,[actor])
            self.engine.permissions.check_tool(scope,actor,'creation_create',{})
            policy=self.engine.tools.policy(scope,'creation_create')
            if not policy.get('enabled') or not policy.get('user_enabled',True):raise ValueError('制作工具当前不可用')
            rows=self.art_library.search(persona,args['brief'])
            return {'status':'assets','assets':rows,'notice':'仅查询素材，没有创建、渲染或发送作品。需要制作时再使用 operation=create。'}
        source_epoch=source.get('memory_epoch',self.engine.memory.epoch(scope))
        autonomous=mode=='assistance'
        if mode not in ('request','assistance'):raise ValueError('制作需明确区分请求和自主使用')
        async with self.admission:
            self.cleanup()
            jobs=self.db.all('creation_job')
            if any(j['creator']==actor and j['status'] in ACTIVE for j in jobs):raise ValueError('你已有一个未完成的制作任务')
            if sum(j['status']=='queued' for j in jobs)>=cfg['queue_limit']:raise ValueError('制作队列已满，稍后再试')
            recent=[j for j in jobs if j['scope']==scope and j['created_at']>now-3600 and j.get('autonomous')==autonomous]
            if len(recent)>=cfg['proactive_hourly' if autonomous else 'request_hourly']:raise ValueError('达到当前制作方式的每小时额度')
            if autonomous and recent and now-max(j['created_at'] for j in recent)<cfg['proactive_cooldown']:raise ValueError('主动制作仍在冷却')
            usage=self.usage()
            if usage['stored_bytes']+usage['reserved_bytes']+RESERVATION>cfg['storage_mib']*MIB:
                raise ValueError('作品空间已满，请清理或等待过期；不会删除未过期作品')
            private=not source.get('remember',True) or not self.db.get('member',scope+':'+actor,{}).get('remember',True)
            target={k:source[k] for k in ('id','scope','user_id','message_id','ts','text') if k in source}
            target.update(persona=persona,remember=not private)
            payload={**args,'target':target,'assets':{},'references':[],'stickers':[],'builtin_assets':[],'asset_manifest':[],
                     'evidence':[{'source_id':source['id'],'text':source.get('text','')[:6000]}]}
            source_ids={source['id']};actors={actor}|(set(audience) if autonomous else set());dependencies=[]
            if args['parent_job_id']:
                parent=self.get(args['parent_job_id'],scope,actor)
                if parent['private'] or parent['persona']!=persona:raise ValueError('不能追改临时或其他角色的作品')
                await self.authorize_fresh(parent)
                old=self.payload(parent)
                payload.update(assets=dict(old['assets']),references=old['references'],stickers=old['stickers'],previous_spec=old.get('spec'))
                payload.update(builtin_assets=old.get('builtin_assets',[]),asset_manifest=old.get('asset_manifest',[]))
                if not args['data']:payload['data']=old['data']
                payload['evidence']+=old.get('evidence',[])[:8]
                source_ids.update(parent['source_ids']);actors.update(parent['actor_ids'])
                dependencies.extend(parent.get('asset_dependencies',[]))
            for mid in args['source_message_ids']:
                row=visible(self.db,scope,persona,mid)
                if not row:raise ValueError('制作引用不属于本群、当前角色或已不可见')
                source_ids.add(row['id']);actors.add(row['user_id'])
                payload['evidence'].append({'source_id':row['id'],'text':row['text'][:6000]})
            # Bind actual successful search/fetch results from this source, not
            # arbitrary claimed citations supplied by the conversation model.
            for entry in self.db.rows("SELECT data FROM audit WHERE scope=? AND kind='tool' AND ts>? ORDER BY ts DESC LIMIT 20",(scope,now-600)):
                tool=json.loads(entry['data'])
                if tool.get('event_id')==source['id'] and tool.get('status')=='success' and tool.get('name') in ('web_search','web_fetch'):
                    payload['evidence'].append({'tool':tool['name'],'source_id':source['id'],'result':str(tool.get('result'))[:10000]})
            payload['evidence']=payload['evidence'][:12]
            payload['character']=self.art_library.character_mode(args['brief'],source.get('text',''),args['character'],persona)
            if payload['character']=='none':
                # A revision can remove the mascot too. Rebuild file names so
                # later on-demand reads cannot overwrite a surviving asset.
                kept=[a for a in payload['asset_manifest'] if a['kind'] not in ('character','role_reference')]
                files={}
                for n,item in enumerate(kept):
                    for key,prefix in (('file','asset'),('vector_file','source')):
                        if key in item:
                            old_name=item[key];item[key]=f'{prefix}_{n}.'+old_name.rsplit('.',1)[-1]
                            files[item[key]]=payload['assets'][old_name]
                payload['assets']=files;payload['asset_manifest']=kept;payload['references']=[]
                ids={a['id'] for a in kept}
                payload['builtin_assets']=[a for a in payload['builtin_assets'] if a['id'] in ids]
            selected,needs_character=self.art_library.select(persona,args['brief'],source.get('text',''),args['asset_ids'],payload['character'],bool(payload['asset_manifest']))
            payload['character_required']=needs_character
            inherited={a['id'] for a in payload.get('asset_manifest',[])}
            for aid in selected:
                if aid in inherited:continue
                if len(payload['asset_manifest'])>=8:raise ValueError('素材不能超过 8 张')
                description={'id':aid,'kind':'source_image','label':aid}
                if aid.startswith('art:'):
                    item,raw,svg=self.art_library.read(aid,persona)
                    ext=item['file'].rsplit('.',1)[-1]
                    if ext=='svg':item,raw,svg=self.art_library.read(aid,persona,preview=True);ext='png'
                    payload['builtin_assets'].append({k:item[k] for k in ('id','sha256','revision','vector_sha256') if k in item})
                    description={k:item[k] for k in ('id','label','kind','width','height','usage')}
                    if svg:
                        vector_file=f'source_{len(payload["asset_manifest"])}.svg'
                        payload['assets'][vector_file]=base64.b64encode(svg).decode()
                        description.update(vector_file=vector_file,layers=item.get('layers',[]))
                    if not item.get('builtin') and svg:description['preview_notice']='PNG 是白底预览；透明合成或分层修改请使用 SVG 文件'
                elif aid.startswith('ref_'):
                    ref,block=self.engine.role_images.read(persona,aid[4:]);payload['references'].append(ref)
                    raw=base64.b64decode(block['image_url']['url'].split(',',1)[1]);ext='jpg'
                    description.update(kind='role_reference',label=ref.get('label','角色参考图'))
                elif aid.startswith('sticker:'):
                    from .messaging import available_sticker,sticker_path
                    sticker=available_sticker(self.db,scope,aid[8:]);raw=sticker_path(self.engine.settings,sticker).read_bytes()
                    ext=sticker.get('ext','jpg');ext='jpg' if ext=='jpeg' else ext
                    payload['stickers'].append({'id':sticker['id'],'sha256':sticker['sha256']})
                    from .stickers import origins
                    dependencies.extend(origins(sticker))
                    local=[o for o in origins(sticker) if o.get('scope')==scope]
                    for origin in local:source_ids.update(origin.get('source_ids',[]));actors.update(origin.get('actor_ids',[]))
                elif aid.startswith('img_'):
                    self.engine.permissions.check_tool(scope,actor,'image_read',{})
                    if not self.engine.tools.policy(scope,'image_read').get('enabled'):raise ValueError('图片读取未授权')
                    item,block=await self.engine.attachments.read(scope,persona,aid,self.engine.memory.epoch(scope),source if private else None)
                    raw=base64.b64decode(block['image_url']['url'].split(',',1)[1])
                    from .image_assets import mime
                    ext={'image/png':'png','image/jpeg':'jpg','image/webp':'webp','image/gif':'gif'}[mime(raw)]
                    source_ids.update(item['source_ids']);actors.update(item['actor_ids'])
                else:raise ValueError('请使用当前可访问的图片、参考图或表情编号')
                if len(raw)>8*MIB:raise ValueError('单个制作素材超过 8 MiB，请先缩小图片')
                filename=f'asset_{len(payload["asset_manifest"])}.{ext}'
                payload['assets'][filename]=base64.b64encode(raw).decode()
                payload['asset_manifest'].append({**description,'file':filename})
            payload['asset_ids']=[a['id'] for a in payload['asset_manifest']]
            if sum(len(v)*3//4 for v in payload['assets'].values())>24*MIB:raise ValueError('素材总量超过 24 MiB')
            job={'id':uid(),'scope':scope,'persona':persona,'creator':actor,'actor_ids':sorted(actors),
                 'authorization_actors':sorted({actor}|(set(audience) if autonomous else set())),
                 'source_ids':sorted(source_ids),'asset_ids':payload['asset_ids'],'asset_dependencies':dependencies,'parent_job_id':args['parent_job_id'],
                 'title':'临时作品' if private else args['brief'][:80],'format':args['format'],'private':private,
                 'status':'queued','autonomous':autonomous,'created_at':now,'queue_until':now+cfg['queue_seconds'],
                 'expires':now+cfg['retention_days']*86400,'favorite':False,'epoch':source_epoch,
                 'persona_version':self.db.get('persona',persona,{}).get('version'),'artifacts':[],
                 'model_calls':0,'sent_ids':[],'sent_parts':[],'cost':0,'currency':self.db.config()['currency']}
            self.persist_payload(job,payload);self.save(job)
            try:await self.authorize_fresh(job)
            except BaseException:self.purge(job,'来源或授权已变化');raise
            return {'job_id':job['id'],'status':'queued','notice':'已进入制作队列，尚未完成或发送。稍后自动发送成品；不要轮询工具或宣称已完成。'}

    def purge(self,job,reason):
        for item in self.db.all('attachment'):
            if item.get('creation_job_id')==job['id']:
                for path in self.engine.attachments.paths(item['id']):
                    path.unlink(missing_ok=True);path.with_suffix(path.suffix+'.tmp').unlink(missing_ok=True)
                self.db.delete('attachment',item['id'])
        folder=self.folder(job['id'])
        if folder.exists():shutil.rmtree(folder)
        self.private.pop(job['id'],None);self.private_files.pop(job['id'],None)
        self.tokens={k:v for k,v in self.tokens.items() if v['job_id']!=job['id']}
        job.update(status='cancelled' if job['status'] in ACTIVE else job['status'],deleted=True,
                   artifacts=[],title='已清理作品',source_ids=[],asset_ids=[],asset_dependencies=[],cleanup_reason=reason)
        for key in ('caption','error'):job.pop(key,None)
        self.db.put('creation_job',job['id'],job)
        if self.active and self.active[0]==job['id']:self.active[1].cancel()

    def cancel_from_message(self,message):
        # Only an unambiguous cancellation in the real sender's own text. Quoted
        # messages, unrelated conversation and another person's task cannot cancel.
        text=message.get('plain_text',message.get('text','')).strip()
        from .persona_names import strip_address
        text=strip_address(text,self.db,message.get('persona',''))
        if not re.fullmatch(r'(?:取消(?:刚才|这次|那个)?(?:的)?(?:制作|绘图|动画|视频|作品|任务)|(?:不用|不要|别)(?:再)?(?:画|做|制作)(?:了|啦))[。！!～~\s]*',text):return []
        jobs=[j for j in self.db.all('creation_job') if j['scope']==message['scope']
              and j['creator']==message['user_id'] and j['status'] in ACTIVE]
        for job in jobs:self.cancel(job['id'],message['scope'],message['user_id'])
        return [j['id'] for j in jobs]

    def invalidate(self,scope,ids,user_id=None):
        for job in self.db.all('creation_job'):
            if job.get('deleted'):continue
            related=job['scope']==scope and (set(job['source_ids'])&set(ids) or user_id in job.get('actor_ids',[]))
            shared=any(d.get('scope')==scope and (set(d.get('source_ids',[]))&set(ids) or user_id in d.get('actor_ids',[]))
                       for d in job.get('asset_dependencies',[]))
            if related or shared:
                self.purge(job,'来源已遗忘或停止记忆')
                if job['scope']!=scope and job.get('sent_ids'):
                    self.db.invalidate_memory(job['scope'],source_ids=[job['scope']+':'+m for m in job['sent_ids']])

    def cancel(self,id,scope=None,actor=None,*,origin=None):
        job=self.get(id,scope,actor)
        if job['status'] not in ACTIVE:return self.public(job)
        inflight=job['status']=='sending'
        job.update(status='unknown' if inflight else 'cancelled',cancel_requested=True,finished_at=time.time(),
                   cancel_source=origin or ('requester' if actor else 'system'),
                   error='已停止后续发送，当前一条回执未知' if inflight else '已取消');self.save(job)
        self.db.audit(job['scope'],'creation_cancel',{'job_id':id,'source':job['cancel_source']})
        self.tokens={k:v for k,v in self.tokens.items() if v['job_id']!=id}
        if self.active and self.active[0]==id:self.active[1].cancel()
        if job['private']:self.purge(job,'临时任务已取消')
        return self.public(job)

    def favorite(self,id,value):
        job=self.get(id)
        if job['private'] or job.get('deleted') or job['status']!='sent':raise ValueError('只能收藏已发送且仍保留的作品')
        if value and not job['favorite'] and self.usage()['favorite_bytes']+self.stored_bytes(job)>self.config()['favorites_mib']*MIB:
            raise ValueError('收藏空间已满')
        job['favorite']=bool(value);self.save(job);return self.public(job)

    def cleanup(self):
        now=time.time()
        for job in self.db.all('creation_job'):
            if not job.get('deleted') and job['status'] not in ACTIVE and not job.get('favorite') and job['expires']<=now:
                self.purge(job,'作品已到保存期限')
        for token,value in list(self.tokens.items()):
            if value['expires']<now:self.tokens.pop(token,None)
        self.last_cleanup=now

    def recover(self):
        for job in self.db.all('creation_job'):
            if job.get('deleted'):continue
            if job['private']:self.purge(job,'临时任务随服务重启清理')
            elif job['status'] in ('queued','ready') and not (self.folder(job['id'])/'inputs.json').is_file():
                self.purge(job,'任务源文件已缺失')
            elif job['status']=='sending':
                job.update(status='unknown',error='重启前发送结果未知，未自动重发');self.save(job)
            elif job['status'] in RUNNING:
                job.update(status='interrupted',error='制作被中断，未重新调用模型');self.save(job)
        self.cleanup()
        known={j['id'] for j in self.db.all('creation_job') if not j.get('deleted')}
        for path in self.root.iterdir():
            if JOB_ID.fullmatch(path.name) and path.name not in known and path.is_dir() and not path.is_symlink():
                shutil.rmtree(self.folder(path.name))

    async def tick(self):
        if self.asset_preview_busy:return
        if time.time()-self.last_cleanup>30:self.cleanup()
        if self.active:
            if self.active[1].done():self.active=None
            return
        candidates=[]
        for job in self.db.all('creation_job'):
            if job.get('deleted') or job['status'] not in ('queued','ready'):continue
            if job['status']=='queued' and time.time()>job['queue_until']:
                reason=job.get('waiting_reason','')
                job.update(status='failed',error='排队超时，本次未开始制作'+('：'+reason if reason else ''),error_code='queue_timeout',finished_at=time.time());self.save(job)
                self.engine.spawn(self.notify_failure(job));continue
            candidates.append(job)
        if not candidates or not self.configured():return
        candidates.sort(key=lambda j:(j['status']!='ready',j['autonomous'],j['scope']==self.last_scope,j['created_at']))
        if candidates[0]['status']!='ready':
            health=await self.renderer.health()
            if not health.get('can_start') or self.asset_preview_busy:
                reason=health.get('reason') or ('正在等待其他制作完成' if health.get('busy') or self.asset_preview_busy else '制作服务暂不可用或宿主内存不足')
                job=candidates[0]
                if job.get('waiting_reason')!=reason:
                    job.update(waiting_reason=reason,waiting_since=time.time());self.save(job)
                return
        job=candidates[0];self.last_scope=job['scope']
        job.pop('waiting_reason',None);job.pop('waiting_since',None);self.save(job)
        self.active=(job['id'],self.engine.spawn(self.run(job['id'])))

    async def run(self,id):
        job=self.get(id)
        try:payload=self.payload(job);target=payload['target']
        except (OSError,KeyError,ValueError):
            self.purge(job,'任务源文件缺失或损坏');return
        trace_id,token=tracing.begin(self.db,job['scope'],job['persona'],[target],kind='creation')
        trace=self.db.get('trace',trace_id);trace.update(source_ids=job['source_ids'],actor_ids=job['actor_ids']);self.db.put('trace',trace_id,trace)
        job['trace_id']=trace_id
        broker_id=None
        try:
            await self.authorize_fresh(job)
            if job['status']!='ready':
                job.update(status='generating',started_at=time.time());self.save(job)
                deadline=time.monotonic()+self.config()['run_seconds']
                async with asyncio.timeout(self.config()['run_seconds']):
                    spec=payload.get('previous_spec');feedback=None;feedback_history=[];repairs=0;approved=False;history=[]
                    from .creation_workflow import matrix_facts
                    math_checks=matrix_facts(payload['data'])
                    if math_checks:payload['math_checks']=math_checks;self.persist_payload(job,payload)
                    if payload['format']=='mp4' or re.search(r'三维|3D|矩阵',payload['brief'],re.I):
                        plan_input={k:payload.get(k) for k in ('brief','format','data','evidence')}
                        plan_input['math_checks']=math_checks
                        result=await self.call_model(job,'creation.plan',[{'role':'user','content':json.dumps(plan_input,ensure_ascii=False)}])
                        if result.get('_finish_reason') not in ('length','max_tokens'):
                            try:
                                plan=parse_json(result.get('content'))
                                if len(json.dumps(plan,ensure_ascii=False))<=6000:payload['plan']=plan;self.persist_payload(job,payload)
                            except ValueError:pass  # A malformed optional blueprint does not restart the whole job.
                    while not approved:
                        await self.authorize_fresh(job)
                        code_input={k:payload[k] for k in ('brief','format','data','evidence','previous_spec','character') if k in payload}
                        code_input['assets']=list(payload['assets'])
                        if payload.get('asset_manifest'):code_input['asset_manifest']=payload['asset_manifest']
                        code_input['character_required']=payload.get('character_required',False)
                        if payload.get('plan'):code_input['design_plan']=payload['plan']
                        if math_checks:code_input['math_checks']=math_checks
                        if payload.get('builtin_assets'):
                            from .art_assets import catalog
                            code_input['palette']=catalog()['palette']
                        code_input['asset_library']='可选素材库：仅需要具体背景、道具、姿势或查看素材时调用 art_search / art_read；普通绘图可以直接原创，不必检索。'
                        if spec:code_input['current_spec']={k:v for k,v in spec.items() if k!='code'}
                        if feedback:code_input['feedback']=feedback
                        if feedback_history:code_input['feedback_history']=feedback_history
                        code_input['budget']={'calls_remaining':MAX_MODEL_CALLS-job['model_calls'],
                                              'repairs_remaining':MAX_REPAIRS-repairs,
                                              'seconds_remaining':max(0,round(deadline-time.monotonic()))}
                        if history:
                            code_input={k:v for k,v in code_input.items() if k in ('current_spec','feedback','feedback_history','budget','math_checks')}
                        generation_input=json.dumps(code_input,ensure_ascii=False)
                        material_views=await self.asset_preview_blocks(payload)
                        if material_views:generation_input=[{'type':'text','text':generation_input},*material_views]
                        if any(item['stage']=='checking' for item in feedback_history):
                            if isinstance(generation_input,str):generation_input=[{'type':'text','text':generation_input}]
                            generation_input.extend([{'type':'text','text':'以下是上一版作品的实际渲染图，用于修复；前面的图片是输入素材。'},*await self.preview_blocks(job)])
                        job['status']='generating';self.save(job)
                        history.append({'role':'user','content':generation_input})
                        response=await self.generate(job,payload,history)
                        history.append(response)
                        try:
                            if response.get('_finish_reason') in ('length','max_tokens'):
                                raise CreationOutputLimit('模型输出达到上限，被截断而不完整。请用更紧凑的完整代码实现原需求，返回完整 JSON。')
                            from .creation_workflow import resolve_draft
                            spec,design=resolve_draft(parse_creation_code(response.get('content')),spec)
                            if design is not None:payload['design']=design
                            if payload['format']!='auto' and spec['format']!=payload['format']:raise ValueError('输出格式与请求不符')
                            job['draft_generated']=True;self.save(job)
                            payload['spec']=spec;self.persist_payload(job,payload)
                            broker_id=uid();job['status']='rendering';self.save(job)
                            await self.renderer.submit(broker_id,spec,payload['data'],payload['assets'],deadline-time.monotonic())
                            while True:
                                self.authorize(job)
                                result=await self.renderer.status(broker_id)
                                if result['state'] in ('failed','cancelled','complete'):
                                    metrics=job.get('metrics',{})
                                    job['metrics']={'peak_memory_bytes':max(metrics.get('peak_memory_bytes',0),result.get('peak_memory_bytes',0)),
                                                    'duration_seconds':round(metrics.get('duration_seconds',0)+result.get('duration_seconds',0),3)}
                                    self.save(job)
                                if result['state'] in ('failed','cancelled'):raise ValueError(result.get('error','渲染未完成'))
                                if result['state']=='complete':break
                                await asyncio.sleep(1)
                            await self.receive_artifacts(job,broker_id,spec,result)
                        except (ValueError,KeyError,TypeError) as exc:
                            if repairs>=MAX_REPAIRS:raise
                            feedback=str(exc)[-4000:];repairs+=1;job['repairs']=repairs;self.save(job)
                            feedback_history.append({'stage':job['status'],'reason':feedback})
                            if broker_id:await self.renderer.cancel(broker_id);broker_id=None
                            continue
                        finally:
                            if broker_id:await self.renderer.cancel(broker_id);broker_id=None
                        await self.authorize_fresh(job)
                        job['status']='checking';self.save(job)
                        recent=[]
                        if job['autonomous']:
                            recent=[{'user':m['user_id'],'text':m['text'][:800]} for m in self.engine.recent_events.get(job['scope'],[])
                                    if m.get('persona')==job['persona'] and m.get('remember',True) and m['ts']>=job['created_at']][-12:]
                            # Any later revocation of a reviewed source invalidates
                            # the result. Do not persist the text in the job itself.
                            for m in self.engine.recent_events.get(job['scope'],[]):
                                if m.get('persona')==job['persona'] and m.get('remember',True) and m['ts']>=job['created_at']:
                                    if m['id'] not in job['source_ids']:job['source_ids'].append(m['id'])
                                    if m['user_id'] not in job['actor_ids']:job['actor_ids'].append(m['user_id'])
                        content=[{'type':'text','text':json.dumps({'brief':payload['brief'],'data':payload['data'],'evidence':payload.get('evidence',[]),
                            'autonomous':job['autonomous'],'recent_chat':recent,'math_checks':math_checks,
                            'spec':{k:v for k,v in spec.items() if k!='code'},'character_required':payload.get('character_required',False),
                            'character':payload.get('character','auto'),'asset_manifest':payload.get('asset_manifest',[])},ensure_ascii=False)}]
                        refs={**payload,'asset_manifest':[a for a in payload.get('asset_manifest',[]) if a['kind'] in ('character','role_reference')]}
                        if refs['asset_manifest']:
                            content.append({'type':'text','text':'以下是角色参考素材，请核对人物外观；这不是待审核成品。'})
                            content.extend(await self.asset_preview_blocks(refs))
                        content.append({'type':'text','text':'以下才是本次实际渲染成品，请检查角色、动作与参考的一致性。'})
                        content.extend(await self.preview_blocks(job))
                        verdict=await self.review(job,content)
                        payload.setdefault('reviews',[]).append({'attempt':repairs+1,'verdict':verdict})
                        self.persist_payload(job,payload)
                        if job['autonomous'] and verdict.get('relevant') is not True:
                            self.cancel(job['id']);return
                        approved=verdict.get('approved') is True
                        if payload.get('character')=='none' and verdict.get('unwanted_character') is True:
                            approved=False
                            verdict['reason']='本次不添加人物装饰，请移除画面中的助手/吉祥物，保留数据与布局。'+str(verdict.get('reason',''))
                        if payload.get('character_required') and verdict.get('character_consistent') is not True:
                            approved=False
                            verdict['reason']='人物须保留参考中的主要辨识特征；姿势、服装和构图可以原创，不要替换成无辨识度的几何人偶。'+str(verdict.get('reason',''))
                        if not approved:
                            if repairs>=MAX_REPAIRS:raise ValueError('作品检查未通过：'+str(verdict.get('reason',''))[:300])
                            from .creation_workflow import repair_feedback
                            feedback=repair_feedback(verdict);repairs+=1;job['repairs']=repairs;self.save(job)
                            feedback_history.append({'stage':'checking','reason':feedback})
                    job.update(status='ready',format=spec['format'],caption=spec['caption'],ready_at=time.time())
                    if job['private']:payload['caption']=spec['caption'];job.pop('caption',None)
                    self.save(job)
            from .creation_delivery import deliver
            await deliver(self,job)
        except asyncio.CancelledError:
            live=self.db.get('creation_job',id,{})
            if live and not live.get('deleted'):
                if not live.get('cancel_requested'):
                    live.update(status='unknown' if live['status'] in ('sending','unknown') else 'interrupted',
                                error='制作进程已中断，未自动重复付费',error_code='worker_interrupted',finished_at=time.time());self.save(live)
            raise
        except Exception as exc:
            live=self.db.get('creation_job',id,{})
            if live and not live.get('deleted') and live['status']!='cancelled':
                error='制作超时' if isinstance(exc,TimeoutError) else ('临时制作失败，内容已清理' if job['private'] else str(exc)[:500])
                code='creation_timeout' if isinstance(exc,TimeoutError) else getattr(exc,'code',None) or type(exc).__name__
                job.update(error_stage=job['status'],status='failed',error_code=code,error=error,finished_at=time.time())
                self.save(job);await self.notify_failure(job)
        finally:
            if broker_id:await self.renderer.cancel(broker_id)
            live=self.db.get('creation_job',id,{})
            tracing.finish(self.db,trace_id,live.get('status','cancelled'),token)
            if job['private'] and live:self.purge(live,'临时作品已结束并清理')

    async def preview_blocks(self,job):
        from .image_assets import prepare
        previews=sorted([a for a in job['artifacts'] if a['mime']=='image/png'],
                        key=lambda a:a['name']!='timeline.png')[:3]
        return [await asyncio.to_thread(prepare,self.artifact_bytes(job,artifact))
                for artifact in previews]

    async def asset_preview_blocks(self,payload):
        from .image_assets import prepare
        blocks=[];used=0
        for item in payload.get('asset_manifest',[])[:4]:
            block=await asyncio.to_thread(prepare,base64.b64decode(payload['assets'][item['file']]))
            size=len(block['image_url']['url'])
            if used+size>3*MIB:continue
            blocks.extend([{'type':'text','text':'输入素材 '+item['file']+'：'+item['label']},block]);used+=size
        return blocks

    async def generate(self,job,payload,history):
        """Only the maker sees asset tools. Reads remain inside this job's authorization."""
        tools=[{'type':'function','function':{'name':'art_search','description':'按需检索已启用素材的名称和用途，不读取图片。原创背景/图表无需检索。',
            'parameters':{'type':'object','properties':{'query':{'type':'string'}},'required':['query'],'additionalProperties':False}}},
            {'type':'function','function':{'name':'art_read','description':'需要复用或观察某素材时才读取，返回预览和可用文件名。SVG 可编辑图层，素材不是必须照搬的模板。',
            'parameters':{'type':'object','properties':{'ids':{'type':'array','items':{'type':'string'},'maxItems':3}},'required':['ids'],'additionalProperties':False}}}]
        for round in range(3):
            # Reserve a generation and review after optional reads; never exceed the task call cap.
            enabled=not job.get('asset_phase_done') and round<2 and job.get('asset_rounds',0)<2 and job['model_calls']<=MAX_MODEL_CALLS-3
            response=await self.call_model(job,'creation.code',history,tools=tools if enabled else None)
            calls=response.get('tool_calls') or []
            if not calls:
                job['asset_phase_done']=True;self.save(job);return response
            if not enabled:raise ValueError('素材查询次数已用完，请直接完成作品')
            job['asset_rounds']=job.get('asset_rounds',0)+1;self.save(job)
            history.append(response)
            viewed=[]
            for call in calls[:3]:
                name=None
                try:
                    args=json.loads(call['function']['arguments']);name=call['function']['name']
                    if name=='art_search':
                        result={'assets':self.art_library.search(job['persona'],str(args.get('query',''))[:200],include_characters=payload.get('character')!='none')}
                    elif name=='art_read':
                        ids=args.get('ids')
                        if not isinstance(ids,list) or len(ids)>3 or any(not isinstance(x,str) for x in ids):raise ValueError('每次最多读取 3 个素材编号')
                        result={'assets':[]}
                        for id in dict.fromkeys(ids):
                            id=next((a['id'] for a in payload['asset_manifest'] if id in (a['file'],a.get('vector_file'))),id)
                            if payload.get('character')=='none' and self.art_library.item(id,job['persona'])['kind']=='character':
                                raise ValueError('本次不添加人物，不读取人物素材；请直接绘制数据和布局。')
                            row=next((a for a in payload['asset_manifest'] if a['id']==id),None)
                            if not row:
                                if len(payload['asset_manifest'])>=8:raise ValueError('本作品素材已达 8 个，请复用已读素材')
                                item,raw,svg=self.art_library.read(id,job['persona'])
                                ext=item['file'].rsplit('.',1)[-1]
                                if ext=='svg':item,raw,svg=self.art_library.read(id,job['persona'],preview=True);ext='png'
                                n=len(payload['asset_manifest']);filename=f'asset_{n}.{ext}'
                                if sum(len(v)*3//4 for v in payload['assets'].values())+len(raw)+len(svg or b'')>24*MIB:raise ValueError('素材总量超过 24 MiB')
                                row={k:item[k] for k in ('id','label','kind','width','height','usage')};row['file']=filename
                                payload['assets'][filename]=base64.b64encode(raw).decode()
                                if svg:
                                    row.update(vector_file=f'source_{n}.svg',layers=item.get('layers',[]))
                                    payload['assets'][row['vector_file']]=base64.b64encode(svg).decode()
                                payload['asset_manifest'].append(row)
                                payload['builtin_assets'].append({k:item[k] for k in ('id','sha256','revision','vector_sha256') if k in item})
                                payload['asset_ids'].append(id);job['asset_ids']=payload['asset_ids']
                            entry=dict(row)
                            if row.get('vector_file'):
                                vector=base64.b64decode(payload['assets'][row['vector_file']]).decode('utf8')
                                entry['svg_source']=vector if len(vector)<=40000 else vector[:40000]
                                entry['svg_truncated']=len(vector)>40000
                            result['assets'].append(entry);viewed.append(row)
                        self.persist_payload(job,payload);self.save(job)
                    else:raise ValueError('仅可查询和读取当前素材库')
                except (ValueError,KeyError,OSError) as exc:result={'error':str(exc)[:300]}
                finally:
                    if name=='art_read':self.persist_payload(job,payload);self.save(job)
                history.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(result,ensure_ascii=False)})
            if len(calls)>3:raise ValueError('单轮素材操作不能超过 3 次')
            if viewed:
                history.append({'role':'user','content':[{'type':'text','text':'以下是你刚刚按需读取的素材预览，是否复用以及如何改编由本次需求决定。'},*await self.asset_preview_blocks({**payload,'asset_manifest':viewed})]})
            await self.authorize_fresh(job)
        raise ValueError('素材查询未结束')

    async def review(self,job,content):
        from .media import MediaError
        inventory=[{k:a.get(k) for k in ('name','mime','width','height','frames','duration')} for a in job['artifacts'] if a['mime'] in ('image/png','image/gif')]
        request=json.loads(content[0]['text']);request['inspectable_artifacts']=inventory
        history=[{'role':'user','content':[{'type':'text','text':json.dumps(request,ensure_ascii=False)},*content[1:]]}]
        tools=[{'type':'function','function':{'name':'creation_inspect','description':'只在小图看不清动作、肢体或文字时裁剪当前作品。GIF可选择真实帧，PNG可裁局部。不能读取其他任务或任意路径。',
            'parameters':{'type':'object','properties':{'name':{'type':'string'},'box':{'type':'array','description':'归一化左上右下坐标，每项0–1000，不是像素坐标','items':{'type':'integer','minimum':0,'maximum':1000},'minItems':4,'maxItems':4},
                'frames':{'type':'array','items':{'type':'integer','minimum':0},'maxItems':3}},'required':['name','box'],'additionalProperties':False}}}]
        inspected=False;retried=False
        for attempt in range(3):
            enabled=not inspected and job['model_calls']<MAX_MODEL_CALLS-1
            response=await self.call_model(job,'creation.review',history,tools=tools if enabled else None)
            calls=response.get('tool_calls') or []
            if calls:
                if not enabled or len(calls)>2:raise CreationReviewError('检查工具交互超出范围，未发送')
                inspected=True;history.append(response);views=[]
                for call in calls:
                    try:
                        if call['function']['name']!='creation_inspect':raise ValueError('检查仅支持查看当前产物')
                        args=json.loads(call['function']['arguments'])
                        await self.authorize_fresh(job)
                        blocks=await asyncio.to_thread(self.inspect_artifact,job,args)
                        views.extend(blocks);result={'ok':True,'name':args['name'],'views':len(blocks)//2}
                    except (ValueError,KeyError,OSError,MediaError) as exc:result={'error':str(exc)[:500]}
                    history.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(result,ensure_ascii=False)})
                history.append({'role':'user','content':[{'type':'text','text':'本轮局部查看已结束。下面是返回的真实视图；请结合之前成品直接给最终检查 JSON，不再请求工具。如果仍有不确定，明确说明证据，不要编造缺失动作。'},*views]})
                continue
            try:
                if response.get('_finish_reason') in ('length','max_tokens'):raise ValueError('检查输出被截断')
                verdict=parse_json(response.get('content'))
                if type(verdict.get('approved')) is not bool:raise ValueError('检查缺少 approved 布尔值')
                return verdict
            except ValueError as exc:
                if retried or job['model_calls']>=MAX_MODEL_CALLS:
                    raise CreationReviewError('画面已渲染，但检查响应无效，未发送：'+str(exc)) from None
                retried=True
                # Retry the known malformed review, not the already rendered source.
                history.append({'role':'user','content':'上次检查响应未能解析。请依据同一组实际图片，返回简明完整 JSON，包含 approved 布尔值及可见依据；无需重画或重复逐帧长篇描述。'})
        raise CreationReviewError('检查未能返回完整结论，未发送')

    def inspect_artifact(self,job,args):
        import io
        from PIL import Image
        from .image_assets import region
        if not isinstance(args,dict) or set(args)-{'name','box','frames'}:raise ValueError('检查参数无效')
        artifact=next((a for a in job['artifacts'] if a['name']==args.get('name') and a['mime'] in ('image/png','image/gif')),None)
        if not artifact:raise ValueError('只能查看本次已经登记的图片或 GIF')
        box=args.get('box');frames=args.get('frames',[0])
        if (not isinstance(box,list) or len(box)!=4 or any(type(v) is not int or not 0<=v<=1000 for v in box)
                or box[0]>=box[2] or box[1]>=box[3]):raise ValueError('box 为 0–1000 的左上右下坐标，须有正面积')
        if not isinstance(frames,list) or not 1<=len(frames)<=3 or any(type(f) is not int or f<0 for f in frames):raise ValueError('每次查看 1–3 个真实帧编号')
        blocks=[]
        with Image.open(io.BytesIO(self.artifact_bytes(job,artifact))) as im:
            for index in frames:
                if index>=getattr(im,'n_frames',1):raise ValueError('帧编号超出成品范围')
                im.seek(index);raw=io.BytesIO();im.convert('RGB').save(raw,'PNG')
                block,info=region(raw.getvalue(),box);block['image_url']['detail']='high'
                blocks.extend([{'type':'text','text':f'本次实际产物 {artifact["name"]}，真实帧 {index}，区域 {box}。'},block])
        return blocks

    async def call_model(self,job,key,history,tools=None):
        if job['model_calls']>=MAX_MODEL_CALLS:raise ValueError('达到每任务十六次模型调用上限')
        await self.authorize_fresh(job)
        job['model_calls']+=1;self.save(job)
        counts=job.setdefault('call_counts',{});counts[key]=counts.get(key,0)+1;self.save(job)
        trace=self.db.get('trace',job['trace_id'],{})
        if trace:
            trace.update(source_ids=job['source_ids'],actor_ids=job['actor_ids'],image_dependencies=job.get('asset_dependencies',[]))
            self.db.put('trace',job['trace_id'],trace)
        try:
            from .creation_workflow import visual_history
            projected=visual_history(history,min(12,self.db.config().get('image_max_count',12)))
            # DeepSeek requires exact reasoning replay while tools are enabled.
            # Once the draft exists, edits run without tools; prior reasoning is
            # neither needed nor counted by that API, so omit it from our budget too.
            if not tools:projected=[{k:v for k,v in m.items() if k!='reasoning_content'} for m in projected]
            response=await self.engine.model.chat(job['scope'],'creation',
                [{'role':'system','content':prompts.render(self.db,job['scope'],key)},*projected],
                json_mode=True,max_tokens=16384 if key=='creation.plan' else 65536,stage=key,**({'tools':tools} if tools else {}))
        finally:
            rows=self.db.get('trace',job['trace_id'],{}).get('models',[])
            job['cost']=sum(r.get('cost',0) for r in rows)
            job['usage']={k:sum(r.get(k,0) or 0 for r in rows) for k in ('input_tokens','cached_tokens','output_tokens')}
            self.save(job)
        await self.authorize_fresh(job);self.save(job)
        return response

    async def receive_artifacts(self,job,broker_id,spec,result):
        wanted=set(output_names(spec));rows=result.get('artifacts',[])
        if {x['name'] for x in rows}!=wanted or len(rows)!=len(wanted):raise ValueError('渲染产物不完整')
        # Remove only prior attempt outputs, never another job's files.
        folder=self.folder(job['id'])
        if not job['private']:folder.mkdir(exist_ok=True)
        artifacts=[]
        if not job['private']:
            for old in job.get('artifacts',[]):(folder/old['name']).unlink(missing_ok=True)
        self.private_files.pop(job['id'],None)
        job['artifacts']=[];self.save(job)
        import tempfile
        with tempfile.TemporaryDirectory(prefix='creation-receive-') as transient:
            for row in rows:
                name=row['name'];suffix=name.rsplit('.',1)[-1]
                if row['bytes']>OUTPUT_LIMITS[suffix]:raise ValueError('渲染产物超限')
                path=Path(transient)/name if job['private'] else folder/name
                await self.renderer.download(broker_id,row,path)
                await self.authorize_fresh(job)
                id='art_'+hashlib.sha256((job['id']+name+row['sha256']).encode()).hexdigest()[:32]
                item={**row,'id':id,'primary':name.startswith('page-') or suffix in ('gif','mp4')}
                if job['private']:
                    self.private_files.setdefault(job['id'],{})[id]=path.read_bytes()
                    path.unlink()
                artifacts.append(item)
        if not job['private']:
            for old in job.get('artifacts',[]):
                if old['name'] not in wanted:(folder/old['name']).unlink(missing_ok=True)
        job['artifacts']=artifacts
        self.save(job)

    def artifact(self,job,id):
        return next((a for a in job['artifacts'] if a['id']==id),None)

    def artifact_bytes(self,job,artifact):
        return self.private_files[job['id']][artifact['id']] if job['private'] else (self.folder(job['id'])/artifact['name']).read_bytes()

    def media_url(self,job,artifact):
        token=secrets.token_urlsafe(32)
        self.tokens[token]={'job_id':job['id'],'artifact_id':artifact['id'],'expires':time.time()+300}
        base=os.getenv('CREATION_MEDIA_BASE','http://qq-rolebot:8080').rstrip('/')
        return base+'/internal/creation-media/'+token

    async def notify_failure(self,job):
        if job.get('notified') or job.get('deleted') or job['autonomous']:return
        try:
            await self.authorize_fresh(job)
            from .creation_delivery import failure
            await failure(self,job)
        except Exception:pass
