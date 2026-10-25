import contextlib
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .config import DEFAULT_PERSONA, DEFAULTS


def uid():
    return uuid.uuid4().hex


def now():
    return time.time()


def tokens(text):
    """CJK unigrams + bigrams, Latin words; no language model in RAM."""
    chunks = re.findall(r"[\u3400-\u9fff]+|[a-zA-Z0-9_]+", text.lower())
    result = []
    for chunk in chunks:
        if re.match(r"[\u3400-\u9fff]", chunk):
            result.extend(chunk)
            result.extend(chunk[i:i+2] for i in range(len(chunk)-1))
        else:
            result.append(chunk)
    return " ".join(result[:4000])


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS objects(kind TEXT,id TEXT,data TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(kind,id));
        CREATE INDEX IF NOT EXISTS objects_kind_updated ON objects(kind,updated);
        CREATE INDEX IF NOT EXISTS objects_status_updated ON objects(kind,json_extract(data,'$.status'),updated);
        CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,scope TEXT NOT NULL,persona TEXT NOT NULL,
          user_id TEXT NOT NULL,role TEXT NOT NULL,text TEXT NOT NULL,data TEXT NOT NULL,ts REAL NOT NULL,excluded INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS messages_scope ON messages(scope,persona,ts);
        CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY,scope TEXT NOT NULL,persona TEXT NOT NULL DEFAULT '',
          text TEXT NOT NULL,data TEXT NOT NULL,ts REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS memories_scope ON memories(scope,persona);
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(id UNINDEXED,content);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY,scope TEXT NOT NULL,kind TEXT NOT NULL,data TEXT NOT NULL,ts REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
        CREATE TABLE IF NOT EXISTS usage(id TEXT PRIMARY KEY,scope TEXT NOT NULL,model TEXT NOT NULL,purpose TEXT NOT NULL,
          input_tokens INTEGER,cached_tokens INTEGER,output_tokens INTEGER,cost REAL,currency TEXT,ts REAL);
        CREATE INDEX IF NOT EXISTS usage_ts_scope ON usage(ts,scope);
        CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY,ts REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS message_order(seq INTEGER PRIMARY KEY AUTOINCREMENT,message_id TEXT UNIQUE NOT NULL);
        INSERT OR IGNORE INTO message_order(message_id) SELECT id FROM messages ORDER BY ts,rowid;
        CREATE TRIGGER IF NOT EXISTS messages_order_insert AFTER INSERT ON messages BEGIN
          INSERT OR IGNORE INTO message_order(message_id) VALUES(new.id);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_order_delete AFTER DELETE ON messages BEGIN
          DELETE FROM message_order WHERE message_id=old.id;
        END;
        CREATE TABLE IF NOT EXISTS memory_processed(message_id TEXT PRIMARY KEY,ts REAL NOT NULL);
        PRAGMA user_version=2;
        """)
        self.conn.commit()
        if not self.get("settings", "global"):
            self.put("settings", "global", DEFAULTS.copy())
        if not self.all("persona"):
            self.put("persona", "default", {**DEFAULT_PERSONA, "id": "default", "version": 1})
            self.put("persona_version", "default:1", {**DEFAULT_PERSONA, "id": "default", "version": 1})
        # A process crash leaves side effects uncertain; never replay them automatically.
        for job in self.all("reminder"):
            if job.get("status") == "sending":
                job["status"] = "unknown"
                self.put("reminder", job["id"], job)

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            nested = self.conn.in_transaction
            savepoint = "s" + uid()
            self.conn.execute("SAVEPOINT " + savepoint if nested else "BEGIN IMMEDIATE")
            try:
                yield self.conn
                if nested:
                    self.conn.execute("RELEASE SAVEPOINT " + savepoint)
                else:
                    self.conn.commit()
            except Exception:
                if nested:
                    self.conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
                    self.conn.execute("RELEASE SAVEPOINT " + savepoint)
                else:
                    self.conn.rollback()
                raise

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.transaction() as c:
            return c.execute(sql, args).rowcount

    def get(self, kind, id, default=None):
        rows = self.rows("SELECT data FROM objects WHERE kind=? AND id=?", (kind, str(id)))
        return json.loads(rows[0]["data"]) if rows else default

    def all(self, kind):
        return [json.loads(x["data"]) for x in self.rows("SELECT data FROM objects WHERE kind=? ORDER BY updated DESC", (kind,))]

    def scoped_objects(self, kind, scope=None, statuses=(), limit=100, oldest=False):
        sql, args = "SELECT data FROM objects WHERE kind=?", [kind]
        if scope is not None:
            sql += " AND json_extract(data,'$.scope')=?"; args.append(scope)
        if statuses:
            sql += " AND json_extract(data,'$.status') IN ("+",".join("?" for _ in statuses)+")"; args.extend(statuses)
        sql += " ORDER BY updated " + ("ASC" if oldest else "DESC")
        if limit is not None:
            sql += " LIMIT ?"; args.append(limit)
        return [json.loads(row["data"]) for row in self.rows(sql, args)]

    def put(self, kind, id, data):
        self.execute("INSERT INTO objects VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data,updated=excluded.updated",
                     (kind, str(id), json.dumps(data, ensure_ascii=False), now()))
        return data

    def delete(self, kind, id):
        self.execute("DELETE FROM objects WHERE kind=? AND id=?", (kind, id))

    def config(self):
        return {**DEFAULTS, **self.get("settings", "global", {})}

    def mark_seen(self, event_id):
        return self.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (event_id, now())) == 1

    def audit(self, scope, kind, data, *, record_id=None):
        if hasattr(self,"scrub_log"):data=self.scrub_log(data)
        self.execute("INSERT INTO audit VALUES(?,?,?,?,?)", (record_id or uid(), scope, kind, json.dumps(data, ensure_ascii=False), now()))

    def add_message(self, id, scope, persona, user_id, role, text, data=None, ts=None, excluded=False):
        self.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?)", (
            str(id), scope, persona, str(user_id), role, text, json.dumps(data or {}, ensure_ascii=False), ts or now(), int(excluded)))

    def recent(self, scope, persona, limit=80):
        reset = self.get("context_reset", scope + ":" + persona, {}).get("ts", 0)
        rows = self.rows("SELECT * FROM messages WHERE scope=? AND persona=? AND excluded=0 AND ts>? ORDER BY ts DESC LIMIT ?", (scope, persona, reset, limit))
        for r in rows:
            r["data"] = json.loads(r["data"])
        return list(reversed(rows))

    def recent_pages(self, scope, persona, page_size=100, max_messages=2000):
        """Stable keyset pages, newest first; never load an entire retained log."""
        floor = max(self.get("context_reset", scope+":"+persona, {}).get("ts", 0), now()-self.config()["retention_days"]*86400)
        anchor, remaining = None, max_messages
        while remaining > 0:
            sql = "SELECT m.* FROM messages m WHERE m.scope=? AND m.persona=? AND m.excluded=0 AND m.ts>?"
            args = [scope, persona, floor]
            sql += " AND NOT EXISTS(SELECT 1 FROM objects o WHERE o.kind='member' AND o.id=m.scope||':'||m.user_id AND json_extract(o.data,'$.remember')=0)"
            if anchor:
                sql += " AND (m.ts<? OR (m.ts=? AND m.id<?))"; args.extend((anchor[0], anchor[0], anchor[1]))
            count = min(page_size, remaining)
            rows = self.rows(sql+" ORDER BY m.ts DESC,m.id DESC LIMIT ?", (*args, count))
            if not rows:
                break
            for row in rows:
                row["data"] = json.loads(row["data"])
            yield rows
            remaining -= len(rows)
            anchor = rows[-1]["ts"], rows[-1]["id"]
            if len(rows) < count:
                break

    def message(self, scope, id, persona=None):
        sql = "SELECT * FROM messages WHERE scope=? AND (id=? OR json_extract(data,'$.message_id')=?) AND excluded=0"
        args = [scope, id, id]
        if persona is not None:
            sql += " AND persona=?"; args.append(persona)
        sql += " AND ts>? LIMIT 1"; args.append(now()-self.config()["retention_days"]*86400)
        rows = self.rows(sql, args)
        if rows:
            rows[0]["data"] = json.loads(rows[0]["data"])
            return rows[0]

    def message_batch(self, scope, persona, after=0, limit=100, unprocessed=False):
        sql = "SELECT m.*,o.seq FROM messages m JOIN message_order o ON o.message_id=m.id WHERE m.scope=? AND m.persona=? AND m.excluded=0 AND o.seq>?"
        if unprocessed:
            sql += " AND m.role='user' AND NOT EXISTS(SELECT 1 FROM memory_processed p WHERE p.message_id=m.id)"
        rows = self.rows(sql + " ORDER BY o.seq LIMIT ?", (scope, persona, after, limit))
        for row in rows:
            row["data"] = json.loads(row["data"])
        return rows

    def patch_message_data(self, scope, id, patch):
        with self.transaction():
            row = self.message(scope, id)
            if row:
                row["data"].update(patch)
                self.execute("UPDATE messages SET data=? WHERE id=? AND scope=? AND excluded=0", (json.dumps(row["data"], ensure_ascii=False), row["id"], scope))

    def save_memory(self, scope, persona, text, data=None, id=None):
        id = id or uid()
        with self.transaction() as c:
            c.execute("INSERT INTO memories VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET text=excluded.text,data=excluded.data,ts=excluded.ts",
                      (id, scope, persona, text, json.dumps(data or {}, ensure_ascii=False), now()))
            c.execute("DELETE FROM memory_fts WHERE id=?", (id,))
            c.execute("INSERT INTO memory_fts VALUES(?,?)", (id, tokens(text)))
        return id

    def memories(self, scope, persona=None):
        sql, args = "SELECT * FROM memories WHERE scope=?", [scope]
        if persona is not None:
            sql += " AND (persona='' OR persona=?)"
            args.append(persona)
        rows = self.rows(sql + " ORDER BY ts DESC", args)
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows


    def recall(self, scope, persona, query, limit=12, subject_ids=()):
        """Retain BM25 rank and lexical coverage; identity boosts never cross scopes."""
        expanded = query
        subjects = set(subject_ids)
        for member in self.all("member"):
            if member.get("scope") != scope or not member.get("remember", True):
                continue
            names = [member.get("name", ""), *member.get("aliases", [])]
            if member["user_id"] in query or any(len(n) >= 2 and n in query for n in names):
                subjects.add(member["user_id"])
                expanded += " " + " ".join(names)
        words = list(dict.fromkeys(tokens(expanded).split()))
        meaningful = [w for w in words if len(w) >= 2 and w not in {"什么", "怎么", "哪个", "一下", "一下子", "记得", "告诉", "的是", "喜欢"}][:80]
        words = meaningful or words[:30]
        matches = {}
        if words:
            expr = " OR ".join('"' + w.replace('"', '') + '"' for w in words)
            found = self.rows("SELECT m.id,bm25(memory_fts) AS relevance FROM memory_fts JOIN memories m ON m.id=memory_fts.id WHERE memory_fts MATCH ? AND m.scope=? AND (m.persona='' OR m.persona=?) ORDER BY relevance LIMIT 150", (expr, scope, persona))
            matches = {r["id"]: (i, r["relevance"]) for i, r in enumerate(found)}
        candidate_ids = set(matches)
        candidate_ids.update(r["id"] for r in self.rows("SELECT id FROM memories WHERE scope=? AND (persona='' OR persona=?) AND json_extract(data,'$.pinned')=1 ORDER BY ts DESC LIMIT 100", (scope, persona)))
        if subjects:
            slots = ",".join("?" for _ in subjects)
            candidate_ids.update(r["id"] for r in self.rows("SELECT id FROM memories WHERE scope=? AND (persona='' OR persona=?) AND EXISTS(SELECT 1 FROM json_each(memories.data,'$.subject_ids') s WHERE s.value IN ("+slots+")) ORDER BY ts DESC LIMIT 150", (scope, persona, *sorted(subjects))))
        if not candidate_ids:
            return []
        rows = self.rows("SELECT * FROM memories WHERE scope=? AND (persona='' OR persona=?) AND id IN ("+",".join("?" for _ in candidate_ids)+")", (scope, persona, *candidate_ids))
        result = []
        for row in rows:
            row["data"] = json.loads(row["data"])
            data = row["data"]
            if data.get("status", "active") == "archived":
                continue
            identity = bool(subjects.intersection(data.get("subject_ids", [])))
            if row["id"] not in matches and not data.get("pinned") and not identity:
                continue
            coverage = sum(w in row["text"].lower() for w in words)/max(1, len(words))
            index, rank = matches.get(row["id"], (10000, 0))
            relevance = 40*coverage + (12/(1+index) if rank else 0)
            status = data.get("status", "active")
            freshness = max(0, 2-(now()-row["ts"])/86400/30)
            stale = data.get("valid_until") is not None and data["valid_until"] < now()
            score = 100*bool(data.get("pinned")) + relevance + 12*identity + 2*float(data.get("confidence", .5)) + freshness - 8*stale - 3*(status in ("conflict", "unverified"))
            row["retrieval"] = {"score": round(score, 4), "bm25": rank, "lexical_coverage": round(coverage, 4), "subject_match": identity, "stale": stale, "status": status}
            result.append(row)
        return sorted(result, key=lambda x: x["retrieval"]["score"], reverse=True)[:limit]

    def clear_derived(self, scope):
        for kind in ("summary", "summary_backup", "summary_cursor", "context_preview", "member_profile", "followup", "context_checkpoint", "compaction_retry", "working_context", "image_focus", "image_search_cache"):
            self.execute("DELETE FROM objects WHERE kind=? AND (id=? OR id LIKE ? ESCAPE '\\')", (kind, scope, scope.replace("_", "\\_").replace("%", "\\%") + ":%"))
        revision = self.get("summary_generation", scope, {}).get("value", 0)+1
        self.put("summary_generation", scope, {"value": revision})

    def invalidate_memory(self, scope, source_ids=(), user_id=None, memory_id=None):
        """Remove dependent memories and summaries; exclude original material from future extraction."""
        ids = set(source_ids)
        if user_id:
            ids.update(x["id"] for x in self.rows("SELECT id FROM messages WHERE scope=? AND user_id=?", (scope, user_id)))
        memories = self.memories(scope)
        if user_id:
            for m in memories:
                if user_id in m["data"].get("subject_ids", []) + m["data"].get("declarant_ids", []) + m["data"].get("object_ids", []):
                    ids.update(m["data"].get("source_ids", []))
        if memory_id:
            for m in memories:
                if m["id"] == memory_id:
                    ids.update(m["data"].get("source_ids", []))
        # Clear message derivatives too, so old assistant replies and reply chains cannot reintroduce forgotten content.
        messages = self.rows("SELECT id,role,data FROM messages WHERE scope=? AND excluded=0", (scope,))
        changed = True
        while changed:
            changed = False
            for message in messages:
                if message["id"] in ids:
                    continue
                metadata = json.loads(message["data"])
                dependencies = set(metadata.get("source_ids", [])) | {scope+":"+str(x) for x in metadata.get("replies", [])}
                unknown_legacy_reply = message["role"] == "assistant" and "source_ids" not in metadata and bool(ids or user_id or memory_id)
                if (unknown_legacy_reply or dependencies.intersection(ids) or (user_id and user_id in metadata.get("subject_ids", []) + metadata.get("object_ids", []) + metadata.get("declarant_ids", [])) or (memory_id and memory_id in metadata.get("memory_ids", []))):
                    ids.add(message["id"]); changed = True
        with self.transaction() as c:
            from .call_records import Records
            Records(self).redact(scope=scope)
            for trace in self.all('trace'):
                if any(d.get('scope') == scope and ids.intersection(d.get('source_ids', []))
                       for d in trace.get('image_dependencies', [])):
                    Records(self).redact(trace_id=trace['id'])
                    trace.update(context=None, stages=[], prompts=[], image_dependencies=[], redacted=True,
                                 notice='引用图片来源已遗忘，已清理相关追踪内容')
                    self.put('trace', trace['id'], trace)
            for entry in self.rows("SELECT id,kind,data FROM audit WHERE scope=? AND kind IN ('tool','decision','error')",(scope,)):
                data=json.loads(entry["data"])
                cleaned={k:v for k,v in data.items() if k in ("name","status","error_code","mode","event_id","actor","trigger_actor","stage","type")}
                cleaned["notice"]="遗忘后已清理日志内容"
                c.execute("UPDATE audit SET data=? WHERE id=?",(json.dumps(cleaned,ensure_ascii=False),entry["id"]))
            for id in ids:
                c.execute("UPDATE messages SET excluded=1,text='',data='{}' WHERE scope=? AND id=?", (scope, id))
            for m in memories:
                if m["id"] == memory_id or ids.intersection(m["data"].get("source_ids", [])) or (user_id and user_id in m["data"].get("subject_ids", []) + m["data"].get("declarant_ids", []) + m["data"].get("object_ids", [])):
                    c.execute("DELETE FROM memories WHERE id=?", (m["id"],))
                    c.execute("DELETE FROM memory_fts WHERE id=?", (m["id"],))
            for kind in ["summary", "context_preview"]:
                c.execute("DELETE FROM objects WHERE kind=? AND id LIKE ? ESCAPE '\\'", (kind, scope.replace("_", "\\_").replace("%", "\\%") + ":%"))
            # Versions and learned material are derived memory too. Never restore forgotten text.
            removed_memory_ids = {m["id"] for m in memories if m["id"] == memory_id or ids.intersection(m["data"].get("source_ids", [])) or (user_id and user_id in m["data"].get("subject_ids", []) + m["data"].get("declarant_ids", []) + m["data"].get("object_ids", []))}
            removed_styles = set()
            for kind in ('sticker', 'sticker_checked', 'sticker_job', 'reminder', 'task_run', 'attachment', 'persona_image'):
                for item in self.all(kind):
                    if kind == 'sticker':
                        from .stickers import origins
                        sources = origins(item)
                    else:
                        sources = [item]
                    if any(source.get('scope') == scope and (
                        ids.intersection(source.get('source_ids', [])) or
                        (user_id and (user_id in source.get('actor_ids', []) or source.get('creator') == user_id))
                    ) for source in sources):
                        if kind == 'reminder':
                            for run in self.all('task_run'):
                                if run.get('task_id') == item['id']: self.delete('task_run', run['id'])
                        if kind == 'sticker':
                            from .messaging import sticker_path
                            from types import SimpleNamespace
                            sticker_path(SimpleNamespace(data_dir=getattr(self, 'asset_root', self.path.parent)), item).unlink(missing_ok=True)
                        if kind == 'attachment':
                            for suffix in ('.jpg', '.image', '.source', '.image.tmp', '.source.tmp'):
                                (Path(getattr(self, 'asset_root', self.path.parent))/'attachments'/(item['id']+suffix)).unlink(missing_ok=True)
                        self.delete(kind, item['id'])
            for item in self.all("learned_style"):
                if item.get("scope") == scope and (ids.intersection(item.get("source_ids", [])) or (user_id and user_id in item.get("actor_ids", []))):
                    removed_styles.add(item["id"]); self.delete("learned_style", item["id"])
            for kind in ("memory_version", "style_version"):
                for item in self.all(kind):
                    if item.get("scope") != scope:
                        continue
                    metadata = item.get("data", item)
                    if (item.get("memory_id") in removed_memory_ids or item.get("style_id") in removed_styles or ids.intersection(metadata.get("source_ids", [])) or (user_id and user_id in metadata.get("subject_ids", []) + metadata.get("actor_ids", []) + metadata.get("declarant_ids", []) + metadata.get("object_ids", []))):
                        self.delete(kind, item["id"])
            self.clear_derived(scope)
            for session in self.scoped_objects('learning_session', scope, limit=None):
                self.delete('learning_session', session['id'])
            for trace in self.scoped_objects("trace", scope, limit=None):
                if trace.get("scope") == scope:
                    trace.update(context=None, stages=[], prompts=[], redacted=True, notice="遗忘操作后已清理本会话追踪内容")
                    self.put("trace", trace["id"], trace)
            if user_id:
                member = self.get("member", scope+":"+user_id)
                if member:
                    member["aliases"] = []; self.put("member", member["id"], member)
        epoch = self.get("memory_epoch", scope, {"value": 0})["value"] + 1
        self.put("memory_epoch", scope, {"value": epoch})
        if hasattr(self,'creation_invalidator'):
            self.creation_invalidator(scope,ids,user_id)

    def backup(self, folder):
        folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
        dest = folder / (time.strftime("%Y-%m-%d", time.gmtime(now()+8*3600)) + ".sqlite")
        with self.lock, sqlite3.connect(dest) as backup:
            self.conn.backup(backup)
            # Diagnostic payloads must not outlive their retention in daily backups.
            tables={r[0] for r in backup.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name in ("call_images","model_calls"):
                if name in tables:backup.execute("DELETE FROM "+name)
            backup.execute("DELETE FROM objects WHERE kind='trace'")
            backup.commit()
            backup.execute("VACUUM")
        for old in sorted(folder.glob("*.sqlite"))[:-7]:
            old.unlink()
        import zipfile
        from .messaging import sticker_path
        from types import SimpleNamespace
        assets = dest.with_suffix('.stickers.zip')
        with sqlite3.connect(dest) as snapshot, zipfile.ZipFile(assets, 'w', zipfile.ZIP_DEFLATED) as archive:
            for (data,) in snapshot.execute("SELECT data FROM objects WHERE kind='sticker'"):
                item = json.loads(data)
                path = sticker_path(SimpleNamespace(data_dir=getattr(self, 'asset_root', self.path.parent)), item)
                if path.exists(): archive.write(path, path.name)
        for old in sorted(folder.glob('*.stickers.zip'))[:-7]: old.unlink()
        return dest

    def close(self):
        with self.lock:
            self.conn.close()
