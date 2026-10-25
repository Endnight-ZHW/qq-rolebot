import hashlib
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit
from .config_schema import GLOBAL_NUMBERS

DEFAULTS = {
    "admin_qq": [], "private_enabled": False, "initialized": False,
    "context_tokens": 256000,
    "gate_max_messages": 80, "gate_context_budget": 32000,
    "working_initial_messages": 40, "working_max_messages": 80, "working_context_budget": 32000,
    "learning_idle_seconds": 600, "learning_max_seconds": 7200,
    "image_max_count": 12, "image_input_budget": 49152, "image_request_mib": 16,
    "retention_days": 30, "media_hours": 24, "model_log_days": 7, "model_log_image_hours": 24,
    "prices": {"input": 0.30, "cached": 0.006, "output": 1.20, "search": 0.00075, "fetch": 0.00075},
    "currency": "USD", "nickname": "机器人",
}

DEFAULTS.update(GLOBAL_NUMBERS)

DEFAULT_PERSONA = {
    "name": "机器人", "description": "", "prompt": "",
    "relationships": {}, "aliases": [],
}


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return f"{salt}:{digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        return secrets.compare_digest(password_hash(password, stored.split(":")[0]), stored)
    except (ValueError, IndexError):
        return False


def normalize_origin(value):
    """Explicit browser origins only; never derive trust from forwarding headers."""
    parts = urlsplit(value)
    if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None
        or parts.password is not None or parts.path not in ('', '/') or parts.query or parts.fragment
        or '*' in value or any(c.isspace() for c in value)):
        raise ValueError('后台访问来源须为明确的 http(s)://主机[:端口]，不能包含路径或通配符')
    host = parts.hostname.lower()
    if ':' in host: host = '['+host+']'
    port = parts.port
    if port is not None and port != (443 if parts.scheme == 'https' else 80):host += ':'+str(port)
    return parts.scheme+'://'+host


class Settings:
    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir or os.getenv("DATA_DIR", "data")).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.data_dir.chmod(0o700)
        except OSError:
            pass
        self.public_url = normalize_origin(os.getenv("PUBLIC_BASE_URL", "http://localhost:8080"))
        self.allowed_origins = {self.public_url} | {
            normalize_origin(value.strip()) for value in os.getenv('ADMIN_ADDITIONAL_ORIGINS', '').split(',') if value.strip()}
        self.secure_cookie = os.getenv("COOKIE_SECURE", "true").lower() != "false"
        self.secret_file = self.data_dir / "secrets.json"
        if self.secret_file.exists():
            self.secret = json.loads(self.secret_file.read_text("utf-8"))
        else:
            password = os.getenv("ADMIN_PASSWORD") or secrets.token_urlsafe(24)
            self.secret = {"password_hash": password_hash(password), "api_key": os.getenv("DEEPSEEK_API_KEY", ""),
                           "onebot_token": os.getenv("ONEBOT_ACCESS_TOKEN") or secrets.token_urlsafe(32)}
            self.save_secrets()
            bootstrap = self.data_dir / "bootstrap-password.txt"
            bootstrap.write_text(password + "\n", encoding="utf-8")
            bootstrap.chmod(0o600)

    def save_secrets(self):
        temporary = self.secret_file.with_suffix(".tmp")
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.secret, f)
        temporary.replace(self.secret_file)
        self.secret_file.chmod(0o600)

    def set_key(self, key: str):
        self.secret["api_key"] = key.strip()
        self.save_secrets()
