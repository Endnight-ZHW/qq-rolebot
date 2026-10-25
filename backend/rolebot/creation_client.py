"""Bounded client for the trusted Unix-socket rendering broker."""
import hashlib
import json
import os
from pathlib import Path
import httpx

from .creation_contract import MIB, RENDER_PROTOCOL


class RenderClient:
    def __init__(self):
        self.socket=os.getenv('CREATION_RENDER_SOCKET','/run/qq-rolebot-render/broker.sock')

    @property
    def configured(self):
        return Path(self.socket).exists()

    def client(self):
        return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=self.socket),
            base_url='http://renderer',timeout=10,trust_env=False)

    async def request(self,method,path,body=None):
        async with self.client() as client:
            response=await client.request(method,path,json=body)
            response.raise_for_status()
            return response.json()

    async def health(self):
        if not self.configured:return {'ready':False,'can_start':False}
        try:
            value=await self.request('GET','/health')
            if value.get('protocol')!=RENDER_PROTOCOL:
                return {**value,'ready':False,'can_start':False,'reason':'制作环境版本不匹配，需要同步更新应用与渲染服务'}
            return value
        except (httpx.HTTPError,OSError):return {'ready':False,'can_start':False}

    async def submit(self,id,spec,data,assets,timeout):
        body={'id':id,'spec':spec,'data':data,'assets':assets,'timeout_seconds':max(1,min(180,int(timeout)))}
        if len(json.dumps(body).encode())>40*MIB:raise ValueError('制作输入超过传输上限')
        return await self.request('POST','/jobs',body)

    async def status(self,id):
        return await self.request('GET','/jobs/'+id)

    async def cancel(self,id):
        try:await self.request('DELETE','/jobs/'+id)
        except (httpx.HTTPError,OSError):pass

    async def download(self,id,item,target):
        if not 0<item['bytes']<=24*MIB:raise ValueError('产物大小无效')
        digest=hashlib.sha256();size=0
        temporary=target.with_suffix('.part')
        try:
            async with self.client() as client:
                async with client.stream('GET','/jobs/'+id+'/'+item['name']) as response:
                    response.raise_for_status()
                    with temporary.open('xb') as output:
                        async for block in response.aiter_bytes(65536):
                            size+=len(block)
                            if size>item['bytes']:raise ValueError('产物实际大小超限')
                            output.write(block);digest.update(block)
            if size!=item['bytes'] or digest.hexdigest()!=item['sha256']:raise ValueError('产物校验失败')
            temporary.replace(target)
        finally:temporary.unlink(missing_ok=True)
