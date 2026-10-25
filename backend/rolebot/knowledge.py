"""Versioned, source-bound facts and independently managed group expressions."""
import hashlib
import re
import time
import math

from .db import uid
from .memory_semantics import normalize_claim, view as memory_view
from . import tracing


def canonical(text):
    return re.sub(r"[\s，。！？、,.!?；;：:]+", "", text).lower()


def strings(value):
    return set(value) if isinstance(value, list) and all(isinstance(x, str) for x in value) else set()


def is_correction(text):
    return bool(re.search(r"纠正|记错|改成|不再|更喜欢|现在.*(?:喜欢|叫|是)|不是.{0,30}(?:是|而是)|以前.{0,50}现在", text))


def is_explicit(text):
    return bool(re.search(r"(?:请|帮我|要)?记住|记一下|记下来|纠正|记错|不再|改成|更喜欢", text))


class Knowledge:
    def __init__(self, db):
        self.db = db

    def allowed(self, scope, users):
        return all(self.db.get("member", scope+":"+u, {}).get("remember", True) for u in users)

    def row(self, scope, id):
        return next((m for m in self.db.memories(scope) if m["id"] == id), None)

    def versions(self, scope, id):
        current = self.row(scope, id)
        if not current:
            return []
        rows = [v for v in self.db.all("memory_version") if v["scope"] == scope and v["memory_id"] == id]
        version = current["data"].get("version", 1)
        if not any(v["version"] == version for v in rows):
            rows.append({**current, "memory_id": id, "id": id+":"+str(version), "version": version, "reason": "initial"})
        return sorted(rows, key=lambda v: v["version"], reverse=True)

    def _snapshot(self, row, reason):
        version = row["data"].get("version", 1)
        id = row["id"]+":"+str(version)
        if not self.db.get("memory_version", id):
            self.db.put("memory_version", id, {"id": id, "memory_id": row["id"], "scope": row["scope"], "persona": row["persona"], "text": row["text"], "data": row["data"], "version": version, "ts": row["ts"], "reason": reason})

    def write(self, scope, persona, text, data, id=None, reason="extracted"):
        if not self.allowed(scope, data.get("subject_ids", []) + data.get("declarant_ids", []) + data.get("object_ids", [])):
            raise ValueError("成员已停止记忆")
        with self.db.transaction():
            old = self.row(scope, id) if id else None
            if old:
                self._snapshot(old, "previous")
            metadata = {**data, "version": old["data"].get("version", 1)+1 if old else 1,
                        "created_at": old["data"].get("created_at", old["ts"]) if old else time.time(), "updated_at": time.time()}
            metadata.setdefault("status", "active")
            trace_id=tracing.active_trace.get()
            trace=self.db.get("trace",trace_id,{}) if trace_id else {}
            metadata["formation"]={"trace_id":trace_id,"call_id":trace.get("last_call_id"),"reason":reason}
            id = self.db.save_memory(scope, persona, text, metadata, id=id)
            # Scope/persona are immutable at the raw storage layer; explicit admin edits can move the projection.
            self.db.execute("UPDATE memories SET persona=? WHERE id=? AND scope=?", (persona, id, scope))
            self._snapshot(self.row(scope, id), reason)
            tracing.stage(self.db,"memory_write",{"id":id,"reason":reason,"previous":old,"saved":self.row(scope,id)})
            if old and (old["text"] != text or old["persona"] != persona or any(old["data"].get(k) != metadata.get(k) for k in ("status", "subject_ids", "declarant_ids", "object_ids", "object_persona", "claim_kind", "temporal_scope"))):
                self.db.clear_derived(scope)
        return id

    def manual(self, scope, persona, text, subjects, pinned, id=None):
        old = self.row(scope, id) if id else None
        data = {**(old["data"] if old else {}), "type": "manual", "evidence_kind": "manual", "subject_ids": subjects,
                "pinned": pinned, "confidence": 1, "status": "active", "source_ids": [], "declarant_ids": [], "evidence": [],
                "claim_kind": "fact", "temporal_scope": "unknown", "object_ids": [], "object_persona": "", "occurred_at": None, "last_observed_at": None}
        return self.write(scope, persona, text, data, id=id, reason="administrator")

    def set_status(self, scope, id, status):
        row = self.row(scope, id)
        if not row:
            raise ValueError("记忆不存在")
        self.write(scope, row["persona"], row["text"], {**row["data"], "status": status}, id, "status")

    def restore(self, scope, id, version):
        row = self.row(scope, id)
        previous = self.db.get("memory_version", id+":"+str(version))
        if not row or not previous or previous["scope"] != scope:
            raise ValueError("版本不存在或已被遗忘")
        return self.write(scope, previous["persona"], previous["text"], {**previous["data"], "status": "active"}, id, "restore")

    def resolve(self, scope, id):
        selected = self.row(scope, id)
        if not selected:
            raise ValueError("记忆不存在")
        conflict = selected["data"].get("conflict_group")
        with self.db.transaction():
            if conflict:
                for row in self.db.memories(scope):
                    if row["id"] != id and row["data"].get("conflict_group") == conflict:
                        self.set_status(scope, row["id"], "archived")
            self.write(scope, selected["persona"], selected["text"], {**selected["data"], "status": "active", "evidence_kind": "manual"}, id, "resolve")

    def apply(self, scope, persona, items, messages, context_messages=()):
        sources = {m["id"]: m for m in messages if m["role"] == "user" and m.get("scope") == scope and m.get("persona") == persona and self.allowed(scope, [m["user_id"]])}
        known_users = {scope.split(":")[0]} | {m["user_id"] for m in sources.values()} | {m["user_id"] for m in self.db.all("member") if m.get("scope") == scope}
        changed = []
        context_ids = {m["id"] for m in context_messages if m.get("scope") == scope and m.get("persona") == persona and self.allowed(scope, [m["user_id"]])}
        for index,item in enumerate(items[:12]):
            tracing.stage(self.db,"memory_candidate",{"index":index,"proposal":item})
            def reject(reason):
                tracing.stage(self.db,"memory_candidate_result",{"index":index,"status":"skipped","reason":reason})
            if not isinstance(item, dict):
                reject("候选不是对象")
                continue
            text = item.get("text", "")
            if not isinstance(text, str):
                reject("记忆文本不是字符串")
                continue
            text = text.strip()[:1000]
            source_ids = strings(item.get("source_ids"))
            subjects = strings(item.get("subject_ids"))
            kind = item.get("type", "fact")
            if not text or not isinstance(item.get("subject_ids"), list) or any(not isinstance(x,str) for x in item["subject_ids"]) or not source_ids or not source_ids <= sources.keys() or not subjects <= known_users or kind not in ("fact", "relation", "story"):
                reject("文本、主体、来源或类型未通过校验")
                continue
            if not self.allowed(scope, subjects):
                reject("主体已停止记忆")
                continue
            declared = {sources[s]["user_id"] for s in source_ids}
            evidence = []
            for part in item.get("evidence", []) if isinstance(item.get("evidence", []), list) else []:
                if isinstance(part, dict) and part.get("source_id") in source_ids:
                    quote = str(part.get("quote", ""))[:500]
                    if len(quote) >= 2 and quote in sources[part["source_id"]]["text"]:
                        evidence.append({"source_id": part["source_id"], "quote": quote, "actor": sources[part["source_id"]]["user_id"]})
            if not evidence or {e["source_id"] for e in evidence} != source_ids:
                reject("增量记忆缺少逐条精确证据")
                continue
            quoted = "\n".join(e["quote"] for e in evidence)
            # "改成" also appears in one-off tool commands; it is not a
            # request to remember a preference or relationship.
            explicit = any(re.search(r"记住|记一下|记下来|纠正|记错", sources[id]["text"]) for id in source_ids)
            plain = re.sub(r"\[@QQ:\d+\]|[\s（）()！!。~～]", "", quoted)
            from .persona_names import pattern as name_pattern
            addressed = name_pattern(self.db, persona, extra=('我',))
            routine = bool(re.fullmatch(r"(?:你好呀?|早上?好|晚上好|晚安|抱抱"+addressed+r"?|抱住"+addressed+r"|摸摸"+addressed+r"?(?:脑袋|头)?|拍拍"+addressed+r"?(?:脑袋|头)?)", plain))
            parameter = bool(re.fullmatch(r".{1,24}(?:就行|就可以)(?:呀|吧|了)?", plain))
            command = bool(re.search(r"(?:改|修改|设置|换).{0,12}(?:名片|昵称|群称号|群名称)|(?:名片|昵称|群称号|群名称).{0,12}(?:改成|换成|设为)", quoted))
            if not explicit and (routine or parameter or command):
                reject("普通互动或临时操作参数不形成长期记忆")
                continue
            semantic = normalize_claim(self.db, scope, persona, item, text, kind, subjects, declared, evidence, [sources[s] for s in sorted(source_ids)])
            if semantic is None:
                reject("语义字段未通过校验")
                continue
            text, kind, subjects, attributes = semantic
            if not set(attributes["object_ids"]) <= known_users or not self.allowed(scope, attributes["object_ids"]):
                reject("对象不存在或已停止记忆")
                continue
            # Evidence verifies who said it; an expression is not a mutual commitment.
            evidence_actors = {e["actor"] for e in evidence}
            evidence_kind = "self_report" if subjects <= evidence_actors and evidence else "third_party"
            joined = "\n".join(e["quote"] for e in evidence)
            if item.get("evidence_kind") == "inference" or re.search(r"猜测|也许|可能是|我猜|据说|听说|他说|她说|转述|开玩笑|逗你|假设|假如", joined):
                evidence_kind = "inference"
            if kind == "story":
                evidence_kind = "story"
            try:
                confidence = float(item.get("confidence", .6))
                if not math.isfinite(confidence):
                    reject("提取分数非有限数值")
                    continue
                confidence = min(1, max(0, confidence))
            except (ValueError, TypeError):
                reject("提取分数格式错误")
                continue
            if confidence < .6:
                reject("未达到提取分数门槛")
                continue
            target_persona = "" if kind == "fact" else persona
            key = str(item.get("fact_key", ""))[:100].strip()
            rows = [r for r in self.db.memories(scope) if r["persona"] == target_persona and set(r["data"].get("subject_ids", [])) == subjects and r["data"].get("status") != "archived"]
            duplicate = next((r for r in rows if canonical(r["text"]) == canonical(text)), None)
            # Auxiliary sources can explain a current reference, never originate
            # a candidate; retain dependencies so forgetting them removes it too.
            data = {**attributes, "type": kind, "fact_key": key, "subject_ids": sorted(subjects), "source_ids": sorted(source_ids | context_ids),
                    "context_source_ids":sorted(context_ids-source_ids),
                    "declarant_ids": sorted(declared), "evidence": evidence, "evidence_kind": evidence_kind, "confidence": confidence,
                    "pinned": False, "status": "active" if evidence_kind in ("self_report", "story") else "unverified"}
            tracing.stage(self.db,"memory_normalized",{"index":index,"text":text,"persona":target_persona,"data":data})
            if duplicate:
                old = duplicate["data"]
                merged_sources = sorted(source_ids | context_ids | set(old.get("source_ids", [])))
                if merged_sources == old.get("source_ids", []):
                    reject("同一来源的重复提取，未另建版本")
                    continue
                observed = [t for t in (old.get("occurred_at"), attributes.get("occurred_at")) if t is not None]
                data = {**old, "source_ids": merged_sources, "occurred_at": min(observed) if observed else None,
                        "context_source_ids":sorted(context_ids | set(old.get("context_source_ids", []))),
                        "last_observed_at": max(old.get("last_observed_at") or 0, attributes.get("last_observed_at") or 0) or None,
                        "declarant_ids": sorted(declared | set(old.get("declarant_ids", []))),
                        "evidence": list({(e["source_id"], e["quote"]): e for e in old.get("evidence", [])+evidence}.values())}
                changed.append(self.write(scope, target_persona, duplicate["text"], data, duplicate["id"], "merge_sources"))
                continue
            same_slot = [r for r in rows if key and r["data"].get("fact_key") == key and attributes["temporal_scope"] != "episode" and r["data"].get("temporal_scope") != "episode"]
            proposed = item.get("supersedes_ids", [])
            predecessors = [r for r in rows if r["id"] in proposed] if isinstance(proposed, list) else []
            if not predecessors:
                predecessors = same_slot
            correction = evidence_kind == "self_report" and is_correction(joined) and len(predecessors) == 1 and predecessors[0]["data"].get("evidence_kind") != "manual" and (bool(subjects) or declared == set(predecessors[0]["data"].get("declarant_ids", [])))
            if correction:
                old = predecessors[0]
                data["pinned"] = old["data"].get("pinned", False)
                data["previous_source_ids"] = old["data"].get("source_ids", [])
                changed.append(self.write(scope, target_persona, text, data, old["id"], "self_correction"))
            else:
                if predecessors:
                    conflict = predecessors[0]["data"].get("conflict_group") or uid()
                    data.update(status="conflict", conflict_group=conflict)
                    for old in predecessors:
                        if old["data"].get("status") != "conflict":
                            self.write(scope, old["persona"], old["text"], {**old["data"], "status": "conflict", "conflict_group": conflict}, old["id"], "conflict")
                changed.append(self.write(scope, target_persona, text, data))
        return changed

    def profiles(self, scope, persona):
        rows = self.db.memories(scope, persona)
        members = [m for m in self.db.all("member") if m.get("scope") == scope]
        result = []
        for member in members:
            active = [m for m in rows if member["user_id"] in m["data"].get("subject_ids", []) and m["data"].get("status", "active") != "archived"] if member.get("remember", True) else []
            confirmed = [m for m in active if m["data"].get("status", "active") == "active" and m["data"].get("evidence_kind") in ("manual", "self_report", "story")]
            result.append({**member, "facts": [{**m, "interpretation": memory_view(self.db, m)} for m in confirmed],
                           "uncertain": [{**m, "interpretation": memory_view(self.db, m)} for m in active if m not in confirmed]})
        return result

    def styles(self, scope, active_only=False):
        return [s for s in self.db.all("learned_style") if s["scope"] == scope and (not active_only or s.get("enabled"))]

    def save_style(self, style, reason):
        old = self.db.get("learned_style", style["id"])
        value = {**style, "version": (old or {}).get("version", 0)+1, "updated_at": time.time()}
        self.db.put("learned_style", value["id"], value)
        snapshot = {**value, "style_id": value["id"], "id": value["id"]+":"+str(value["version"]), "reason": reason}
        self.db.put("style_version", snapshot["id"], snapshot)
        return value

    def learn(self, scope, proposals, messages, context_messages=()):
        eligible = {}
        for m in messages:
            data = m["data"]
            if m["role"] != "user" or not self.allowed(scope, [m["user_id"]]) or data.get("replies") or any(t in data.get("types", []) for t in ("forward", "record")):
                continue
            plain = data.get("plain_text", m["text"])
            if re.search(r"^(?:>|引用)|(?:他说|她说|转述|据说|听说)", plain):
                continue
            eligible[m["id"]] = m
        changed = []
        for item in proposals[:8]:
            if not isinstance(item, dict):
                continue
            expression = str(item.get("expression", "")).strip()[:100]
            if len(expression) < 2:
                continue
            samples = []
            for e in item.get("examples", []) if isinstance(item.get("examples", []), list) else []:
                if not isinstance(e, dict) or e.get("source_id") not in eligible:
                    continue
                m = eligible[e["source_id"]]
                quote = str(e.get("quote", ""))[:200]
                if expression not in quote or quote not in m["data"].get("plain_text", m["text"]):
                    continue
                samples.append({"source_id": m["id"], "actor": m["user_id"], "quote": quote,
                                "fragment": m["data"].get("fragment_id", "legacy:"+str(int(m["ts"])//300))})
            if not samples:
                continue
            # The model identifies an expression; count all verified occurrences locally,
            # rather than depending on how many representative examples it chose to quote.
            for m in eligible.values():
                plain = m["data"].get("plain_text", m["text"])
                at = plain.find(expression)
                if at >= 0 and not any(e["source_id"] == m["id"] for e in samples):
                    samples.append({"source_id": m["id"], "actor": m["user_id"], "quote": plain[max(0, at-40):at+len(expression)+100],
                                    "fragment": m["data"].get("fragment_id", "legacy:"+str(int(m["ts"])//300))})
            id = hashlib.sha256((scope+":"+canonical(expression)).encode()).hexdigest()[:32]
            old = self.db.get("learned_style", id, {})
            examples = list({e["source_id"]: e for e in old.get("examples", [])+samples}.values())
            sources = sorted(e["source_id"] for e in examples)
            if sources == sorted(e["source_id"] for e in old.get("examples", [])):
                continue
            valid = len(sources) >= 5 and len({e["fragment"] for e in examples}) >= 2
            style = {"id": id, "scope": scope, "expression": expression,
                     "meaning": old.get("meaning", str(item.get("meaning", ""))[:500]), "usage": old.get("usage", str(item.get("usage", ""))[:500]),
                     "examples": examples, "source_ids": sources, "actor_ids": sorted({e["actor"] for e in examples}),
                     "eligible": valid, "enabled": old.get("enabled", False) if old.get("manual") else valid,
                     "manual": old.get("manual", False), "fragments": len({e["fragment"] for e in examples})}
            # Auxiliary context never counts toward the five-example threshold.
            dependencies = set(old.get("context_source_ids", [])) | {m["id"] for m in context_messages if m.get("scope") == scope}
            style["context_source_ids"] = sorted(dependencies)
            style["source_ids"] = sorted(set(sources) | dependencies)
            changed.append(self.save_style(style, "learning")["id"])
        return changed

    def edit_style(self, scope, id, meaning, usage, enabled):
        old = self.db.get("learned_style", id)
        if not old or old["scope"] != scope:
            raise ValueError("学习记录不存在")
        if not self.allowed(scope, old["actor_ids"]):
            raise ValueError("学习来源已停止记忆")
        return self.save_style({**old, "meaning": meaning, "usage": usage, "enabled": enabled, "manual": True}, "administrator")

    def restore_style(self, scope, id, version):
        previous = self.db.get("style_version", id+":"+str(version))
        if not self.db.get("learned_style", id) or not previous or previous["scope"] != scope:
            raise ValueError("学习版本不存在或已被遗忘")
        return self.edit_style(scope, id, previous["meaning"], previous["usage"], previous["enabled"])
