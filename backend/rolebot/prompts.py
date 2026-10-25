"""Versioned, literal-only prompt templates. A trace pins one release snapshot."""
import copy
import hashlib
import json
import re
import time
from contextvars import ContextVar

from .prompt_defaults import DEFAULTS

active_snapshot = ContextVar("prompt_snapshot", default=None)
VARIABLE = re.compile(r"\{\{\s*([a-zA-Z][a-zA-Z0-9_]*)\s*\}\}")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def object_id(scope, key):
    return digest([scope, key])


def snapshot(db, scope):
    previous = active_snapshot.get()
    if previous and previous["db"] == id(db) and previous["scope"] == scope:
        return previous
    overrides = {(p["scope"], p["key"]): p for p in db.all("prompt_release")}
    entries = {}
    for key, spec in DEFAULTS.items():
        release = overrides.get((scope, key)) or overrides.get(("", key))
        entries[key] = copy.deepcopy(release or {"key":key,"scope":"","body":spec["body"],"version":0,"builtin":True})
        entries[key]["sha256"] = digest(entries[key]["body"])
    group = db.get("group", scope, {})
    return {"db":id(db),"scope":scope,"entries":entries,"group":group,
            "personas":{p["id"]:p for p in db.all("persona")}}


def note(db, entry):
    from .tracing import active_trace
    trace_id = active_trace.get()
    trace = db.get("trace", trace_id) if trace_id else None
    if trace and not trace.get("redacted"):
        if hasattr(db,"scrub_log"):entry=db.scrub_log(entry)
        used = trace.setdefault("prompts", [])
        if not any(x["key"] == entry["key"] and x["sha256"] == entry["sha256"] for x in used):
            used.append(entry)
            db.put("trace", trace_id, trace)


def validate(key, body):
    if key not in DEFAULTS:
        raise ValueError("提示词不存在")
    if not isinstance(body, str) or not body.strip() or len(body) > 64000:
        raise ValueError("提示词须为 1 至 64000 字")
    required = set(VARIABLE.findall(DEFAULTS[key]["body"]))
    supplied = set(VARIABLE.findall(body))
    if supplied != required or "{{" in VARIABLE.sub("", body):
        raise ValueError("模板变量必须保持为："+(", ".join(sorted(required)) or "无变量"))
    return body


def render_body(key, body, values):
    validate(key, body)
    if set(values) != set(VARIABLE.findall(body)):
        raise ValueError("预览或渲染所需变量不完整")
    return VARIABLE.sub(lambda m: str(values[m.group(1)]), body)


def render(db, scope, key, **values):
    entry = snapshot(db, scope)["entries"][key]
    result = render_body(key, entry["body"], values)
    note(db, {**entry, "rendered_sha256":digest(result), "rendered_chars":len(result)})
    return result


def persona(db, scope, persona_id):
    card = copy.deepcopy(snapshot(db, scope)["personas"].get(persona_id, {}))
    body = card.get("prompt", "")
    note(db, {"key":"persona:"+persona_id,"scope":scope,"version":card.get("version",0),"body":body,"sha256":digest(body),"source":"角色书"})
    return card


def guidance(db, scope):
    group = snapshot(db, scope)["group"]
    body = group.get("guidance", "")
    note(db, {"key":"group:guidance","scope":scope,"version":group.get("revision",0),"body":body,"sha256":digest(body),"source":"群与会话"})
    return body


class PromptStore:
    def __init__(self, db):
        self.db = db

    def catalog(self, scope=""):
        current = snapshot(self.db, scope)["entries"]
        return [{"key":key, "title":spec["title"], "category":spec["category"], "variables":sorted(set(VARIABLE.findall(spec["body"]))),
                 "default_body":spec["body"], "effective":current[key], "draft":self.db.get("prompt_draft",object_id(scope,key)),
                 "local_version":self.db.get("prompt_release",object_id(scope,key),{}).get("version",0),
                 "usage": "当前模板", "active_for_scope": True} for key,spec in DEFAULTS.items()]

    def draft(self, scope, key, body, base_version):
        validate(key, body)
        with self.db.transaction():
            current = self.db.get("prompt_release",object_id(scope,key),{}).get("version",0)
            if base_version != current:
                raise ValueError("已发布版本有变化，请刷新后再编辑")
            value={"key":key,"scope":scope,"body":body,"base_version":base_version,"sha256":digest(body),"ts":time.time()}
            self.db.put("prompt_draft",object_id(scope,key),value)
            return value

    def publish(self, scope, key, base_version, body=None, reason="publish", draft_hash=None):
        with self.db.transaction():
            if body is None:
                draft=self.db.get("prompt_draft",object_id(scope,key))
                if not draft or draft["base_version"] != base_version:
                    raise ValueError("草稿不存在或版本已变化")
                if draft_hash is not None and draft.get("sha256") != draft_hash:
                    raise ValueError("草稿已在别处修改，请重新检查后发布")
                body=draft["body"]
            validate(key,body)
            old=self.db.get("prompt_release",object_id(scope,key),{})
            if old.get("version",0) != base_version:
                raise ValueError("已发布版本有变化，请刷新")
            value={"key":key,"scope":scope,"body":body,"version":base_version+1,"ts":time.time(),"reason":reason}
            self.db.put("prompt_release",object_id(scope,key),value)
            self.db.put("prompt_version",object_id(scope,key)+":"+str(value["version"]),value)
            self.db.delete("prompt_draft",object_id(scope,key))
            self.db.audit(scope,"prompt_publish",{"key":key,"version":value["version"],"reason":reason})
            return value

    def versions(self, scope, key):
        rows=[x for x in self.db.all("prompt_version") if x["scope"]==scope and x["key"]==key]
        return sorted(rows,key=lambda x:x["version"],reverse=True)

    def restore(self, scope, key, version, base_version):
        if version==0:
            body=DEFAULTS[key]["body"]
        else:
            value=self.db.get("prompt_version",object_id(scope,key)+":"+str(version))
            if not value:raise ValueError("历史版本不存在")
            body=value["body"]
        return self.publish(scope,key,base_version,body,"restore:"+str(version))
