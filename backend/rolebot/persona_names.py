"""Names are operator data, never hard-coded character identities."""
import re


def names(db, persona):
    card = db.get('persona', persona, {})
    values = [card.get('name'), *card.get('aliases', [])]
    return sorted({v.strip() for v in values if isinstance(v, str) and v.strip()}, key=lambda v: (-len(v), v))


def pattern(db, persona, *, extra=()):
    values = names(db, persona) + list(extra)
    return '(?:' + '|'.join(re.escape(v) for v in dict.fromkeys(values) if v) + ')' if values else r'(?!)'


def strip_address(text, db, persona):
    text = re.sub(r'^(?:\s|\[@QQ:\d+\])*', '', text)
    return re.sub(r'^' + pattern(db, persona) + r'(?:[，,：:！!\s]+|(?=帮|请|给我|能不能|可以|制作|对比|不用|取消|停止))', '', text, flags=re.I)
