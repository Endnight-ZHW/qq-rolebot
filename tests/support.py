import json
from rolebot.onebot import ActionUnknown, normalize
from rolebot.providers import ModelConfigWrite


"""Shared synthetic QQ messages and model/OneBot fakes; no network side effects."""

SCOPE='10000001:g:20000001'
OTHER='10000001:g:20000002'
USER='30000001'
SECOND='30000002'
class FakeBot:
    self_id = '10000001'
    online = True
    def __init__(self): self.calls=[];self.sent=[];self.unknown=False
    async def call(self, action, params=None, mutation=False, **kwargs):
        self.calls.append((action,params,mutation))
        if action == 'get_group_member_info': return {'role':'admin' if params['user_id']==self.self_id else 'member','user_id':params['user_id']}
        if mutation and self.unknown: raise ActionUnknown('unknown')
        return {'message_id':999}
    async def send(self,scope,message):
        self.sent.append((scope,message))
        if self.unknown: raise ActionUnknown('unknown')
        return {'message_id':str(len(self.sent)+500)}

class FakeModel:
    configured=True
    def __init__(self,script=None): self.script=script or [];self.calls=[]
    async def chat(self,scope,purpose,messages,**kwargs):
        self.calls.append((scope,purpose,json.loads(json.dumps(messages)),kwargs))
        if self.script: return self.script.pop(0)
        return {'role':'assistant','content':'{"speak":false,"text":"","needs_context":false,"action_needed":false}'}

def event(message='你好', id=1, scope=SCOPE, user=USER):
    self_id,_,gid=scope.split(':')
    return {'post_type':'message','self_id':self_id,'message_type':'group','group_id':gid,'user_id':user,'message_id':id,
            'message':[{'type':'text','data':{'text':message}}],'sender':{'nickname':'朋友'}}

def message(runtime,text='机器人你好',id=1):
    m=normalize(event(text,id));m.update(persona='default',remember=True)
    runtime.db.add_message(m['id'],SCOPE,'default',USER,'user',text,{'message_id':str(id),'name':'朋友'})
    return m

def grant_management(runtime):
    from rolebot.permissions import AccessConfig
    value=AccessConfig(members={USER:{'allow':['tool:member_mute']}}).model_dump()
    runtime.db.put('qq_access',SCOPE,value)

def auth(client):
    r=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-123456'})
    assert r.status_code==200
    return {'x-csrf-token':r.json()['csrf']}
def source(r, text, id, actor=USER, fragment="first", ts=None, replies=None):
    m = normalize(event(text, id, user=actor))
    m.update(persona="default", remember=True, role="user")
    m["data"] = {"name": "朋友", "message_id": str(id), "plain_text": text, "fragment_id": fragment, "replies": replies or [], "types": ["text"]}
    if ts is not None: m["ts"] = ts
    r.db.add_message(m["id"], SCOPE, "default", actor, "user", text, m["data"], ts=m["ts"])
    r.db.put("member", SCOPE+":"+actor, {"id": SCOPE+":"+actor, "scope": SCOPE, "user_id": actor, "name": "朋友", "remember": True})
    return m

def fact(m, text, subjects=None, **extra):
    return {"text": text, "type": "fact", "fact_key": "favorite_fruit", "subject_ids": subjects or [m["user_id"]],
            "source_ids": [m["id"]], "confidence": .9, "evidence": [{"source_id": m["id"], "quote": m["text"]}], **extra}

def reply_decision(m, text="你好。", **extra):
    return {"role": "assistant", "content": json.dumps({"speak": True, "text": text, "reply_to_message_id": m["id"],
        "mode": "direct", "continues_dialogue": False, "expects_followup": False, "reason": "正在与机器人说话", **extra})}

def participation(speak=True, **extra):
    return {"role": "assistant", "content": json.dumps({
        "speak": speak, "needs_context": False, "action_needed": False, **extra})}


def tool_reply(name, arguments, *, id, content=''):
    """One native model tool proposal; callers keep the case-specific arguments."""
    return {'role':'assistant','content':content,'tool_calls':[{
        'id':id,'type':'function','function':{'name':name,'arguments':json.dumps(arguments)}}]}

def configure_model(model, protocol="anthropic-messages", effort="high"):
    cfg = model.catalog.get()
    cfg["providers"][0].update(protocol=protocol, base_url="https://api.deepseek.com/anthropic/v1" if protocol == "anthropic-messages" else "https://api.deepseek.com/v1")
    cfg["routes"]["decision"]["reasoning_effort"] = effort
    model.catalog.save(ModelConfigWrite(**cfg))


def image_blocks(messages):
    return [block for message in messages
            for block in (message.get('content') if isinstance(message.get('content'), list) else [])
            if block.get('type') == 'image_url']
