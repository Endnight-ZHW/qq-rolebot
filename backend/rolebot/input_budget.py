"""Separate text estimates, visual allowances and actual transport bytes."""
import json

IMAGE_ALLOWANCE = 4096  # Conservative across both configured provider protocols.


def measure(value):
    images = 0

    def text_size(node):
        nonlocal images
        if isinstance(node, dict):
            if node.get('type') in ('image_url', 'image'):
                images += 1
                return 0
            return sum(len(k) + text_size(v) for k, v in node.items() if not k.startswith('_'))
        if isinstance(node, list):
            return sum(text_size(x) for x in node)
        return len(node) if isinstance(node, str) else 1

    text = text_size(value)
    return {'text_estimate': text, 'image_count': images,
            'visual_estimate': images * IMAGE_ALLOWANCE,
            'estimated_input': text + images * IMAGE_ALLOWANCE,
            'request_bytes': len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')),
            'estimate_source': 'conservative_characters_plus_image_allowance' if images else 'conservative_characters'}


def projected(messages):
    return [({'role': m['role'], 'content': m['_native_content']} if m.get('_native_content') is not None else
             {k: v for k, v in m.items() if not k.startswith('_')}) for m in messages]


def check_images(metrics, config):
    from .context import ContextTooLarge
    if metrics['image_count'] > config['image_max_count']:
        raise ContextTooLarge('本轮图片数量超过图片预算')
    if metrics['visual_estimate'] > config['image_input_budget']:
        raise ContextTooLarge('本轮图片超过视觉输入预算')
    if metrics['request_bytes'] > config['image_request_mib'] * 1024 * 1024:
        raise ContextTooLarge('本轮请求体超过传输大小预算')
