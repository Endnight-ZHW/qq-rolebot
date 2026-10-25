"""Administrator-uploaded persona reference art, independent of chat attachments."""
import base64
import hashlib
import io
import re
import time

from PIL import Image

from .db import uid
from .media import MediaError, normalize_image


class RoleImages:
    def __init__(self, engine):
        self.db = engine.db
        self.root = engine.settings.data_dir / 'role-images'
        self.root.mkdir(exist_ok=True)

    def path(self, ref):
        if not re.fullmatch(r'[0-9a-f]{32}', ref['id']) or not re.fullmatch(r'[0-9a-f]{64}', ref['sha256']):
            raise ValueError('参考图编号无效')
        return self.root / (ref['id']+'-'+ref['sha256'][:16]+'.jpg')

    def available(self, ref):
        return ref.get('source_type') == 'admin_upload' and self.path(ref).is_file()

    def list(self, persona, enabled_only=True):
        return sorted([r for r in self.db.all('role_image') if r.get('source_type') == 'admin_upload'
                       and r.get('persona') == persona and (not enabled_only or r.get('enabled', True))],
                      key=lambda r: (r['ts'], r['id']), reverse=True)

    def save(self, persona, raw, label='', replace_id=None):
        if not self.db.get('persona', persona):
            raise ValueError('角色不存在')
        if len(raw) > 8*1024*1024:
            raise ValueError('参考图不能超过 8 MiB')
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in ('JPEG', 'PNG', 'GIF', 'WEBP'):
                    raise ValueError('仅支持 JPEG、PNG、GIF、WebP')
            data = normalize_image(raw)
        except (ValueError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError('图片内容或尺寸无效') from exc
        previous = self.db.get('role_image', replace_id, {}) if replace_id else {}
        if replace_id and (previous.get('persona') != persona or previous.get('source_type') != 'admin_upload'):
            raise ValueError('参考图不存在或不属于当前角色')
        ref = {**previous, 'id': replace_id or uid(), 'persona': persona, 'source_type': 'admin_upload',
               'label': str(label or previous.get('label') or '角色参考图')[:80],
               'enabled': previous.get('enabled', True), 'sha256': hashlib.sha256(data).hexdigest(),
               'mime': 'image/jpeg', 'bytes': len(data), 'revision': previous.get('revision', 0)+1,
               'ts': time.time()}
        ref['image_id'] = 'ref_'+ref['id']
        temporary = self.path(ref).with_suffix('.tmp')
        temporary.write_bytes(data)
        temporary.replace(self.path(ref))
        try:
            self.db.put('role_image', ref['id'], ref)
        except Exception:
            if not previous or self.path(previous) != self.path(ref):
                self.path(ref).unlink(missing_ok=True)
            raise
        if previous and self.path(previous) != self.path(ref):
            self.path(previous).unlink(missing_ok=True)
        self.db.audit('', 'persona_image_upload', {'id': ref['id'], 'persona': persona, 'revision': ref['revision']})
        return ref

    def current(self, snapshot):
        ref = self.db.get('role_image', snapshot['id'], {})
        return (ref.get('persona') == snapshot.get('persona') and ref.get('enabled', True)
                and ref.get('revision') == snapshot.get('revision') and ref.get('sha256') == snapshot.get('sha256')
                and self.available(ref))

    def read(self, persona, ref_id):
        ref = self.db.get('role_image', ref_id, {})
        if ref.get('persona') != persona or not ref.get('enabled', True) or not self.available(ref):
            raise MediaError('reference_unavailable', '后台角色参考图不存在、已停用或文件不可读取')
        data = self.path(ref).read_bytes()
        return ref, {'type': 'image_url', 'image_url': {
            'url': 'data:image/jpeg;base64,'+base64.b64encode(data).decode(), 'detail': 'auto'}}

    def delete(self, persona, ref_id):
        ref = self.db.get('role_image', ref_id, {})
        if ref.get('persona') != persona or ref.get('source_type') != 'admin_upload':
            raise ValueError('后台参考图不存在')
        self.db.delete('role_image', ref_id)
        self.path(ref).unlink(missing_ok=True)
        self.db.audit('', 'persona_image_remove', {'id': ref_id, 'persona': persona})
