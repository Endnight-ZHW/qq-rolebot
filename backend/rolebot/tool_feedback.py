"""Keep operational claims grounded in this turn's execution evidence."""
import re


def ground_reply(reply, batch, executed, runtime=None, *, target=None):
    explanations = {
        'like_daily_limit': '今天累计提交的赞已经到后台设置的每日上限啦，这次没再提交。这个限制来自机器人配置。',
        'like_target_restricted': 'QQ 返回对方的点赞权限不允许，这次没点上。调整对方设置后可以再叫我试试。',
        'like_platform_daily_limit': 'QQ 返回今天对这个账号的点赞额度已经用尽啦，这次没点上。',
        'like_result_unknown': '上次点赞有没有成功还没确认，我先不补发，免得重复。',
        'like_request_in_progress': '这个账号的点赞还在处理中，我先等结果，不重复提交。',
        'like_request_already_processed': '这条消息的点赞请求已经处理过啦，我没重复提交。需要重试时再发一条请求就行。',
        'like_retry_cooldown': '刚才没点上，还在短暂冷却中；稍后可以再叫我试试。',
        'poke_cooldown': '刚戳过这个 QQ，还在冷却中，等一会儿再戳吧。',
        'like_times_limit': '这次的点赞次数超过后台单次上限，我还没提交。',
        'social_platform_rejected': 'QQ 接口没接受这次互动请求，我没有继续重试。',
        'self_target_only': '这次没执行，你的权限只能操作自己哦。',
        'request_access_denied': '这个工具的群内规则暂未允许你使用，我这次没执行。得在后台调整对应身份的使用范围。',
        'request_identity_unavailable': '你的 QQ 群身份暂时没核实到，我这次还没执行，不能据此说你没有权限。',
        'self_recall_only': '这条不是你发的，我还没执行。你可以请求撤回自己的消息，请引用本人发的那条。',
        'recall_target_unavailable': '我还没核实到原消息，这次没执行。请引用那条消息；如果已过期或读不到，需要在 QQ 客户端处理。',
        'recall_target_mismatch': '原消息的群或作者没对上，这次没执行。请在本群引用正确的那条。',
        'explicit_target_protected': '后台给这个操作设了目标保护，这次没执行。得让管理员检查保护设置；换条消息也不能解除。',
        'implicit_target_protected': '这个账号受默认管理保护，我这次没执行。',
        'platform_admin_required': '我在 QQ 群里还缺这项操作需要的管理权限，这次没执行。后台授权不能代替 QQ 权限。',
        'platform_hierarchy_denied': 'QQ 的群主、管理员身份规则不允许我对这个目标操作，这次没执行。',
        'platform_identity_unavailable': 'QQ 群身份还没核实完，我这次没执行。',
        'user_permission_denied': '这个功能是有的，不过当前后台规则还没允许我按你的请求用，这次没执行。',
        'user_requests_disabled': '后台关闭了这个工具的用户请求，这次没执行；不是我没有这个功能。',
        'autonomous_disabled': '后台关闭了这个工具的自主使用，我这次没执行。',
        'platform_owner_required': '设置专属头衔需要我在 QQ 群里是群主，当前身份不满足，这次没执行。',
        'platform_packet_unavailable': '设置头衔用的 Packet 服务暂时不可用，这次没执行，不是你的授权有问题。',
        'conversation_paused': '当前会话已暂停或未启用，我这次没执行。',
        'management_paused': '后台紧急停用了管理工具，我这次没执行。',
        'interaction_disabled': '这个群的互动扩展还没开启，我这次没执行。',
        'tool_disabled': '后台还没开启这个工具，我这次没执行。',
        'service_unavailable': '这个工具的服务暂时没就绪，我这次没执行，不是账号授权不足。',
        'group_required': '这个工具需要在群聊里使用，我这次没执行。',
        'rejoin_restriction_disabled': '后台没有允许禁止再次入群，我这次没执行。',
        'mute_duration_limit': '这次要求的禁言时长超过后台设置的上限，我还没执行。',
        'tool_hourly_limit': '这个工具已到后台设置的每小时上限，我这次没执行。',
        'platform_rejected': 'QQ 接口明确拒绝了这次操作，我没继续重试；不能把它说成后台没有授权。',
        'platform_transport_error': 'QQ 连接或查询出了问题，这次没拿到成功回执，不能据此说你没权限。',
    }
    # Reading a member/permission snapshot or preparing a caption is not an action receipt.
    observations = {'message_send','sticker_send','sticker_search','sticker_list','capabilities',
                    'member_info','member_find','group_info','history_search','history_read','memory_search',
                    'web_search','web_fetch','image_search','image_read','forward_read','calculate',
                    'task_list','reminder_list','creation_status'}
    outcomes = [x for x in executed if not (x.get('status') == 'success' and x.get('name') in observations)]
    context_codes = {'request_context_unclear', 'request_verification_failed', 'source_mismatch', 'context_changed'}
    context_failures = [x for x in outcomes if x.get('status') == 'denied' and x.get('error_code') in context_codes]
    if context_failures and len(context_failures) == len(outcomes):
        if all(x['error_code'] == 'request_context_unclear' for x in context_failures):
            return '还没确定这句话接的是哪个操作，我这次没执行。把要做的事和对象一起告诉我就行。', 'context_failure_is_not_permission_denial'
        return '刚才的请求来源还没核对清楚，我这次没执行。请把目标和内容说完整一点。', 'context_failure_is_not_permission_denial'
    failures = [x for x in outcomes if x.get('status') in ('denied','error') and x.get('error_code') in explanations]
    if failures and len(failures)==len(outcomes) and len({x['error_code'] for x in failures})==1:
        code=failures[0]['error_code']
        if code in ('mute_duration_limit','tool_hourly_limit'):
            # The service supplies actual numeric limits; never invent QQ limits.
            return failures[0].get('error') or explanations[code], 'grounded_tool_failure:'+code
        if code=='implicit_target_protected' and all(x.get('name')=='message_recall' for x in failures):
            return '这个账号受默认管理保护，这次没执行。撤回本人消息，需要本人明确提出，并核对请求者就是消息作者。', 'grounded_tool_failure:'+code
        return explanations[code], 'grounded_tool_failure:'+code
    likes = [x for x in outcomes if x.get('name') == 'profile_like']
    if likes and len(likes) == len(outcomes):
        if all(x.get('status') == 'unknown' for x in likes):
            return explanations['like_result_unknown'], 'grounded_like_unknown'
        # Likes cannot be scheduled by this tool. Correct unsupported promises,
        # but otherwise keep the character's natural wording on success.
        future_promise = re.search(r'(?:等会|稍后|明天|过会|到时候|自动|回头)[^。！？\n]{0,45}(?:补|赞|点)', reply)
        if future_promise and all(x.get('status') == 'success' and isinstance(x.get('result'), dict) and x['result'].get('submitted') for x in likes):
            if len(likes) == 1 and type(likes[0]['result'].get('submitted_times')) is int:
                count = likes[0]['result']['submitted_times']
                return f'这次的 {count} 赞请求 QQ 已经接收啦。', 'grounded_like_no_automatic_followup'
            return '这次的点赞请求 QQ 已经接收啦。', 'grounded_like_no_automatic_followup'
    if outcomes and all(x.get('status') == 'unknown' for x in outcomes):
        return '这次操作的结果还没确认，我先不重复执行，免得做两遍。', 'grounded_operation_unknown'
    claims = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"', '', reply)
    claims = re.sub(r'(?:不是|并非)[^，,。！？\n]{0,12}(?:没(?:有)?(?:这|那)?(?:个)?(?:本事|能力|功能|权限)|不会)', '', claims)
    denied_claim = re.search(r"(?:没有|没给|不给|不具备|无权|缺少|没[有给]?)[^。！？\n]{0,28}(?:权限|授权)|受保护|保护(?:账号|名单)|(?:我|我|我).{0,12}(?:没那本事|不会禁言|没有能力|没有功能)", claims)
    if not denied_claim:
        return reply, None
    if re.search(r"上次|之前|以前|当时", reply) and not re.search(r"这次|现在|目前|仍然|还是", reply):
        return reply, None
    from .capability_facts import LABELS, capability_text
    selected = [target] if target else batch
    text = '\n'.join(m.get('plain_text', m['text']) for m in selected)
    names = {name for name, (_, pattern) in LABELS.items() if re.search(pattern, text)}
    if 'member_unmute' in names or 'group_mute' in names: names.discard('member_mute')
    if 'image_search' in names: names.discard('web_search')
    relevant = [x for x in outcomes if x.get('name') in names]
    if len(names) == 1 and not relevant and runtime:
        name = next(iter(names))
        tool = next((t for t in runtime['tools'] if t['name'] == name), None)
        actors = {m.get('user_id') for m in selected if m.get('user_id')}
        actor = next(iter(actors)) if len(actors) == 1 else None
        access = tool.get('requester_access', {}).get(actor) if tool and actor else None
        if access:
            if (not access['available'] and access.get('error_code') in ('user_permission_denied','request_access_denied')
                and re.search(r'你[^。！？]{0,16}(?:没|无)[^。！？]{0,16}(?:权限|授权)', claims)):
                return reply, None
            return capability_text({'name': name, 'access': access}), 'grounded_request_permission'
        if tool and not (tool['enabled'] and tool['user_requests'] and (tool['authorized_requesters'] or tool.get('self_only_requesters'))):
            return reply, None  # Older snapshots lack an actor-specific reason.
    if names and not relevant:
        return "我这轮还没实际执行，也没收到系统拒绝，暂时不能判断是不是权限问题。", "no_attempt_is_not_permission_denial"
    return reply, None
