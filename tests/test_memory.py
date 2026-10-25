from support import tool_reply
import json
import time

import pytest

from rolebot.history import History
from rolebot.knowledge import Knowledge
from rolebot.memory import Memory
from rolebot.memory_semantics import view
from rolebot.tool_loop import ToolLoop

from support import OTHER, SCOPE, SECOND, USER, auth, fact, reply_decision, source


SECOND = "30000002"


@pytest.mark.parametrize("quote,text,claim", [
    ("就是想让助手一心一意嘛", "本人希望助手一心一意（专一）", "wish"),
    ("但是我真的喜欢助手啊", "本人表示真的很喜欢助手", "expression"),
    ("希望机器人永远只陪着我", "机器人已同意永远只陪着朋友", "wish"),
])
def test_role_expressions_are_quoted_dated_and_scoped(runtime, quote, text, claim):
    r = runtime; m = source(r, quote, 1, ts=1792373280)
    k = r.engine.memory.knowledge
    id = k.apply(SCOPE, "default", [fact(m, text)], [{**m, "role": "user"}])[0]
    row = k.row(SCOPE, id); d = row["data"]
    assert row["persona"] == "default" and d["type"] == "relation"
    assert d["claim_kind"] == claim and d["temporal_scope"] == "episode"
    assert d["subject_ids"] == [USER] and d["declarant_ids"] == [USER]
    assert d["object_ids"] == [r.bot.self_id] and d["object_persona"] == "default"
    assert d["occurred_at"] == 1792373280 and quote in row["text"] and "已同意" not in row["text"]
    assert not r.db.recall(SCOPE, "another", quote) and not r.db.recall(OTHER, "default", quote)
    projection = view(r.db, row)
    assert projection["subjects"] == [{"qq": USER, "name": "朋友"}]
    assert projection["objects"][0]["name"] == "机器人"
    assert "不表示对象同意" in projection["interpretation"]

@pytest.mark.parametrize("quote,text", [("我喜欢草莓", "本人喜欢草莓"), ("机器人，我喜欢助手题材的动漫，不是特指你", "本人喜欢助手题材动漫")])
def test_general_preferences_remain_shared(runtime, quote, text):
    r = runtime; m = source(r, quote, 1)
    id = r.engine.memory.knowledge.apply(SCOPE, "default", [fact(m, text, claim_kind="preference", temporal_scope="ongoing")], [{**m, "role": "user"}])[0]
    row = r.engine.memory.knowledge.row(SCOPE, id)
    assert row["persona"] == "" and row["text"].startswith("朋友")
    assert row["data"]["claim_kind"] == "preference" and row["data"]["object_ids"] == []
    assert r.db.recall(SCOPE, "another", "喜欢")

def test_relations_between_members_do_not_invent_bot_as_object(runtime):
    r = runtime; other = "30000002"
    source(r, "你好", 1, actor=other)
    m = source(r, "我和小明一起打过游戏", 2)
    id = r.engine.memory.knowledge.apply(SCOPE, "default", [fact(m, "朋友曾和小明打游戏", type="relation", object_ids=[other], claim_kind="event")], [{**m, "role": "user"}])[0]
    assert r.engine.memory.knowledge.row(SCOPE, id)["data"]["object_ids"] == [other]

def test_report_about_bot_does_not_turn_into_speakers_own_expression(runtime):
    r=runtime; m=source(r,"机器人喜欢草莓",1)
    proposal=fact(m,"机器人喜欢草莓",subjects=[r.bot.self_id])
    id=r.engine.memory.knowledge.apply(SCOPE,"default",[proposal],[{**m,"role":"user"}])[0]
    row=r.engine.memory.knowledge.row(SCOPE,id)
    assert row['data']['subject_ids']==[r.bot.self_id] and row['data']['declarant_ids']==[USER]
    assert row['data']['evidence_kind']=='third_party' and row['data']['status']=='unverified'
    assert row['text']=='机器人喜欢草莓' and not row['data']['object_ids']

@pytest.mark.parametrize("kind,claim",[("fact","fact"),("relation","expression")])
def test_explicit_general_preference_corrects_inconsistent_model_tags(runtime,kind,claim):
    r=runtime; m=source(r,"机器人，我喜欢助手题材的动漫，不是特指你",1)
    proposal=fact(m,"朋友喜欢机器人",type=kind,claim_kind=claim,object_persona="default",object_ids=[r.bot.self_id])
    id=r.engine.memory.knowledge.apply(SCOPE,"default",[proposal],[{**m,"role":"user"}])[0]
    row=r.engine.memory.knowledge.row(SCOPE,id)
    assert row['persona']=='' and row['data']['object_ids']==[] and row['data']['object_persona']==''
    assert row['data']['claim_kind']=='preference' and '不是特指你' in row['text']

def test_episodes_do_not_become_conflicting_stable_preferences(runtime):
    r = runtime; k = r.engine.memory.knowledge
    for i, quote in enumerate(["希望机器人一心一意", "希望机器人陪我看星星", "希望机器人一心一意"]):
        m = source(r, quote, i+1, ts=[1792373282,1792373281,1792373280][i])
        k.apply(SCOPE, "default", [fact(m, quote, fact_key="relationship")], [{**m, "role": "user"}])
    rows = r.db.memories(SCOPE)
    assert len(rows) == 2 and all(x["data"]["status"] == "active" for x in rows)
    merged = next(x for x in rows if "一心一意" in x["text"])
    assert len(merged["data"]["source_ids"]) == 2
    assert merged["data"]["occurred_at"] == 1792373280 and merged["data"]["last_observed_at"] == 1792373282

@pytest.mark.parametrize("extra", [{"object_ids":["999"]}, {"object_ids":[123]}, {"object_persona":"another"}, {"confidence":float("nan")}, {"temporal_scope":"forever"}])
def test_invalid_semantic_metadata_rejected(runtime, extra):
    r=runtime; m=source(r, "喜欢机器人", 1)
    assert not r.engine.memory.knowledge.apply(SCOPE,"default",[fact(m,"喜欢机器人",**extra)],[{**m,"role":"user"}])

async def test_extraction_receives_identities_and_rules(runtime):
    r = runtime; m = source(r, "机器人，请记住我希望你一心一意", 1)
    r.model.script = [{"content":json.dumps({"memories":[fact(m,"本人希望助手一心一意")],"styles":[]})}]
    job = r.engine.memory.enqueue(SCOPE, "default", explicit_ids=[m["id"]])
    assert await r.engine.memory.process_job(job) == "complete"
    messages = r.model.calls[0][2]; material=json.loads(messages[1]["content"])
    assert material["current_role"]["id"] == "default" and material["members"][0]["qq"] == USER
    assert "愿望不等于已实现事实" in messages[0]["content"]
    row = r.db.memories(SCOPE)[0]
    _, preview = r.engine.memory.build(SCOPE,"default","一心一意")
    found = next(x for x in preview["context"]["相关记忆"] if x["id"] == row["id"])
    assert found["subject_ids"] == [USER] and found["object_persona"] == "default"
    assert found["temporal_scope"] == "episode"
    result = await r.tools.invoke(SCOPE, "default", USER, "memory_search", {"query":"一心一意"})
    assert result[0]["subject_ids"] == [USER] and result[0]["claim_kind"] == "wish"

def test_forgetting_object_removes_versions_index_and_derivatives(runtime):
    r=runtime; other="30000002"
    source(r,"你好",1,actor=other); m=source(r,"我和小明一起去过蓝桥",2)
    k=r.engine.memory.knowledge
    proposal=fact(m,"朋友和小明一起去过蓝桥",type="relation",object_ids=[other],claim_kind="event")
    id=k.apply(SCOPE,"default",[proposal],[{**m,"role":"user"}])[0]
    r.db.put("summary",SCOPE+":default",{"text":"蓝桥"})
    r.db.invalidate_memory(SCOPE,user_id=other)
    assert not k.row(SCOPE,id) and not k.versions(SCOPE,id)
    assert not r.db.rows("SELECT * FROM memory_fts") and not r.db.get("summary",SCOPE+":default")
    assert r.db.message(SCOPE,m["id"]) is None
    member=r.db.get("member",SCOPE+":"+other); member["remember"]=False;r.db.put("member",member["id"],member)
    assert not k.apply(SCOPE,"default",[proposal],[{**m,"role":"user"}])

def test_api_exposes_identity_and_preserves_old_version(client):
    h=auth(client); db=client.app.state.db
    m=source(client.app.state,"我喜欢机器人",1)
    member=db.get('member',SCOPE+':'+USER);member['name']='测试成员乙';db.put('member',member['id'],member)
    k=Knowledge(db)
    id=k.apply(SCOPE,'default',[fact(m,'测试成员乙表达喜欢机器人',type='relation',object_persona='default',object_ids=['10000001'])],[m])[0]
    before=client.get('/api/v1/memories',params={'scope':SCOPE,'persona':'default'}).json()[0]
    assert before['interpretation']['objects'][0]['name']=='机器人'
    response=client.put('/api/v1/memories/'+id,headers=h,json={'scope':SCOPE,'persona':'default','text':'测试成员乙曾表达对机器人的喜欢','subject_ids':[USER]})
    assert response.status_code==200
    row=client.get('/api/v1/memories',params={'scope':SCOPE,'persona':'default'}).json()[0]
    assert row['interpretation']['subjects']==[{'qq':USER,'name':'测试成员乙'}]
    versions=client.get('/api/v1/memories/'+id+'/versions',params={'scope':SCOPE}).json()
    assert [v['version'] for v in versions]==[2,1]
    assert client.get('/api/v1/memories',params={'scope':SCOPE,'persona':'another'}).json()==[]

def test_tool_retrieved_memory_propagates_all_identities(runtime):
    r=runtime
    result=r.engine.reply_provenance(SCOPE,"default",{},[],[{"name":"memory_search","status":"success","result":[{
        "id":"m1","source_ids":["s1"],"subject_ids":[USER],"declarant_ids":["30000002"],"object_ids":["30000003"]}]}])
    assert result['subject_ids']==[USER,'30000002','30000003']
    assert result['source_ids']==['s1'] and result['memory_ids']==['m1']

def test_memory_correction_versions_dedup_and_conflict(runtime):
    r=runtime;k=r.engine.memory.knowledge
    a=source(r,"我喜欢草莓",1)
    id=k.apply(SCOPE,"default",[fact(a,"喜欢草莓")],[{**a,"role":"user"}])[0]
    assert k.apply(SCOPE,"default",[fact(a,"喜欢草莓")],[{**a,"role":"user"}]) == []
    b=source(r,"纠正，我现在更喜欢芒果",2)
    k.apply(SCOPE,"default",[fact(b,"现在更喜欢芒果",supersedes_ids=[id])],[{**b,"role":"user"}])
    assert len(r.db.memories(SCOPE))==1 and k.row(SCOPE,id)["text"]=="现在更喜欢芒果"
    assert [v["version"] for v in k.versions(SCOPE,id)]==[2,1]
    c=source(r,"小明喜欢香蕉",3,actor=SECOND)
    ids=k.apply(SCOPE,"default",[fact(c,"喜欢香蕉",subjects=[USER])],[{**c,"role":"user"}])
    assert k.row(SCOPE,ids[0])["data"]["evidence_kind"]=="third_party"
    assert all(m["data"]["status"]=="conflict" for m in r.db.memories(SCOPE))
    k.resolve(SCOPE,id)
    assert k.row(SCOPE,ids[0])["data"]["status"]=="archived"
    assert k.row(SCOPE,id)["data"]["status"]=="active"

def test_memory_evidence_and_subject_constraints(runtime):
    r=runtime;k=r.engine.memory.knowledge
    m=source(r,"这是群里的一条闲聊",1)
    bad=fact(m,"捏造的偏好",evidence=[{"source_id":m["id"],"quote":"不存在的原文"}])
    assert not k.apply(SCOPE,"default",[bad],[{**m,"role":"user"}])
    assert not k.apply(SCOPE,"default",[fact(m,"跨范围",subjects=["99999999"])],[{**m,"role":"user"}])
    joke=source(r,"开玩笑，我喜欢喝汽油",2)
    id=k.apply(SCOPE,"default",[fact(joke,"喜欢喝汽油",fact_key="drink")],[{**joke,"role":"user"}])[0]
    assert k.row(SCOPE,id)["data"]["status"]=="unverified"

def test_group_common_knowledge_and_bot_story_can_be_recorded(runtime):
    r=runtime;k=r.engine.memory.knowledge
    m=source(r,"我们群的聚会时间是周五晚上",1)
    item=fact(m,"群聚会时间是周五晚上");item["subject_ids"]=[]
    assert k.apply(SCOPE,"default",[item],[{**m,"role":"user"}])
    item=fact(m,"机器人参加聚会",subjects=["10000001"],type="story",fact_key="outing")
    assert k.apply(SCOPE,"default",[item],[{**m,"role":"user"}])

async def test_forget_also_removes_bot_derivatives_and_old_queued_events(runtime):
    r=runtime;m=source(r,"机器人，秘密地点是蓝桥",1);m["memory_epoch"]=0
    r.model.script=[reply_decision(m, text="秘密地点蓝桥，我知道啦")]
    await r.engine.turn(SCOPE,[m]);assert len(r.bot.sent)==1
    r.db.invalidate_memory(SCOPE,user_id=USER)
    assert not r.db.recent(SCOPE,"default")
    await r.engine.turn(SCOPE,[m]);assert len(r.bot.sent)==1

def test_manual_correction_preserves_history_without_erasing_original(runtime):
    r=runtime;k=r.engine.memory.knowledge;m=source(r,"我喜欢草莓",1)
    id=k.apply(SCOPE,"default",[fact(m,"喜欢草莓")],[{**m,"role":"user"}])[0]
    k.manual(SCOPE,"","实际喜欢芒果",[USER],True,id)
    assert r.db.message(SCOPE,m["id"])["text"]==m["text"]
    assert len(k.versions(SCOPE,id))==2
    k.restore(SCOPE,id,1)
    assert k.row(SCOPE,id)["text"]=="喜欢草莓" and k.row(SCOPE,id)["data"]["version"]==3

def test_style_learning_threshold_manual_disable_and_forget(runtime):
    r=runtime;k=r.engine.memory.knowledge
    samples=[{**source(r,"猫猫开会啦",i,fragment="first" if i<3 else "second"),"role":"user"} for i in range(5)]
    item={"expression":"猫猫开会","meaning":"开始聊天","usage":"轻松讨论","examples":[{"source_id":m["id"],"quote":m["text"]} for m in samples]}
    k.learn(SCOPE,[item],samples[:4]);assert not k.styles(SCOPE,True)
    k.learn(SCOPE,[item],samples);s=k.styles(SCOPE,True)[0]
    k.edit_style(SCOPE,s["id"],"修改含义","日常",False)
    more={**source(r,"猫猫开会啦",6,fragment="third"),"role":"user"}
    item["examples"].append({"source_id":more["id"],"quote":more["text"]})
    k.learn(SCOPE,[item],[more]);assert not k.styles(SCOPE,True)
    k.restore_style(SCOPE,s["id"],2);assert k.styles(SCOPE,True)
    r.db.invalidate_memory(SCOPE,user_id=USER)
    assert not k.styles(SCOPE) and not r.db.all("style_version")
    with pytest.raises(ValueError):k.restore_style(SCOPE,s["id"],2)

def test_style_ignores_assistant_quotes_and_opted_out_sources(runtime):
    r=runtime;k=r.engine.memory.knowledge
    m={**source(r,"猫猫开会啦",1),"role":"assistant"}
    item={"expression":"猫猫开会","examples":[{"source_id":m["id"],"quote":m["text"]}]}
    k.learn(SCOPE,[item],[m]);assert not k.styles(SCOPE)
    m["role"]="user";m["data"]["replies"]=["42"]
    k.learn(SCOPE,[item],[m]);assert not k.styles(SCOPE)
    m["data"]["replies"]=[];member=r.db.get("member",SCOPE+":"+USER);member["remember"]=False;r.db.put("member",member["id"],member)
    k.learn(SCOPE,[item],[m]);assert not k.styles(SCOPE)


async def test_explicit_memory_runs_before_threshold_and_is_not_repeated(runtime):
    r=runtime;m=source(r,"请记住我喜欢草莓",1)
    item=fact(m,"喜欢草莓")
    r.model.script=[{"role":"assistant","content":json.dumps({"memories":[item],"styles":[]})}]
    assert not r.engine.memory.enqueue(SCOPE,"default")
    id=r.engine.memory.enqueue(SCOPE,"default",explicit_ids=[m["id"]]);assert id
    assert await r.engine.memory.process_job(id)=="complete"
    assert r.db.memories(SCOPE)[0]["text"]=="喜欢草莓"
    assert r.engine.memory.enqueue(SCOPE,"default",explicit_ids=[m["id"]]) is None

async def test_fact_job_recovery_and_bounded_failure(runtime):
    r=runtime;m=source(r,"普通内容",1,ts=time.time()-130)
    id=r.engine.memory.enqueue(SCOPE,"default",explicit_ids=[m['id']]);assert id
    job=r.db.get("memory_job",id);job["status"]="running";r.db.put("memory_job",id,job)
    r.engine.memory.recover();assert r.db.get("memory_job",id)["status"]=="pending"
    r.model.script=[{"content":"invalid"} for _ in range(3)]
    for _ in range(3):
        await r.engine.memory.process_job(id)
        job=r.db.get("memory_job",id);job["retry_at"]=0;r.db.put("memory_job",id,job)
    assert job["status"]=="failed" and job["attempts"]==3
    assert not r.db.rows("SELECT * FROM memory_processed")
    assert r.engine.memory.enqueue(SCOPE,"default") is None

async def test_forgetting_inflight_job_discards_result_and_trace(runtime):
    r=runtime;m=source(r,"请记住秘密水果",1)
    async def forgotten(*args,**kwargs):
        r.db.invalidate_memory(SCOPE,user_id=USER)
        return {"content":json.dumps({"memories":[fact(m,"秘密水果")],"styles":[]})}
    r.model.chat=forgotten
    id=r.engine.memory.enqueue(SCOPE,"default",explicit_ids=[m["id"]])
    assert await r.engine.memory.process_job(id)=="cancelled"
    assert not r.db.memories(SCOPE)
    assert all(t["redacted"] for t in r.db.all("trace"))
    assert "秘密水果" not in str(r.db.all("trace"))


def test_api_memory_versions_and_replay(client):
    h=auth(client)
    client.put('/api/v1/settings',headers=h,json={'admin_qq':['90000001'],'api_key':'test'})
    body={"name":"测试","enabled":True,"learning_enabled":True,"followup_seconds":120}
    assert client.put('/api/v1/groups/'+SCOPE,headers=h,json=body).status_code==200
    m={"scope":SCOPE,"text":"草莓","subject_ids":[USER]}
    id=client.post('/api/v1/memories',headers=h,json=m).json()["id"]
    assert client.put('/api/v1/memories/'+id,headers=h,json={**m,"text":"芒果"}).status_code==200
    assert len(client.get(f'/api/v1/memories/{id}/versions?scope={SCOPE}').json())==2
    assert client.post(f'/api/v1/memories/{id}/restore/1?scope={SCOPE}',headers=h).status_code==200
    assert client.post('/api/v1/memory/search',headers=h,json={"scope":OTHER,"query":"草莓"}).status_code==404
    report=client.post('/api/v1/evaluation/replay',headers=h).json()
    assert report['passed']==report['total']
    client.delete(f'/api/v1/memories/{id}?scope={SCOPE}',headers=h)
    assert client.post(f'/api/v1/memories/{id}/restore/1?scope={SCOPE}',headers=h).status_code==409

@pytest.mark.parametrize("flag", ["paused", "enabled"])
async def test_background_commit_rechecks_live_group_switch(runtime, flag):
    r=runtime;m=source(r,"请记住我喜欢草莓",1)
    async def change(*args,**kwargs):
        group=r.db.get("group",SCOPE);group[flag]=flag=="paused";r.db.put("group",SCOPE,group)
        return {"content":json.dumps({"memories":[fact(m,"喜欢草莓")],"styles":[]})}
    r.model.chat=change
    id=r.engine.memory.enqueue(SCOPE,"default",explicit_ids=[m["id"]])
    assert await r.engine.memory.process_job(id)=="pending"
    assert not r.db.memories(SCOPE) and not r.db.rows("SELECT * FROM memory_processed")

def test_job_listing_is_scoped_and_filtered_without_loading_history(db):
    for i,status in enumerate(["complete","failed","pending","running"]):
        db.put("memory_job",str(i),{"id":str(i),"scope":SCOPE,"status":status})
    db.put("memory_job","other",{"id":"other","scope":OTHER,"status":"pending"})
    assert {j["status"] for j in db.scoped_objects("memory_job",SCOPE,statuses=("pending","running"))}=={"pending","running"}

def candidate(m, quote, text, **kwargs):
    return fact(m, text, evidence=[{"source_id":m["id"], "quote":quote}], **kwargs)

def test_neighboring_wish_does_not_reclassify_past_event(runtime):
    r=runtime; m=source(r,"昨天和机器人一起打游戏很开心，希望她下次也来",1)
    item=candidate(m,"昨天和机器人一起打游戏很开心","朋友昨天与机器人一起打游戏",type="relation",claim_kind="event",object_ids=[r.bot.self_id],object_persona="default")
    id=r.engine.memory.knowledge.apply(SCOPE,"default",[item],[m])[0]
    row=r.engine.memory.knowledge.row(SCOPE,id)
    assert row["data"]["claim_kind"]=="event" and row["text"]==item["text"]
    wish=candidate(m,"希望她下次也来","朋友希望机器人下次也一起玩",type="relation",claim_kind="wish",object_ids=[r.bot.self_id],object_persona="default")
    id=r.engine.memory.knowledge.apply(SCOPE,"default",[wish],[m])[0]
    assert r.engine.memory.knowledge.row(SCOPE,id)["data"]["claim_kind"]=="wish"

def test_neighboring_joke_does_not_contaminate_preference(runtime):
    r=runtime;m=source(r,"我最喜欢草莓，开玩笑说助手要变成水果",1)
    item=candidate(m,"我最喜欢草莓","朋友最喜欢草莓",claim_kind="preference")
    id=r.engine.memory.knowledge.apply(SCOPE,"default",[item],[m])[0]
    assert r.engine.memory.knowledge.row(SCOPE,id)["data"]["evidence_kind"]=="self_report"

@pytest.mark.parametrize("quote",["你好呀","（抱住助手）","小助手就行","把群名片改成小助手"])
def test_routine_chat_and_parameters_do_not_become_memory(runtime,quote):
    r=runtime;m=source(r,quote,1)
    assert not r.engine.memory.knowledge.apply(SCOPE,"default",[fact(m,"朋友喜欢小助手")],[m])

def test_explicit_expression_is_dated_and_role_scoped(runtime):
    r=runtime;m=source(r,"机器人，请记住我希望你陪我看星星",1)
    item=fact(m,"朋友希望机器人陪着看星星",claim_kind="wish",object_persona="default")
    id=r.engine.memory.knowledge.apply(SCOPE,"default",[item],[m])[0]
    row=r.engine.memory.knowledge.row(SCOPE,id)
    assert row["persona"]=="default" and row["data"]["temporal_scope"]=="episode"

def test_new_memory_requires_exact_evidence_for_every_source(runtime):
    r=runtime;a=source(r,"我喜欢草莓",1);b=source(r,"我是另一个人",2,actor="30000002")
    item=fact(a,"朋友喜欢草莓",source_ids=[a["id"],b["id"]])
    assert not r.engine.memory.knowledge.apply(SCOPE,"default",[item],[a,b])
    assert not r.engine.memory.knowledge.apply(SCOPE,"default",[fact(a,"捏造",evidence=[])],[a])

@pytest.mark.parametrize("value",[None,""])
def test_nullable_optional_role_does_not_drop_explicit_preference(runtime,value):
    r=runtime;m=source(r,"请记住，我最喜欢的水果是草莓。",1)
    id=r.engine.memory.knowledge.apply(SCOPE,"default",[fact(m,"朋友最喜欢草莓",claim_kind="preference",object_persona=value)],[m])[0]
    row=r.engine.memory.knowledge.row(SCOPE,id)
    assert row["persona"]=="" and row["data"]["object_persona"]=="" and row["data"]["status"]=="active"

def test_current_fruit_correction_is_versioned_and_compatible_hobby_survives(runtime):
    r=runtime;k=r.engine.memory.knowledge
    a=source(r,"我最喜欢草莓",1);id=k.apply(SCOPE,"default",[fact(a,"朋友最喜欢草莓")],[a])[0]
    b=source(r,"我喜欢画画",2);h=k.apply(SCOPE,"default",[fact(b,"朋友喜欢画画",fact_key="hobby_drawing")],[b])[0]
    c=source(r,"纠正，我现在更喜欢芒果",3)
    k.apply(SCOPE,"default",[fact(c,"朋友现在更喜欢芒果",supersedes_ids=[id])],[c])
    assert len(k.versions(SCOPE,id))==2 and k.row(SCOPE,h)["data"]["status"]=="active"
    assert "芒果" in k.row(SCOPE,id)["text"]

async def test_extraction_support_is_readonly_and_scoped(runtime):
    r=runtime;now=time.time();a=source(r,"我最喜欢草莓",1,ts=now-3);b=source(r,"请记住这个偏好",2,ts=now)
    r.db.add_message("other",OTHER,"default",USER,"user","别群秘密",ts=now-1)
    r.model.script=[{"content":json.dumps({"memories":[fact(a,"朋友最喜欢草莓")],"styles":[]})}]
    job=r.engine.memory.enqueue(SCOPE,"default",explicit_ids=[b["id"]])
    assert await r.engine.memory.process_job(job)=="complete"
    data=json.loads(r.model.calls[0][2][1]["content"])
    assert [m["id"] for m in data["messages"]]==[b["id"]]
    assert [m["id"] for m in data["supporting_context"]]==[a["id"]]
    assert not r.db.memories(SCOPE),"A supporting source cannot silently become a fresh fact"
    t=r.db.get("trace",r.db.get("memory_job",job)["trace_id"])
    assert a["id"] in t["source_ids"]
    r.db.invalidate_memory(SCOPE,source_ids=[a["id"]])
    assert r.db.get("trace",t["id"])["redacted"]


def test_auxiliary_memory_dependencies_are_forgotten_together(runtime):
    r=runtime;a=source(r,"我喜欢草莓",1);b=source(r,"请记住这个偏好",2)
    id=r.engine.memory.knowledge.apply(SCOPE,"default",[fact(b,"朋友喜欢草莓")],[b],context_messages=[a])[0]
    row=r.engine.memory.knowledge.row(SCOPE,id)
    assert a["id"] in row["data"]["context_source_ids"]
    r.db.invalidate_memory(SCOPE,source_ids=[a["id"]])
    assert not r.engine.memory.knowledge.row(SCOPE,id) and not r.engine.memory.knowledge.versions(SCOPE,id)


@pytest.fixture
def active(runtime, monkeypatch):
    runtime.db.put('group', SCOPE, runtime.group)
    runtime.clock = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: runtime.clock[0])
    return runtime


def answer(m, speak=True, **extra):
    return {'role': 'assistant', 'content': json.dumps({'speak': speak, 'text': '回答'+m['id'] if speak else '',
        'reply_to_message_id': m['id'], 'mode': 'direct', **extra})}


async def speak(r, i=1):
    m = source(r, '机器人，第'+str(i)+'次聊天', i)
    r.model.script = [answer(m)]
    await r.engine.turn(SCOPE, [m])
    return m


def empty_result(r):
    r.model.script = [{'content': '{"memories":[],"styles":[]}'}]*10


async def test_silent_archives_never_schedule_learning_or_compaction(active):
    r = active
    for i in range(200): source(r, '归档闲聊'+str(i)+'长'*1000, i)
    r.clock[0] += 86400
    await r.engine.memory.background_once()
    assert not r.model.calls and not r.db.all('memory_job')
    assert not r.db.rows('SELECT * FROM memory_processed')
    assert History(r.db, SCOPE, 'default').search('归档闲聊0')['messages']


async def test_idle_timer_ignores_group_traffic_and_freezes_evidence(active):
    r = active
    m = await speak(r)
    assert len(r.model.calls) == 1
    window = r.db.all('learning_session')[0]
    r.clock[0] += 599
    late = source(r, '机器人沉默以后群里继续聊', 2)
    await r.engine.memory.background_once()
    assert len(r.model.calls) == 1
    r.clock[0] += 2; empty_result(r)
    await r.engine.memory.background_once()
    call = r.model.calls[-1]
    material = json.loads(call[2][1]['content'])
    assert call[3]['stage'] == 'extract'
    assert [x['id'] for x in material['messages']] == [m['id']]
    assert late['id'] not in str(material)
    assert r.db.get('learning_session', window['id'])['last_participated_at'] == window['last_participated_at']
    for i in range(3, 70): source(r, '沉默期间积压'+str(i), i)
    r.clock[0] += 7201
    await r.engine.memory.background_once()
    assert len(r.model.calls) == 2 and len(r.db.all('memory_job')) == 1


async def test_only_successful_chat_refreshes_participation(active):
    r = active
    await speak(r)
    first = r.db.all('learning_session')[0]['last_participated_at']
    r.clock[0] += 590
    m = source(r, '机器人，没事', 2);r.model.script = [answer(m, False)]
    await r.engine.turn(SCOPE, [m])
    assert r.db.all('learning_session')[0]['last_participated_at'] == first
    r.clock[0] += 11; empty_result(r)
    await r.engine.memory.background_once()
    assert len(r.db.all('memory_job')) == 1
    assert m['id'] not in r.db.all('memory_job')[0]['source_ids']


async def test_new_reply_moves_idle_deadline_but_not_two_hour_deadline(active):
    r = active
    await speak(r)
    started = r.clock[0]
    r.clock[0] += 590
    await speak(r, 2)
    status = r.engine.memory.participation.status(SCOPE, 'default')
    assert status['next_due_at'] == started+1190
    r.clock[0] = started+601
    await r.engine.memory.background_once()
    assert len(r.model.calls) == 2
    # Simulate ongoing successful sends without adding artificial model calls.
    for elapsed in range(1000, 7200, 500):
        r.clock[0] = started+elapsed
        r.engine.memory.participation.record(SCOPE, 'default', [SCOPE+':1'], 0)
    assert r.engine.memory.participation.status(SCOPE, 'default')['next_due_at'] == started+7200
    r.clock[0] = started+7201;empty_result(r)
    await r.engine.memory.background_once()
    assert len(r.db.all('memory_job')) == 1


async def test_restart_resumes_due_session_once(active):
    r = active
    await speak(r);r.clock[0] += 601
    recovered = Memory(r.db, r.model);recovered.recover();empty_result(r)
    await recovered.background_once()
    await recovered.background_once()
    assert len(r.model.calls) == 2
    assert recovered.participation.status(SCOPE, 'default')['pending_messages'] == 0


async def test_resume_only_learns_current_working_set_not_silent_backlog(active):
    r = active
    await speak(r);r.clock[0] += 601;empty_result(r)
    await r.engine.memory.background_once()
    for i in range(2, 202): source(r, '沉默期间的记录'+str(i), i)
    r.clock[0] += 7200
    m = await speak(r, 202)
    r.clock[0] += 601;empty_result(r)
    await r.engine.memory.background_once()
    learned = r.db.all('memory_job')[0]['source_ids']
    assert m['id'] in learned and SCOPE+':2' not in learned and len(learned) <= 40
    assert History(r.db, SCOPE, 'default').read(SCOPE+':2')['messages']


async def test_legacy_backlog_is_ignored_but_preserved_for_rollback(active):
    r = active
    for i in range(20): source(r, '旧队列'+str(i), i)
    old = 'archived-backlog'
    r.db.put('memory_job', old, {'id': old, 'scope': SCOPE, 'persona': 'default', 'kind': 'extract', 'status': 'pending', 'urgent': False, 'epoch': 0, 'ts': time.time(), 'source_ids': [SCOPE+':0']})
    r.engine.memory.recover()
    await r.engine.memory.background_once()
    assert not r.model.calls and r.db.get('memory_job', old)['status'] == 'pending'
    assert await r.engine.memory.process_job(old) == 'superseded'
    m = source(r, '请记住我喜欢草莓', 21)
    r.model.script = [{'content': json.dumps({'memories': [fact(m, '喜欢草莓')], 'styles': []})}]
    urgent = r.engine.memory.enqueue(SCOPE, 'default', explicit_ids=[m['id']])
    await r.engine.memory.background_once()
    assert r.db.get('memory_job', urgent)['status'] == 'complete'
    assert r.db.get('memory_job', old)['status'] == 'pending'


@pytest.mark.parametrize('when', ['before_due', 'inflight'])
async def test_forgetting_removes_windows_and_blocks_pending_results(active, when):
    r = active
    m = await speak(r)
    if when == 'before_due':
        r.db.invalidate_memory(SCOPE, user_id=USER)
        r.clock[0] += 601
        await r.engine.memory.background_once()
        assert len(r.model.calls) == 1
    else:
        async def forgotten(*args, **kwargs):
            r.db.invalidate_memory(SCOPE, user_id=USER)
            return {'content': json.dumps({'memories': [fact(m, '不该写入')], 'styles': []})}
        r.model.chat = forgotten;r.clock[0] += 601
        await r.engine.memory.background_once()
    assert not r.db.all('learning_session') and not r.db.all('working_context')
    assert not r.db.memories(SCOPE)


async def test_retries_bounded_and_new_sessions_are_not_blocked(active):
    r = active
    await speak(r);r.clock[0] += 601
    job = r.engine.memory.enqueue(SCOPE, 'default')
    r.model.script = [{'content': 'invalid'}]*3
    for i in range(3):
        await r.engine.memory.process_job(job)
        r.clock[0] += 1000
    assert r.db.get('memory_job', job)['status'] == 'failed'
    before = len(r.model.calls)
    await r.engine.memory.background_once()
    assert len(r.model.calls) == before
    await speak(r, 2);r.clock[0] += 601;empty_result(r)
    await r.engine.memory.background_once()
    assert any(j['status'] == 'complete' for j in r.db.all('memory_job'))


async def test_role_change_cancels_pending_participation_window(active):
    r=active
    await speak(r);r.clock[0]+=601
    job=r.engine.memory.enqueue(SCOPE,'default')
    r.group['persona']='another-role';r.db.put('group',SCOPE,r.group)
    assert await r.engine.memory.process_job(job)=='cancelled'
    assert r.db.all('learning_session')[0]['status']=='cancelled'
    r.group['persona']='default';r.db.put('group',SCOPE,r.group)
    assert r.engine.memory.participation.status(SCOPE,'default')['pending_sessions']==0
    assert len(r.model.calls)==1


async def test_large_participation_session_drains_only_its_sources(active):
    r = active
    ms = [source(r, '参与记录'+str(i), i) for i in range(185)]
    r.engine.memory.participation.record(SCOPE, 'default', [m['id'] for m in ms], 0)
    later = source(r, '后续非参与消息', 200)
    r.clock[0] += 601;empty_result(r)
    for _ in range(3): await r.engine.memory.background_once()
    assert len(r.model.calls) == 2
    assert r.db.rows('SELECT COUNT(*) n FROM memory_processed')[0]['n'] == 185
    assert not r.db.get('memory_cursor', SCOPE+':default')
    assert not r.db.rows('SELECT 1 FROM memory_processed WHERE message_id=?', (later['id'],))


async def test_failed_send_and_scheduled_reminder_do_not_start_learning(active):
    r = active
    async def fail(*args, **kwargs): raise RuntimeError('mock send failed')
    send = r.bot.send;r.bot.send = fail
    await speak(r)
    assert not r.db.all('learning_session')
    r.bot.send = send
    r.group['interaction_enabled'] = True;r.db.put('group', SCOPE, r.group)
    task = await r.engine.scheduler.create(SCOPE, 'default', 'admin-console', {'text': '后台提醒', 'delay_seconds': 5}, principal='admin')
    await r.engine.scheduler.execute(task)
    assert r.db.get('reminder', task['id'])['status'] == 'sent'
    assert not r.db.all('learning_session')


def test_working_history_is_bounded_stable_and_can_query_omitted_source(active):
    r = active
    for i in range(100): source(r, '话题'+str(i), i)
    first, preview = r.engine.memory.build(SCOPE, 'default', '现在')
    assert len(preview['context']['消息']) == 40
    assert preview['omitted_messages'] == 60
    for i in range(100, 110): source(r, '继续话题'+str(i), i)
    second, preview = r.engine.memory.build(SCOPE, 'default', '继续')
    assert second[1]['content'].startswith(first[1]['content']+'\n')
    assert len(preview['context']['消息']) == 50
    for i in range(110, 142): source(r, '新增'+str(i), i)
    _, preview = r.engine.memory.build(SCOPE, 'default', '继续')
    assert len(preview['context']['消息']) == 40
    assert r.db.message(SCOPE, SCOPE+':0')
    assert History(r.db, SCOPE, 'default').search('话题0')['messages'][0]['id'] == SCOPE+':0'


def test_soft_budget_quotes_and_pending_question_are_preserved(active):
    r = active
    old = source(r, '尚未回答的问题', 0)
    r.db.put('followup', SCOPE+':default', {'source_id': old['id'], 'expires': r.clock[0]+120})
    for i in range(1, 100): source(r, '背景'*1000, i)
    m = source(r, '机器人，请看引用', 100, replies=['1'])
    messages, p = r.engine.memory.build(SCOPE, 'default', m['text'], [m])
    ids = {m['id'] for m in p['context']['消息']}
    assert old['id'] in ids and m['id'] in ids
    assert p['context']['quotes'][0]['id'] == SCOPE+':1'
    assert p['estimated_tokens'] <= p['soft_budget']
    assert 'history_coverage' in messages[-1]['content']


async def test_history_lookup_can_reach_omitted_records_and_learn_used_evidence(active):
    r = active
    for name in ('history_read', 'history_search'): r.group['tools'][name]['enabled'] = True
    r.db.put('group', SCOPE, r.group)
    old = source(r, '上次约在公园见面', 0)
    for i in range(1, 100): source(r, '闲聊'+str(i), i)
    m = source(r, '机器人，上次约在哪里', 100)
    r.model.script = [tool_reply('history_read', {'message_id': old['id'], 'source_message_id': m['id']}, id='h1'), answer(m)]
    await r.engine.turn(SCOPE, [m])
    assert len(r.model.calls) == 2 and len(r.bot.sent) == 1
    tool_messages = [json.loads(x['content']) for x in r.model.calls[1][2] if x['role']=='tool']
    assert tool_messages[0]['status'] == 'success', tool_messages
    first = r.model.calls[0][2]
    assert old['id'] not in first[1]['content']
    assert old['id'] in r.db.all('learning_session')[0]['source_ids']
    _, p = r.engine.memory.build(SCOPE, 'default', '继续')
    assert old['id'] in {m['id'] for m in p['context']['retrieved_history']}
    r.db.invalidate_memory(SCOPE, source_ids=[old['id']])
    assert not r.db.all('working_context')


async def test_history_tool_calls_are_limited_without_breaking_tool_results(active):
    r = active;executions=[]
    call=lambda i: {'id':str(i),'type':'function','function':{'name':'history_search','arguments':'{"query":"话题"}'}}
    r.model.script = [{'role':'assistant','content':'','tool_calls':[call(i) for i in range(4)]}, {'role':'assistant','content':'已核对'}]
    async def execute(name,args): executions.append(name);return {'status':'success','result':{'messages':[]}}
    messages=[{'role':'user','content':'查旧事'}]
    response, executed = await ToolLoop(r.model).run(SCOPE,'chat',messages,[],execute,lambda:True,stage='conversation.agent',history_limit=3)
    assert len(executions)==3 and len(executed)==4
    assert executed[-1]['status']=='denied'
    assert len([m for m in messages if m['role']=='tool'])==4 and response['content']=='已核对'


async def test_tool_growth_stays_bounded_with_complete_structured_results(active):
    r=active
    r.model.script=[{'role':'assistant','content':'','tool_calls':[
        {'id':'h','type':'function','function':{'name':'history_read','arguments':'{}'}}]}, {'role':'assistant','content':'完成'}]
    async def execute(*args):
        return {'status':'success','result':{'messages':[{'id':str(i),'text':'内容'*1000} for i in range(10)]}}
    messages=[{'role':'user','content':'旧事'}]
    response,_=await ToolLoop(r.model).run(SCOPE,'chat',messages,[],execute,lambda:True,stage='conversation.agent',history_limit=3,input_budget=5000)
    tool=json.loads(next(m['content'] for m in messages if m['role']=='tool'))
    assert tool['status']=='success' and tool['truncated']
    assert len(json.dumps(r.model.calls[-1][2],ensure_ascii=False,separators=(',',':'))) <= 5000
    assert response['content']=='完成'


def test_participation_sources_do_not_cross_scope_or_include_optout(active):
    r=active
    allowed=source(r,'允许保留',1)
    stopped=source(r,'停止保留',2,actor='40000001')
    r.db.put('member',SCOPE+':40000001',{'remember':False})
    r.db.add_message(OTHER+':3',OTHER,'default',USER,'user','其他群')
    r.engine.memory.participation.record(SCOPE,'default',[allowed['id'],stopped['id'],OTHER+':3'],0)
    assert r.db.all('learning_session')[0]['source_ids']==[allowed['id']]
    assert r.engine.memory.participation.status(OTHER,'default')['pending_messages']==0
    assert r.engine.memory.participation.status(SCOPE,'other-persona')['pending_messages']==0


async def test_partial_delivery_counts_only_real_sent_part(active):
    from rolebot import delivery
    r=active;m=source(r,'机器人讲两句',1)
    r.group.update(segmented_reply=True,segment_threshold=40,segment_max_parts=3)
    r.db.put('group',SCOPE,r.group)
    def current():return not r.bot.sent
    result=await delivery.deliver(r.engine,SCOPE,'default',m,[m],{},'甲'*45+'。\n\n'+'乙'*45+'。',{},[],{},current,False)
    assert result=='partial' and len(r.bot.sent)==1
    assert r.db.all('learning_session')[0]['source_ids']==sorted([m['id'],SCOPE+':501'])


def test_new_settings_validate_and_status_has_no_side_effect(authed_client):
    c=authed_client
    cfg=c.get('/api/v1/settings').json()
    assert cfg['working_initial_messages']==40 and cfg['working_max_messages']==80
    assert cfg['learning_idle_seconds']==600 and cfg['learning_max_seconds']==7200
    for body in [{'working_initial_messages':81},{'working_max_messages':0},{'working_context_budget':True},
                 {'learning_idle_seconds':8000},{'learning_max_seconds':False}]:
        assert c.put('/api/v1/settings',json=body).status_code==422
    assert c.put('/api/v1/settings',json={'working_initial_messages':30,'learning_idle_seconds':900}).status_code==200
    c.app.state.db.put('group',SCOPE,{'id':SCOPE,'persona':'default'})
    assert c.get('/api/v1/groups/'+SCOPE+'/learning-status').json()['pending_messages']==0
    assert not c.app.state.db.all('learning_session')
