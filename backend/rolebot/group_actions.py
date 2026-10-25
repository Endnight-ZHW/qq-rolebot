"""Per-operation QQ checks; a mutation is not necessarily a punishment."""
import asyncio


def target_user(db, scope, name, args):
    from .tools import ToolDenied
    if name != "message_recall":
        return args.get("user_id")
    from .message_targets import canonical_id, evidence, local_target
    verified = evidence(db,scope,args['message_id'])
    if verified: return verified.author
    row = local_target(db,scope,db.get('group',scope,{}).get('persona','default'),canonical_id(scope,args['message_id']))
    if row is None:
        raise ToolDenied('目标消息尚未核验，请引用要撤回的原消息',code='recall_target_unavailable')
    return str(row['user_id'])


def protected_target(db, bot, scope, name, args, policy, *, consenting_actor=None, defer_recall=False):
    from .tools import ToolDenied
    target = target_user(db, scope, name, args)
    if not target:
        return
    protected = {str(x) for x in policy.get("protected_ids", [])}
    if target in protected:
        raise ToolDenied('目标在此操作的显式保护名单中，本次未执行',code='explicit_target_protected')
    punitive = name in ('member_mute','member_kick') or (name=='message_recall' and target != bot.self_id and target != consenting_actor and not defer_recall)
    if punitive and target in {str(x) for x in db.config()['admin_qq']} | {bot.self_id}:
        raise ToolDenied('该账号受到默认管理保护；他人或自主操作不能对其执行此动作',code='implicit_target_protected')


async def platform_member(bot, gid, qq):
    from .tools import ToolDenied
    try:
        info = await bot.call("get_group_member_info", {"group_id": gid, "user_id": qq, "no_cache": True})
    except Exception as exc:
        raise ToolDenied('QQ 群身份查询暂未完成，本次未提交操作', code='platform_identity_unavailable') from exc
    if (not isinstance(info, dict) or info.get("role") not in ("member", "admin", "owner")
        or str(info.get("user_id", qq)) != qq or str(info.get("group_id", gid)) != gid):
        raise ToolDenied("无法确认当前群成员及其 QQ 身份",code='platform_identity_unavailable')
    return info


async def platform_check(db, bot, scope, name, args):
    from .tools import ToolDenied
    gid = scope.split(":", 2)[2]
    async def member(qq): return await platform_member(bot, gid, qq)

    me = await member(bot.self_id)
    if name in ('profile_like', 'member_poke'):
        target = await member(args['user_id'])
        if str(target.get('user_id', '')) != args['user_id']:
            raise ToolDenied('无法确认互动目标的 QQ 群身份', code='platform_identity_unavailable')
        return  # Social interactions do not require QQ moderation privileges.
    if name == 'member_title':
        if me['role'] != 'owner':
            raise ToolDenied('QQ 专属头衔需要机器人账号是群主；后台授权不能替代此权限', code='platform_owner_required')
        try:
            await bot.call('nc_get_packet_status', {})
        except Exception as exc:
            raise ToolDenied('NapCat Packet 服务不可用，无法设置专属头衔', code='platform_packet_unavailable') from exc
    target = target_user(db, scope, name, args)
    if name in ("member_card", "message_recall") and target == bot.self_id:
        return  # QQ members can edit their own card / recall their own message.
    if name == "group_name":
        # QQ also supports member renames in groups where this is permitted.
        # NapCat's API enforces that group setting and reports a refusal.
        return
    if me["role"] not in ("owner", "admin"):
        raise ToolDenied("此操作需要机器人具有 QQ 群管理权限",code='platform_admin_required')
    if target:
        other = await member(target)
        if name in ("member_mute", "member_unmute", "member_kick", "message_recall"):
            if other["role"] == "owner" or (other["role"] == "admin" and me["role"] != "owner"):
                raise ToolDenied("QQ 不允许机器人对该成员执行此管理操作",code='platform_hierarchy_denied')
        # Member-card edits are not subject to the punitive rank comparison.
        # QQ performs the final operation-specific check; successful edits are
        # independently read back below.


async def verify_change(bot, scope, name, args):
    """Read back profile edits once; never resend a possibly completed mutation."""
    from .onebot import ActionUnknown
    gid = scope.split(":", 2)[2]
    try:
        if name in ("member_card", 'member_title'):
            actual = await bot.call("get_group_member_info", {"group_id": gid, "user_id": args["user_id"], "no_cache": True})
            valid = (isinstance(actual, dict) and str(actual.get("user_id")) == args["user_id"]
                     and str(actual.get("group_id", gid)) == gid and actual.get('title' if name == 'member_title' else 'card') == args['title' if name == 'member_title' else 'card'])
            if name == 'member_title' and not valid:
                # QQ updates title cache asynchronously. Poll reads only; never
                # repeat the mutation just because its new value is not visible yet.
                for delay in (.5, 1, 2, 4, 6):
                    await asyncio.sleep(delay)
                    actual = await bot.call('get_group_member_info', {'group_id': gid, 'user_id': args['user_id'], 'no_cache': True})
                    valid = (isinstance(actual, dict) and str(actual.get('user_id')) == args['user_id']
                             and str(actual.get('group_id', gid)) == gid and actual.get('title') == args['title'])
                    if valid: break
        elif name == "group_name":
            actual = await bot.call("get_group_info", {"group_id": gid, "no_cache": True})
            valid = isinstance(actual, dict) and str(actual.get("group_id")) == gid and actual.get("group_name") == args["name"]
        else:
            return None
    except Exception as exc:
        raise ActionUnknown("QQ 已接收修改，但读取新值失败；结果待核实，未重复提交") from exc
    if not valid:
        raise ActionUnknown("QQ 已接收修改，但回读值尚未匹配；结果待核实，未重复提交")
    field = {'member_card': 'card', 'member_title': 'title', 'group_name': 'name'}[name]
    return {'verified': True, 'field': 'group_name' if name == 'group_name' else field, 'value': args[field]}
