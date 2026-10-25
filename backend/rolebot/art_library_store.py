"""Managed art files. Metadata is in SQLite; bytes stay outside the database."""
import copy,hashlib,io,re,time
from pathlib import Path
from xml.etree import ElementTree as ET
from PIL import Image
from . import art_assets
from .db import uid
from .persona_names import pattern as name_pattern, strip_address

MIB=1024*1024
MAX_BYTES=8*MIB
MAX_STORAGE=512*MIB
MAX_COUNT=256
SVG_LIMIT=96*1024
KINDS={'character','background','decoration','prop','panel','reference'}

def svg_source(raw):
    if len(raw)>SVG_LIMIT:raise ValueError('SVG 超过 96 KiB，请简化路径')
    text=raw.decode('utf-8-sig')
    if re.search(r'<!|<\?',text.replace('<?xml version="1.0" encoding="UTF-8"?>',''),re.I):
        raise ValueError('SVG 不允许外部声明或处理指令')
    try:root=ET.fromstring(text)
    except ET.ParseError:raise ValueError('SVG XML 格式无效') from None
    tags={'svg','g','defs','path','rect','circle','ellipse','line','polyline','polygon','text','tspan',
          'linearGradient','radialGradient','stop','clipPath','mask','use','title','desc','style'}
    nodes=list(root.iter())
    if len(nodes)>3000 or root.tag.rsplit('}',1)[-1]!='svg':raise ValueError('SVG 结构过大或无效')
    for node in nodes:
        if node.tag.startswith('{') and not node.tag.startswith('{http://www.w3.org/2000/svg}'):
            raise ValueError('SVG 命名空间无效')
        if node.tag.rsplit('}',1)[-1] not in tags:raise ValueError('SVG 含不支持的元素')
        for key,value in node.attrib.items():
            key=key.rsplit('}',1)[-1]
            if key.lower().startswith('on') or key in ('base','src') or (key=='href' and not re.fullmatch(r'#[A-Za-z_][\w.-]*',value)):
                raise ValueError('SVG 不允许脚本或外部链接')
        css=' '.join(node.attrib.values())+' '+(node.text or '')
        if re.search(r'@import|@font|javascript:|https?:|data:|file:|expression\s*\(|\\|/\*',css,re.I):
            raise ValueError('SVG 不允许外部资源或动态内容')
        for target in re.findall(r'url\s*\((.*?)\)',css,re.I):
            if not re.fullmatch(r'[\s\'"]*#[\w.-]+[\s\'"]*',target):raise ValueError('SVG 只能引用内部图层')
    box=root.get('viewBox','').replace(',',' ').split()
    try:
        w,h=(float(box[2]),float(box[3])) if len(box)==4 else (float(root.get('width','0').removesuffix('px')),float(root.get('height','0').removesuffix('px')))
        if not 1<=w<=4096 or not 1<=h<=4096 or w*h>8_000_000:raise ValueError()
    except (ValueError,IndexError):raise ValueError('SVG 需要有效 viewBox 或尺寸，最长边不能超过 4096') from None
    return text,int(w),int(h),[n.get('id') for n in nodes if n.tag.rsplit('}',1)[-1]=='g' and n.get('id')][:60]

def raster(raw):
    if not 0<len(raw)<=MAX_BYTES:raise ValueError('素材大小须在 8 MiB 以内')
    with Image.open(io.BytesIO(raw)) as im:
        if im.format not in ('PNG','JPEG','WEBP','GIF') or im.width*im.height>8_000_000 or max(im.size)>4096:
            raise ValueError('图片格式或尺寸不支持，最长边 4096、最多 800 万像素')
        preview=io.BytesIO();im.convert('RGBA').save(preview,'PNG')
        ext={'PNG':'png','JPEG':'jpg','WEBP':'webp','GIF':'gif'}[im.format]
        return ext,im.width,im.height,preview.getvalue()

class ArtLibrary:
    def __init__(self,service):
        self.service=service;self.db=service.db
        self.root=service.engine.settings.data_dir/'creation-assets';self.root.mkdir(exist_ok=True,mode=0o700)

    def listing(self,persona=None,admin=False):
        overrides={a['id']:a for a in self.db.all('creation_asset')}
        values=[]
        for base in art_assets.manifest()['assets']:
            row={**base,'builtin':True,'enabled':True,'deleted':False,'revision':0,**overrides.pop(base['id'],{})}
            values.append(row)
        values.extend(overrides.values())
        if not admin:values=[a for a in values if a.get('enabled') and not a.get('deleted') and (not a.get('persona') or a['persona']==persona)]
        return sorted(values,key=lambda a:(a['kind']!='character',a['label'],a['id']))

    def item(self,id,persona=None,admin=False):
        row=next((a for a in self.listing(persona,admin) if a['id']==id),None)
        if not row:raise ValueError('素材不存在、已停用或不属于当前角色')
        return row

    def path(self,row,preview=False):
        name=row['preview_file' if preview else 'file']
        path=self.root/name
        if not re.fullmatch(r'[0-9a-f]{32}(?:-preview)?\.(?:png|jpg|gif|webp|svg)',name) or path.is_symlink() or path.parent.resolve()!=self.root.resolve():
            raise ValueError('素材路径无效')
        return path

    def read(self,id,persona=None,preview=False,admin=False):
        row=self.item(id,persona,admin)
        if row.get('deleted'):raise ValueError('素材已删除')
        if row.get('builtin'):
            original=next(a for a in art_assets.manifest()['assets'] if a['id']==id)
            original,raw=art_assets.read(id,original.get('persona'))
            svg=(art_assets.ROOT/'sources'/original['source']).read_bytes()
        else:
            raw=self.path(row,preview).read_bytes();svg=self.path(row).read_bytes() if row['file'].endswith('.svg') else None
        expected=row.get('preview_sha256') if preview and not row.get('builtin') else row['sha256']
        if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('素材文件已损坏')
        if row.get('builtin'):
            row['vector_sha256']=hashlib.sha256(svg).hexdigest()
            row['layers']=svg_source(svg)[3]
        return row,raw,svg

    def current(self,snapshot,persona):
        try:
            row,_,_=self.read(snapshot['id'],persona)
            return all(snapshot.get(k,row.get(k))==row.get(k) for k in ('sha256','vector_sha256')) and snapshot.get('revision',0)==row['revision']
        except (ValueError,OSError,KeyError):return False

    def edit(self,id,changes,revision):
        before=self.item(id,admin=True)
        if before['revision']!=revision:raise RuntimeError('素材已在别处修改，请刷新')
        row={**before,**changes,'revision':revision+1}
        if row['kind'] not in KINDS or not 1<=len(row['label'].strip())<=80 or len(row.get('usage',''))>500:raise ValueError('素材名称、用途或类别无效')
        if row.get('persona') and not self.db.get('persona',row['persona']):raise ValueError('绑定角色不存在')
        if row['kind']=='character' and not row.get('persona'):raise ValueError('人物素材必须绑定一个角色')
        if before.get('deleted') and not before.get('builtin'):raise ValueError('已删除的上传素材需要重新上传')
        stored={k:row[k] for k in ('id','enabled','deleted','revision','label','usage','kind','persona') if k in row} if row.get('builtin') else row
        self.db.put('creation_asset',id,stored);self.db.audit('','creation_asset_edit',{'id':id,'fields':list(changes),'revision':row['revision']})
        return row

    def delete(self,id,revision):
        row=self.edit(id,{'deleted':True,'enabled':False},revision)
        if not row.get('builtin'):
            self.path(row).unlink(missing_ok=True);self.path(row,True).unlink(missing_ok=True)
        return row

    def store(self,raw,preview,ext,width,height,label,kind,persona,usage,layers=()):
        if kind not in KINDS or not 1<=len(label.strip())<=80 or len(usage)>500:raise ValueError('素材信息无效')
        if persona and not self.db.get('persona',persona):raise ValueError('绑定角色不存在')
        if kind=='character' and not persona:raise ValueError('人物素材必须绑定一个角色')
        if len([a for a in self.listing(admin=True) if not a.get('deleted')])>=MAX_COUNT:raise ValueError('素材库最多 256 个，请先清理')
        used=sum(p.stat().st_size for p in self.root.iterdir() if p.is_file() and not p.is_symlink())
        if used+len(raw)+len(preview)>MAX_STORAGE:raise ValueError('素材库已达到 512 MiB，请先清理')
        id=uid();row={'id':'art:custom_'+id,'label':label.strip(),'kind':kind,'persona':persona or None,'usage':usage,
            'builtin':False,'enabled':True,'deleted':False,'revision':1,'file':id+'.'+ext,'preview_file':id+'-preview.png',
            'width':width,'height':height,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
            'preview_sha256':hashlib.sha256(preview).hexdigest(),'layers':list(layers),'created_at':time.time()}
        try:
            self.path(row).write_bytes(raw);self.path(row,True).write_bytes(preview)
            self.db.put('creation_asset',row['id'],row)
        except BaseException:
            self.path(row).unlink(missing_ok=True);self.path(row,True).unlink(missing_ok=True);raise
        self.db.audit('','creation_asset_upload',{'id':row['id'],'bytes':len(raw),'kind':kind})
        return row

    def character_mode(self,brief,source,character='auto',persona=''):
        query=(source+' '+brief).lower()
        identity=name_pattern(self.db,persona,extra=('人物','角色','你本人'))
        if character=='none' or re.search(r'(不要|不含|不画|去掉|不加).{0,4}'+identity,query,re.I):return 'none'
        if character=='current':return 'current'
        query=strip_address(source,self.db,persona)+' '+strip_address(brief,self.db,persona)
        subject=name_pattern(self.db,persona,extra=('你本人',))
        if re.search(subject,query,re.I) or re.search(r'(画|绘制|做).{0,6}你.{0,6}(抱|躺|坐|站|像|图)',query):return 'current'
        return 'auto'

    def select(self,persona,brief,source,explicit=(),character='auto',inherited=False):
        # Search metadata only when a creation is requested. No per-chat asset list.
        query=(source+' '+brief).lower()
        character=self.character_mode(brief,source,character,persona)
        needs=character=='current'
        candidates=self.listing(persona)
        def score(row):
            text=(row['label']+' '+row.get('usage','')+' '+str(row.get('tags',''))).lower()
            words=set(re.findall(r'[a-z]{3,}|[\u4e00-\u9fff]{2}',query))
            value=sum(1 for word in words if word in text)
            for words,weight in ((('躺','卧','床'),20),(('坐','椅'),16),(('鱼','抱鱼'),8),(('杯','喝','茶'),8),(('晚安','夜','星空'),5),(('招手','你好'),5)):
                positive=(row['label']+' '+str(row.get('tags',''))).lower()
                if any(w in query for w in words) and any(w in positive for w in words):value+=weight
            return value
        chosen=list(dict.fromkeys(explicit));provided=[self.item(id,persona) for id in chosen if id.startswith('art:')]
        if character=='none':
            excluded={a['id'] for a in provided if a['kind']=='character'}
            chosen=[id for id in chosen if id not in excluded and not id.startswith('ref_')]
        if needs and not any(a['kind']=='character' for a in provided):
            chars=[a for a in candidates if a['kind']=='character' and a.get('persona')==persona]
            if not chars:raise ValueError('当前角色的人物素材均已停用或删除，请在后台启用或上传人物素材')
            best=max(chars,key=score)
            if len(chosen)>=8:raise ValueError('人物作品需要角色参考，请为人物素材留出一个位置')
            chosen.append(best['id'])
        # Backgrounds/props are creative choices for the maker, not automatic defaults.
        return chosen,needs

    def search(self,persona,query,include_characters=True):
        query=query.lower()
        rows=[a for a in self.listing(persona) if include_characters or a['kind']!='character']
        tokens=re.findall(r'[a-z]{3,}|[\u4e00-\u9fff]{1,2}',query)
        def score(a):return sum(t in (a['label']+' '+a.get('usage','')).lower() for t in tokens)
        rows=sorted(rows,key=score,reverse=True)
        return [{k:a[k] for k in ('id','label','kind','width','height','usage')} for a in rows[:6] if not tokens or score(a)>0]
