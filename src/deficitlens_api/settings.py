from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATA_DIR = str(Path(__file__).resolve().parents[2] / "data" / "app")

def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")

def _origins(raw: str) -> tuple[str, ...]:
    return tuple(o.strip().rstrip("/") for o in raw.split(",") if o.strip() and o.strip() != "*")

def _proxies(raw: str) -> tuple[str, ...]:
    from .security import parse_networks

    items = tuple(x.strip() for x in raw.split(",") if x.strip())
    parse_networks(items)
    return items

def _int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default

@dataclass
class Settings:
    api_keys: tuple[str, ...] = field(default=(), repr=False)
    ui_rate_per_min: int = 30
    max_upload_mb: int = 10
    max_batch: int = 10000
    host: str = "0.0.0.0"
    port: int = 8080
    max_json_kb: int = 256
    generated_key: bool = field(default=False)
    data_dir: str = DEFAULT_DATA_DIR
    data_key: str | None = field(default=None, repr=False)
    data_key_file: str | None = None
    data_max_mb: int = 512
    max_signups_per_day: int = 1000
    signup_ip_per_hour: int = 30
    cookie_secure: bool = False
    cors_origins: tuple[str, ...] = ()
    trusted_proxies: tuple[str, ...] = ()
    auth_rate_per_min: int = 10
    auth_ip_rate_per_min: int = 60
    cabinet_rate_per_min: int = 240

    @property
    def cookie_name(self) -> str:
        return "__Host-jm_session" if self.cookie_secure else "jm_session"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        keys = tuple(k.strip() for k in os.environ.get("DL_API_KEYS", "").split(",") if k.strip())
        return cls(
            api_keys=keys,
            ui_rate_per_min=_int("DL_UI_RATE_PER_MIN", 30),
            max_upload_mb=_int("DL_MAX_UPLOAD_MB", 10),
            max_batch=_int("DL_MAX_BATCH", 10000),
            host=os.environ.get("DL_HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=_int("DL_PORT", 8080),
            data_dir=os.environ.get("DL_DATA_DIR", "").strip() or DEFAULT_DATA_DIR,
            data_key=os.environ.get("DL_DATA_KEY", "").strip() or None,
            data_key_file=os.environ.get("DL_DATA_KEY_FILE", "").strip() or None,
            data_max_mb=_int("DL_DATA_MAX_MB", 512),
            max_signups_per_day=_int("DL_MAX_SIGNUPS_PER_DAY", 1000),
            signup_ip_per_hour=_int("DL_SIGNUP_IP_PER_HOUR", 30),
            cookie_secure=_flag("DL_COOKIE_SECURE"),
            cors_origins=_origins(os.environ.get("DL_CORS_ORIGINS", "")),
            trusted_proxies=_proxies(os.environ.get("DL_TRUSTED_PROXIES", "")),
        )

    def ensure_key(self) -> "Settings":
        if not self.api_keys:
            self.api_keys = (secrets.token_urlsafe(24),)
            self.generated_key = True
        return self
