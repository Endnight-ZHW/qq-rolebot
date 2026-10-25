"""Immutable hand-authored assets. No network access or generated executable code."""
import copy
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).with_name('art_library')

def manifest():
    path=ROOT/'manifest.json'
    if not path.exists():
        return {'version':1,'origin':'empty','palette':{},'assets':[]}
    return json.loads(path.read_text('utf8'))

def catalog(persona=None):
    data=manifest()
    return {'version':data['version'],'palette':data['palette'],
            'assets':[{k:a[k] for k in ('id','label','kind','width','height','usage')}
                      for a in data['assets'] if not a.get('persona') or a['persona']==persona]}

def read(id,persona=None):
    item=next((a for a in manifest()['assets'] if a['id']==id),None)
    if not item:raise ValueError('内置美术素材不存在')
    if item.get('persona') and item['persona']!=persona:
        raise ValueError('这份人物素材不属于当前角色')
    path=ROOT/item['file']
    if path.is_symlink() or path.parent.resolve()!=ROOT.resolve() or path.suffix!='.png':
        raise ValueError('内置美术素材路径无效')
    raw=path.read_bytes()
    if len(raw)!=item['bytes'] or hashlib.sha256(raw).hexdigest()!=item['sha256']:
        raise ValueError('内置美术素材已变化或损坏')
    return copy.deepcopy(item),raw

def current(snapshot,persona=None):
    try:item,_=read(snapshot['id'],persona);return item['sha256']==snapshot['sha256']
    except (OSError,ValueError,KeyError):return False
