"""Central logging infrastructure.

All modules must obtain loggers via get_logger() and annotate non-trivial
functions with @log_call.  Sensitive fields are redacted automatically.

Every HTTP request is assigned a 12-character request ID by
``RequestLoggingMiddleware`` and propagated via ``contextvars`` so every log
line emitted while handling that request carries the same ID.  This makes
end-to-end debugging possible with ``grep "<request_id>" app.log``.
"""

import contextvars
import functools
import inspect
import logging
import os
import time
import uuid
from typing import (
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    cast,
    overload,
)

from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

F = TypeVar("F", bound=Callable[..., object])

# Names whose values are replaced with "***" wherever they appear in logged
# data: dict keys, Pydantic model fields, keyword-argument names, and
# positional-parameter names (the restic wrappers take the repo password as a
# positional `password` argument; send_notification takes the ntfy token as
# `token`).
#
# RESTIC_PASSWORD is the same secret one layer down: the password reaches
# restic as an environment variable, so the dict that carries it
# (restic.build_restic_env_overrides) is logged both as a return value and as
# an argument to restic_process.run_restic. Matching only `password` left the
# key that decrypts every backup in the repository sitting in the DEBUG log.
SENSITIVE_FIELDS: Set[str] = {
    "restic_password",
    "ntfy_token",
    "password",
    "token",
    "RESTIC_PASSWORD",
}

# Maximum repr length for logged return values; protects logs from being
# flooded by large restic stdout blobs and similar.
_MAX_RETURN_REPR_LEN: int = 500


def _truncate_repr(value: object) -> str:
    s = repr(value)
    if len(s) > _MAX_RETURN_REPR_LEN:
        return s[:_MAX_RETURN_REPR_LEN] + "...<truncated>"
    return s


# Per-request ID populated by RequestLoggingMiddleware; readable anywhere in
# the async call stack via get_request_id().
_request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "request_id", default=None
)


def get_request_id() -> Optional[str]:
    """Return the current request's ID, or None if called outside a request."""
    return _request_id_var.get()


class RequestIdFilter(logging.Filter):
    """Inject the current request_id (or ``"none"``) onto every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "none"
        return True


@overload
def sanitize(data: Dict[str, object]) -> Dict[str, object]: ...


@overload
def sanitize(data: List[object]) -> List[object]: ...


@overload
def sanitize(data: Tuple[object, ...]) -> Tuple[object, ...]: ...


@overload
def sanitize(data: object) -> object: ...


def sanitize(data: object) -> object:
    """Recursively redact sensitive keys from dicts and Pydantic models;
    leave other types unchanged."""
    if isinstance(data, BaseModel):
        # A model's repr prints every field (restic_password included), so it
        # must never reach a log line as-is. Dump to a dict and recurse —
        # the dict branch below applies the key redaction.
        return sanitize(data.model_dump())
    if isinstance(data, dict):
        dict_data: Dict[str, object] = cast(Dict[str, object], data)
        sanitized_items: List[Tuple[str, object]] = [
            (k, "***" if k in SENSITIVE_FIELDS else sanitize(v))
            for k, v in dict_data.items()
        ]
        sanitized_dict: Dict[str, object] = dict(sanitized_items)
        return sanitized_dict
    if isinstance(data, list):
        list_data: List[object] = cast(List[object], data)
        sanitized_list: List[object] = [sanitize(item) for item in list_data]
        return sanitized_list
    if isinstance(data, tuple):
        tuple_data: Tuple[object, ...] = cast(Tuple[object, ...], data)
        sanitized_tuple_items: List[object] = [sanitize(item) for item in tuple_data]
        sanitized_tuple: Tuple[object, ...] = tuple(sanitized_tuple_items)
        return sanitized_tuple
    return data


def get_logger(name: str) -> logging.Logger:
    """Return a named logger for the given module."""
    return logging.getLogger(name)


def _sensitive_positions(fn: Callable[..., object]) -> Set[int]:
    """Positional indices of parameters whose *names* are sensitive.

    ``sanitize`` redacts by dict key, which covers kwargs — but the restic
    wrappers receive the repo password positionally, where no key exists to
    match on. Resolved once at decoration time from the function signature.
    Returns an empty set when the signature cannot be inspected (builtins,
    C extensions) so redaction degrades gracefully instead of breaking the
    decorator.
    """
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return set()
    return {
        i
        for i, p in enumerate(params)
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        and p.name in SENSITIVE_FIELDS
    }


def _redact_positional(
    args: Tuple[object, ...], sensitive_positions: Set[int]
) -> Tuple[object, ...]:
    """Replace sensitive positional argument values with "***"."""
    if not sensitive_positions:
        return args
    return tuple(
        "***" if i in sensitive_positions else value for i, value in enumerate(args)
    )


def log_call(fn: F) -> F:
    """Decorator that logs entry, exit, and exceptions for any function.

    Works for both sync and async functions.  Sanitizes all logged args/kwargs
    so that sensitive field values are never written to logs — by dict key /
    model field / kwarg name, and by positional-parameter name (e.g. the
    ``password`` argument of every restic wrapper).
    """
    logger: logging.Logger = get_logger(fn.__module__)
    sensitive_positions: Set[int] = _sensitive_positions(fn)

    if not inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        def sync_wrapper(*args: object, **kwargs: object) -> object:
            # Both sanitize() and _truncate_repr() build throwaway copies of
            # the data, so they must not run when the DEBUG line they feed
            # would be discarded anyway. _truncate_repr in particular repr()s
            # the whole return value before trimming it: on restic_backup,
            # whose return carried the full stdout blob, that one call roughly
            # doubled peak memory (measured 549 MB → 951 MB on a 210 MB stream)
            # at the default INFO level.
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "%s called args=%s kwargs=%s",
                    fn.__name__,
                    sanitize(_redact_positional(args, sensitive_positions)),
                    sanitize(kwargs),
                )
            try:
                result: object = fn(*args, **kwargs)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "%s returned %s", fn.__name__, _truncate_repr(sanitize(result))
                    )
                return result
            except Exception as exc:
                logger.exception("%s raised %s", fn.__name__, exc)
                raise

        return cast(F, sync_wrapper)

    @functools.wraps(fn)
    async def async_wrapper(*args: object, **kwargs: object) -> object:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "%s called args=%s kwargs=%s",
                fn.__name__,
                sanitize(_redact_positional(args, sensitive_positions)),
                sanitize(kwargs),
            )
        try:
            result: object = await fn(*args, **kwargs)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "%s returned %s", fn.__name__, _truncate_repr(sanitize(result))
                )
            return result
        except Exception as exc:
            logger.exception("%s raised %s", fn.__name__, exc)
            raise

    return cast(F, async_wrapper)


LOG_FORMAT: str = (
    "%(asctime)s [%(request_id)s] %(levelname)s %(name)s:%(funcName)s - %(message)s"
)

_OUR_HANDLER_FLAG: str = "_billa_gates_log_handler"
_factory_installed: bool = False


def _install_request_id_factory() -> None:
    """Install a LogRecordFactory that stamps every record with request_id.

    Using a factory (in addition to RequestIdFilter) means the attribute is
    present on every LogRecord at creation time, so handlers added later
    (e.g. pytest's caplog handler) see it without extra wiring.  Idempotent.
    """
    global _factory_installed
    if _factory_installed:
        return
    base_factory = logging.getLogRecordFactory()

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = base_factory(*args, **kwargs)
        record.request_id = get_request_id() or "none"
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True


def setup_logging() -> None:
    """Configure root logger from LOG_LEVEL env var (default INFO).

    Called once from the FastAPI lifespan handler.  Configures the format to
    include the per-request ID and ensures every LogRecord carries the
    ``request_id`` attribute via both a record factory and a filter.

    Idempotent and non-destructive: never removes external handlers (e.g.
    pytest's caplog handler) and never adds duplicate stream handlers.
    """
    level_str: str = os.environ.get("LOG_LEVEL", "INFO").upper()
    root_logger = logging.getLogger()
    root_logger.setLevel(level_str)

    has_our_handler: bool = any(
        getattr(h, _OUR_HANDLER_FLAG, False) for h in root_logger.handlers
    )
    if not has_our_handler:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.addFilter(RequestIdFilter())
        setattr(handler, _OUR_HANDLER_FLAG, True)
        root_logger.addHandler(handler)

    _install_request_id_factory()


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log every HTTP request with method, path, response status, and duration.

    Generates a 12-character hex request ID per request, stores it in the
    ``_request_id_var`` ContextVar so every log line emitted while handling
    the request carries the same ID, and clears the ContextVar on exit.

    Emits two log lines per request (per design doc §16):
    - entry: ``→ METHOD /path``
    - exit:  ``← METHOD /path status=<code> duration_ms=<ms>``

    Request bodies are not logged here; the ``@log_call`` decorator on each
    route handler already logs the sanitized Pydantic body parameter.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id: str = uuid.uuid4().hex[:12]
        token = _request_id_var.set(request_id)
        logger: logging.Logger = get_logger(__name__)
        method: str = request.method
        path: str = request.url.path
        start: float = time.perf_counter()
        logger.info("→ %s %s", method, path)
        try:
            response: Response = await call_next(request)
            duration_ms: int = int((time.perf_counter() - start) * 1000)
            logger.info(
                "← %s %s status=%d duration_ms=%d",
                method,
                path,
                response.status_code,
                duration_ms,
            )
            return response
        finally:
            _request_id_var.reset(token)
