"""Bounded editing and visual evidence for the isolated creation workflow."""
import ast
import json
import math
from fractions import Fraction

from .creation_contract import validate_spec


def resolve_draft(value, previous=None):
    """Apply exact source edits; never execute or guess generated Python/HTML."""
    value=dict(value)
    design=value.pop('design',None)
    if design is not None and (not isinstance(design,dict) or len(json.dumps(design,ensure_ascii=False))>6000):
        raise ValueError('design 须为不超过 6000 字符的构图/分层/时间线说明')
    if 'patches' in value:
        if not previous or set(value)-{'patches','caption'}:
            raise ValueError('局部修补需要上一版源码，只能修改 patches 和 caption')
        edits=value['patches']
        if not isinstance(edits,list) or not 1<=len(edits)<=12:raise ValueError('每次局部修补限 1–12 处')
        spec=dict(previous)
        for edit in edits:
            if not isinstance(edit,dict) or set(edit)!={'old','new'} or not all(isinstance(v,str) for v in edit.values()):
                raise ValueError('修补须提供精确 old/new 字符串')
            if not edit['old'] or spec['code'].count(edit['old'])!=1:
                raise ValueError('修补原文必须唯一匹配当前源码；请增加定位上下文，不能猜测替换位置')
            spec['code']=spec['code'].replace(edit['old'],edit['new'],1)
        if 'caption' in value:spec['caption']=value['caption']
    else:spec=value
    spec=validate_spec(spec)
    if spec['runtime']=='python':
        try:ast.parse(spec['code'])
        except SyntaxError as exc:raise ValueError(f'Python 第 {exc.lineno} 行语法错误：{exc.msg}') from None
    return spec,design


def repair_feedback(verdict):
    """Keep actionable issues and suggestions instead of only a lossy summary."""
    return json.dumps({k:verdict[k] for k in ('observations','blocking_issues','suggestions','quality','reason') if k in verdict},ensure_ascii=False)[:6000]


def visual_history(history,limit):
    """Project recent image evidence without mutating the saved conversation."""
    remaining=max(0,limit);result=[]
    for message in reversed(history):
        item=dict(message)
        if isinstance(item.get('content'),list):
            blocks=[]
            for block in reversed(item['content']):
                if block.get('type')=='image_url':
                    if remaining:remaining-=1
                    else:
                        blocks.append({'type':'text','text':'[较早预览已省略，继续依据最近的实际预览及保留的源码和工具结果。]'});continue
                blocks.append(block)
            item['content']=list(reversed(blocks))
        result.append(item)
    return list(reversed(result))


def matrix_facts(data):
    """Check explicit small transformation matrices; supplied labels are not proof."""
    value=next((data[k] for k in ('matrix','矩阵','transformation_matrix') if k in data),None)
    if isinstance(value,str):
        text=value.split('=',1)[-1].strip()
        try:value=json.loads(text)
        except ValueError:return None
    if not isinstance(value,list) or len(value) not in (2,3):return None
    n=len(value)
    if any(not isinstance(row,list) or len(row)!=n for row in value):return None
    if any(type(v) not in (int,float) or abs(v)>1e9 or not math.isfinite(v) for row in value for v in row):return None
    rows=[[Fraction(str(v)) for v in row] for row in value]
    if n==2:det=rows[0][0]*rows[1][1]-rows[0][1]*rows[1][0]
    else:
        a,b,c=rows[0];d,e,f=rows[1];g,h,i=rows[2]
        det=a*(e*i-f*h)-b*(d*i-f*g)+c*(d*h-e*g)
    return {'matrix':value,'determinant':float(det),'determinant_exact':str(det),'volume_scale':float(abs(det)),
            'basis_convention':'列向量为基向量变换后的坐标',
            'notice':'这是对输入矩阵的确定性计算。若 brief/data 的行列式或体积文字不一致，请明确更正并使用计算结果；不是外部产品实测数据。'}
