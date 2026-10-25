"""Administrator art management. SVG previews run in the existing isolated broker."""
import asyncio,io,json,tempfile
from pathlib import Path
from typing import Literal
from fastapi import UploadFile,File,Form,HTTPException
from fastapi.responses import Response
from pydantic import BaseModel,ConfigDict,Field
from .art_library_store import MAX_BYTES,MAX_STORAGE,MAX_COUNT,raster,svg_source
from .db import uid

class Edit(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    expected_revision:int=Field(ge=0)
    enabled:bool|None=None
    label:str|None=Field(default=None,min_length=1,max_length=80)
    usage:str|None=Field(default=None,max_length=500)
    kind:Literal['character','background','decoration','prop','panel','reference']|None=None
    persona:str|None=Field(default=None,max_length=80)
    deleted:bool|None=None

def register(api,service):
    library=service.art_library
    def attempt(fn):
        try:return fn()
        except RuntimeError as e:raise HTTPException(409,str(e)) from None
        except (ValueError,OSError) as e:raise HTTPException(400,str(e)) from None

    @api.get('/creation-assets')
    async def listing():
        rows=library.listing(admin=True)
        return {'items':rows,'limits':{'bytes':MAX_BYTES,'storage_bytes':MAX_STORAGE,'count':MAX_COUNT},
            'stored_bytes':sum(p.stat().st_size for p in library.root.iterdir() if p.is_file() and not p.is_symlink())}

    @api.post('/creation-assets')
    async def upload(file:UploadFile=File(...),label:str=Form(...),kind:str=Form('decoration'),persona:str=Form(''),usage:str=Form('')):
        raw=await file.read(MAX_BYTES+1)
        if not 0<len(raw)<=MAX_BYTES:raise HTTPException(413,'素材不能超过 8 MiB')
        try:
            if (file.filename or '').lower().endswith('.svg'):
                text,w,h,layers=svg_source(raw);raw=text.encode('utf8')
                async with service.admission:
                    if service.active or service.asset_preview_busy:raise HTTPException(409,'制作正在运行，稍后再上传 SVG')
                    health=await service.renderer.health()
                    if not health.get('can_start'):raise HTTPException(409,'渲染服务暂忙或不可用，稍后再上传 SVG')
                    if service.active or service.asset_preview_busy:raise HTTPException(409,'制作正在运行，稍后再上传 SVG')
                    service.asset_preview_busy=True
                id=uid()
                try:
                    scale=min(1,1024/max(w,h));pw,ph=max(64,round(w*scale)),max(64,round(h*scale))
                    code='<html><style>body{margin:0;background:white}svg{width:100vw;height:100vh}</style>'+text+'<script>window.ready=Promise.resolve();window.renderFrame=()=>{};</script></html>'
                    spec={'runtime':'web','format':'png','width':pw,'height':ph,'pages':1,'fps':1,'duration':0,'code':code,'caption':''}
                    async with asyncio.timeout(75):
                        await service.renderer.submit(id,spec,{}, {},60)
                        while True:
                            result=await service.renderer.status(id)
                            if result['state']=='complete':break
                            if result['state'] in ('failed','cancelled'):raise ValueError('SVG 预览未能渲染，请检查文件')
                            await asyncio.sleep(.5)
                        item=next(x for x in result['artifacts'] if x['name']=='page-0.png')
                        with tempfile.TemporaryDirectory(prefix='art-preview-',dir=library.root) as folder:
                            target=Path(folder)/'preview.png';await service.renderer.download(id,item,target);preview=target.read_bytes()
                    # Validate returned preview before persisting the uploaded file.
                    raster(preview)
                    return library.store(raw,preview,'svg',w,h,label,kind,persona,usage,layers)
                finally:
                    try:await service.renderer.cancel(id)
                    finally:service.asset_preview_busy=False
            ext,w,h,preview=raster(raw)
            return library.store(raw,preview,ext,w,h,label,kind,persona,usage)
        except HTTPException:raise
        except TimeoutError:raise HTTPException(504,'SVG 预览超时，素材未保存') from None
        except (ValueError,OSError) as e:raise HTTPException(400,str(e)) from None

    @api.put('/creation-assets/{id}')
    async def edit(id:str,body:Edit):
        changes=body.model_dump(exclude_unset=True);revision=changes.pop('expected_revision')
        if any(v is None for k,v in changes.items() if k!='persona'):raise HTTPException(422,'字段不能为空')
        if changes.get('deleted') is True:raise HTTPException(422,'删除请使用删除入口')
        return attempt(lambda:library.edit(id,changes,revision))

    @api.delete('/creation-assets/{id}')
    async def delete(id:str,expected_revision:int):return attempt(lambda:library.delete(id,expected_revision))

    @api.get('/creation-assets/{id}/file')
    async def file(id:str,source:bool=False):
        row,raw,svg=attempt(lambda:library.read(id,admin=True,preview=True))
        mime='image/png'
        ext='png'
        if source:
            row,raw,svg=attempt(lambda:library.read(id,admin=True))
            if svg:raw=svg;ext='svg'
            else:ext=row['file'].rsplit('.',1)[-1]
            mime={'svg':'image/svg+xml','png':'image/png','jpg':'image/jpeg','webp':'image/webp','gif':'image/gif'}[ext]
        headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
                 'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; sandbox"}
        if source:headers['Content-Disposition']='attachment; filename="artwork.'+ext+'"'
        return Response(raw,media_type=mime,headers=headers)
