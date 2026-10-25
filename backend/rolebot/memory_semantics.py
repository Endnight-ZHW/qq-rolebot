from . import prompts
"""Attribution and temporal meaning accompany memory text at every read boundary."""
import re
import math
from .persona_names import pattern as name_pattern


CLAIMS = {"fact", "preference", "wish", "expression", "event", "story"}
TIME_SCOPES = {"ongoing", "episode", "unknown"}


def person(db, scope, qq, persona=""):
    card = db.get("persona", persona, {}) if persona else {}
    if qq == scope.split(":")[0]:
        return {"qq": qq, "name": card.get("name", "机器人"), "persona": persona}
    member = db.get("member", scope+":"+qq, {})
    return {"qq": qq, "name": member.get("name") or card.get("relationships", {}).get(qq) or qq}


def extraction_context(db, scope, persona, sources):
    card = db.get("persona", persona, {})
    return {"current_role": {"id": persona, "name": card.get("name", persona), "bot_qq": scope.split(":")[0], "aliases": card.get("aliases", [])},
            "members": [person(db, scope, qq, persona) for qq in sorted({m["user_id"] for m in sources})]}


def normalize_claim(db, scope, persona, item, text, kind, subjects, declared, evidence, sources):
    """Conservative corrections for role expressions, with exact source wording."""
    card = db.get("persona", persona, {})
    bot = scope.split(":")[0]
    aliases = [x for x in [card.get("name", ""), *card.get("aliases", [])] if isinstance(x, str) and len(x) >= 2]
    objects_raw = item.get("object_ids", [])
    if not isinstance(objects_raw, list) or any(not isinstance(x, str) for x in objects_raw):
        return None
    objects = set(objects_raw)
    object_persona = item.get("object_persona", "")
    # JSON null is the provider's spelling of an absent optional relationship,
    # not an unknown or cross-persona identity. Do not drop otherwise valid facts.
    if object_persona is None:
        object_persona = ""
    if object_persona not in ("", persona):
        return None
    claim = item.get("claim_kind", "story" if kind == "story" else "event" if kind == "relation" else "preference" if re.search(r"喜欢|偏好|爱好", text) else "fact")
    temporal = item.get("temporal_scope", "episode" if kind in ("relation", "story") else "unknown")
    if claim not in CLAIMS or temporal not in TIME_SCOPES:
        return None
    # Classify the cited claim, not every other clause in its source messages.
    joined = "\n".join(e["quote"] for e in evidence)
    addressed = name_pattern(db, persona, extra=('你',))
    category = r'(?:角色|机器人|助手).{0,10}(?:题材|类型|设定|动漫|形象)'
    disclaimer = r'不是(?:特指|指)' + addressed
    generic = bool(re.search(category + '|' + disclaimer, joined))
    # An explicit disclaimer about the addressed bot overrides a model's
    # inconsistent role/object tags. Keep the exact preference statement.
    generic_quote = next((e["quote"] for e in evidence if re.search(r"喜欢|爱好|偏好", e["quote"])
                          and re.search(category, e["quote"]) and generic
                          and re.search(disclaimer, joined)), None)
    if generic_quote and len(declared) == 1 and subjects == declared and objects <= {bot}:
        kind, claim, object_persona = "fact", "preference", ""
        temporal = "ongoing" if temporal == "ongoing" else "unknown"
        objects.clear()
        text = f"{person(db, scope, next(iter(declared)), persona)['name']}曾表示题材偏好：“{generic_quote}”"
    role_named = any(a in text for a in aliases)
    role_target = bool(persona and object_persona == persona) or bot in objects or bot in subjects
    if not generic and role_named and re.search(r"喜欢|爱|希望|想让|专一|一心一意|关系|陪伴|陪着|一起", text+"\n"+joined):
        role_target = True
    role_claim = kind in ("relation", "story") or role_target
    if role_claim:
        kind = "story" if kind == "story" else "relation"
        object_persona = persona if role_target else ""
        if role_target and kind != "story" and bot not in subjects:
            objects.add(bot)
        # Sources, not the model's paraphrase, decide whether this is a wish.
        wish = re.search(r"希望|想让|愿望|想要.{0,12}" + addressed, joined)
        expression = re.search(r"喜欢|爱你|讨厌|想念|在乎", joined)
        if role_target and kind != "story" and not generic and (wish or expression):
            claim, temporal = ("wish" if wish else "expression"), "episode"
            if len(declared) == 1 and declared <= subjects <= declared | {bot}:
                subjects = set(declared)
                # Preserve literal evidence, without adding 'exclusive', mutual
                # consent, an accepted identity or a permanently held preference.
                actor = next(iter(declared))
                snippets = [e["quote"] for e in evidence if e["actor"] == actor]
                if snippets:
                    label = person(db, scope, actor, persona)["name"]
                    quote = snippets[0]
                    text = f"{label}曾在与{card.get('name', persona)}互动时表达{'愿望' if wish else '感受'}：“{quote}”"
        elif claim in ("wish", "expression", "event", "story"):
            temporal = "episode"
    if claim in ("wish", "expression"):
        temporal = "episode"
    # A standalone first-person paraphrase must remain tied to its real speaker.
    if len(subjects) == 1 and subjects <= declared:
        label = person(db, scope, next(iter(subjects)), persona)["name"]
        text = re.sub(r"^(?:本人|我(?=表示|希望|喜欢|偏好|想|叫|是|住|不喜欢))", lambda _: label, text, count=1)
    times = [m["data"].get("sent_at", m["ts"]) for m in sources]
    times = [t for t in times if isinstance(t, (int, float)) and math.isfinite(t) and t > 0]
    return text, kind, subjects, {"claim_kind":claim, "temporal_scope":temporal, "object_ids":sorted(objects),
                                 "object_persona":object_persona, "occurred_at":min(times) if times else None,
                                 "last_observed_at":max(times) if times else None}


def view(db, memory, *, compact=False):
    d, scope, persona = memory["data"], memory["scope"], memory["persona"]
    object_persona = d.get("object_persona") or persona
    result = {"id":memory["id"], "text":memory["text"], "persona":persona, "type":d.get("type", "fact"),
            "subject_ids":d.get("subject_ids", []), "declarant_ids":d.get("declarant_ids", []), "object_ids":d.get("object_ids", []),
            "subjects":[person(db,scope,q,persona) for q in d.get("subject_ids",[])],
            "declarants":[person(db,scope,q,persona) for q in d.get("declarant_ids",[])],
            "objects":[person(db,scope,q,object_persona) for q in d.get("object_ids",[])], "object_persona":d.get("object_persona", ""),
            "claim_kind":d.get("claim_kind", "story" if d.get("type")=="story" else "unknown"),
            "temporal_scope":d.get("temporal_scope", "unknown"), "occurred_at":d.get("occurred_at"),
            "last_observed_at":d.get("last_observed_at"), "evidence_kind":d.get("evidence_kind", "legacy"),
            "status":d.get("status", "active"), "source_ids":d.get("source_ids", []),
            "context_source_ids":d.get("context_source_ids", []),
            "stale":memory.get("retrieval", {}).get("stale", False)}
    if not compact:
        result["interpretation"] = prompts.render(db, scope, 'memory.expression_notice') if d.get("claim_kind") in ("wish","expression") else prompts.render(db, scope, 'memory.evidence_notice')
    return result
