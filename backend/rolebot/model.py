from . import prompts
import asyncio
import hashlib
import json
import time
from urllib.parse import urlsplit

import httpx

from .db import uid
from .providers import ProviderCatalog
from .tracing import model_usage
from . import tracing
from .call_records import Records
from .input_budget import measure, check_images


class ModelError(Exception):
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


CREATION_TIMEOUT = 300


def parse_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value


class Model:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(65, connect=10), follow_redirects=False)
        self.limit = asyncio.Semaphore(2)
        self.creation_limit = asyncio.Semaphore(1)
        self.catalog = ProviderCatalog(db, settings)
        self.records = Records(db, settings)
        db.scrub_log = self.records.clean
        db.execute('CREATE TABLE IF NOT EXISTS usage_details(usage_id TEXT PRIMARY KEY,call_id TEXT UNIQUE,stage TEXT,provider_id TEXT,input_known INTEGER,cache_known INTEGER,output_known INTEGER,cache_miss_tokens INTEGER,estimated_input INTEGER)')

    @property
    def configured(self):
        return bool(self.catalog.ready())

    async def close(self):
        await self.client.aclose()

    def context_budget(self):
        return min(self.db.config()["context_tokens"], *(self.catalog.resolve(p)["model"]["context_window"] - self.catalog.resolve(p)["route"]["max_tokens"] - 3000 for p in ("chat", "decision")))

    def record(self, scope, model, purpose, usage=None, flat_cost=0, duration_ms=0, estimated_tokens=None, *, stage=None, call_id=None, provider_id=None, estimate_source='conservative_characters'):
        cfg, usage = self.db.config(), usage or {}
        prices = cfg["prices"]
        incoming = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
        cached = int(usage.get("prompt_cache_hit_tokens", usage.get("cache_read_input_tokens", (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))))
        if "input_tokens" in usage and "prompt_tokens" not in usage:
            incoming += cached + int(usage.get("cache_creation_input_tokens", 0))
        outgoing = int(usage.get("completion_tokens", usage.get("output_tokens", 0)))
        input_known = 'prompt_tokens' in usage or 'input_tokens' in usage
        cache_known = any(k in usage for k in ('prompt_cache_hit_tokens', 'cache_read_input_tokens')) or 'cached_tokens' in (usage.get('prompt_tokens_details') or {})
        output_known = 'completion_tokens' in usage or 'output_tokens' in usage
        miss = int(usage['prompt_cache_miss_tokens']) if 'prompt_cache_miss_tokens' in usage else max(0, incoming-cached) if input_known and cache_known else None
        cost = (max(0, incoming-cached)*prices["input"] + cached*prices["cached"] + outgoing*prices["output"])/1e6 + flat_cost
        id = uid()
        self.db.execute("INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?,?)", (id, scope, model, purpose, incoming, cached, outgoing, cost, cfg["currency"], time.time()))
        self.db.execute('INSERT INTO usage_details VALUES(?,?,?,?,?,?,?,?,?)', (id,call_id,stage or purpose,provider_id,int(input_known),int(cache_known),int(output_known),miss,estimated_tokens))
        metrics = {'input_tokens': incoming if input_known else None, 'cached_tokens': cached if cache_known else None,
                   'cache_miss_tokens': miss, 'output_tokens': outgoing if output_known else None,
                   'estimated_input': estimated_tokens, 'estimate_source': estimate_source, 'usage_id': id}
        if call_id:
            rows = self.db.rows('SELECT meta FROM model_calls WHERE id=?', (call_id,))
            if rows:
                meta = json.loads(rows[0]['meta']); meta['usage'] = metrics
                self.db.execute('UPDATE model_calls SET meta=? WHERE id=?', (json.dumps(meta,ensure_ascii=False),call_id))
        model_usage(self.db, {"usage_id": id, "model": model, "purpose": purpose, "input_tokens": incoming, "cached_tokens": cached,
                             "output_tokens": outgoing, "cost": cost, "currency": cfg["currency"], "duration_ms": duration_ms, "estimated_tokens": estimated_tokens,
                             'stage': stage or purpose, 'call_id': call_id, 'availability': metrics})

    @staticmethod
    def endpoint(provider, resource):
        base = provider["base_url"].rstrip("/")
        if resource in ("messages", "models"):
            return base + ("" if urlsplit(base).path.endswith("/v1") else "/v1") + "/" + resource
        if resource.startswith("tools/"):
            # Native Search/Fetch live beside /anthropic, never below it.
            for suffix in ("/v1", "/anthropic"):
                if base.endswith(suffix):
                    base = base[:-len(suffix)]
            return base + "/v1/" + resource
        return base + "/chat/completions"

    async def request(self, resource, payload, provider, key, method="POST", *, scope="", purpose="diagnostic", internal=None):
        operation_id = uid()
        if not provider["enabled"]:
            id = self.records.start(scope,purpose,resource,provider,payload,operation_id=operation_id,internal=internal)
            self.records.finish(id,"not_sent",error={"type":"ProviderDisabled"})
            raise ModelError("该供应商已停用")
        if not key:
            id = self.records.start(scope,purpose,resource,provider,payload,operation_id=operation_id,internal=internal)
            self.records.finish(id,"not_sent",error={"type":"KeyMissing"})
            raise ModelError("该供应商尚未配置 API Key")
        url = self.endpoint(provider, resource)
        headers = {"Authorization": "Bearer " + key}
        if provider["protocol"] == "anthropic-messages" and not resource.startswith("tools/"):
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        async with self.creation_limit if purpose == 'creation' else self.limit:
            # Creation has an explicit per-job call budget. A lost response may
            # already have incurred charges; let the job report it, never repay.
            attempts=1 if purpose=='creation' else 3
            for attempt in range(attempts):
                attempt_started = time.perf_counter()
                id = self.records.start(scope,purpose,resource,provider,payload,attempt=attempt+1,operation_id=operation_id,internal=internal)
                try:
                    # Code generation uses high reasoning and a non-streaming
                    # response. Do not impose the short chat read deadline or
                    # mutate the shared client's timeout during concurrent chat.
                    options = {'timeout': httpx.Timeout(CREATION_TIMEOUT, connect=10, pool=10)} if purpose == 'creation' else {}
                    response = await self.client.request(method, url, json=payload if method != "GET" else None, headers=headers, **options)
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                    self.records.finish(id,"network_error",error={"type":type(exc).__name__,"notice":"未取得接口响应；不能确定服务端是否已处理"})
                    if attempt < attempts-1:
                        await asyncio.sleep(attempt+1)
                        continue
                    raise ModelError("等待模型响应超时" if isinstance(exc,httpx.TimeoutException) else "模型服务网络连接中断",
                                     code='model_timeout' if isinstance(exc,httpx.TimeoutException) else 'model_network') from exc
                except asyncio.CancelledError:
                    self.records.finish(id,"cancelled",error={"type":"CancelledError"}); raise
                except Exception as exc:
                    self.records.finish(id,"error",error={"type":type(exc).__name__}); raise
                try:
                    result = response.json()
                except ValueError:
                    self.records.finish(id,"http_error" if response.status_code>=400 else "invalid_response",response={"raw_text":response.text},http_status=response.status_code,headers=response.headers)
                    if (response.status_code==429 or response.status_code>=500) and attempt<attempts-1:
                        await asyncio.sleep(attempt+1);continue
                    raise ModelError("模型接口返回了无效 JSON")
                self.records.finish(id,"http_error" if response.status_code>=400 else "error" if not isinstance(result,dict) or result.get("error") else "success",response=result,http_status=response.status_code,headers=response.headers)
                if resource in ('messages', 'chat') and isinstance(result, dict) and (result.get('usage') is not None or response.status_code < 400):
                    self.record(scope, (payload or {}).get('model','unknown'), purpose, result.get('usage'),
                        duration_ms=round((time.perf_counter()-attempt_started)*1000), estimated_tokens=(internal or {}).get('estimated_input'),
                        stage=(internal or {}).get('stage',purpose), call_id=id, provider_id=provider['id'],
                        estimate_source=(internal or {}).get('estimate_source', 'conservative_characters'))
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < attempts-1:
                        await asyncio.sleep(attempt+1)
                        continue
                if response.status_code >= 400:
                    raise ModelError(f"模型接口 HTTP {response.status_code}；请检查密钥、模型配置及服务状态")
                try:
                    if not isinstance(result, dict) or result.get("error"):
                        raise ModelError("模型服务返回错误或格式异常；请检查服务状态")
                    return {**result,"_record_id":id}
                except ValueError as exc:
                    raise ModelError("模型接口返回了无效 JSON") from exc

    async def chat(self, scope, purpose, messages, tools=None, json_mode=False, max_tokens=None, stage=None):
        token = None
        if not tracing.active_trace.get():
            trace_id,token=tracing.begin(self.db,scope,self.db.get("group",scope,{}).get("persona",""),[],kind=stage or purpose)
        status="complete"
        previous=self.records.listing(trace_id=tracing.active_trace.get(),limit=1)
        try:
            return await self._chat(scope,purpose,messages,tools,json_mode,max_tokens,stage)
        except BaseException as exc:
            status="error"
            if self.records.listing(trace_id=tracing.active_trace.get(),limit=1)==previous:
                resolved=self.catalog.resolve(purpose)
                id=self.records.start(scope,purpose,"messages" if resolved["provider"]["protocol"]=="anthropic-messages" else "chat",resolved["provider"],None,
                                      internal={"stage":stage or purpose,"messages":messages,"tools":tools,"notice":"请求在准备阶段失败，未提交接口"})
                self.records.finish(id,"not_sent",error={"type":type(exc).__name__,"message":str(exc)})
            raise
        finally:
            if token is not None:tracing.finish(self.db,trace_id,status,token)

    async def _chat(self, scope, purpose, messages, tools=None, json_mode=False, max_tokens=None, stage=None):
        started = time.perf_counter()
        resolved = self.catalog.resolve(purpose)
        provider, model, route = (resolved[k] for k in ("provider", "model", "route"))
        if not model["enabled"]:
            raise ModelError("该任务使用的模型已停用")
        # Abort a tool chain if its protocol/model/reasoning mode changed mid-turn.
        signature = hashlib.sha256(json.dumps([provider, model["model_id"], route["reasoning_effort"]], sort_keys=True).encode()).hexdigest()
        if any(m.get("_route") and m["_route"] != signature for m in messages):
            raise ModelError("本轮模型配置已变更，停止旧工具链；下轮将使用新配置")
        projected = []
        missing_images = 0
        for message in messages:
            clean = {k: v for k, v in message.items() if not k.startswith("_")}
            if isinstance(clean.get("content"), list) and "image" not in model["input_modalities"]:
                clean["content"] = [({"type": "text", "text": prompts.render(self.db, scope, 'vision.unsupported')} if block.get("type") == "image_url" else block) for block in clean["content"]]
                missing_images += sum(b.get("type") == "image_url" for b in message["content"])
            if message.get("_native_content") is not None:
                clean["_native_content"] = message["_native_content"]
            projected.append(clean)
        if missing_images:
            self.db.audit(scope, "model_capability", {"model": model["model_id"], "images_skipped": missing_images})
        output = min(max_tokens or route["max_tokens"], route["max_tokens"], model["max_tokens"])
        effort = route["reasoning_effort"]
        payload = {"model": model["model_id"], "max_tokens": output, "stream": False,
                   "thinking": {"type": "disabled" if effort == "off" else "enabled"}}
        native = provider["protocol"] == "anthropic-messages"
        if native:
            system, history = messages_payload(projected)
            payload.update(system=system, messages=history)
            if effort != "off":
                payload["output_config"] = {"effort": effort}
            if json_mode:
                payload["system"] += prompts.render(self.db, scope, 'protocol.json_instruction')
        else:
            payload["messages"] = [{k: v for k, v in m.items() if not k.startswith("_")} for m in projected]
            if effort != "off":
                payload["reasoning_effort"] = effort
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""), "input_schema": t["function"]["parameters"]} for t in tools] if native else tools
        budget = measure(payload)
        check_images(budget, self.db.config())
        if purpose == 'creation':
            # Creation receives explicit job materials, not the chat window.
            # Keep its budget independent while reserving output capacity.
            budget['context_budget'] = self.db.get('creation_config','global',{}).get('context_tokens',256000)
            if budget['estimated_input'] + output > budget['context_budget']:
                raise ModelError('制作资料与预留输出超过制作上下文预算，请缩小本次资料或提高制作预算',code='creation_context_budget')
        if budget['estimated_input'] + output > model["context_window"]:
            raise ModelError("当前请求估算长度超过模型上下文容量，请减少上下文预算或提高模型容量配置")
        data = await self.request("messages" if native else "chat", payload, provider, resolved["key"],scope=scope,purpose=purpose,
                                  internal={"stage":stage or purpose,"messages":messages,"json_mode":json_mode,
                                            **budget})
        try:
            if native:
                blocks = data["content"]
                message = {"role": "assistant", "content": "".join(b.get("text", "") for b in blocks if b["type"] == "text"), "_native_content": blocks, "_route": signature,"_call_id":data["_record_id"]}
                if purpose=='creation':message['_finish_reason']=data.get('stop_reason')
                calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"], ensure_ascii=False)}} for b in blocks if b["type"] == "tool_use"]
                if calls:
                    message["tool_calls"] = calls
                return message
            message = data["choices"][0]["message"]
            return {**{k: message[k] for k in ("role", "content", "tool_calls", "reasoning_content") if k in message}, "_route": signature,"_call_id":data["_record_id"],
                    **({'_finish_reason':data['choices'][0].get('finish_reason')} if purpose=='creation' else {})}
        except (KeyError, IndexError, TypeError) as exc:
            self.records.finish(data["_record_id"],"invalid_response",response={k:v for k,v in data.items() if k!="_record_id"},http_status=200,error={"type":type(exc).__name__,"notice":"响应缺少可解析的消息"})
            raise ModelError("模型返回缺少消息") from exc

    async def search(self, scope, query):
        provider, key = self.search_connection()
        data = await self.request("tools/search", {"query": query[:128], "count": 5, "need_content": False}, provider, key,scope=scope,purpose="search")
        if data.get("results"):
            self.record(scope, "search", "search", flat_cost=self.db.config()["prices"]["search"])
        return [{k: x.get(k) for k in ("title", "url", "snippet", "summary")} for x in data.get("results", [])[:5]]

    async def fetch(self, scope, url):
        provider, key = self.search_connection()
        data = await self.request("tools/fetch", {"url": url}, provider, key,scope=scope,purpose="fetch")
        if data.get("content"):
            self.record(scope, "fetch", "fetch", flat_cost=self.db.config()["prices"]["fetch"])
        return {"title": data.get("title", ""), "content": data.get("content", "")[:12000], "url": url}

    def search_connection(self):
        cfg = self.catalog.get()
        provider = next(p for p in cfg["providers"] if p["id"] == cfg["search_provider"])
        return provider, self.catalog.key(provider["id"])

    async def discover(self, id):
        provider = next((p for p in self.catalog.get()["providers"] if p["id"] == id), None)
        if not provider:
            raise ModelError("供应商不存在")
        data = await self.request("models", None, provider, self.catalog.key(id), method="GET")
        items = data.get("data", [])
        return [{"id": m["id"], "name": m.get("display_name", m["id"])} for m in items[:100] if isinstance(m, dict) and isinstance(m.get("id"), str)]


def messages_payload(messages):
    """Translate internal messages; replay exact native reasoning/signature blocks."""
    system, history = [], []
    for message in messages:
        role, content = message["role"], message.get("content")
        if role == "system":
            system.append(content or "")
            continue
        if role == "tool":
            role, blocks = "user", [{"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": content or ""}]
        elif message.get("_native_content") is not None:
            blocks = message["_native_content"]
        else:
            blocks = []
            for item in content if isinstance(content, list) else [{"type": "text", "text": content or ""}]:
                if item["type"] == "text":
                    if item.get("text"):
                        blocks.append(item)
                elif item["type"] == "image_url":
                    url = item["image_url"]["url"]
                    if url.startswith("data:"):
                        prefix, encoded = url.split(",", 1)
                        if ";base64" not in prefix:
                            raise ModelError("图片需要使用 base64 编码")
                        source = {"type": "base64", "media_type": prefix[5:].split(";")[0], "data": encoded}
                    else:
                        source = {"type": "url", "url": url}
                    blocks.append({"type": "image", "source": source})
            for call in message.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": json.loads(call["function"]["arguments"])})
        if not blocks:
            continue
        if history and history[-1]["role"] == role:
            history[-1]["content"].extend(blocks)
        else:
            history.append({"role": role, "content": list(blocks)})
    return "\n\n".join(system), history
