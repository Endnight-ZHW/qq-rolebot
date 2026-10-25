"""Lossless screenshot previews and source-bound, non-generative region reads."""
import base64
import hashlib
import io
import math

from PIL import Image, ImageOps

from .media import MAX_SOURCE_BYTES, MediaError, normalize_image

VERSION = 2
MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000


def decoded(raw, max_bytes=MAX_SOURCE_BYTES):
    if len(raw) > max_bytes:
        raise MediaError('image_too_large', '原图超过处理大小上限')
    try:
        im = Image.open(io.BytesIO(raw))
        if im.width * im.height > MAX_PIXELS:
            im.close()
            raise MediaError('image_dimensions_exceeded', '原图超过 2000 万像素，请缩小尺寸后再发')
        if im.format not in ('PNG', 'JPEG', 'WEBP', 'GIF'):
            im.close()
            raise ValueError('不支持的图片格式')
        im.seek(0)
        im.load()
        return im
    except MediaError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise MediaError('image_invalid', '图片格式或尺寸无效') from exc


def mime(raw):
    with decoded(raw, MAX_BYTES) as im:
        return Image.MIME[im.format]


class ImageBlock(dict):
    """Private source bytes are attributes, never serialized into model input."""
    def __init__(self, raw, source=None, metadata=None, *, source_path=None):
        super().__init__(type='image_url', image_url={
            'url': 'data:'+mime(raw)+';base64,'+base64.b64encode(raw).decode(), 'detail': 'auto'})
        self._source, self._source_path = source, source_path
        self.metadata = metadata or {}

    @property
    def source(self):
        # Persisted originals are only loaded when a region read needs them.
        return self._source_path.read_bytes() if self._source_path else self._source


def prepare(raw):
    with decoded(raw) as original:
        fmt = original.format
        orientation = original.getexif().get(274, 1)
        im = ImageOps.exif_transpose(original) if orientation != 1 else original
        source_size = [im.width, im.height]
        # Existing preview dimensions stay bounded; region reads use the source.
        keep = len(raw) <= MAX_BYTES and max(im.size) <= 1600 and fmt in ('PNG', 'JPEG', 'WEBP') and orientation == 1 and not getattr(original, 'is_animated', False)
        if keep:
            preview = raw
        else:
            im.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            if fmt == 'JPEG':
                im.convert('RGB').save(out, 'JPEG', quality=85)
            else:
                im.convert('RGBA' if 'A' in im.getbands() else 'RGB').save(out, 'PNG')
            preview = out.getvalue()
            if len(preview) > MAX_BYTES:
                raise MediaError('image_too_large', '保真预览超过图片大小预算')
        metadata = {'image_processing_version': VERSION, 'source_size': source_size,
                    'preview_size': [im.width, im.height], 'mime': mime(preview),
                    'source_mime': Image.MIME[fmt], 'source_sha256': hashlib.sha256(raw).hexdigest()}
        if im is not original:
            im.close()
    # Do not keep multiple full-resolution decoded copies alive at once.
    # This preserves matching with existing JPEG-normalized role references.
    metadata['reference_sha256'] = hashlib.sha256(normalize_image(raw, max_bytes=MAX_SOURCE_BYTES)).hexdigest()
    return ImageBlock(preview, raw, metadata)


def region(raw, box):
    if (len(box) != 4 or any(type(v) is not int or not 0 <= v <= 1000 for v in box)
            or box[0] >= box[2] or box[1] >= box[3]):
        raise MediaError('invalid_region', '区域应为 0～1000 的左、上、右、下坐标，且具有正面积')
    with decoded(raw) as original:
        im = ImageOps.exif_transpose(original) if original.getexif().get(274, 1) != 1 else original
        x1, y1, x2, y2 = box
        bounds = (math.floor(x1*im.width/1000), math.floor(y1*im.height/1000),
                  math.ceil(x2*im.width/1000), math.ceil(y2*im.height/1000))
        if bounds[2]-bounds[0] < 4 or bounds[3]-bounds[1] < 4:
            raise MediaError('invalid_region', '区域过小，请包含完整文字及周边')
        crop = im.crop(bounds)
        crop.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        crop.convert('RGBA' if 'A' in crop.getbands() else 'RGB').save(out, 'PNG')
        raw_crop = out.getvalue()
        if len(raw_crop) > MAX_BYTES:
            raise MediaError('image_too_large', '局部图超过图片大小预算')
        return ImageBlock(raw_crop), {'region': list(box), 'source_size': list(im.size),
            'pixel_bounds': list(bounds), 'view_size': list(crop.size)}
