"""Authenticated administration and token-bound, non-public NapCat media delivery."""
import json
import time

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict

from .creations import CreationConfig


class Favorite(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    favorite: bool


def register(app,api,service):
    db=service.db
    from .art_library_api import register as register_art
    register_art(api,service)
    def get(id):
        try:return service.get(id)
        except ValueError as exc:raise HTTPException(404,str(exc)) from None
    def operate(fn):
        try:return fn()
        except ValueError as exc:raise HTTPException(400,str(exc)) from None
    def response(job,artifact,download=False):
        if not artifact or job.get('deleted'):raise HTTPException(410,'产物已清理')
        media=artifact['mime']
        if media not in ('image/png','image/gif','video/mp4'):raise HTTPException(410,'产物类型无效')
        headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}
        if job['private']:
            try:return Response(service.artifact_bytes(job,artifact),media_type=media,headers=headers)
            except KeyError:raise HTTPException(410,'临时产物已清理') from None
        path=service.folder(job['id'])/artifact['name']
        if not path.is_file() or path.is_symlink():raise HTTPException(410,'产物已清理')
        return FileResponse(path,media_type=media,filename=artifact['name'] if download else None,headers=headers)

    @api.get('/creations/settings')
    async def settings():
        return {'config':service.config(),'usage':service.usage(),'renderer':await service.renderer.health()}

    @api.put('/creations/settings')
    async def save_settings(body: CreationConfig,expected_revision: int):
        with db.transaction():
            current=service.config()
            if current['revision']!=expected_revision:raise HTTPException(409,'制作设置已变化，请刷新')
            value=body.model_dump();value['revision']=current['revision']+1
            if 'context_tokens' not in body.model_fields_set:value['context_tokens']=current['context_tokens']
            usage=service.usage()
            if usage['stored_bytes']+usage['reserved_bytes']>value['storage_mib']*1024**2 or usage['favorite_bytes']>value['favorites_mib']*1024**2:
                raise HTTPException(400,'新配额低于已占用或预留空间，请先清理')
            db.put('creation_config','global',value)
            db.audit('','creation_config',{'revision':value['revision'],'enabled':value['enabled']})
        return value

    @api.get('/creations')
    async def listing(scope: str='',limit: int=100):
        service.cleanup()
        return [service.public(j) for j in sorted(db.all('creation_job'),key=lambda j:j['created_at'],reverse=True)
                if not scope or j['scope']==scope][:max(1,min(limit,200))]

    @api.get('/creations/{id}')
    async def detail(id: str):
        return service.public(get(id),detail=True)

    @api.post('/creations/{id}/cancel')
    async def cancel(id: str):
        return operate(lambda:service.cancel(id,origin='administrator'))

    @api.delete('/creations/{id}')
    async def delete(id: str):
        job=get(id);service.purge(job,'管理员清理')
        return {'deleted':True}

    @api.put('/creations/{id}/favorite')
    async def favorite(id: str,body: Favorite):
        return operate(lambda:service.favorite(id,body.favorite))

    @api.get('/creations/{id}/source')
    async def source(id: str):
        job=get(id)
        if job['private'] or job.get('deleted'):raise HTTPException(410,'源文件未保存或已清理')
        payload=service.payload(job)
        value={k:payload.get(k) for k in ('brief','data','evidence','spec','design','reviews','math_checks','plan')}
        return Response(json.dumps(value,ensure_ascii=False,indent=2),media_type='application/json',
                        headers={'Content-Disposition':'attachment; filename="creation-'+id+'.json"','X-Content-Type-Options':'nosniff'})

    @api.get('/creations/{id}/artifacts/{artifact_id}')
    async def artifact(id: str,artifact_id: str,download: bool=False):
        job=get(id)
        return response(job,service.artifact(job,artifact_id),download)

    @app.get('/internal/creation-media/{token}')
    async def internal(token: str):
        item=service.tokens.get(token)
        if not item or item['expires']<time.time():raise HTTPException(404)
        job=get(item['job_id'])
        if job['status'] not in ('sending','sent','unknown'):raise HTTPException(404)
        # NapCat downloads in a separate HTTP task, which does not inherit the
        # sending worker's request-local QQ identity observations.
        try:await service.authorize_fresh(job)
        except Exception:raise HTTPException(404) from None
        return response(job,service.artifact(job,item['artifact_id']))
