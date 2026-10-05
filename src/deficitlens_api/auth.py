from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import unicodedata
from typing import Any, Optional
from urllib.parse import urlsplit

from .service import ServiceError

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN, SALT_BYTES = 2 ** 14, 8, 1, 32, 16
SCRYPT_MAXMEM = 64 * 1024 * 1024
LOGIN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9._@-]{3,64}")
LATIN_RE = re.compile(r"[A-Za-z]")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
PASSWORD_MIN, PASSWORD_MAX = 8, 128
CLINIC_MAX = 120
ROLES = ("patient", "doctor")
COOKIE_TTL = 7 * 86400
LOGIN_TTL = COOKIE_TTL
API_TTL = 30 * 86400
BAD_TEXT_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
CSRF_HEADER = "x-justmedit"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
BAD_CREDENTIALS = "Неверный логин или пароль."

def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")

def hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"

def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        got = hashlib.scrypt(password.encode("utf-8", "surrogateescape"), salt=base64.b64decode(salt), n=int(n),
                             r=int(r), p=int(p), dklen=len(expected), maxmem=SCRYPT_MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)

_DUMMY_HASH: Optional[str] = None

def ensure_dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH

def dummy_verify(password: str) -> None:
    verify_password(password if isinstance(password, str) else "", ensure_dummy_hash())

def login_key(login: str) -> str:
    return login.casefold()

def valid_login(login) -> bool:
    return isinstance(login, str) and bool(LOGIN_RE.fullmatch(login))

def check_login(login) -> str:
    if not valid_login(login):
        raise ServiceError(422, "invalid_login", "Логин — от 3 до 64 символов: буквы, цифры и знаки «._-@».", "login")
    if LATIN_RE.search(login) and CYRILLIC_RE.search(login):
        raise ServiceError(422, "invalid_login", "Логин — либо латиницей, либо кириллицей: смешивать алфавиты "
                                                 "нельзя (похожие буквы делают логины неотличимыми).", "login")
    return login

def clean_text(value: Any, field: str, max_len: int, *, code: str = "invalid_value",
               default: Optional[str] = None, required: bool = True) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is not None:
            return default
        if not required:
            return None
        raise ServiceError(422, "missing_field", "Не указано обязательное поле.", field)
    if not isinstance(value, str):
        raise ServiceError(422, code, f"Строка до {max_len} символов без управляющих знаков.", field)
    text = value.strip()
    if len(text) > max_len or any(unicodedata.category(ch) in BAD_TEXT_CATEGORIES for ch in text):
        raise ServiceError(422, code, f"Строка до {max_len} символов без управляющих и невидимых знаков.", field)
    return text

def check_password(password, field: str = "password") -> str:
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise ServiceError(422, "invalid_password",
                           f"Пароль — от {PASSWORD_MIN} до {PASSWORD_MAX} символов.", field)
    try:
        password.encode("utf-8")
    except UnicodeEncodeError:
        raise ServiceError(422, "invalid_password", "Пароль содержит недопустимые символы.", field) from None
    return password

def check_role(role) -> str:
    if role not in ROLES:
        raise ServiceError(422, "invalid_role", "Роль: patient (пациент) или doctor (врач).", "role")
    return role

def check_clinic(clinic, role: str) -> Optional[str]:
    if role != "doctor" or clinic is None:
        return None
    return clean_text(clinic, "clinic", CLINIC_MAX, code="invalid_clinic", required=False)

def login_hash(login) -> str:
    lk = login_key(login) if isinstance(login, str) else ""
    return hashlib.sha256(lk.encode("utf-8", "surrogateescape")).hexdigest()[:16]

def rate_key(client: str, login) -> str:
    return client + "|" + login_hash(login)

def bearer_token(headers) -> Optional[str]:
    raw = headers.get("authorization")
    if raw is None:
        return None
    scheme, _, value = raw.strip().partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return ""
    return value.strip()

def unauthorized(message: str = "Войдите в кабинет: нужен действующий токен (Authorization: Bearer …) "
                                "или вход на сайте.") -> ServiceError:
    err = ServiceError(401, "unauthorized", message)
    err.www_authenticate = 'Bearer realm="Justmedit"'
    return err

def forbidden(message: str = "Нет доступа.") -> ServiceError:
    return ServiceError(403, "forbidden", message)

DEFAULT_PORTS = {"http": 80, "https": 443}

def origin_of(url: str) -> Optional[tuple[str, str, int]]:
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if scheme not in DEFAULT_PORTS or not host or parts.username is not None or parts.password is not None:
        return None
    return scheme, host.lower(), port or DEFAULT_PORTS[scheme]

def _same_origin(url: str, scheme: str, host_header: str) -> bool:
    if not host_header:
        return False
    own, got = origin_of(f"{scheme}://{host_header}"), origin_of(url)
    if own is None or got is None:
        return False
    if got == own:
        return True
    try:
        explicit_port = urlsplit(f"{scheme}://{host_header}").port is not None
    except ValueError:
        return False
    return own[0] == "http" and got[0] == "https" and got[1] == own[1] and got[2] == 443 and not explicit_port

def check_csrf(method: str, headers, scheme: str = "http") -> None:
    if method.upper() not in MUTATING:
        return
    err = ServiceError(403, "csrf", "Запрос отклонён: нет защиты от подделки запроса (заголовок X-Justmedit) "
                                    "или он пришёл с чужого сайта.")
    if headers.get(CSRF_HEADER) != "1":
        raise err
    host = (headers.get("host") or "").strip()
    origin = headers.get("origin")
    if origin and origin.strip().lower() != "null":
        if not _same_origin(origin, scheme, host):
            raise err
        return
    referer = headers.get("referer")
    if referer and not _same_origin(referer, scheme, host):
        raise err
