from __future__ import annotations

import contextvars
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import re
import threading
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterable, Iterator, Optional

import anyio

from .service import ServiceError

LOGGER_NAME = "deficitlens.access"
log = logging.getLogger(LOGGER_NAME)

TILES = "https://tile.openstreetmap.org"
CSP = (f"default-src 'self'; img-src 'self' data: {TILES}; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")
SECURITY_HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=(self)",
}
NO_STORE_PREFIXES = ("/api/", "/ui/", "/healthz")
CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_FONTS = "public, max-age=604800"
HSTS = "max-age=31536000"
_VERSION_QUERY = re.compile(rb"(?:^|&)v=[^&]+")

def cache_control_for(path: str, query: bytes, status: int) -> str:
    if path.startswith(NO_STORE_PREFIXES):
        return "no-store"
    if path.startswith("/static/") and status in (200, 304):
        if path.startswith("/static/fonts/"):
            return CACHE_FONTS
        if _VERSION_QUERY.search(query or b""):
            return CACHE_IMMUTABLE
    return "no-cache"

def setup_logging() -> None:
    logger = logging.getLogger("deficitlens")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).digest()

def check_api_key(presented: Optional[str], keys: Iterable[str]) -> None:
    if not presented:
        raise ServiceError(401, "unauthorized", "Нужен ключ API (заголовок X-API-Key) или токен кабинета "
                                                "(Authorization: Bearer <токен>).")
    given = _digest(presented)
    ok = False
    for key in keys:
        ok = hmac.compare_digest(given, _digest(key)) | ok
    if not ok:
        raise ServiceError(403, "forbidden", "Ключ API не подходит.")

def key_id(presented: Optional[str]) -> str:
    return "key:" + hashlib.sha256((presented or "").strip().encode("utf-8", "surrogateescape")).hexdigest()[:16]

class RateLimiter:

    def __init__(self, limit: int, window: float = 60.0, max_clients: int = 20000):
        self.limit, self.window, self.max_clients = int(limit), float(window), max_clients
        self._hits: OrderedDict[str, deque] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, client: str, now: Optional[float] = None) -> Optional[int]:
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits.get(client)
            if q is None:
                if len(self._hits) >= self.max_clients:
                    self._purge(now)
                q = self._hits[client] = deque()
            else:
                self._hits.move_to_end(client)
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return max(1, int(self.window - (now - q[0])) + 1)
            q.append(now)
            return None

    def release(self, client: str) -> None:
        with self._lock:
            q = self._hits.get(client)
            if q:
                q.pop()

    def _purge(self, now: float) -> None:
        for key in [k for k, q in self._hits.items() if not q or now - q[-1] >= self.window]:
            del self._hits[key]
        target = max(0, int(self.max_clients * 0.9))
        while len(self._hits) > target:
            self._hits.popitem(last=False)

GUARD_FAILURES = 10
GUARD_WINDOW = 15 * 60.0
GUARD_BASE_LOCK = 60.0
GUARD_MAX_LOCK = 15 * 60.0

class LoginGuard:

    def __init__(self, failures: int = GUARD_FAILURES, window: float = GUARD_WINDOW, base: float = GUARD_BASE_LOCK,
                 cap: float = GUARD_MAX_LOCK, max_keys: int = 20000):
        self.failures, self.window, self.base, self.cap, self.max_keys = int(failures), window, base, cap, max_keys
        self._state: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, key: str, now: Optional[float] = None) -> Optional[int]:
        now = time.monotonic() if now is None else now
        with self._lock:
            st = self._state.get(key)
            if st is None or now >= st["until"]:
                return None
            return max(1, math.ceil(st["until"] - now))

    def failure(self, key: str, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            st = self._state.get(key)
            if st is None:
                if len(self._state) >= self.max_keys:
                    self._purge(now)
                st = self._state[key] = {"hits": deque(), "until": 0.0, "level": 0, "last": now}
            else:
                self._state.move_to_end(key)
            if now - max(st["last"], st["until"]) >= self.window:
                st["level"] = 0
            st["last"] = now
            q = st["hits"]
            while q and now - q[0] >= self.window:
                q.popleft()
            q.append(now)
            if len(q) >= self.failures:
                st["level"] += 1
                st["until"] = now + min(self.base * 2 ** (st["level"] - 1), self.cap)
                q.clear()

    def success(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)

    def _purge(self, now: float) -> None:
        for key in [k for k, st in self._state.items() if now - max(st["last"], st["until"]) >= self.window]:
            del self._state[key]
        target = max(0, int(self.max_keys * 0.9))
        while len(self._state) > target:
            self._state.popitem(last=False)

def client_key(host: Optional[str]) -> str:
    if not host:
        return "unknown"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return str(host)[:64]
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)

def request_client(request) -> str:
    return client_key(request.client.host if request.client else None)

def parse_networks(items: Iterable[str]) -> tuple:
    out = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        try:
            out.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise ValueError(f"DL_TRUSTED_PROXIES: «{item}» — не адрес и не сеть (пример: 127.0.0.1, 10.0.0.0/8)") \
                from None
    return tuple(out)

_HOST_RE = re.compile(r"[A-Za-z0-9.\-]{1,253}(:[0-9]{1,5})?|\[[0-9A-Fa-f:.]{2,45}\](:[0-9]{1,5})?")

def _strip_port(value: str) -> str:
    value = value.strip()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else value
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value

class ProxyHeadersMiddleware:

    def __init__(self, app, trusted: Iterable = ()):
        self.app = app
        self.networks = tuple(trusted)

    def trusted(self, host: Optional[str]) -> bool:
        if not host or not self.networks:
            return False
        try:
            ip = ipaddress.ip_address(_strip_port(host))
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return any(ip.version == n.version and ip in n for n in self.networks)

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and self.networks:
            client = scope.get("client")
            if client and self.trusted(client[0]):
                scope = self._apply(scope)
        await self.app(scope, receive, send)

    def _apply(self, scope: dict) -> dict:
        values: dict[bytes, list[str]] = {}
        for k, v in scope.get("headers", []):
            values.setdefault(k.lower(), []).append(v.decode("latin-1"))

        def items(name: bytes) -> list[str]:
            return [x.strip() for x in ",".join(values.get(name, [])).split(",") if x.strip()]

        scope = dict(scope)
        chain = items(b"x-forwarded-for")
        if chain:
            real = None
            for entry in reversed(chain):
                if not self.trusted(entry):
                    real = entry
                    break
            real = _strip_port(real if real is not None else chain[0])
            try:
                ipaddress.ip_address(real)
            except ValueError:
                real = None
            if real is not None:
                scope["client"] = (real, 0)
        proto = items(b"x-forwarded-proto")
        if proto and proto[-1].lower() in ("http", "https"):
            scope["scheme"] = proto[-1].lower()
        host = items(b"x-forwarded-host")
        if host and _HOST_RE.fullmatch(host[-1]):
            headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"host"]
            scope["headers"] = [(b"host", host[-1].encode("latin-1"))] + headers
        return scope

MAX_JSON_DEPTH = 32

def _depth_ok(value: Any, limit: int) -> bool:
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        if depth > limit:
            return False
        stack.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return True

def parse_json_body(body: bytes) -> Any:
    err = ServiceError(422, "invalid_json", "Тело запроса должно быть корректным JSON в кодировке UTF-8 "
                                            f"с вложенностью не глубже {MAX_JSON_DEPTH}.")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise err from None
    if not _depth_ok(data, MAX_JSON_DEPTH):
        raise err
    return data

def rate_limit_error(retry_after: int, message: Optional[str] = None) -> ServiceError:
    err = ServiceError(429, "rate_limited",
                       message or f"Слишком много запросов с вашего адреса. Повторите через {retry_after} с.")
    err.retry_after = retry_after
    return err

HEAVY_CONCURRENCY = 2
HEAVY_PER_CLIENT = 1
CLIENT_WAIT_SECONDS = 6.0
BUSY_RETRY_AFTER = 5

current_client: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("deficitlens_client", default=None)

def _busy(own: bool = False) -> ServiceError:
    if own:
        err = ServiceError(503, "service_busy", "Ваш предыдущий файл ещё обрабатывается: дождитесь результата "
                                                "и повторите.")
    else:
        err = ServiceError(503, "service_busy",
                           "Сервис занят обработкой других файлов. Повторите через несколько секунд.")
    err.retry_after = BUSY_RETRY_AFTER
    return err

class HeavyGate:

    def __init__(self, slots: int = HEAVY_CONCURRENCY, per_client: int = HEAVY_PER_CLIENT,
                 client_wait: float = CLIENT_WAIT_SECONDS):
        self.slots = int(slots)
        self.per_client = max(1, int(per_client))
        self.client_wait = float(client_wait)
        self._sem = threading.BoundedSemaphore(self.slots)
        self._lock = threading.Lock()
        self._active: dict[str, int] = {}

    def _take_client(self, client: Optional[str]) -> bool:
        if client is None:
            return True
        with self._lock:
            n = self._active.get(client, 0)
            if n >= self.per_client:
                return False
            self._active[client] = n + 1
            return True

    def _release_client(self, client: Optional[str]) -> None:
        if client is None:
            return
        with self._lock:
            n = self._active.get(client, 0) - 1
            if n > 0:
                self._active[client] = n
            else:
                self._active.pop(client, None)

    def _take_slot(self, client: Optional[str]) -> None:
        if not self._sem.acquire(blocking=False):
            self._release_client(client)
            raise _busy()

    def _release(self, client: Optional[str]) -> None:
        self._sem.release()
        self._release_client(client)

    @contextmanager
    def slot(self, client: Optional[str] = None) -> Iterator[None]:
        client = client if client is not None else current_client.get()
        if not self._take_client(client):
            raise _busy(own=True)
        self._take_slot(client)
        try:
            yield
        finally:
            self._release(client)

    @asynccontextmanager
    async def aslot(self, client: Optional[str] = None) -> AsyncIterator[None]:
        client = client if client is not None else current_client.get()
        deadline = time.monotonic() + self.client_wait
        while not self._take_client(client):
            if time.monotonic() >= deadline:
                raise _busy(own=True)
            await anyio.sleep(0.05)
        self._take_slot(client)
        try:
            yield
        finally:
            self._release(client)

async def read_body(request, limit: int) -> bytes:
    too_large = ServiceError(413, "payload_too_large",
                             f"Слишком большой запрос: допустимо не больше {_human(limit)}.")
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > limit:
                raise too_large
        except ValueError:
            raise ServiceError(400, "bad_request", "Некорректный заголовок Content-Length.") from None
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)

def _human(n: int) -> str:
    return f"{n // (1024 * 1024)} МБ" if n >= 1024 * 1024 else f"{max(1, n // 1024)} КБ"

class SecurityMiddleware:

    def __init__(self, app, hsts: bool = False):
        self.app = app
        self.headers = [(k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
        if hsts:
            self.headers.append((b"strict-transport-security", HSTS.encode()))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        t0 = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                present = {k.lower() for k, _ in message.get("headers", [])}
                headers = list(message.get("headers", []))
                headers.extend((k, v) for k, v in self.headers if k not in present)
                if b"cache-control" not in present:
                    cache = cache_control_for(path, scope.get("query_string", b""), message["status"])
                    headers.append((b"cache-control", cache.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            log.info("%s %r %d %.1fms", method, path[:200], status["code"], 1000 * (time.perf_counter() - t0))

CORS_PREFIX = "/api/v1/"
CORS_METHODS = "GET, POST, PATCH, DELETE"
CORS_HEADERS = "Authorization, Content-Type, X-API-Key"
CORS_EXPOSE = "Retry-After, Content-Disposition"
CORS_MAX_AGE = "600"

class CorsMiddleware:

    def __init__(self, app, origins: Iterable[str] = ()):
        self.app = app
        self.origins = frozenset(o.strip().rstrip("/") for o in origins if o and o.strip())

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(CORS_PREFIX):
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        raw_origin = headers.get(b"origin")
        if raw_origin is None:
            await self.app(scope, receive, send)
            return
        origin = raw_origin.decode("latin-1").strip()
        allowed = origin in self.origins
        if scope.get("method") == "OPTIONS" and b"access-control-request-method" in headers:
            if allowed:
                await _send_plain(send, 204, b"", [
                    (b"access-control-allow-origin", origin.encode("latin-1")), (b"vary", b"Origin"),
                    (b"access-control-allow-methods", CORS_METHODS.encode()),
                    (b"access-control-allow-headers", CORS_HEADERS.encode()),
                    (b"access-control-max-age", CORS_MAX_AGE.encode())])
            else:
                body = ('{"error":{"code":"cors_forbidden","message":"Запросы с этого сайта не разрешены '
                        '(DL_CORS_ORIGINS).","field":null}}').encode()
                await _send_plain(send, 403, body, [(b"content-type", b"application/json"), (b"vary", b"Origin")])
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                extra = [(b"vary", b"Origin")]
                if allowed:
                    extra += [(b"access-control-allow-origin", origin.encode("latin-1")),
                              (b"access-control-expose-headers", CORS_EXPOSE.encode())]
                message = {**message, "headers": list(message.get("headers", [])) + extra}
            await send(message)

        await self.app(scope, receive, send_wrapper)

GZIP_PREFIXES = ("/static/",)
GZIP_PATHS = frozenset({"/", "/benchmark", "/developers", "/login", "/cabinet", "/share",
                        "/api/v1/openapi.json", "/ui/reference", "/ui/examples"})
GZIP_MIN_SIZE = 1024
GZIP_LEVEL = 6

def gzip_allowed(path: str) -> bool:
    return path in GZIP_PATHS or path.startswith(GZIP_PREFIXES)

class GZipByPathMiddleware:

    def __init__(self, app, minimum_size: int = GZIP_MIN_SIZE, compresslevel: int = GZIP_LEVEL):
        from starlette.middleware.gzip import GZipMiddleware

        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size, compresslevel=compresslevel)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and gzip_allowed(scope.get("path", "")):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)

async def _send_plain(send, status: int, body: bytes, headers: list) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": headers + [(b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
