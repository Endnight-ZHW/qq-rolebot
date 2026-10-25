import asyncio
import hashlib
import io
import ipaddress
import socket
from urllib.parse import urlsplit, urljoin

import httpx
from PIL import Image

Image.MAX_IMAGE_PIXELS = 20_000_000
MAX_SOURCE_BYTES = 32 * 1024 * 1024


class MediaError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def image_error(exc):
    """Safe diagnostic codes only: never persist signed QQ URLs or credentials."""
    if isinstance(exc, MediaError): return exc.code
    if isinstance(exc, httpx.TimeoutException): return "download_timeout"
    if isinstance(exc, httpx.HTTPStatusError): return f"download_http_{exc.response.status_code}"
    if isinstance(exc, httpx.RequestError): return "download_network"
    if isinstance(exc, ValueError): return "source_rejected"
    return "image_unavailable"


async def public_url(url, qq_only=False):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.port not in (None, 80, 443):
        raise ValueError("仅支持公网 HTTP(S) 地址")
    host = parts.hostname.lower().rstrip(".")
    if qq_only and host != "multimedia.nt.qq.com.cn" and not any(host == x or host.endswith("."+x) for x in ("qq.com", "qpic.cn", "gtimg.cn", "gtimg.com")):
        raise ValueError("图片来源不在 QQ 媒体域名范围内")
    addresses = await asyncio.get_running_loop().getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("不允许读取内网地址")
    return url


def normalize_image(raw, *, max_bytes=8*1024*1024):
    if len(raw) > max_bytes:
        raise ValueError("图片超过处理大小上限")
    with Image.open(io.BytesIO(raw)) as im:
        im.seek(0)
        im.thumbnail((1600, 1600))
        im = im.convert("RGB")
        out = io.BytesIO(); im.save(out, "JPEG", quality=85)
        return out.getvalue()


class Media:
    def __init__(self, settings):
        self.root = settings.data_dir / "media"
        self.root.mkdir(exist_ok=True)
        self.decode_lock = asyncio.Semaphore(1)

    async def image(self, url, persist=True):
        await public_url(url, qq_only=True)
        key = hashlib.sha256(url.encode()).hexdigest()
        # Version the cache rather than treating old JPEG conversions as originals.
        path = self.root / (key + ".source-v2")
        if path.exists():
            raw = path.read_bytes()
        else:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=12), follow_redirects=False) as client:
                target = url
                for attempt in range(4):
                    await public_url(target, qq_only=True)
                    async with client.stream("GET", target) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location or attempt == 3:
                                raise MediaError("redirect_limit", "图片跳转次数过多或地址无效")
                            target = urljoin(target, location)
                            continue
                        response.raise_for_status()
                        length = response.headers.get('content-length', '')
                        if length.isdigit() and int(length) > MAX_SOURCE_BYTES:
                            raise MediaError("image_too_large", "原图超过 32 MiB，请压缩文件后再发")
                        chunks, size = [], 0
                        async for part in response.aiter_bytes(64*1024):
                            size += len(part)
                            if size > MAX_SOURCE_BYTES:
                                raise MediaError("image_too_large", "原图超过 32 MiB，请压缩文件后再发")
                            chunks.append(part)
                        break
            raw = b"".join(chunks)
            del chunks
        from .image_assets import prepare
        try:
            async with self.decode_lock:
                block = await asyncio.to_thread(prepare, raw)
        except MediaError:
            raise
        except Exception as exc:
            raise MediaError("image_invalid", "图片解码失败或尺寸超出限制") from exc
        if persist and not path.exists():
            path.write_bytes(raw)
        return block
