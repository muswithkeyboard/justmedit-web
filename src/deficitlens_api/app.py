from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import mimetypes
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

import anyio
import anyio.lowlevel
import anyio.to_thread
from jinja2 import Environment, FileSystemLoader, select_autoescape

mimetypes.add_type("font/woff2", ".woff2")
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import service
from .security import (
    CACHE_FONTS,
    CorsMiddleware,
    GZipByPathMiddleware,
    HeavyGate,
    ProxyHeadersMiddleware,
    RateLimiter,
    SecurityMiddleware,
    current_client,
    parse_json_body,
    parse_networks,
    rate_limit_error,
    read_body,
    request_client,
    setup_logging,
)
from .service import ServiceError
from .settings import Settings

HERE = Path(__file__).resolve().parent
log = logging.getLogger("deficitlens.app")
MULTIPART_OVERHEAD = 64 * 1024
DISCLAIMER = "Исследовательский прототип, не медицинское изделие. Решение принимает врач."
ANALYZE_CONCURRENCY = 4
REFERENCE_CACHE = "public, max-age=300"
STATIC_VERSION_TTL = 2.0
NOT_API_PREFIXES = ("/api", "/ui", "/static")
INTERNAL_ERROR = {"error": {"code": "internal", "message": "Внутренняя ошибка", "field": None}}
_analyze_limiter: anyio.lowlevel.RunVar[anyio.CapacityLimiter] = anyio.lowlevel.RunVar("deficitlens_analyze")

def _json(data: Any, status: int = 200, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=headers)

def _error_response(e: ServiceError) -> JSONResponse:
    headers = {}
    if e.status in (429, 503):
        headers["Retry-After"] = str(getattr(e, "retry_after", 60 if e.status == 429 else 5))
    if e.status == 401:
        headers["WWW-Authenticate"] = getattr(e, "www_authenticate", 'ApiKey realm="Justmedit", header="X-API-Key"')
    return _json(e.body(), e.status, headers)

def _log_internal(exc: BaseException) -> None:
    frames = traceback.extract_tb(exc.__traceback__)
    where = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "?"
    log.error("internal error %s at %s", type(exc).__name__, where)
    try:
        exc._deficitlens_logged = True
    except Exception:
        pass

def _internal_error(exc: BaseException) -> JSONResponse:
    _log_internal(exc)
    return _json(INTERNAL_ERROR, 500)

def _analyze_capacity() -> anyio.CapacityLimiter:
    try:
        return _analyze_limiter.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(ANALYZE_CONCURRENCY)
        _analyze_limiter.set(limiter)
        return limiter

def _etag_matches(header: Optional[str], etag: str) -> bool:
    if not header:
        return False
    tags = [t.strip() for t in header.split(",")]
    return "*" in tags or any(t.removeprefix("W/") == etag for t in tags)

def _wants_html_404(request: Request) -> bool:
    path = request.url.path
    if any(path == p or path.startswith(p + "/") for p in NOT_API_PREFIXES):
        return False
    return "text/html" in request.headers.get("accept", "")

class _GuardedStarlette(Starlette):

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await super().__call__(scope, receive, send)
            return
        started = False

        async def guarded_send(message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await super().__call__(scope, receive, guarded_send)
        except Exception as exc:
            if not getattr(exc, "_deficitlens_logged", False):
                _log_internal(exc)
            if started:
                return
            body = json.dumps(INTERNAL_ERROR, ensure_ascii=False).encode("utf-8")
            with contextlib.suppress(Exception):
                await send({"type": "http.response.start", "status": 500, "headers": [
                    (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]})
                await send({"type": "http.response.body", "body": body})

def _parse_json(body: bytes) -> Any:
    return parse_json_body(body)

class _InMemoryMultiPartParser(MultiPartParser):
    spool_max_size = 1 << 40

async def _read_upload(request: Request, limit: int) -> tuple[dict[str, str], Optional[tuple[str, bytes]]]:
    body = await read_body(request, limit)

    async def stream():
        yield body

    parser = _InMemoryMultiPartParser(request.headers, stream(), max_files=1, max_fields=4,
                                      max_part_size=64 * 1024)
    try:
        form = await parser.parse()
    except MultiPartException:
        raise ServiceError(422, "invalid_form", "Не удалось разобрать форму multipart/form-data: "
                           "передайте один файл в поле file.", "file") from None
    try:
        fields = {k: v for k, v in form.multi_items() if isinstance(v, str)}
        upload = form.get("file")
        if isinstance(upload, UploadFile):
            return fields, (upload.filename or "", await upload.read())
        return fields, None
    finally:
        await form.close()

def create_app(settings: Optional[Settings] = None) -> Starlette:
    settings = (settings or Settings.from_env()).ensure_key()
    setup_logging()
    ui_limiter = RateLimiter(settings.ui_rate_per_min)
    heavy_key_limiter = RateLimiter(settings.ui_rate_per_min)
    gate = HeavyGate()
    from .cabinet_api import Cabinet
    from .store import Store
    store = Store(settings.data_dir, settings.data_key, key_file=settings.data_key_file,
                  max_bytes=settings.data_max_mb * 1024 * 1024, max_signups_per_day=settings.max_signups_per_day)
    cabinet = Cabinet(settings, store, gate, heavy_key_limiter)
    templates = Environment(loader=FileSystemLoader(str(HERE / "templates")),
                            autoescape=select_autoescape(["html"]))
    static_dir = HERE / "static"

    static_version = {"checked": -1e9, "stamp": None, "v": "0"}

    def asset_version() -> str:
        now = time.monotonic()
        if now - static_version["checked"] < STATIC_VERSION_TTL:
            return static_version["v"]
        files = sorted(p for p in static_dir.rglob("*") if p.is_file())
        stamp = tuple((p.relative_to(static_dir).as_posix(), st.st_size, st.st_mtime_ns)
                      for p in files for st in (p.stat(),))
        if stamp != static_version["stamp"]:
            digest = hashlib.sha256()
            for p in files:
                digest.update(p.relative_to(static_dir).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
            static_version.update(stamp=stamp, v=digest.hexdigest()[:12])
        static_version["checked"] = now
        return static_version["v"]

    def endpoint(handler: Callable, *, protected: bool = False, limited: bool = False,
                 heavy: bool = False) -> Callable:
        async def wrapped(request: Request) -> Response:
            client = request_client(request)
            token = current_client.set(client)
            try:
                ident = None
                if protected:
                    ident = await cabinet.authorize_api(request, settings.api_keys)
                if limited:
                    retry = ui_limiter.check(client)
                    if retry is not None:
                        raise rate_limit_error(retry)
                if heavy and protected:
                    retry = heavy_key_limiter.check(ident)
                    if retry is not None:
                        raise rate_limit_error(retry, (
                            f"Слишком много тяжёлых запросов (пакет, файл, разбор бланка): не больше "
                            f"{heavy_key_limiter.limit} в минуту на ключ. Повторите через {retry} с."))
                return await handler(request)
            except ServiceError as e:
                return _error_response(e)
            except HTTPException as e:
                return _json({"error": {"code": "bad_request", "message": "Некорректный запрос.", "field": None}},
                             e.status_code if 400 <= e.status_code < 500 else 400)
            except Exception as e:
                return _internal_error(e)
            finally:
                current_client.reset(token)
        return wrapped

    async def healthz(request: Request) -> Response:
        return _json(await run_in_threadpool(service.health))

    async def analyze(request: Request) -> Response:
        payload = _parse_json(await read_body(request, settings.max_json_kb * 1024))
        user = getattr(request.state, "cab_user", None) or await cabinet.optional_cookie_user(request)
        applied = await cabinet.apply_doctor_refs(user, payload)
        result = await anyio.to_thread.run_sync(service.analyze_one, payload, limiter=_analyze_capacity())
        return _json(cabinet.label_doctor_refs(result, applied))

    async def batch(request: Request) -> Response:
        payload = _parse_json(await read_body(request, settings.max_upload_bytes))
        if not isinstance(payload, dict):
            raise ServiceError(422, "invalid_body", "Тело запроса: {\"records\": [...], \"with_reports\": false}.")
        async with gate.aslot():
            result = await run_in_threadpool(service.analyze_many, payload.get("records"),
                                             bool(payload.get("with_reports", False)), settings.max_batch)
        return _json(result)

    async def benchmark(request: Request) -> Response:
        if "multipart/form-data" not in request.headers.get("content-type", ""):
            raise ServiceError(422, "file_required", "Передайте файл в поле file (multipart/form-data).", "file")
        fields, upload = await _read_upload(request, settings.max_upload_bytes + MULTIPART_OVERHEAD)
        if upload is None:
            raise ServiceError(422, "file_required", "Передайте файл в поле file (multipart/form-data).", "file")
        filename, content = upload
        mode = fields.get("mode") or request.query_params.get("mode") or "auto"
        if len(content) > settings.max_upload_bytes:
            raise ServiceError(413, "payload_too_large",
                               f"Файл больше {settings.max_upload_mb} МБ.", "file")
        async with gate.aslot():
            result = await run_in_threadpool(service.benchmark, content, filename, str(mode), settings.max_batch,
                                             fields.get("mapping"))
        if request.query_params.get("format") == "csv":
            return Response(result["csv"], media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": 'attachment; filename="deficitlens_pred.csv"'})
        return _json(result)

    async def columns(request: Request) -> Response:
        if "multipart/form-data" not in request.headers.get("content-type", ""):
            raise ServiceError(422, "file_required", "Передайте файл в поле file (multipart/form-data).", "file")
        _fields, upload = await _read_upload(request, settings.max_upload_bytes + MULTIPART_OVERHEAD)
        if upload is None:
            raise ServiceError(422, "file_required", "Передайте файл в поле file (multipart/form-data).", "file")
        filename, content = upload
        if len(content) > settings.max_upload_bytes:
            raise ServiceError(413, "payload_too_large", f"Файл больше {settings.max_upload_mb} МБ.", "file")
        async with gate.aslot():
            result = await run_in_threadpool(service.columns_preview, content, filename, settings.max_batch)
        return _json(result)

    async def parse(request: Request) -> Response:
        if "multipart/form-data" in request.headers.get("content-type", ""):
            _, upload = await _read_upload(request, settings.max_upload_bytes + MULTIPART_OVERHEAD)
            if upload is None:
                raise ServiceError(422, "file_required", "Передайте файл в поле file (multipart/form-data).", "file")
            filename, content = upload
            if len(content) > settings.max_upload_bytes:
                raise ServiceError(413, "payload_too_large", f"Файл больше {settings.max_upload_mb} МБ.", "file")
            async with gate.aslot():
                result = await run_in_threadpool(service.parse_file, content, filename)
            return _json(result)
        payload = _parse_json(await read_body(request, settings.max_json_kb * 1024))
        if not isinstance(payload, dict):
            raise ServiceError(422, "invalid_body", "Тело запроса: {\"text\": \"…\"}.", "text")
        return _json(await run_in_threadpool(service.parse_text, payload.get("text")))

    async def reference(request: Request) -> Response:
        return _json(await run_in_threadpool(service.reference))

    static_json: dict[str, tuple[bytes, str]] = {}

    def static_json_route(name: str, build: Callable[[], Any]) -> Callable:
        async def handler(request: Request) -> Response:
            item = static_json.get(name)
            if item is None:
                body = JSONResponse(await run_in_threadpool(build)).body
                item = static_json[name] = (body, '"' + hashlib.sha256(body).hexdigest()[:20] + '"')
            body, etag = item
            headers = {"ETag": etag, "Cache-Control": REFERENCE_CACHE}
            if _etag_matches(request.headers.get("if-none-match"), etag):
                return Response(status_code=304, headers=headers)
            return Response(body, media_type="application/json", headers=headers)
        return handler

    async def openapi_spec(request: Request) -> Response:
        return Response((static_dir / "openapi.json").read_bytes(), media_type="application/json")

    def render_page(request: Request, template: str, active: str, status: int = 200) -> HTMLResponse:
        html = templates.get_template(template).render(
            active=active, v=asset_version(), disclaimer=DISCLAIMER, versions=service.health(),
            max_upload_mb=settings.max_upload_mb, has_session=bool(request.cookies.get(settings.cookie_name)))
        return HTMLResponse(html, status_code=status)

    def page(template: str, active: str, login_required: bool = False) -> Callable:
        async def handler(request: Request) -> Response:
            if login_required and not request.cookies.get(settings.cookie_name):
                return RedirectResponse("/login?next=/cabinet", status_code=303)
            return render_page(request, template, active)
        return handler

    async def favicon(request: Request) -> Response:
        return FileResponse(static_dir / "favicon.png", media_type="image/png", headers={"Cache-Control": CACHE_FONTS})

    async def http_error(request: Request, exc: HTTPException) -> Response:
        if exc.status_code == 404 and _wants_html_404(request):
            return render_page(request, "404.html", "", status=404)
        messages = {404: ("not_found", "Страница не найдена."), 405: ("method_not_allowed", "Метод не поддерживается.")}
        code, message = messages.get(exc.status_code, ("http_error", "Запрос не выполнен."))
        return _json({"error": {"code": code, "message": message, "field": None}}, exc.status_code,
                     getattr(exc, "headers", None))

    async def unhandled(request: Request, exc: Exception) -> Response:
        return _internal_error(exc)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        if settings.generated_key:
            print(f"DL_API_KEY={settings.api_keys[0]}", flush=True)
        await run_in_threadpool(service.warmup)
        try:
            yield
        finally:
            store.close()

    cabinet_routes = [Route(path, endpoint(handler), methods=methods) for path, handler, methods in cabinet.routes()]
    routes = [
        Route("/healthz", endpoint(healthz), methods=["GET"]),
        Route("/api/v1/analyze", endpoint(analyze, protected=True), methods=["POST"]),
        Route("/api/v1/batch", endpoint(batch, protected=True, heavy=True), methods=["POST"]),
        Route("/api/v1/benchmark", endpoint(benchmark, protected=True, heavy=True), methods=["POST"]),
        Route("/api/v1/parse", endpoint(parse, protected=True, heavy=True), methods=["POST"]),
        Route("/api/v1/reference", endpoint(reference, protected=True), methods=["GET"]),
        Route("/api/v1/openapi.json", endpoint(openapi_spec), methods=["GET"]),
        Route("/ui/examples", endpoint(static_json_route("examples", service.examples)), methods=["GET"]),
        Route("/ui/reference", endpoint(static_json_route("reference", service.reference)), methods=["GET"]),
        Route("/ui/analyze", endpoint(analyze, limited=True), methods=["POST"]),
        Route("/ui/benchmark", endpoint(benchmark, limited=True, heavy=True), methods=["POST"]),
        Route("/ui/columns", endpoint(columns, limited=True, heavy=True), methods=["POST"]),
        Route("/api/v1/columns", endpoint(columns, protected=True, heavy=True), methods=["POST"]),
        Route("/ui/parse", endpoint(parse, limited=True, heavy=True), methods=["POST"]),
        Route("/", endpoint(page("index.html", "index")), methods=["GET"]),
        Route("/benchmark", endpoint(page("benchmark.html", "benchmark")), methods=["GET"]),
        Route("/developers", endpoint(page("developers.html", "developers")), methods=["GET"]),
        Route("/login", endpoint(page("login.html", "cabinet")), methods=["GET"]),
        Route("/cabinet", endpoint(page("cabinet.html", "cabinet", login_required=True)), methods=["GET"]),
        Route("/share", endpoint(page("share.html", "cabinet")), methods=["GET"]),
        Route("/favicon.ico", endpoint(favicon), methods=["GET"]),
        *cabinet_routes,
        Mount("/static", app=StaticFiles(directory=str(static_dir)), name="static"),
    ]
    app = _GuardedStarlette(routes=routes, lifespan=lifespan,
                            exception_handlers={HTTPException: http_error, Exception: unhandled})
    app.add_middleware(GZipByPathMiddleware)
    app.add_middleware(CorsMiddleware, origins=settings.cors_origins)
    app.add_middleware(SecurityMiddleware, hsts=settings.cookie_secure)
    app.add_middleware(ProxyHeadersMiddleware, trusted=parse_networks(settings.trusted_proxies))
    app.state.settings = settings
    app.state.heavy_gate = gate
    app.state.store = store
    app.state.cabinet = cabinet
    return app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    _s: Settings = app.state.settings
    uvicorn.run(app, host=_s.host, port=_s.port, log_level="warning", proxy_headers=False, access_log=False,
                server_header=False)
