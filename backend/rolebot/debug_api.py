"""Administrator-only diagnostics and editable prompts, behind the shared API auth."""
import copy
import json
import time

from fastapi import HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from .prompts import PromptStore, DEFAULTS, render_body
from . import tracing
from .config_schema import number_field, save_global


class Body(BaseModel):
    model_config=ConfigDict(extra="forbid")


class Draft(Body):
    scope: str = Field(default="",max_length=120)
    body: str = Field(min_length=1,max_length=64000)
    base_version: int = Field(ge=0)


class Publish(Body):
    scope: str = Field(default="",max_length=120)
    base_version: int = Field(ge=0)
    draft_hash: str | None = Field(default=None,min_length=64,max_length=64)


class Preview(Body):
    body: str = Field(min_length=1,max_length=64000)
    variables: dict[str,str] = Field(default_factory=dict)


class Retention(Body):
    days: int = number_field("global.model_log_days")
    image_hours: int = number_field("global.model_log_image_hours")


class Replay(Body):
    request: dict | None = None
    mock_results: dict = Field(default_factory=dict)
    rounds: int = Field(default=1,ge=1,le=4)


def register(api,db,model):
    store=PromptStore(db);records=model.records

    def require(key,scope=""):
        if key not in DEFAULTS or (scope and not db.get("group",scope)):
            raise HTTPException(404,"提示词或会话不存在")

    def operate(fn):
        try:return fn()
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc

    @api.get("/prompts")
    async def catalog(scope: str=""):
        if scope and not db.get("group",scope):raise HTTPException(404,"会话不存在")
        return store.catalog(scope)

    @api.put("/prompts/{key}/draft")
    async def draft(key: str,body: Draft):
        require(key,body.scope)
        return operate(lambda:store.draft(body.scope,key,body.body,body.base_version))

    @api.post("/prompts/{key}/preview")
    async def preview(key: str,body: Preview):
        require(key)
        text=operate(lambda:render_body(key,body.body,body.variables))
        return {"text":text,"characters":len(text),"model_called":False}

    @api.post("/prompts/{key}/publish")
    async def publish(key: str,body: Publish):
        require(key,body.scope)
        if not body.draft_hash:raise HTTPException(409,"请先保存并核对草稿")
        return operate(lambda:store.publish(body.scope,key,body.base_version,draft_hash=body.draft_hash))

    @api.get("/prompts/{key}/versions")
    async def versions(key: str,scope: str=""):
        require(key,scope)
        return [{"key":key,"scope":"","body":DEFAULTS[key]["body"],"version":0,"reason":"builtin"}]+store.versions(scope,key)

    @api.post("/prompts/{key}/restore/{version}")
    async def restore(key: str,version: int,body: Publish):
        require(key,body.scope)
        return operate(lambda:store.restore(body.scope,key,version,body.base_version))

    @api.get("/model-calls/settings")
    async def retention():
        cfg=db.config()
        return {"days":cfg["model_log_days"],"image_hours":cfg["model_log_image_hours"]}

    @api.put("/model-calls/settings")
    async def set_retention(body: Retention):
        save_global(db, {'model_log_days': body.days, 'model_log_image_hours': body.image_hours})
        records.cleanup()
        db.audit("","model_log_retention",body.model_dump())
        return body

    @api.get("/model-calls")
    async def calls(scope: str | None=None,trace_id: str | None=None,purpose: str | None=None,status: str | None=None,limit: int=40,offset: int=0,start: float | None=None,end: float | None=None,model: str | None=None):
        return records.listing(scope,trace_id,purpose,status,limit,offset,start,end,model)

    @api.get("/model-calls/images/{id}")
    async def image(id: str):
        rows=db.rows("SELECT i.* FROM call_images i JOIN model_calls c ON c.id=i.call_id WHERE i.id=? AND i.expires>? AND c.expires>?",(id,time.time(),time.time()))
        if not rows:raise HTTPException(410,"图片已过期、遗忘或不存在")
        item=rows[0];mime=item["mime"] if item["mime"] in ("image/png","image/jpeg","image/webp","image/gif") else "application/octet-stream"
        return Response(item["content"],media_type=mime,headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})

    @api.get("/model-calls/{id}")
    async def detail(id: str):
        value=records.detail(id)
        if not value:raise HTTPException(404,"记录未采集、已过期或不存在")
        return value

    @api.get("/model-calls/{id}/export")
    async def export(id: str,include_images: bool=False):
        value=await detail(id)
        if include_images:
            for key in ("request","response","input"):
                if key in value:value[key]=operate(lambda:records.expand(value[key]))
        return Response(json.dumps(value,ensure_ascii=False,indent=2),media_type="application/json",headers={"Content-Disposition":'attachment; filename="model-call-'+id+'.json"',"Cache-Control":"no-store"})

    @api.post("/model-calls/{id}/replay")
    async def replay(id: str,body: Replay):
        original=await detail(id)
        if original.get("redacted") or not original.get("request") or original["resource"] not in ("chat","messages"):
            raise HTTPException(409,"该记录不可回放（已清理、未发送或不是模型对话）")
        catalog=model.catalog.get()
        provider=next((p for p in catalog["providers"] if p["id"]==original["provider_id"] and p["enabled"]),None)
        if not provider or provider["protocol"]!=original["protocol"]:raise HTTPException(409,"供应商协议已变化或已停用")
        payload=operate(lambda:records.expand(copy.deepcopy(body.request if body.request is not None else original["request"])))
        allowed={"model","messages","system","max_tokens","stream","thinking","reasoning_effort","output_config","tools","response_format"}
        configured=next((m for m in catalog["models"] if m["provider_id"]==provider["id"] and m["model_id"]==payload.get("model") and m["enabled"]),None)
        if set(payload)-allowed or not configured or not isinstance(payload.get("messages"),list) or type(payload.get("max_tokens")) is not int or not 1<=payload["max_tokens"]<=configured["max_tokens"]:
            raise HTTPException(422,"请求字段、模型、消息或输出上限无效")
        # The preview uses exactly one provider; it never invokes the QQ/tool runner.
        payload["stream"]=False
        epoch=db.get("memory_epoch",original["scope"],{"value":0})["value"]
        trace_id,token=tracing.begin(db,original["scope"],original.get("persona") or "",[],kind="replay")
        trace=db.get("trace",trace_id);trace.update(parent_id=original.get("trace_id"),source_ids=original.get("source_ids",[]),actor_ids=original.get("actor_ids",[]),replay_of=id)
        db.put("trace",trace_id,trace)
        calls=[];status="complete"
        try:
            for index in range(body.rounds):
                if db.get("memory_epoch",original["scope"],{"value":0})["value"]!=epoch:raise HTTPException(409,"来源已被遗忘，回放停止")
                live=model.catalog.get()
                if provider not in live["providers"] or configured not in live["models"]:
                    raise HTTPException(409,"供应商或模型配置已变化，试跑停止")
                # Count images by allowance rather than raw base64, like normal requests.
                def estimate(v):
                    if isinstance(v,dict):return 4096 if v.get("type") in ("image","image_url") else sum(len(k)+estimate(x) for k,x in v.items())
                    if isinstance(v,list):return sum(estimate(x) for x in v)
                    return len(v) if isinstance(v,str) else 1
                if estimate(payload)+payload["max_tokens"]>configured["context_window"]:raise HTTPException(422,"回放请求超过模型容量")
                data=await model.request(original["resource"],payload,provider,model.catalog.key(provider["id"]),scope=original["scope"],purpose="replay",internal={"stage":"replay","replay_of":id})
                calls.append(data["_record_id"])
                if original["resource"]=="messages":
                    blocks=data.get("content",[]);tools=[x for x in blocks if x.get("type")=="tool_use"]
                    payload["messages"].append({"role":"assistant","content":blocks})
                    if not tools:break
                    results=[{"type":"tool_result","tool_use_id":t["id"],"content":json.dumps({"simulated":True,"result":body.mock_results.get(t["name"],{"notice":"未提供模拟结果；真实工具未执行"})},ensure_ascii=False)} for t in tools]
                    payload["messages"].append({"role":"user","content":results})
                else:
                    message=data.get("choices",[{}])[0].get("message",{});tools=message.get("tool_calls") or []
                    payload["messages"].append(message)
                    if not tools:break
                    for t in tools:
                        payload["messages"].append({"role":"tool","tool_call_id":t["id"],"content":json.dumps({"simulated":True,"result":body.mock_results.get(t["function"]["name"],{"notice":"未提供模拟结果；真实工具未执行"})},ensure_ascii=False)})
                tracing.stage(db,"replay_tools",{"proposals":tools,"executed":False,"mock_results":body.mock_results})
            return {"trace_id":trace_id,"call_ids":calls,"tools_simulated":True,"qq_messages_sent":0,"notice":"副本试跑完成；只调用模型，未保存业务记忆或执行真实工具"}
        except BaseException:
            status="error";raise
        finally:tracing.finish(db,trace_id,status,token)
