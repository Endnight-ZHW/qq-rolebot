"""Disposable geometry for asset tests; no artwork is packaged or downloaded."""
import hashlib
import io
import json
from PIL import Image, ImageDraw


def make_library(root):
    root.mkdir(parents=True)
    (root/'sources').mkdir()
    rows=[]
    samples=[('grid','background','网格背景','测试背景 网格'),
             ('dark_background','background','深色背景','夜晚星空 深色'),
             ('triangle','decoration','三角形','几何装饰'),
             ('character_front','character','示例角色正面','助手 招手 正面'),
             ('character_horizontal','character','示例角色横放','助手 侧躺 抱鱼 躺卧 床')]
    for index,(name,kind,label,usage) in enumerate(samples):
        color=(20+index*25,100,160,255)
        image=Image.new('RGBA',(64,64),(0,0,0,0))
        ImageDraw.Draw(image).rectangle((16,16,48,48),fill=color)
        buf=io.BytesIO();image.save(buf,'PNG');raw=buf.getvalue()
        (root/(name+'.png')).write_bytes(raw)
        vector=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><g id="shape"><rect x="16" y="16" width="32" height="32" fill="rgb({color[0]},100,160)"/></g></svg>'
        (root/'sources'/(name+'.svg')).write_text(vector,encoding='utf8')
        rows.append({'id':'art:'+name,'label':label,'kind':kind,'persona':'default' if kind=='character' else None,
                     'usage':usage,'file':name+'.png','source':name+'.svg','width':64,'height':64,'bytes':len(raw),
                     'sha256':hashlib.sha256(raw).hexdigest(),'tags':usage.split()})
    (root/'manifest.json').write_text(json.dumps({'version':1,'origin':'synthetic-test','palette':{},'assets':rows}),encoding='utf8')
    return root
