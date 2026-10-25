import json

# HTTP endpoints below are synthetic RFC1918 fixtures; requests are mocked.

import httpx
import pytest

from rolebot.model import Model, ModelError
from rolebot.providers import ModelConfigWrite, Provider

from support import configure_model


def test_password_nine_chars_and_all_sessions_revoked(authed_client):
    first_cookie = authed_client.cookies.get("rolebot_session")
    second = authed_client.post("/api/v1/auth/login", json={"username": "admin", "password": "test-password-123456", "remember_me": True})
    authed_client.headers["x-csrf-token"] = second.json()["csrf"]
    assert authed_client.put("/api/v1/auth/password", json={"current_password": "wrong", "new_password": "new-pass9"}).status_code == 403
    assert authed_client.put("/api/v1/auth/password", json={"current_password": "test-password-123456", "new_password": "new-pass9"}).status_code == 200
    assert authed_client.get("/api/v1/auth/me").status_code == 401
    assert authed_client.get("/api/v1/auth/me", headers={"Cookie": "rolebot_session=" + first_cookie}).status_code == 401
    assert authed_client.post("/api/v1/auth/login", json={"username": "admin", "password": "test-password-123456"}).status_code == 401
    assert authed_client.post("/api/v1/auth/login", json={"username": "admin", "password": "new-pass9"}).status_code == 200

def test_catalog_redacts_credentials_validates_references_and_hot_reloads(authed_client):
    cfg = authed_client.get("/api/v1/models").json()
    cfg.pop("key_configured")
    cfg["keys"] = {"deepseek": "secret-provider-sentinel"}
    cfg["providers"][0]["protocol"] = "anthropic-messages"
    cfg["providers"][0]["base_url"] = "https://api.deepseek.com/anthropic"
    response = authed_client.put("/api/v1/models", json=cfg)
    assert response.status_code == 200
    assert response.json()["key_configured"]["deepseek"]
    for path in ("/models", "/settings", "/audit"):
        assert "secret-provider-sentinel" not in authed_client.get("/api/v1" + path).text
    assert "secret-provider-sentinel" not in str(authed_client.app.state.db.all("inference"))
    assert authed_client.app.state.model.catalog.resolve("chat")["provider"]["protocol"] == "anthropic-messages"
    cfg["routes"]["chat"]["model"] = "missing"
    response = authed_client.put("/api/v1/models", json=cfg)
    assert response.status_code == 422 and "secret-provider-sentinel" not in response.text
    assert authed_client.app.state.model.catalog.resolve("chat")["model"]["model_id"] == "deepseek-flash"

def test_catalog_disable_key_clear_and_no_key_reexposure(authed_client):
    cfg = authed_client.get("/api/v1/models").json(); cfg.pop("key_configured")
    cfg["keys"] = {"deepseek": "secret-key"}
    assert authed_client.put("/api/v1/models", json=cfg).status_code == 200
    assert authed_client.get("/api/v1/status").json()["model_configured"]
    cfg["keys"] = {"deepseek": ""}
    assert authed_client.put("/api/v1/models", json=cfg).json()["key_configured"]["deepseek"]
    cfg["providers"][0]["enabled"] = False
    assert authed_client.put("/api/v1/models", json=cfg).status_code == 200
    assert not authed_client.get("/api/v1/status").json()["model_configured"]
    cfg["keys"] = {}; cfg["clear_keys"] = ["deepseek"]
    assert not authed_client.put("/api/v1/models", json=cfg).json()["key_configured"]["deepseek"]

async def test_messages_reasoning_signatures_parallel_tools_and_usage(model):
    configure_model(model)
    requests = []
    native = [{"type": "thinking", "thinking": "private reasoning", "signature": "opaque-signature"},
              {"type": "tool_use", "id": "t1", "name": "calculate", "input": {"expression": "1+1"}},
              {"type": "tool_use", "id": "t2", "name": "calculate", "input": {"expression": "2+2"}}]
    def handle(request):
        assert str(request.url) == "https://api.deepseek.com/anthropic/v1/messages"
        assert request.headers["x-api-key"] == "test-only-provider-key"
        body = json.loads(request.content); requests.append(body)
        assert body["output_config"] == {"effort": "high"}
        assert body["thinking"] == {"type": "enabled"}
        return httpx.Response(200, json={"content": native if len(requests) == 1 else [{"type": "text", "text": '{"speak":true}'}],
                                         "usage": {"input_tokens": 10, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5, "output_tokens": 7}})
    model.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    history = [{"role": "system", "content": "Return JSON"}, {"role": "user", "content": "calculate"}]
    tool = {"type": "function", "function": {"name": "calculate", "parameters": {"type": "object"}}}
    response = await model.chat("g", "decision", history, [tool], json_mode=True)
    assert response["content"] == "" and "reasoning_content" not in response
    assert len(response["tool_calls"]) == 2
    history += [response, {"role": "tool", "tool_call_id": "t1", "content": "2"}, {"role": "tool", "tool_call_id": "t2", "content": "4"}]
    await model.chat("g", "decision", history, [tool], json_mode=True)
    assert requests[1]["messages"][-2]["content"] == native
    assert [b["tool_use_id"] for b in requests[1]["messages"][-1]["content"]] == ["t1", "t2"]
    usage = model.db.rows("SELECT * FROM usage")[0]
    assert (usage["input_tokens"], usage["cached_tokens"], usage["output_tokens"]) == (35, 20, 7)
    assert "private reasoning" not in str(model.db.all("context_preview"))

@pytest.mark.parametrize("protocol", ["openai-completions", "anthropic-messages"])
async def test_images_supported_and_explicit_text_only_fallback(model, protocol):
    configure_model(model, protocol, "off")
    requests = []
    def handle(request):
        body = json.loads(request.content); requests.append(body)
        assert body["thinking"] == {"type": "disabled"}
        assert "output_config" not in body and "reasoning_effort" not in body
        return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}]} if protocol == "anthropic-messages" else {"choices": [{"message": {"role": "assistant", "content": "OK"}}]})
    model.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    history = [{"role": "user", "content": [{"type": "text", "text": "图片？"}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,aGVsbG8="}}]}]
    await model.chat("g", "chat", history)
    assert "aGVsbG8=" in str(requests[0])
    cfg = model.catalog.get(); cfg["models"][0]["input_modalities"] = ["text"]
    model.catalog.save(ModelConfigWrite(**cfg))
    await model.chat("g", "chat", history)
    assert "aGVsbG8=" not in str(requests[1]) and "内容未知" in str(requests[1])

async def test_completions_reasoning_off_and_tool_chain_config_change(model):
    configure_model(model, "openai-completions", "max")
    requests = []
    def handle(request):
        body = json.loads(request.content); requests.append(body)
        assert str(request.url) == "https://api.deepseek.com/v1/chat/completions"
        assert body["reasoning_effort"] == "max"
        assert "_route" not in str(body)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK", "reasoning_content": "internal"}}]})
    model.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    history = [{"role": "user", "content": "hello"}]
    response = await model.chat("g", "decision", history)
    await model.chat("g", "decision", history + [response])
    assert requests[1]["messages"][-1]["reasoning_content"] == "internal"
    configure_model(model, "anthropic-messages")
    with pytest.raises(ModelError, match="配置已变更"):
        await model.chat("g", "decision", history + [response])
    assert len(requests) == 2

async def test_provider_key_isolation_and_discovery_does_not_overwrite_catalog(model):
    cfg = model.catalog.get()
    cfg["providers"].append({"id": "other", "name": "Other", "base_url": "http://10.23.45.67:7700/v1", "protocol": "openai-completions", "enabled": True})
    model.catalog.save(ModelConfigWrite(**cfg, keys={"other": "other-secret"}))
    def handle(request):
        assert request.headers["authorization"] == "Bearer other-secret"
        assert str(request.url) == "http://10.23.45.67:7700/v1/models"
        assert request.method == "GET"
        return httpx.Response(200, json={"data": [{"id": "listed-only"}]})
    model.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    assert (await model.discover("other"))[0]["id"] == "listed-only"
    assert not any(m["model_id"] == "listed-only" for m in model.catalog.get()["models"])
    with pytest.raises(ModelError):
        await model.discover("unknown")


@pytest.mark.parametrize("base,allowed", [
    ("https://provider.example/v1", True),
    ("http://10.23.45.67:7700/v1", True),
    ("http://10.0.0.5:8080/v1", True),
    ("http://172.31.255.254/v1", True),
    ("http://172.32.0.1/v1", False),
    ("http://8.8.8.8/v1", False),
    ("http://169.254.169.254/v1", False),
    ("http://provider.example/v1", False),
    ("http://10.23.45.67.example/v1", False),
    ("http://user:password@10.23.45.67/v1", False),
    ("http://10.23.45.67/v1?key=secret", False),
])
def test_provider_endpoint_transport_policy(base, allowed):
    if allowed:
        assert Provider(id="gateway", name="Gateway", base_url=base).base_url == base
    else:
        with pytest.raises(ValueError):
            Provider(id="gateway", name="Gateway", base_url=base)


async def test_no_redirect_secret_leak_no_retry_auth_error_and_context_limit(model):
    count = 0
    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(401, json={"error": {"message": "echo-private-key"}})
    model.client = httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False)
    with pytest.raises(ModelError, match="HTTP 401") as err:
        await model.chat("g", "chat", [{"role": "user", "content": "test"}])
    assert "echo-private-key" not in str(err.value) and count == 1
    cfg = model.catalog.get(); cfg["models"][0].update(context_window=4096, max_tokens=1024)
    for route in cfg["routes"].values():
        route["max_tokens"] = 1024
    model.catalog.save(ModelConfigWrite(**cfg))
    with pytest.raises(ModelError, match="上下文容量"):
        await model.chat("g", "chat", [{"role": "user", "content": "长" * 5000}])
    assert count == 1

@pytest.mark.parametrize("base", ["https://api.deepseek.com", "https://api.deepseek.com/v1", "https://api.deepseek.com/anthropic", "https://api.deepseek.com/anthropic/v1"])
def test_native_search_url_is_not_nested_under_messages(base):
    assert Model.endpoint({"base_url": base}, "tools/search") == "https://api.deepseek.com/v1/tools/search"


def test_expanded_model_capacity_still_checks_relationships(authed_client):
    c=authed_client;cfg=c.get('/api/v1/models').json();cfg.pop('key_configured')
    for m in cfg['models']: m.update(context_window=4000000,max_tokens=600000)
    cfg['routes']['chat']['max_tokens']=500000
    assert c.put('/api/v1/models',json=cfg).status_code==200
    cfg['routes']['chat']['max_tokens']=600001
    assert c.put('/api/v1/models',json=cfg).status_code==422
