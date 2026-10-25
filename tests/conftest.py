import pytest
from fastapi.testclient import TestClient
from rolebot.api import create_app
from rolebot.config import Settings
from rolebot.db import Database
from rolebot.engine import Engine, group_defaults
from rolebot.media import Media
from rolebot.model import Model
from rolebot.tools import ToolRunner
from support import FakeBot, FakeModel, SCOPE, auth
from types import SimpleNamespace


"""Shared fixtures; feature-specific fixtures stay with their tests."""
@pytest.fixture
def db(tmp_path):
    store = Database(tmp_path/'test.sqlite')
    card=store.get('persona','default')
    card.update(prompt='仅使用合成测试资料。',aliases=['机器人','助手','小助手'])
    store.put('persona','default',card)
    yield store
    store.close()

@pytest.fixture
def runtime(db,tmp_path):
    settings=Settings(tmp_path/'runtime');bot=FakeBot();model=FakeModel();media=Media(settings)
    tools=ToolRunner(db,settings,bot,model);engine=Engine(db,settings,bot,model,media,tools)
    cfg=db.config();cfg.update(admin_qq=['90000001'],initialized=True);db.put('settings','global',cfg)
    g=group_defaults(SCOPE,'测试群');g['enabled']=True;
    g['listening_probability']=100  # Existing behavior tests bypass randomness; sampling tests set their own rate.
    db.put('group',SCOPE,g)
    return SimpleNamespace(db=db,settings=settings,bot=bot,model=model,tools=tools,engine=engine,group=g)

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setenv('COOKIE_SECURE','false');monkeypatch.setenv('ADMIN_PASSWORD','test-password-123456')
    monkeypatch.setenv('PUBLIC_BASE_URL','http://testserver')
    app=create_app(tmp_path/'api',start_engine=False)
    card=app.state.db.get('persona','default')
    card.update(prompt='仅使用合成测试资料。',aliases=['机器人','助手','小助手'])
    app.state.db.put('persona','default',card)
    with TestClient(app) as c: yield c
@pytest.fixture
async def model(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    settings = Settings(tmp_path / "data")
    settings.set_key("test-only-provider-key")
    instance = Model(db, settings)
    await instance.client.aclose()
    yield instance
    await instance.close()
    db.close()

@pytest.fixture
def authed_client(client):
    client.headers.update(auth(client))
    return client


@pytest.fixture
def sample_art(tmp_path,monkeypatch):
    from rolebot import art_assets
    from art_samples import make_library
    root=make_library(tmp_path/'art')
    monkeypatch.setattr(art_assets,'ROOT',root)
    return root
