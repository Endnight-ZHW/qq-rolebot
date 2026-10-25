"""Provider catalog and task routing, inspired by RuoYi-AI and DeepSeek Harness.

Credentials remain in Settings' restricted file, never in catalog JSON or audit.
"""
from typing import Literal
from .config_schema import number_field
from urllib.parse import urlsplit
from ipaddress import IPv4Address, IPv4Network

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CORE_PURPOSES = ("chat", "decision", "memory")
PURPOSES = (*CORE_PURPOSES, "creation")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Provider(Strict):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,60}$")
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(max_length=500)
    protocol: Literal["openai-completions", "anthropic-messages"] = "openai-completions"
    enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value):
        parsed = urlsplit(value)
        private_http = False
        if parsed.scheme == "http":
            try:
                address = IPv4Address(parsed.hostname or "")
                private_http = any(address in IPv4Network(network) for network in
                                   ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
            except ValueError:
                pass
        if (parsed.scheme != "https" and not private_http) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("API 地址须为 HTTPS，或私有 IPv4 地址的 HTTP；不可含账号、查询参数或片段")
        return value.rstrip("/")


class CatalogModel(Strict):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,60}$")
    provider_id: str
    model_id: str = Field(min_length=1, max_length=150)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    enabled: bool = True
    context_window: int = number_field("model.context_window")
    max_tokens: int = number_field("model.max_tokens")
    input_modalities: list[Literal["text", "image"]] = Field(default_factory=lambda: ["text"])

    @model_validator(mode="after")
    def capabilities(self):
        if "text" not in self.input_modalities or len(set(self.input_modalities)) != len(self.input_modalities):
            raise ValueError("输入能力必须包含 text，且不可重复")
        if self.max_tokens >= self.context_window:
            raise ValueError("输出容量必须小于上下文窗口")
        return self


class Route(Strict):
    model: str
    reasoning_effort: Literal["off", "low", "high", "max"] = "off"
    max_tokens: int = number_field("route.max_tokens")


class ModelConfig(Strict):
    providers: list[Provider] = Field(min_length=1, max_length=20)
    models: list[CatalogModel] = Field(min_length=1, max_length=100)
    routes: dict[str, Route]
    search_provider: str

    @model_validator(mode="after")
    def references(self):
        providers = {x.id: x for x in self.providers}
        models = {x.id: x for x in self.models}
        if len(providers) != len(self.providers) or len(models) != len(self.models):
            raise ValueError("供应商和模型的内部 ID 不可重复")
        if any(m.provider_id not in providers for m in self.models):
            raise ValueError("模型引用的供应商不存在")
        if not set(CORE_PURPOSES) <= set(self.routes) <= set(PURPOSES) or self.search_provider not in providers:
            raise ValueError("必须配置聊天、决策、记忆及搜索供应商")
        for route in self.routes.values():
            if route.model not in models:
                raise ValueError("任务引用的模型不存在")
            if route.max_tokens > models[route.model].max_tokens:
                raise ValueError("任务输出上限超过模型容量")
        return self


class ModelConfigWrite(ModelConfig):
    # Write-only map; omitted/empty values preserve existing credentials.
    keys: dict[str, str] = Field(default_factory=dict)
    clear_keys: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def credentials(self):
        ids = {p.id for p in self.providers}
        if (set(self.keys) | set(self.clear_keys)) - ids:
            raise ValueError("密钥引用的供应商不存在")
        if set(self.keys) & set(self.clear_keys):
            raise ValueError("不可同时更新和清除同一密钥")
        if any(len(k) > 512 or any(c.isspace() for c in k.strip()) for k in self.keys.values()):
            raise ValueError("API Key 格式无效")
        return self


class ProviderCatalog:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        if not db.get("inference", "global"):
            initial = ModelConfig(providers=[Provider(id="deepseek", name="DeepSeek", base_url="https://api.deepseek.com")],
                models=[self.entry('deepseek-flash', 'deepseek-flash'), self.entry('deepseek-pro', 'deepseek-v4-pro')],
                routes={purpose: Route(model='deepseek-flash', max_tokens=limit) for purpose, limit in
                        [('chat', 65536), ('decision', 8192), ('memory', 16384)]}, search_provider='deepseek')
            db.put("inference", "global", initial.model_dump())

    @staticmethod
    def entry(id, model_id):
        known = model_id in ("deepseek-flash", "deepseek-v4-pro")
        return {"id": id, "provider_id": "deepseek", "model_id": model_id,
                "name": {"deepseek-flash": "DeepSeek-V4.1-Flash", "deepseek-v4-pro": "DeepSeek-V4-Pro"}.get(model_id, model_id),
                "description": "Harness 内置目录；实际可用性需用账号测试" if known else "自定义模型，请核对模型能力",
                "context_window": 1000000 if known else 64000, "max_tokens": 256000 if known else 4096,
                "input_modalities": ["text", "image"] if model_id == "deepseek-flash" else ["text"], "enabled": True}

    def get(self):
        return self.db.get("inference", "global")

    def key(self, provider_id):
        if provider_id == "deepseek":
            return self.settings.secret.get("api_key", "")
        return self.settings.secret.get("provider_keys", {}).get(provider_id, "")

    def public(self):
        cfg = self.get()
        return {**cfg, "routes": {**cfg["routes"], "creation": self.creation_route(cfg)},
                "key_configured": {p["id"]: bool(self.key(p["id"])) for p in cfg["providers"]}}

    @staticmethod
    def creation_route(cfg):
        if 'creation' in cfg['routes']:
            return cfg['routes']['creation']
        chat = cfg['routes']['chat']
        model = next(m for m in cfg['models'] if m['id'] == chat['model'])
        return {'model':chat['model'], 'reasoning_effort':'high', 'max_tokens':min(65536,model['max_tokens'])}

    def save(self, body: ModelConfigWrite):
        cfg = body.model_dump(exclude={"keys", "clear_keys"})
        if 'creation' not in cfg['routes'] and 'creation' in self.get()['routes']:
            cfg['routes']['creation'] = dict(self.get()['routes']['creation'])
            ModelConfig.model_validate(cfg)
        # Persist credentials first so a crash cannot activate a route with an absent new key.
        secret = self.settings.secret
        keys = secret.setdefault("provider_keys", {})
        for id, key in {**{k: v.strip() for k, v in body.keys.items() if v.strip()}, **{k: "" for k in body.clear_keys}}.items():
            if id == "deepseek":
                secret["api_key"] = key
            else:
                keys[id] = key
        live = {p.id for p in body.providers}
        for id in list(keys):
            if id not in live:
                del keys[id]
        if "deepseek" not in live:
            secret["api_key"] = ""
        self.settings.save_secrets()
        self.db.put("inference", "global", cfg)
        self.refresh_initialized()

    def resolve(self, purpose):
        cfg = self.get()
        route = self.creation_route(cfg) if purpose == 'creation' else cfg["routes"][purpose]
        model = next(m for m in cfg["models"] if m["id"] == route["model"])
        provider = next(p for p in cfg["providers"] if p["id"] == model["provider_id"])
        return {"provider": provider, "model": model, "route": route, "key": self.key(provider["id"])}

    def ready(self):
        return all((r := self.resolve(p))["key"] and r["model"]["enabled"] and r["provider"]["enabled"] for p in CORE_PURPOSES)

    def refresh_initialized(self):
        cfg = self.db.config()
        cfg["initialized"] = bool(cfg["admin_qq"] and self.ready())
        self.db.put("settings", "global", cfg)
