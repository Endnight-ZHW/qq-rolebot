"""Complete business payloads, compressed in SQLite; images expire separately."""
import base64
import gzip
import hashlib
import json
import re
import time

from .db import uid
from .tracing import active_trace


def pack(value):
    return gzip.compress(json.dumps(value,ensure_ascii=False,separators=(",",":")).encode(),mtime=0)


def unpack(value):
    return json.loads(gzip.decompress(value))


def image_reference(value):
    return (isinstance(value,dict) and isinstance(value.get("__record_image__"),str)
            and bool(re.fullmatch(r"[0-9a-f]{32}",value["__record_image__"]))
            and {"mime","sha256","bytes","expires","encoding"} <= value.keys())


class Records:
    def __init__(self, db, settings=None):
        self.db,self.settings=db,settings
        with db.transaction() as c:
            c.execute("CREATE TABLE IF NOT EXISTS model_calls(id TEXT PRIMARY KEY,trace_id TEXT,scope TEXT NOT NULL,ts REAL NOT NULL,expires REAL NOT NULL,status TEXT NOT NULL,meta TEXT NOT NULL,payload BLOB NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS model_calls_scope_ts ON model_calls(scope,ts)")
            c.execute("CREATE INDEX IF NOT EXISTS model_calls_trace ON model_calls(trace_id,ts)")
            c.execute("CREATE INDEX IF NOT EXISTS model_calls_ts_status ON model_calls(ts,status)")
            c.execute("CREATE TABLE IF NOT EXISTS call_images(id TEXT PRIMARY KEY,call_id TEXT NOT NULL,scope TEXT NOT NULL,expires REAL NOT NULL,mime TEXT NOT NULL,sha256 TEXT NOT NULL,content BLOB NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS call_images_expiry ON call_images(expires)")

    def clean(self, value, notices=None, path=""):
        notices=notices if notices is not None else []
        secret=(self.settings.secret if self.settings else {})
        known=[v for k,v in secret.items() if isinstance(v,str) and len(v)>=8]
        known += [v for v in secret.get("provider_keys",{}).values() if isinstance(v,str) and len(v)>=8]
        known += [v for v in secret.get("service_keys",{}).values() if isinstance(v,str) and len(v)>=8]
        def visit(item, p):
            if isinstance(item,dict):
                out={}
                for k,v in item.items():
                    if k.lower() in ("authorization","x-api-key","api_key","password","password_hash","onebot_token"):
                        out[k]="[凭据已隐藏]";notices.append({"path":p+"/"+k,"reason":"credential"})
                    else:out[k]=visit(v,p+"/"+k)
                return out
            if isinstance(item,list):return [visit(x,p+"/"+str(i)) for i,x in enumerate(item)]
            if isinstance(item,str):
                for text in known:
                    if text in item:item=item.replace(text,"[凭据已隐藏]");notices.append({"path":p,"reason":"credential"})
                return item
            return item
        return visit(value,path)

    def _images(self, value, call_id, scope, expires, images):
        def save(encoded,mime,encoding):
            try:raw=base64.b64decode(encoded,validate=True)
            except (ValueError,TypeError):raw=str(encoded).encode();encoding+="_invalid"
            id=uid();sha=hashlib.sha256(raw).hexdigest()
            self.db.execute("INSERT INTO call_images VALUES(?,?,?,?,?,?,?)",(id,call_id,scope,expires,mime,sha,raw))
            ref={"__record_image__":id,"mime":mime,"sha256":sha,"bytes":len(raw),"expires":expires,"encoding":encoding}
            images.append(ref)
            return ref
        if isinstance(value,str) and value.startswith("data:image/") and ";base64," in value:
            prefix,data=value.split(",",1)
            return save(data,prefix[5:].split(";")[0],"data_url")
        if isinstance(value,list):return [self._images(x,call_id,scope,expires,images) for x in value]
        if isinstance(value,dict):
            if value.get("type")=="base64" and str(value.get("media_type","")).startswith("image/") and isinstance(value.get("data"),str):
                return {**value,"data":save(value["data"],value["media_type"],"base64")}
            return {k:self._images(v,call_id,scope,expires,images) for k,v in value.items()}
        return value

    def start(self, scope, purpose, resource, provider, payload, *, attempt=1, operation_id=None, internal=None):
        id=uid();now=time.time();trace_id=active_trace.get();trace=self.db.get("trace",trace_id,{}) if trace_id else {}
        redacted=trace.get("redacted",False)
        notices=[];images=[]
        meta={"id":id,"trace_id":trace_id,"scope":scope,"purpose":purpose,"resource":resource,"provider_id":provider["id"],
              "protocol":provider["protocol"],"model":(payload or {}).get("model"),"ts":now,"attempt":attempt,"operation_id":operation_id or id,
              "status":"running","redacted":redacted,"epoch":self.db.get("memory_epoch",scope,{"value":0})["value"],
              "source_ids":trace.get("source_ids",[]),"actor_ids":trace.get("actor_ids",[]),"persona":trace.get("persona"),
              "stage":(internal or {}).get("stage",trace.get("kind",purpose)),"expires":now+self.db.config().get("model_log_days",7)*86400}
        with self.db.transaction():
            data={"notice":"本轮包含停止记忆的成员，仅保留运行指标"} if redacted else {
                "request":self._images(self.clean(payload,notices,"request"),id,scope,now+self.db.config().get("model_log_image_hours",24)*3600,images),
                "prompts":self.clean(trace.get("prompts",[]),notices,"prompts"),"input":self.clean(internal or {},notices,"input")}
            # Internal projections must not duplicate base64 payloads beyond image TTL.
            if not redacted:data["input"]=self._images(data["input"],id,scope,now+self.db.config().get("model_log_image_hours",24)*3600,images)
            meta.update(images=images,redactions=notices)
            self.db.execute("INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?)",(id,trace_id,scope,now,meta["expires"],"running",json.dumps(meta,ensure_ascii=False),pack(data)))
        return id

    def finish(self, id, status, *, response=None, http_status=None, headers=None, error=None):
        rows=self.db.rows("SELECT * FROM model_calls WHERE id=?",(id,))
        if not rows:return
        row=rows[0];meta=json.loads(row["meta"])
        trace=self.db.get("trace",row["trace_id"],{}) if row["trace_id"] else {}
        redacted=meta.get("redacted") or trace.get("redacted") or meta["epoch"] != self.db.get("memory_epoch",row["scope"],{"value":0})["value"]
        notices=meta.get("redactions",[])
        with self.db.transaction():
            if redacted:
                data={"notice":"内容因停止记忆或遗忘操作已清理"}
                self.db.execute("DELETE FROM call_images WHERE call_id=?",(id,))
                meta.update(images=[],redactions=[],redacted=True)
            else:
                data=unpack(row["payload"])
                data["response"]=self._images(self.clean(response,notices,"response"),id,row["scope"],time.time()+self.db.config().get("model_log_image_hours",24)*3600,meta["images"])
                data["error"]=self.clean(error,notices,"error")
            meta.update(status=status,http_status=http_status,duration_ms=round((time.time()-meta["ts"])*1000),redactions=notices if not redacted else [],
                        response_headers=self.clean({k:v for k,v in (headers or {}).items() if k.lower() in ("x-request-id","request-id","content-type","retry-after")},notices,"response_headers"))
            self.db.execute("UPDATE model_calls SET status=?,meta=?,payload=? WHERE id=?",(status,json.dumps(meta,ensure_ascii=False),pack(data),id))
            if trace:
                trace["last_call_id"]=id
                self.db.put("trace",trace["id"],trace)

    def listing(self, scope=None, trace_id=None, purpose=None, status=None, limit=40, offset=0, start=None, end=None, model=None):
        from .dashboard import validate_range
        validate_range(start, end)
        sql="SELECT meta,expires FROM model_calls WHERE expires>?";args=[time.time()]
        for key,value in (("scope",scope),("trace_id",trace_id),("status",status)):
            if key == 'status' and value == 'failure':
                sql += " AND status IN ('http_error','network_error','invalid_response','error')"
            elif value is not None:sql+=" AND "+key+"=?";args.append(value)
        if purpose:sql+=" AND json_extract(meta,'$.purpose')=?";args.append(purpose)
        if start is not None:sql+=' AND ts>=?';args.append(start)
        if end is not None:sql+=' AND ts<?';args.append(end)
        if model:sql+=" AND json_extract(meta,'$.model')=?";args.append(model)
        return [{**json.loads(r["meta"]),"expires":r["expires"]} for r in self.db.rows(sql+" ORDER BY ts DESC LIMIT ? OFFSET ?",(*args,max(1,min(limit,100)),max(0,offset)))]

    def detail(self,id):
        rows=self.db.rows("SELECT meta,payload FROM model_calls WHERE id=? AND expires>?",(id,time.time()))
        if not rows:return None
        value={**json.loads(rows[0]["meta"]),**unpack(rows[0]["payload"])}
        # A retention edit affects existing rows as well as newly created ones.
        value["expires"]=self.db.rows("SELECT expires FROM model_calls WHERE id=?",(id,))[0]["expires"]
        def state(item):
            if isinstance(item,list):return [state(x) for x in item]
            if isinstance(item,dict):
                if image_reference(item):
                    available=bool(self.db.rows("SELECT 1 FROM call_images WHERE id=? AND expires>?",(item["__record_image__"],time.time())))
                    return {**item,"status":"available" if available else "expired","url":"/api/v1/model-calls/images/"+str(item["__record_image__"]) if available else None}
                return {k:state(v) for k,v in item.items()}
            return item
        return state(value)

    def expand(self,value):
        if isinstance(value,list):return [self.expand(x) for x in value]
        if isinstance(value,dict):
            if image_reference(value):
                rows=self.db.rows("SELECT * FROM call_images WHERE id=? AND expires>?",(value["__record_image__"],time.time()))
                if not rows:raise ValueError("实际图片已过期或遗忘，无法原样回放")
                item=rows[0];encoded=item["content"].decode() if value["encoding"].endswith("_invalid") else base64.b64encode(item["content"]).decode()
                return "data:"+item["mime"]+";base64,"+encoded if value["encoding"].startswith("data_url") else encoded
            return {k:self.expand(v) for k,v in value.items()}
        return value

    def redact(self,scope=None,trace_id=None):
        key,value=("trace_id",trace_id) if trace_id is not None else ("scope",scope)
        with self.db.transaction():
            for row in self.db.rows("SELECT id,meta FROM model_calls WHERE "+key+"=?",(value,)):
                meta=json.loads(row["meta"]);meta.update(redacted=True,images=[],redactions=[])
                self.db.execute("UPDATE model_calls SET meta=?,payload=? WHERE id=?",(json.dumps(meta,ensure_ascii=False),pack({"notice":"内容因停止记忆或遗忘操作已清理"}),row["id"]))
                self.db.execute("DELETE FROM call_images WHERE call_id=?",(row["id"],))

    def cleanup(self):
        now=time.time()
        with self.db.transaction():
            self.db.execute("DELETE FROM call_images WHERE expires<? OR call_id IN (SELECT id FROM model_calls WHERE expires<?)",(now,now))
            self.db.execute("DELETE FROM model_calls WHERE expires<?",(now,))

    def recover(self):
        for r in self.db.rows("SELECT id,trace_id FROM model_calls WHERE status='running'"):
            self.finish(r["id"],"interrupted",error={"type":"ProcessInterrupted","notice":"进程中断；未记录到完整返回，不自动重发"})
            trace=self.db.get("trace",r["trace_id"]) if r["trace_id"] else None
            if trace and trace["status"]=="running":
                trace.update(status="interrupted",duration_ms=round((time.time()-trace["ts"])*1000),recovery_notice="进程中断，未补发模型请求")
                self.db.put("trace",trace["id"],trace)
