"""Journaled completion messages sharing ordinary chat rate limits."""
import asyncio
import time

from .config_schema import runtime_config
from .delivery import send_slots, split_reply
from .onebot import ActionUnknown
from . import tracing


async def slot(service,job):
    engine=service.engine;started=time.monotonic()
    while True:
        await service.authorize_fresh(job)
        limit=runtime_config(service.db,job['scope'])
        if send_slots(service.db.get('chat_state',job['scope'],{}),limit['send_per_minute']):return
        if job['autonomous'] or time.monotonic()-started>=limit['send_wait_seconds']:
            raise ValueError('发送额度已满，本次未补发')
        await engine.delivery_sleep(1)


def reserve(service,job):
    db=service.db;state=db.get('chat_state',job['scope'],{})
    state['sent']=[t for t in state.get('sent',[]) if t>time.time()-60]
    if not send_slots(state,runtime_config(db,job['scope'])['send_per_minute']):raise ValueError('发送额度已满')
    state['sent'].append(time.time());db.put('chat_state',job['scope'],state)


async def deliver(service,job):
    engine,db=service.engine,service.db
    payload=service.payload(job);target=payload['target']
    if job['autonomous'] and time.time()-job.get('ready_at',job['created_at'])>180:raise ValueError('自主制作话题已过时')
    caption=payload.get('caption',job.get('caption')) or '做好啦。'
    group=db.get('group',job['scope'],{})
    parts=split_reply(caption,group.get('segment_threshold',80),group.get('segment_max_parts',3)) if group.get('segmented_reply') else [caption]
    packets=[('text:'+str(i),[{'type':'text','data':{'text':text}}],text,None) for i,text in enumerate(parts)]
    for artifact in job['artifacts']:
        if not artifact['primary']:continue
        file=service.media_url(job,artifact)
        if artifact['mime']=='video/mp4':
            poster=next(a for a in job['artifacts'] if a['name']=='poster.png')
            segment={'type':'video','data':{'file':file,'thumb':service.media_url(job,poster),'name':'机器人的作品.mp4'}}
        else:
            segment={'type':'image','data':{'file':file,'sub_type':1 if artifact['mime']=='image/gif' else 0,'summary':'[机器人制作的作品]'}}
        packets.append((artifact['id'],[segment],'[制作作品]',artifact))
    for key,segments,text,artifact in packets:
        if key in job.get('sent_parts',[]):continue
        await slot(service,job)
        if job['sent_ids']:await engine.delivery_sleep(1)
        await service.authorize_fresh(job)
        if not job['sent_ids'] and str(target.get('message_id','')).lstrip('-').isdigit():
            segments=[{'type':'reply','data':{'id':target['message_id']}},*segments]
        with db.transaction():
            reserve(service,job)
            job.update(status='sending',inflight_part=key);service.save(job)
        try:
            receipt=await engine.onebot.send(job['scope'],segments)
            if not receipt or receipt.get('message_id') is None:raise ActionUnknown('未收到消息编号')
        except ActionUnknown:
            job.update(status='unknown',error='发送结果未知，未自动重试',finished_at=time.time());service.save(job);return
        except Exception:
            job.update(status='failed',error='作品发送失败，未自动重试',finished_at=time.time());service.save(job);return
        mid=str(receipt['message_id'])
        job['sent_parts'].append(key);job['sent_ids'].append(mid);job.pop('inflight_part',None)
        service.save(job)
        remember=not job['private'] and job['epoch']==engine.memory.epoch(job['scope']) and not db.get('creation_job',job['id'],{}).get('deleted')
        metadata={'message_id':mid,'source_ids':job['source_ids'] if remember else [],'actor_ids':job['actor_ids'] if remember else [],
                  'creation_job_id':job['id'],'trace_id':tracing.active_trace.get(),
                  'types':['video' if artifact and artifact['mime']=='video/mp4' else 'image' if artifact else 'text']}
        if artifact and remember:metadata.update(creation_artifact_id=artifact['id'],image_count=int(artifact['mime']!='video/mp4'))
        db.add_message(job['scope']+':'+mid,job['scope'],job['persona'],engine.onebot.self_id,'assistant',text if remember else '',metadata,excluded=not remember)
        tracing.stage(db,'creation_sent',{'job_id':job['id'],'message_id':mid,'part':key},content=False)
    job.update(status='sent',finished_at=time.time());service.save(job)


def failure_text(job):
    code=job.get('error_code')
    if code=='queue_timeout':
        if '内存' in job.get('waiting_reason',''):return '这次一直没等到足够的制作内存，尚未调用模型。后台可以查看资源等待原因。'
        return '这次排队等太久，还没开始制作。稍后可以重新叫我。'
    if code=='model_timeout':return '这次等模型响应超时了，作品还没做完。想继续的话叫我再试一次就好。'
    if code=='model_network':return '制作时模型连接断了，作品还没做完。连接恢复后可以再叫我试一次。'
    if code=='creation_context_budget':return '这次制作资料超过上下文预算了，还没提交给模型。可以拆成几份，或在后台提高制作上下文预算。'
    if code=='model_output_limit':return '这次代码太长，被模型的输出上限截断了，我修过后还是没写完整。可以分步制作，或在后台提高制作模型的输出上限。'
    if code=='creation_timeout':return '这次制作超过了等待时限，我先停下了。可以再试一次，或者先做其中一部分。'
    if code=='review_response_invalid':return '画面已经渲染出来了，但检查回复不完整，我暂时没发；后台保留了成品和具体原因。'
    if job.get('error_stage')=='rendering':return '这次画面渲染没成功，我修过后还是没做好。可以换个画法再试一次。'
    if job.get('error_stage')=='checking':return '这次成品检查没过，我修过后还是没达到要求。可以换个画法再试一次。'
    return '这次制作没完成，还没有成品可以发。后台留了失败记录，可以查到具体原因。'


async def failure(service,job):
    if job.get('notified'):return
    await slot(service,job)
    with service.db.transaction():
        reserve(service,job);job['notified']=True;service.save(job)
    text=failure_text(job)
    try:
        result=await service.engine.onebot.send(job['scope'],[{'type':'text','data':{'text':text}}])
        job['notification_status']='sent' if result and result.get('message_id') is not None else 'unknown'
    except ActionUnknown:job['notification_status']='unknown'
    except Exception:job['notification_status']='failed'
    service.save(job)
