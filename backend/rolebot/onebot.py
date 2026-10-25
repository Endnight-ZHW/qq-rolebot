import asyncio
import time
import uuid
from . import tracing


class OneBotError(Exception):
    def __init__(self, message, *, retcode=None, upstream_message='', request_sent=None):
        super().__init__(message)
        self.retcode = retcode
        self.upstream_message = upstream_message[:500] if isinstance(upstream_message, str) else ''
        self.request_sent = request_sent


class ActionUnknown(OneBotError):
    """Request may have reached QQ; a retry could duplicate a side effect."""


class OneBot:
    def __init__(self):
        self.socket = None
        self.self_id = ""
        self.pending = {}
        self.connected_at = 0
        self.last_event = 0
        self.send_lock = asyncio.Lock()
        self.trace_db = None
        self.pending_traces = {}
        self.late_like_receipts = {}

    @property
    def online(self):
        return self.socket is not None

    async def connect(self, socket, self_id):
        previous = self.socket
        if previous:
            await self.disconnect(previous)
            try:
                await previous.close(code=1012)
            except Exception:
                pass
        self.socket, self.self_id = socket, str(self_id)
        self.connected_at = self.last_event = time.time()

    async def disconnect(self, socket):
        if self.socket is not socket:
            return
        self.socket = None
        for future, mutation in list(self.pending.values()):
            if not future.done():
                future.set_exception(ActionUnknown("连接中断，操作结果未知") if mutation else OneBotError("NapCat 已断开"))
        self.pending.clear()

    def response(self, data):
        self.last_event = time.time()
        echo = data.get("echo")
        if not echo or echo not in self.pending:
            self.late_like_receipts = {k:v for k,v in self.late_like_receipts.items() if v['expires'] > time.time()}
            late = self.late_like_receipts.pop(echo, None)
            if late and self.trace_db:
                response = {k:data[k] for k in ('echo','status','retcode','message','wording') if k in data}
                for key in ('message','wording'):
                    if key in response: response[key] = str(response[key])[:500]
                tracing.stage_for(self.trace_db, late['trace_id'], 'onebot_response',
                    {'echo':echo, 'response':response, 'late':True})
                return True
            return False
        future, _ = self.pending.pop(echo)
        trace_id = self.pending_traces.get(echo)
        if self.trace_db and trace_id:
            tracing.stage_for(self.trace_db,trace_id,"onebot_response",{"echo":echo,"response":data})
        if not future.done():
            if data.get("status") == "ok" and data.get("retcode", 0) == 0:
                future.set_result(data.get("data"))
            else:
                future.set_exception(OneBotError(f"QQ 接口失败，retcode={data.get('retcode', 'unknown')}",
                    retcode=data.get('retcode'), upstream_message=data.get('wording') or data.get('message', ''), request_sent=True))
        return True

    async def call(self, action, params=None, mutation=False, timeout=12):
        socket = self.socket
        if not socket:
            raise OneBotError("NapCat 未连接", request_sent=False)
        echo = uuid.uuid4().hex
        trace_id=tracing.active_trace.get()
        self.pending_traces[echo]=trace_id
        if self.trace_db and trace_id:
            tracing.stage_for(self.trace_db,trace_id,"onebot_request",{"echo":echo,"action":action,"params":params or {},"mutation":mutation})
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = (future, mutation)
        sent = False
        retain_receipt = False
        try:
            async with self.send_lock:
                if self.socket is not socket:
                    raise OneBotError("NapCat 连接已切换", request_sent=False)
                sent = True
                await socket.send_json({"action": action, "params": params or {}, "echo": echo})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            retain_receipt = sent and mutation
            raise ActionUnknown("QQ 操作超时，结果未知；未自动重试") if mutation else OneBotError("QQ 查询超时")
        except (ActionUnknown, asyncio.CancelledError):
            retain_receipt = sent and mutation
            raise
        except OneBotError:
            raise
        except Exception as exc:
            retain_receipt = sent and mutation
            raise ActionUnknown("发送中断，结果未知") if sent and mutation else OneBotError("QQ 连接不可用", request_sent=sent) from exc
        finally:
            if retain_receipt and action == 'send_like' and trace_id and self.trace_db:
                self.late_like_receipts = {k:v for k,v in self.late_like_receipts.items() if v['expires'] > time.time()}
                self.late_like_receipts[echo] = {'trace_id':trace_id, 'expires':time.time()+120}
                while len(self.late_like_receipts) > 128:
                    self.late_like_receipts.pop(next(iter(self.late_like_receipts)))
            self.pending.pop(echo, None)
            self.pending_traces.pop(echo,None)

    async def send(self, scope, message):
        _, kind, target = scope.split(":", 2)
        action = "send_group_msg" if kind == "g" else "send_private_msg"
        field = "group_id" if kind == "g" else "user_id"
        return await self.call(action, {field: target, "message": message}, mutation=True)


def scope_of(event):
    self_id = str(event.get("self_id", ""))
    if event.get("message_type") == "group" or event.get("group_id"):
        return f"{self_id}:g:{event['group_id']}"
    return f"{self_id}:p:{event.get('user_id', '')}"


def normalize(event):
    segments = event.get("message", [])
    if isinstance(segments, str):
        segments = [{"type": "text", "data": {"text": segments}}]
    text, images, mentions, replies, plain, types, forwards = [], [], [], [], [], [], []
    for segment in segments[:200]:
        kind, data = segment.get("type"), segment.get("data", {})
        types.append(str(kind))
        if kind == "text":
            text.append(str(data.get("text", "")))
            plain.append(str(data.get("text", "")))
        elif kind == "at":
            mentions.append(str(data.get("qq", "")))
            text.append(f"[@QQ:{data.get('qq', '')}]")
        elif kind == "reply":
            replies.append(str(data.get("id", "")))
            text.append(f"[回复消息:{data.get('id', '')}]")
        elif kind == "image":
            # Preserve the image slot even when NapCat omitted an expired URL;
            # the observer can refresh that known message through get_msg.
            images.append(str(data.get("url") or ""))
            text.append("[图片]")
        elif kind == "face":
            text.append(f"[QQ表情:{data.get('id', '')}]")
        elif kind == "forward":
            fid = str(data.get("id") or "")
            if fid and len(fid) <= 256:
                forwards.append(fid)
            text.append("[合并转发聊天记录，正文需读取]")
        else:
            text.append({"record": "[语音，暂不支持识别]", "video": "[视频，暂不支持识别]"}.get(kind, "[其他消息]"))
    user_id = str(event.get("user_id", ""))
    scope = scope_of(event)
    raw_text = "".join(text)
    received = time.time()
    sent = event.get("time", received)
    sent = float(sent) if isinstance(sent, (int, float)) and 0 < sent <= received+300 else received
    return {"id": f"{scope}:{event.get('message_id', uuid.uuid4().hex)}", "message_id": str(event.get("message_id", "")),
            "scope": scope, "user_id": user_id, "text": raw_text[:12000], "plain_text": "".join(plain)[:12000],
            "types": list(dict.fromkeys(types)), "sent_at": sent, "truncated": len(raw_text) > 12000, "original_chars": len(raw_text),
            "name": str(event.get("sender", {}).get("card") or event.get("sender", {}).get("nickname") or user_id)[:80],
            "mentions": mentions, "replies": replies, "images": images, "forwards": forwards[:8], "ts": received, "event": event}
