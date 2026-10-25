from . import prompts
from .prompt_defaults import DEFAULTS
import asyncio
import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, model_validator, ValidationError
from .config_schema import FIELDS, number_field, validate_values, save_global, RUNTIME_DEFAULTS

from .config import Settings, password_hash, verify_password, normalize_origin
from .db import Database, uid
from .engine import Engine, group_defaults
from .media import Media
from .model import Model, ModelError
from .onebot import OneBot, OneBotError
from .providers import ModelConfigWrite, PURPOSES
from .tools import SPECS, ToolRunner, configured_policies, SOCIAL_TOOLS, INTERACTION_TOOLS, default_policies
from .insights import register as register_insights
from .debug_api import register as register_debug
from .permissions import AccessConfig, RequestAccess, supported_scopes
from .dashboard import dashboard, usage_summary, validate_range
from .memory_semantics import view as memory_view
from .image_search import LensService, register as register_image_search
from .access_control import register as register_access, policy_update, write_permissions, check_revision


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    username: str = Field(max_length=80)
    password: str = Field(max_length=256)
    remember_me: bool = Field(default=False, strict=True)


class Password(Input):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class Persona(Input):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    prompt: str = Field(default="", max_length=12000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    relationships: dict[str, str] = Field(default_factory=dict)


class Policy(Input):
    enabled: bool = False
    user_enabled: bool = True
    autonomous_enabled: bool = True
    max_per_hour: int = number_field("tool.max_per_hour")
    max_duration: int = number_field("tool.max_duration")
    allow_reject_rejoin: bool = False
    protected_ids: list[str] = Field(default_factory=list, max_length=100)
    request_access: RequestAccess | None = None
    like_max_times: int = number_field("tool.like_max_times")
    like_daily_limit: int = number_field("tool.like_daily_limit")
    poke_cooldown_seconds: int = number_field("tool.poke_cooldown_seconds")


class Group(Input):
    name: str = Field(default="", max_length=80)
    enabled: bool = False
    paused: bool = False
    management_paused: bool = False
    persona: str = "default"
    guidance: str = Field(default="", max_length=6000)
    cooldown: int = number_field("group.cooldown")
    proactive_hourly: int = number_field("group.proactive_hourly")
    listening_probability: float = number_field("group.listening_probability")
    learning_enabled: bool = True
    followup_seconds: int = number_field("group.followup_seconds")
    interaction_enabled: bool = False
    sticker_autocollect: bool = False
    sticker_limit: int = number_field("library.sticker_limit")
    sticker_max_mib: int = number_field("library.sticker_max_mib")
    segmented_reply: bool = False
    segment_threshold: int = number_field("group.segment_threshold")
    segment_max_parts: int = number_field("group.segment_max_parts")
    segment_interval_min: float = number_field("group.segment_interval_min")
    segment_interval_max: float = number_field("group.segment_interval_max")

    send_per_minute: int = number_field("group.send_per_minute")
    send_wait_seconds: int = number_field("group.send_wait_seconds")
    reply_timeout_seconds: int = number_field("group.reply_timeout_seconds")
    forward_reply_timeout_seconds: int = number_field("group.forward_reply_timeout_seconds")
    tool_max_rounds: int = number_field("group.tool_max_rounds")
    tool_max_calls: int = number_field("group.tool_max_calls")

    @model_validator(mode="after")
    def intervals(self):
        if {"segment_interval_min", "segment_interval_max"} <= self.model_fields_set and self.segment_interval_min > self.segment_interval_max:
            raise ValueError("分段最短间隔不能超过最长间隔")
        return self


class MemoryWrite(Input):
    scope: str
    persona: str = ""
    text: str = Field(min_length=1, max_length=4000)
    pinned: bool = False
    subject_ids: list[str] = Field(default_factory=list)


class MemberWrite(Input):
    remember: bool


from .scheduler import TaskCreate, TaskUpdate
from .messaging import sticker_path


class TaskWrite(TaskCreate):
    scope: str = Field(min_length=5, max_length=80)


class StickerWrite(Input):
    name: str = Field(min_length=1, max_length=80)
    tags: str = Field(default='', max_length=500)
    usage: str = Field(default='', max_length=500)
    enabled: bool = True


class InteractionWrite(Input):
    interaction_enabled: bool = False
    sticker_autocollect: bool = False
    sticker_limit: int = number_field("library.sticker_limit")
    sticker_max_mib: int = number_field("library.sticker_max_mib")


class StickerLibraryWrite(Input):
    sticker_limit: int = number_field("library.sticker_limit", required=True)
    sticker_max_mib: int = number_field("library.sticker_max_mib", required=True)


def validate_qq(values):
    if not isinstance(values, list) or len(values) > 100 or any(not isinstance(x, str) or not x.isdigit() or not 5 <= len(x) <= 20 for x in values):
        raise HTTPException(422, "QQ 号需要填写 5 至 20 位数字字符串")


def create_app(data_dir=None, start_engine=True):
    settings = Settings(data_dir)
    db = Database(settings.data_dir / "app.sqlite")
    onebot = OneBot(); model = Model(db, settings); media = Media(settings)
    tools = ToolRunner(db, settings, onebot, model)
    engine = Engine(db, settings, onebot, model, media, tools)
    image_search = LensService(db, settings)
    engine.image_search = image_search
    attempts = {}

    @asynccontextmanager
    async def lifespan(app):
        if start_engine:
            await engine.start()
        yield
        await engine.stop()
        await model.close()
        await image_search.client.aclose()
        db.close()

    app = FastAPI(title="机器人 · QQ 角色管理", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.settings = db, settings
    app.state.onebot, app.state.model, app.state.engine, app.state.tools = onebot, model, engine, tools
    app.state.image_search = image_search

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        from fastapi.responses import JSONResponse
        # Pydantic's default response includes rejected input, possibly API keys.
        return JSONResponse({"detail": "；".join(e["msg"] for e in exc.errors())[:800], "fields": [list(e["loc"]) for e in exc.errors()]}, status_code=422)

    @app.exception_handler(OneBotError)
    async def ob_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(ModelError)
    async def model_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)}, status_code=502)

    def origin_check(request):
        origin = request.headers.get("origin")
        if origin:
            try: allowed = normalize_origin(origin) in settings.allowed_origins
            except ValueError: allowed = False
            if not allowed: raise HTTPException(403, "请求来源不匹配")

    async def auth(request: Request):
        raw = request.cookies.get("rolebot_session", "")
        token = hashlib.sha256(raw.encode()).hexdigest()
        rows = db.rows("SELECT * FROM sessions WHERE token=? AND expires>?", (token, time.time()))
        if not raw or not rows:
            raise HTTPException(401, "请先登录")
        session = rows[0]
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin_check(request)
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
                raise HTTPException(403, "CSRF 校验失败，请刷新页面")
        request.state.session = session
        return session

    @app.middleware("http")
    async def headers(request, call_next):
        if request.url.path.startswith("/api/v1/") and request.url.path != "/api/v1/auth/login":
            try:
                await auth(request)
            except HTTPException as exc:
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers={"Cache-Control":"no-store"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/v1/auth/login")
    async def login(body: Login, request: Request, response: Response):
        origin_check(request)
        current = time.time()
        for k in list(attempts):
            attempts[k] = [x for x in attempts[k] if x > current-900]
            if not attempts[k]:
                attempts.pop(k)
        key = "admin"  # Global account limit cannot be bypassed with spoofed forwarded IPs.
        failures = attempts.setdefault(key, [])
        if len(failures) >= 10:
            raise HTTPException(429, "登录尝试过多，请 15 分钟后重试")
        valid = await asyncio.to_thread(verify_password, body.password, settings.secret["password_hash"])
        if body.username != "admin" or not valid:
            failures.append(current)
            raise HTTPException(401, "用户名或密码错误")
        attempts.pop(key, None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        lifetime = 30*24*3600 if body.remember_me else 12*3600
        db.execute("INSERT INTO sessions VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), csrf, current+lifetime))
        response.set_cookie("rolebot_session", token, httponly=True, secure=settings.secure_cookie, samesite="strict", max_age=lifetime, path="/")
        return {"username": "admin", "csrf": csrf}

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(auth)])
    register_image_search(api, image_search)

    @api.get("/auth/me")
    async def me(request: Request):
        return {"username": "admin", "csrf": request.state.session["csrf"]}

    @api.post("/auth/logout")
    async def logout(request: Request, response: Response):
        db.execute("DELETE FROM sessions WHERE token=?", (request.state.session["token"],))
        response.delete_cookie("rolebot_session", path="/", secure=settings.secure_cookie, httponly=True, samesite="strict")
        return {"ok": True}

    @api.put("/auth/password")
    async def password(body: Password):
        if not await asyncio.to_thread(verify_password, body.current_password, settings.secret["password_hash"]):
            raise HTTPException(403, "原密码错误")
        settings.secret["password_hash"] = await asyncio.to_thread(password_hash, body.new_password)
        settings.save_secrets(); db.execute("DELETE FROM sessions")
        bootstrap = settings.data_dir / "bootstrap-password.txt"
        if bootstrap.exists():
            bootstrap.unlink()
        return {"ok": True, "message": "密码已更新，请重新登录"}

    @api.get("/status")
    async def status():
        return {"name": "机器人", "onebot_connected": onebot.online, "self_id": onebot.self_id,
                "connected_at": onebot.connected_at, "last_event": onebot.last_event,
                "model_configured": model.configured, "initialized": db.config()["initialized"],
                "uptime": time.time()-engine.started, "groups": len([x for x in db.all("group") if x.get("enabled")]),
                "pending_reminders": len([x for x in db.all("reminder") if x["status"] == "pending"]),
                "queue_depth": sum(q.qsize() for q in engine.queues.values()), "public_url": settings.public_url}

    @api.get("/config-schema")
    async def config_schema():
        return {"version": 1, "fields": FIELDS}

    @api.get("/settings")
    async def get_settings():
        return {**db.config(), "api_key_configured": model.configured}

    @api.put("/settings")
    async def update_settings(body: dict, expected_revision: int | None = None):
        cfg = db.config()
        check_revision(cfg.get("revision",0), expected_revision, "系统设置")
        from .config import DEFAULTS
        allowed = set(DEFAULTS) - {"initialized"}
        if set(body) - allowed - {"api_key"}:
            raise HTTPException(422, "包含未知设置字段")
        if "admin_qq" in body:
            validate_qq(body["admin_qq"])
        try:
            validate_values('global', body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        merged = {**cfg, **body}
        if merged['working_initial_messages'] > merged['working_max_messages']:
            raise HTTPException(422, '初始消息条数不能超过工作上下文条数上限')
        if merged['learning_idle_seconds'] > merged['learning_max_seconds']:
            raise HTTPException(422, '停止参与等待时间不能超过持续参与整理间隔')
        for k in ("private_enabled",):
            if k in body and not isinstance(body[k], bool):
                raise HTTPException(422, "开关必须为布尔值")
        for k in ("nickname",):
            if k in body and (not isinstance(body[k],str) or not 1 <= len(body[k]) <= 100):
                raise HTTPException(422, "名称长度不正确")
        if "prices" in body:
            if not isinstance(body["prices"], dict) or set(body["prices"]) != {"input", "cached", "output", "search", "fetch"}:
                raise HTTPException(422, "计费参数不完整")
            if any(type(x) not in (int,float) or not 0 <= x <= 10000 for x in body["prices"].values()):
                raise HTTPException(422, "价格参数无效")
        if "currency" in body and body["currency"] not in ("USD", "CNY"):
            raise HTTPException(422, "币种只支持 USD 或 CNY")
        key = body.pop("api_key", None)
        if key is not None:
            if not isinstance(key, str) or len(key) > 512:
                raise HTTPException(422, "API Key 格式无效")
            settings.set_key(key)
        cfg.update(body)
        cfg["initialized"] = bool(cfg["admin_qq"] and model.configured)
        save_global(db, {**body, "initialized": cfg["initialized"]})
        if "model_log_days" in body or "model_log_image_hours" in body:
            model.records.cleanup()
        return {**db.config(), "api_key_configured": model.configured}

    @api.get("/models")
    async def models():
        return model.catalog.public()

    @api.put("/models")
    async def save_models(body: ModelConfigWrite):
        model.catalog.save(body)
        db.audit("", "models", {"providers": [p.id for p in body.providers], "models": len(body.models),
                               "routes": {k: v.model_dump() for k, v in body.routes.items()},
                               "credentials_changed": list(body.keys) + body.clear_keys})
        return model.catalog.public()

    @api.post("/models/discover/{provider_id}")
    async def discover_models(provider_id: str):
        return {"models": await model.discover(provider_id), "notice": "接口列出的模型；图片和容量参数仍需核对供应商说明"}

    @api.post("/models/test/{purpose}")
    async def test_route(purpose: str):
        if purpose not in PURPOSES:
            raise HTTPException(422, "未知任务类型")
        result = await ping(purpose,128)
        return {"ok": True, "reply": result.get("content", "")[:100], "call_id":result.get("_call_id")}

    @api.post("/settings/test")
    async def test_model():
        result = await ping("chat",32)
        return {"ok": True, "reply": result.get("content", "")[:100], "call_id":result.get("_call_id")}

    async def ping(purpose,maximum):
        from . import tracing
        id,token=tracing.begin(db,"","",[],kind="diagnostic")
        status="complete"
        try:
            return await model.chat("",purpose,[{"role":"user","content":prompts.render(db,"","diagnostic.ping")}],max_tokens=maximum,stage="diagnostic")
        except BaseException:status="error";raise
        finally:tracing.finish(db,id,status,token)

    @api.get("/personas")
    async def personas():
        return db.all("persona")

    def save_persona(id, body):
        validate_qq(list(body.relationships))
        if any(len(x) > 100 for x in body.aliases) or any(len(x) > 500 for x in body.relationships.values()):
            raise HTTPException(422, "别名或关系说明过长")
        old = db.get("persona", id, {})
        value = {**body.model_dump(), "id": id, "version": old.get("version", 0)+1}
        db.put("persona", id, value); db.put("persona_version", f"{id}:{value['version']}", value)
        db.audit("", "persona", {"id": id, "version": value["version"]})
        return value

    @api.post("/personas")
    async def add_persona(body: Persona):
        return save_persona(uid()[:12], body)

    @api.put("/personas/{id}")
    async def edit_persona(id: str, body: Persona):
        if not db.get("persona", id):
            raise HTTPException(404, "角色不存在")
        return save_persona(id, body)

    @api.get("/personas/{id}/versions")
    async def versions(id: str):
        return sorted([x for x in db.all("persona_version") if x["id"] == id], key=lambda x:x["version"], reverse=True)

    @api.get('/personas/{id}/images')
    async def persona_images(id: str):
        return [{**x, 'image_available': engine.role_images.available(x)}
                for x in engine.role_images.list(id, enabled_only=False)]

    @api.post('/personas/{id}/images')
    async def add_persona_image(id: str, file: UploadFile = File(...), name: str = '角色参考图'):
        raw = await file.read(8*1024*1024+1)
        try:
            return engine.role_images.save(id, raw, name)
        except (ValueError, OSError):
            raise HTTPException(422, '图片无效、超过 8 MiB 或角色不存在')

    @api.put('/personas/{id}/images/{reference_id}')
    async def update_persona_image(id: str, reference_id: str, body: dict):
        ref = db.get('role_image', reference_id)
        if not ref or ref.get('persona') != id or ref.get('source_type') != 'admin_upload':
            raise HTTPException(404, '后台角色参考图不存在')
        if not body or set(body)-{'enabled', 'label'} or ('enabled' in body and type(body['enabled']) is not bool):
            raise HTTPException(422, '只支持 enabled 开关和 label 名称')
        if 'label' in body and (not isinstance(body['label'], str) or not 1 <= len(body['label'].strip()) <= 80):
            raise HTTPException(422, '参考图名称须为 1 至 80 字')
        ref.update(body)
        ref['revision'] += 1
        db.put('role_image', reference_id, ref)
        db.audit('', 'persona_image_config', {'id': reference_id, **body})
        return ref

    @api.delete('/personas/{id}/images/{reference_id}')
    async def delete_persona_image(id: str, reference_id: str):
        try:
            engine.role_images.delete(id, reference_id)
        except ValueError:
            raise HTTPException(404, '后台角色参考图不存在')
        return {'ok': True}

    @api.put('/personas/{id}/images/{reference_id}/file')
    async def replace_persona_image(id: str, reference_id: str, file: UploadFile = File(...)):
        raw = await file.read(8*1024*1024+1)
        try:
            return engine.role_images.save(id, raw, replace_id=reference_id)
        except (ValueError, OSError):
            raise HTTPException(422, '参考图不存在、图片无效或超过 8 MiB')

    @api.get('/personas/{id}/images/{reference_id}/file')
    async def persona_image_file(id: str, reference_id: str):
        ref = db.get('role_image', reference_id, {})
        if ref.get('persona') != id or not engine.role_images.available(ref):
            raise HTTPException(404, '参考图不存在或文件不可读取')
        return FileResponse(engine.role_images.path(ref), media_type='image/jpeg', headers={'Cache-Control': 'no-store'})

    @api.get('/attachments/{image_id}/file')
    async def attachment_file(image_id: str):
        from .attachments import linked_sticker
        from .messaging import sticker_path
        item = db.get('attachment', image_id)
        if not item:
            raise HTTPException(404, '图片不存在')
        try:
            engine.attachments.item(item['scope'], item['persona'], image_id)
        except ValueError:
            raise HTTPException(410, '图片来源已失效')
        sticker = linked_sticker(db, item)
        if sticker:
            path = sticker_path(settings, sticker)
        else:
            path = engine.attachments.path(image_id)
            if time.time()-item['ts'] > db.config()['media_hours']*3600:
                raise HTTPException(410, '图片已过期')
        if not path.is_file():
            raise HTTPException(410, '图片文件不可取得')
        return FileResponse(path, media_type=None if sticker else item.get('mime', 'image/jpeg'), headers={'Cache-Control': 'no-store'})

    @api.post("/personas/{id}/restore/{version}")
    async def restore(id: str, version: int):
        old = db.get("persona_version", f"{id}:{version}")
        if not old:
            raise HTTPException(404, "版本不存在")
        return save_persona(id, Persona(**{k:v for k,v in old.items() if k not in ("id","version")}))

    @api.get("/groups")
    async def groups():
        return [{**group_defaults(g["id"]), **g, "tools": configured_policies(g)} for g in db.all("group")]

    @api.get("/groups/discover")
    async def discover():
        result = await onebot.call("get_group_list")
        return [{"id": f"{onebot.self_id}:g:{x['group_id']}", "name": x.get("group_name",str(x["group_id"])), "member_count": x.get("member_count")} for x in result or []]

    @api.put("/groups/{scope}")
    async def save_group(scope: str, body: Group):
        parts = scope.split(":")
        if len(parts) != 3 or parts[1] not in ("g","p") or not parts[0].isdigit() or not parts[2].isdigit():
            raise HTTPException(422, "会话标识不正确")
        if onebot.self_id and parts[0] != onebot.self_id:
            raise HTTPException(422, "会话不属于当前机器人")
        if not db.get("persona", body.persona):
            raise HTTPException(422, "角色不存在")
        if body.enabled and not db.config()["initialized"]:
            raise HTTPException(422, "请先配置模型密钥和管理员 QQ 号")
        old = db.get("group", scope, group_defaults(scope))
        patch = body.model_dump()
        for key in ("listening_probability", "learning_enabled", "followup_seconds", "segmented_reply", "segment_threshold", "segment_max_parts", "segment_interval_min", "segment_interval_max", 'interaction_enabled', 'sticker_autocollect', 'sticker_limit', 'sticker_max_mib', *RUNTIME_DEFAULTS):
            if key not in body.model_fields_set:
                patch.pop(key)
        value = {**old, **patch, "id": scope, "revision": old.get("revision",0)+1}
        # Validate the merged interval pair as older clients may omit either field.
        if value.get("segment_interval_min", 1) > value.get("segment_interval_max", 2.5):
            raise HTTPException(422, "分段最短间隔不能超过最长间隔")
        db.put("group", scope, value); db.audit(scope, "group", {"enabled": body.enabled, "persona": body.persona, "paused": body.paused})
        return value

    @api.get("/tools")
    async def tool_catalog():
        return [{"name": name, "defaults": default_policies()[name], "title": spec.title, "description": spec.description, "mutation": spec.mutation, "group_only": spec.group_only, "requires_interaction": name in INTERACTION_TOOLS, "service_configured": name != "image_search" or bool(image_search.key()), "schema": spec.args.model_json_schema(), 'supported_scopes': supported_scopes(name), 'category': '群互动' if name in SOCIAL_TOOLS else '群管理' if spec.mutation else '常用工具'} for name,spec in SPECS.items()]

    @api.put("/groups/{scope}/tools")
    async def save_tools(scope: str, body: dict[str, Policy], expected_revision: int | None = None):
        group = db.get("group", scope)
        if not group:
            raise HTTPException(404, "请先保存群配置")
        changes_access = any('request_access' in p.model_fields_set and
            (p.request_access.model_dump() if p.request_access else None) != group.get('tools', {}).get(name, {}).get('request_access')
            for name, p in body.items())
        if changes_access and expected_revision is None:
            raise HTTPException(422, '保存请求权限必须携带 expected_revision')
        with db.transaction():
            group = db.get('group', scope)
            if expected_revision is not None and group.get('revision', 0) != expected_revision:
                raise HTTPException(409, '群工具权限已被修改，请刷新后再保存')
            group['tools'] = policy_update(group, body, validate_qq)
            group["revision"] += 1; db.put("group", scope, group)
            db.audit(scope, "tool_policy", {"revision": group['revision'], "policies": {k:v.model_dump(exclude_unset=True) for k,v in body.items()}})
        return group["tools"]

    @api.get("/permissions/catalog")
    async def permission_catalog():
        return tools.permissions.catalog()

    @api.get("/permissions/{scope}")
    async def get_permissions(scope: str):
        if not db.get("group", scope):
            raise HTTPException(404, "会话不存在")
        return tools.permissions.config(scope)

    @api.put("/permissions/{scope}")
    async def save_permissions(scope: str, body: AccessConfig):
        if not db.get("group", scope):
            raise HTTPException(404, "会话不存在")
        with db.transaction():
            return write_permissions(db, scope, body)

    @api.get("/permissions/{scope}/effective/{user_id}")
    async def effective_permissions(scope: str, user_id: str):
        validate_qq([user_id])
        if not db.get("group", scope):
            raise HTTPException(404, "会话不存在")
        result = await tools.permissions.effective(scope, user_id)
        result["tools"] = {name: tools.permission_status(scope, user_id, name, result) for name in SPECS}
        from .permissions import SELF_RECALL, recall_grants
        recall_policy = tools.policy(scope, 'message_recall')
        if result['tool_access']['message_recall']['mode'] == 'legacy' and not recall_grants(result)[0]:
            result['tools']['message_recall'].update(available=False, reason='未授权完整撤回；本人撤回请查看对应权限')
        result['tools'][SELF_RECALL.removeprefix('tool:')] = {
            'available': recall_grants(result)[1] and bool(recall_policy.get('enabled')) and recall_policy.get('user_enabled', True),
            'mode': result['tool_access']['message_recall']['mode'], 'target_scope': 'self',
            'identity_source': result['tool_access']['message_recall']['identity_source'],
            'reason': '' if recall_grants(result)[1] else '未单独授予本人撤回权限',
            'group_policy': recall_policy}
        return result

    @api.get("/members")
    async def members(scope: str):
        return [x for x in db.all("member") if x["scope"] == scope]

    @api.put("/members/{scope}/{user_id}")
    async def edit_member(scope: str, user_id: str, body: MemberWrite):
        value = db.get("member", scope+":"+user_id)
        if not value:
            raise HTTPException(404, "成员不存在")
        value["remember"] = body.remember; db.put("member", value["id"], value)
        if not body.remember:
            db.invalidate_memory(scope, user_id=user_id)
        return value

    @api.delete("/members/{scope}/{user_id}/memory")
    async def forget_member(scope: str, user_id: str):
        db.invalidate_memory(scope, user_id=user_id)
        db.audit(scope, "forget", {"user_id": user_id})
        return {"ok": True}

    @api.get('/groups/{scope}/learning-status')
    async def learning_status(scope: str):
        group = db.get('group', scope)
        if not group: raise HTTPException(404, '会话不存在')
        return engine.memory.participation.status(scope, group['persona'])

    @api.get("/memories")
    async def memories(scope: str, persona: str | None = None):
        return [{**m, "interpretation": memory_view(db, m)} for m in db.memories(scope, persona)]

    @api.post("/memories")
    async def create_memory(body: MemoryWrite):
        if not db.get("group", body.scope) or (body.persona and not db.get("persona",body.persona)):
            raise HTTPException(422, "会话或角色不存在")
        validate_qq(body.subject_ids)
        try:
            id = engine.memory.knowledge.manual(body.scope, body.persona, body.text, body.subject_ids, body.pinned)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"id":id}

    @api.put("/memories/{id}")
    async def edit_memory(id: str, body: MemoryWrite):
        rows = [m for m in db.memories(body.scope) if m["id"] == id]
        if not rows:
            raise HTTPException(404, "记忆不存在")
        if body.persona and not db.get("persona",body.persona):
            raise HTTPException(422, "角色不存在")
        validate_qq(body.subject_ids)
        try:
            engine.memory.knowledge.manual(body.scope, body.persona, body.text, body.subject_ids, body.pinned, id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        db.put("memory_epoch", body.scope, {"value": engine.memory.epoch(body.scope)+1})
        return {"ok":True}

    @api.delete("/memories/{id}")
    async def delete_memory(id: str, scope: str):
        db.invalidate_memory(scope, memory_id=id); db.audit(scope,"forget",{"memory_id":id})
        return {"ok":True}

    @api.get("/context")
    async def context(scope: str, persona: str):
        return {"preview": db.get("context_preview",scope+":"+persona)}

    @api.post("/context/reset")
    async def reset_context(body: dict):
        scope, persona = str(body.get("scope","")), str(body.get("persona",""))
        if not db.get("group",scope) or not db.get("persona",persona):
            raise HTTPException(422,"会话或角色不存在")
        db.put("context_reset",scope+":"+persona,{"ts":time.time()})
        db.delete("summary",scope+":"+persona); db.delete("context_preview",scope+":"+persona)
        db.put("memory_epoch",scope,{"value":engine.memory.epoch(scope)+1})
        return {"ok":True}

    @api.get("/reminders")
    async def reminders(scope: str = ""):
        return [x for x in db.all("reminder") if not scope or x["scope"] == scope]

    @api.get('/tasks')
    async def task_list(scope: str = ''):
        return [x for x in db.all('reminder') if not scope or x['scope'] == scope]

    @api.post('/tasks')
    async def task_create(body: TaskWrite):
        data = body.model_dump(); scope = data.pop('scope')
        group = db.get('group', scope, {})
        try:
            return await engine.scheduler.create(scope, group.get('persona', ''), 'admin-console', data, principal='admin')
        except ValueError as exc: raise HTTPException(422, str(exc))

    @api.put('/tasks/{id}')
    async def task_update(id: str, body: TaskUpdate):
        item = db.get('reminder', id)
        if not item or body.id != id: raise HTTPException(404, '任务不存在')
        try: return engine.scheduler.update(item['scope'], 'admin-console', body.model_dump(), admin=True)
        except ValueError as exc: raise HTTPException(409, str(exc))

    @api.get('/tasks/{id}/runs')
    async def task_runs(id: str):
        return sorted([x for x in db.all('task_run') if x.get('task_id') == id], key=lambda x: x.get('started_at', 0), reverse=True)[:100]

    @api.get('/groups/{scope}/capabilities')
    async def capabilities(scope: str):
        if not db.get('group', scope): raise HTTPException(404, '会话不存在')
        return await tools.invoke(scope, db.get('group', scope)['persona'], 'admin-console', 'capabilities', {})

    @api.put('/groups/{scope}/interaction')
    async def interaction_config(scope: str, body: InteractionWrite):
        group = db.get('group', scope)
        if not group: raise HTTPException(404, '会话不存在')
        group.update(body.model_dump(exclude_unset=True)); group['revision'] = group.get('revision', 0)+1
        db.put('group', scope, group); db.audit(scope, 'interaction_config', body.model_dump(exclude_unset=True))
        return group

    @api.delete("/reminders/{id}")
    async def cancel_reminder(id: str):
        r = db.get("reminder",id)
        if not r or r["status"] != "pending":
            raise HTTPException(409,"提醒已执行或不存在")
        r["status"] = "cancelled"; db.put("reminder",id,r)
        return {"ok":True}

    @api.get("/requests")
    async def requests(scope: str = ""):
        return [{k:v for k,v in x.items() if k != "flag"} for x in db.all("join_request") if not scope or x["scope"] == scope]

    @api.get("/audit")
    async def audit(scope: str = "", kind: str = "", offset: int = 0, limit: int = 100, start: float | None = None, end: float | None = None, status: str = ''):
        validate_range(start, end)
        sql, args = "SELECT * FROM audit WHERE 1=1", []
        if scope: sql += " AND scope=?"; args.append(scope)
        if kind: sql += " AND kind=?"; args.append(kind)
        if start is not None: sql += " AND ts>=?"; args.append(start)
        if end is not None: sql += " AND ts<?"; args.append(end)
        if status: sql += " AND json_extract(data,'$.status')=?"; args.append(status)
        rows = db.rows(sql+" ORDER BY ts DESC LIMIT ? OFFSET ?", (*args,min(max(limit,1),200),max(offset,0)))
        for row in rows: row["data"] = json.loads(row["data"])
        return rows

    @api.get("/usage")
    async def usage(scope: str = '', start: float | None = None, end: float | None = None):
        return usage_summary(db, scope, start, end)

    @api.get('/dashboard')
    async def console_dashboard(scope: str = '', start: float | None = None, end: float | None = None):
        return dashboard(db, await status(), scope, start, end)

    @api.get('/sticker-library')
    async def sticker_library():
        return engine.stickers.config()

    @api.put('/sticker-library')
    async def configure_sticker_library(body: StickerLibraryWrite):
        value = body.model_dump()
        db.put('sticker_library', 'global', value)
        db.audit('', 'sticker_library_config', value)
        return value

    @api.get("/stickers")
    async def stickers():
        return db.all('sticker')

    @api.post("/stickers")
    async def upload_sticker(file: UploadFile = File(...), name: str = "表情", tags: str = "", scope: str = ''):
        raw = await file.read(8*1024*1024+1)
        if scope and not db.get('group', scope): raise HTTPException(404, '会话不存在')
        try: return engine.stickers.register(scope, raw, {'name': name[:80], 'tags': tags[:300]})
        except Exception: raise HTTPException(422, '图片无效、文件超限或表情库已满')

    @api.put('/stickers/{id}')
    async def edit_sticker(id: str, body: StickerWrite):
        item = db.get('sticker', id)
        if not item: raise HTTPException(404, '表情不存在')
        item.update(body.model_dump()); db.put('sticker', id, item)
        db.audit(item.get('scope', ''), 'sticker_updated', {'id': id, 'enabled': item['enabled']})
        return item

    @api.get("/stickers/{id}/image")
    async def sticker_image(id: str):
        sticker = db.get("sticker",id)
        if not sticker: raise HTTPException(404,"表情包不存在")
        return FileResponse(sticker_path(settings, sticker), media_type=sticker.get('mime', 'image/jpeg'))

    @api.delete("/stickers/{id}")
    async def delete_sticker(id: str):
        sticker = db.get("sticker",id)
        if not sticker: raise HTTPException(404,"表情包不存在")
        sticker_path(settings, sticker).unlink(missing_ok=True)
        db.delete("sticker",id); return {"ok":True}

    @api.post("/backup")
    async def backup():
        path = db.backup(settings.data_dir/"backups")
        return {"file":path.name,"retention":7}

    register_insights(api, db, engine)
    register_debug(api, db, model)
    register_access(api, db, tools, onebot, Group, Policy, validate_qq)
    from .creation_api import register as register_creations
    register_creations(app,api,engine.creations)
    app.include_router(api)

    @app.get("/health/live")
    async def health():
        return {"ok":True}

    @app.get("/health/ready")
    async def ready():
        db.rows("SELECT 1")
        return {"ready":True,"onebot":onebot.online,"configured":db.config()["initialized"]}

    @app.websocket("/onebot/v11/ws")
    async def socket(ws: WebSocket):
        authorization = ws.headers.get("authorization", "")
        token = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        self_id = ws.headers.get("x-self-id", "")
        if not token or not secrets.compare_digest(token,settings.secret["onebot_token"]) or not self_id.isdigit():
            await ws.close(code=1008); return
        await ws.accept(); await onebot.connect(ws,self_id)
        db.audit("","connection",{"connected":True,"self_id":self_id})
        try:
            while True:
                event = await ws.receive_json()
                if not isinstance(event,dict): continue
                if not onebot.response(event):
                    onebot.last_event = time.time()
                    engine.spawn(engine.receive(event))
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            await onebot.disconnect(ws)
            db.audit("","connection",{"connected":False,"self_id":self_id})

    return app
