"""Visible source records and bounded supporting context for fact extraction."""

def available(db, scope, persona, id):
    row = db.message(scope, id, persona)
    if row and db.get("member", scope+":"+row["user_id"], {}).get("remember", True):
        return row
    return None


def supporting_context(db, scope, persona, sources):
    """A bounded look-behind, never a second incremental extraction window."""
    if not sources:
        return []
    start = min(m["ts"] for m in sources)
    ids = {m["id"] for m in sources}
    rows = db.rows("SELECT id FROM messages WHERE scope=? AND persona=? AND excluded=0 AND ts<? AND ts>=? ORDER BY ts DESC LIMIT 12",
                   (scope, persona, start, start-600))
    referenced = [id for m in sources for id in m.get("data", {}).get("replies", [])[:3]]
    result = {}
    for id in [r["id"] for r in reversed(rows)]+referenced:
        m = available(db, scope, persona, id)
        if m and m["id"] not in ids:
            result[m["id"]] = m
    selected, chars = [], 0
    for m in sorted(result.values(), key=lambda m:m["ts"], reverse=True):
        if chars+len(m["text"]) > 8000:
            continue
        selected.append(m); chars += len(m["text"])
        if len(selected) == 20:
            break
    return list(reversed(selected))


def message_data(m):
    data = m.get("data", {})
    return {"id":m["id"], "qq":m["user_id"], "role":m["role"], "text":m["text"],
            "sent_at":data.get("sent_at", m["ts"]), "fragment":data.get("fragment_id"),
            "reply_ids":data.get("replies", [])}
