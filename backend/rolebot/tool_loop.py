"""One bounded tool loop shared by interactive and scheduled turns."""
from .config_schema import runtime_config, RUNTIME_DEFAULTS
import copy
import json
from .input_budget import measure, projected, check_images
from .history import HISTORY_TOOLS


def tool_result(value, maximum=12000):
    """Trim data, never serialized JSON; identities/status and provenance survive."""
    result = copy.deepcopy(value)
    encode = lambda x: json.dumps(x, ensure_ascii=False, separators=(',', ':'))
    if len(encode(result)) <= maximum:
        return encode(result)
    protected = {'id', 'source_id', 'source_ids', 'status', 'error_code', 'message_id', 'qq', 'user_id', 'name', 'tool', 'next_offset'}
    def candidates(node):
        found = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key not in protected and isinstance(value, str) and len(value) > 200:
                    found.append((len(value), node, key))
                elif isinstance(value, (dict, list)):
                    found.extend(candidates(value))
        elif isinstance(node, list):
            for value in node: found.extend(candidates(value))
        return found
    while len(encode(result)) > maximum-100:
        choices = candidates(result)
        if not choices: break
        _, parent, key = max(choices, key=lambda x: x[0])
        parent[key] = parent[key][:max(200, len(parent[key])//2)]+'…'
    if isinstance(result, dict):
        result['truncated'] = True
        result['notice'] = '部分详情已省略；需要原文时使用历史读取或对应查询工具，不能声称已读省略内容'
    if len(encode(result)) > maximum:
        # Bound very large arrays by dropping complete entries, never half a JSON token.
        while len(encode(result)) > maximum:
            arrays = []
            def collect(node):
                if isinstance(node, dict):
                    for key,v in node.items():
                        if key not in protected and isinstance(v,list) and v: arrays.append(v)
                        elif isinstance(v,dict): collect(v)
                elif isinstance(node,list):
                    if node: arrays.append(node)
            collect(result)
            if not arrays: break
            max(arrays,key=lambda v:len(encode(v))).pop()
    if len(encode(result)) > maximum:
        result = {k:v for k,v in result.items() if k in ('status','error_code')} if isinstance(result,dict) else {}
        result.update(truncated=True, notice='结果超出预算，详情未送入模型；不得据此猜测执行结果')
    return encode(result)


class ToolLoop:
    def __init__(self, model, db=None):
        self.model = model
        self.db = db or getattr(model, "db", None)

    async def run(self, scope, purpose, messages, schemas, execute, current, *, stage, json_mode=False, prepared=False, history_limit=None, input_budget=None, after_tools=None, completion_guard=None, initial_tool_calls=0, extend_prepared=None):
        executed, calls, history_calls = [], initial_tool_calls, 0
        def size():
            value = measure({'messages': projected(messages), 'tools': schemas})
            if hasattr(self.model, 'db'):
                check_images(value, self.model.db.config())
            return value['estimated_input']
        limits = runtime_config(self.db, scope) if self.db is not None else RUNTIME_DEFAULTS
        rounds, maximum = limits['tool_max_rounds'], limits['tool_max_calls']
        for index in range(rounds):
            if not current():
                return None, executed
            if input_budget is not None and size() > input_budget:
                from .context import ContextTooLarge
                raise ContextTooLarge('历史补读或工具链超过输入硬预算；保留完整工具链并停止本轮，不猜测省略内容')
            response = await self.model.chat(scope, purpose, messages, tools=schemas if index < rounds-1 and calls < maximum else None, json_mode=json_mode, stage=stage)
            messages.append(response)
            if not response.get('tool_calls'):
                if completion_guard:
                    response, correction = completion_guard(response, index < rounds-2 and calls < maximum)
                    if correction:
                        messages.append({'role': 'system', 'content': correction})
                        continue
                return response, executed
            for call in response['tool_calls']:
                if not current(): return None, executed
                calls += 1
                name = call.get('function', {}).get('name', '')
                if name in HISTORY_TOOLS: history_calls += 1
                try: args = json.loads(call.get('function', {}).get('arguments', '{}'))
                except (ValueError, TypeError): args = None
                duplicate = prepared and name in ('message_send', 'sticker_send') and any(isinstance(x.get('result'),dict) and x['result'].get('prepared') for x in executed)
                if duplicate and extend_prepared and extend_prepared(name, args, executed):
                    duplicate = False
                result = ({'status':'denied','error':'本轮已准备最终回复'} if duplicate else
                    {'status':'denied','error':'达到本轮历史查询上限；请根据已有资料回答或澄清'} if history_limit is not None and name in HISTORY_TOOLS and history_calls > history_limit else
                    {'status':'denied','error':'达到本轮工具上限'} if calls > maximum or index == rounds-1 else await execute(name,args))
                executed.append({'name': name, **result})
                result_budget = min(12000, max(256, input_budget-size()-512)) if input_budget is not None else 12000
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': tool_result(result, result_budget)})
            if after_tools and current():
                await after_tools()
        return None, executed
