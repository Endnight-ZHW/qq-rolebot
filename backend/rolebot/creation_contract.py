"""Small, dependency-free contract shared by the app and isolated renderer."""
import json
import math
import re

MIB = 1024 * 1024
RENDER_PROTOCOL = 2
FORMATS = ('png', 'gif', 'mp4')
OUTPUT_LIMITS = {'png': 8*MIB, 'gif': 8*MIB, 'mp4': 24*MIB}
JOB_ID = re.compile(r'^[0-9a-f]{32}$')
ASSET_NAME = re.compile(r'^(?:asset_[0-7]\.(?:png|jpg|gif|webp)|source_[0-7]\.svg)$')


def validate_spec(value):
    if not isinstance(value, dict) or set(value)-{'runtime','format','width','height','duration','fps','pages','code','caption'}:
        raise ValueError('制作清单字段无效')
    spec = dict(runtime='python', format='png', width=1200, height=800, duration=0, fps=1, pages=1, caption='', **{})
    spec.update(value)
    if spec['runtime'] not in ('python','web') or spec['format'] not in FORMATS:
        raise ValueError('仅支持 Python／网页制作 PNG、GIF、MP4')
    if any(type(spec[k]) is not int for k in ('width','height','fps','pages')):
        raise ValueError('尺寸、帧率与页数必须为整数')
    w,h,fps,pages = (spec[k] for k in ('width','height','fps','pages'))
    duration = spec['duration']
    if type(duration) not in (int,float) or not math.isfinite(duration):
        raise ValueError('时长无效')
    if not 64 <= min(w,h) or max(w,h)>4096 or w*h*pages>8_000_000 or not 1<=pages<=3:
        raise ValueError('图片尺寸或页数超限')
    if spec['format']=='png':
        spec.update(duration=0,fps=1)
    else:
        seconds, edge, rate = (8,640,12) if spec['format']=='gif' else (20,1280,24)
        if not 0<duration<=seconds or not 1<=fps<=rate or pages!=1 or max(w,h)>edge:
            raise ValueError('动画时长、帧率或尺寸超限')
        if w%2 or h%2 or (spec['format']=='mp4' and min(w,h)>720):
            raise ValueError('动画尺寸须为偶数，视频须在 720p 内')
    if not isinstance(spec.get('code'),str) or not spec['code'].strip() or len(spec['code'].encode())>200_000:
        raise ValueError('代码为空或超过 200 KB')
    if not isinstance(spec['caption'],str) or len(spec['caption'])>1000:
        raise ValueError('作品说明过长')
    return spec


def output_names(spec):
    if spec['format']=='png':
        return [f'page-{i}.png' for i in range(spec['pages'])]
    return ['animation.gif' if spec['format']=='gif' else 'video.mp4', 'poster.png', 'proof-mid.png', 'proof-end.png','timeline.png']


def timeline_layout(spec):
    scale=min(320/spec['width'],240/spec['height'],1)
    tile=(max(1,round(spec['width']*scale)),max(1,round(spec['height']*scale)))
    return tile,(tile[0]*4,(tile[1]+20)*3)


def validate_data(value):
    if not isinstance(value,dict) or len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode())>80_000:
        raise ValueError('制作资料须为不超过 80 KB 的 JSON 对象')
    return value
