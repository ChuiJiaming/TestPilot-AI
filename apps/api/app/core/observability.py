"""Request correlation, structured logging, and Prometheus-style metrics.

Design notes:

* A request ID is accepted from ``X-Request-ID`` only when it is a safe token,
  otherwise a fresh UUID is generated. It is echoed back on the response and
  attached to every log record produced while the request is in flight, so a
  user-reported failure can be traced to a concrete log line.
* The access log never records query strings or bodies. Tokens routinely travel
  in query strings (SSE tickets, verification links); logging them would leak
  credentials into whatever aggregates stdout.
* Metrics keep label cardinality bounded: the route label is the *route template*
  (``/api/v1/projects/{id}``), never the raw path, and every unmatched request
  collapses into a single ``unmatched`` bucket.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from uuid import UUID, uuid4

from starlette.datastructures import MutableHeaders

REQUEST_ID_HEADER = "X-Request-ID"
request_id_var: ContextVar[str] = ContextVar("testpilot_request_id", default="")

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_ROUTE_UNMATCHED = "unmatched"

access_logger = logging.getLogger("testpilot.access")


def new_request_id() -> str:
    return str(uuid4())


def resolve_request_id(raw: str | None) -> str:
    if raw and _SAFE_REQUEST_ID.fullmatch(raw):
        return raw
    return new_request_id()


def current_request_id() -> str:
    return request_id_var.get()


def trace_id_for_request(request: Any | None = None) -> UUID | str:
    """Return the correlated request ID as a UUID when possible, else a fresh one.

    The problem-details contract declares ``trace_id`` as a UUID, so a caller
    supplied non-UUID request ID is mapped to a stable UUIDv5 derived from it.
    """
    value = current_request_id()
    if not value:
        return uuid4()
    try:
        return UUID(value)
    except (TypeError, ValueError):
        digest = hashlib.sha256(value.encode("utf-8")).digest()
        return UUID(bytes=digest[:16], version=4)


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line; safe for log shippers and greppable by request_id."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        for key in ("method", "path", "status_code", "duration_ms", "client_ip"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class TextLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        request_id = getattr(record, "request_id", None) or request_id_var.get()
        prefix = f"[{request_id}] " if request_id else ""
        base = f"{self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z')} {record.levelname} {record.name} {prefix}"
        message = f"{base}{record.getMessage()}"
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return message


def configure_logging(*, level: str | None = None, fmt: str | None = None) -> None:
    """Configure the root logger once. Safe to call repeatedly."""
    resolved_level = (level or os.getenv("TESTPILOT_LOG_LEVEL", "INFO")).upper()
    resolved_format = (fmt or os.getenv("TESTPILOT_LOG_FORMAT", "json")).lower()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter() if resolved_format == "json" else TextLogFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, resolved_level, logging.INFO))
    # uvicorn installs its own handlers; make them propagate into ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    # httpx logs a line per outbound call at INFO; that is noise next to our
    # access log and would duplicate every provider probe.
    for name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


class MetricsRegistry:
    """Minimal Prometheus text-format registry for the RED metrics that matter here."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, str, str], int] = defaultdict(int)
        self._duration_sum: dict[tuple[str, str, str], float] = defaultdict(float)
        self._in_flight = 0
        self._started_at = time.time()
        self._extra_gauges: dict[str, Callable[[], Iterable[tuple[dict[str, str], float]]]] = {}

    def observe(self, method: str, route: str, status: int, duration_seconds: float) -> None:
        key = (method, route, str(status))
        with self._lock:
            self._counters[key] += 1
            self._duration_sum[key] += duration_seconds

    def in_flight(self, delta: int) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight + delta)

    def register_gauge(self, name: str, provider: Callable[[], Iterable[tuple[dict[str, str], float]]]) -> None:
        self._extra_gauges[name] = provider

    @staticmethod
    def _labels(labels: dict[str, str]) -> str:
        if not labels:
            return ""
        rendered = ",".join(
            f'{key}="{str(value).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
            for key, value in sorted(labels.items())
        )
        return "{" + rendered + "}"

    def render(self, *, build_version: str) -> str:
        lines: list[str] = []
        with self._lock:
            counters = dict(self._counters)
            durations = dict(self._duration_sum)
            in_flight = self._in_flight
            uptime = time.time() - self._started_at

        lines.append("# HELP testpilot_http_requests_total Completed HTTP requests.")
        lines.append("# TYPE testpilot_http_requests_total counter")
        for (method, route, status), value in sorted(counters.items()):
            labels = self._labels({"method": method, "route": route, "status": status})
            lines.append(f"testpilot_http_requests_total{labels} {value}")

        lines.append("# HELP testpilot_http_request_duration_seconds HTTP request latency by route.")
        lines.append("# TYPE testpilot_http_request_duration_seconds summary")
        for (method, route, status), value in sorted(durations.items()):
            labels = self._labels({"method": method, "route": route, "status": status})
            lines.append(f"testpilot_http_request_duration_seconds_sum{labels} {value:.6f}")
            lines.append(
                f"testpilot_http_request_duration_seconds_count{labels} {counters[(method, route, status)]}"
            )

        lines.append("# HELP testpilot_http_requests_in_flight Requests currently being served.")
        lines.append("# TYPE testpilot_http_requests_in_flight gauge")
        lines.append(f"testpilot_http_requests_in_flight {in_flight}")

        lines.append("# HELP testpilot_process_uptime_seconds Seconds since the API process started.")
        lines.append("# TYPE testpilot_process_uptime_seconds gauge")
        lines.append(f"testpilot_process_uptime_seconds {uptime:.3f}")

        lines.append("# HELP testpilot_build_info Build identity of the running API.")
        lines.append("# TYPE testpilot_build_info gauge")
        lines.append(f"testpilot_build_info{self._labels({'version': build_version})} 1")

        for name, provider in list(self._extra_gauges.items()):
            try:
                samples = list(provider())
            except Exception:  # a broken probe must not break the scrape
                continue
            lines.append(f"# TYPE {name} gauge")
            for labels, value in samples:
                lines.append(f"{name}{self._labels(labels)} {value}")
        return "\n".join(lines) + "\n"


class ObservabilityMiddleware:
    """Pure ASGI middleware: never buffers the response body, so SSE keeps streaming."""

    def __init__(self, app, *, metrics: MetricsRegistry):
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw_request_id = headers.get(REQUEST_ID_HEADER.lower().encode("latin-1"))
        request_id = resolve_request_id(
            raw_request_id.decode("latin-1") if raw_request_id else None
        )
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_with_request_id(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                mutable = MutableHeaders(scope=message)
                mutable[REQUEST_ID_HEADER] = request_id
            await send(message)

        self.metrics.in_flight(1)
        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            self.metrics.in_flight(-1)
            duration = time.perf_counter() - started
            route = scope.get("route")
            route_label = getattr(route, "path", None) or _ROUTE_UNMATCHED
            method = scope.get("method", "UNKNOWN")
            self.metrics.observe(method, route_label, status_code, duration)
            client = scope.get("client")
            access_logger.info(
                "%s %s -> %s in %.1fms",
                method,
                scope.get("path", ""),
                status_code,
                duration * 1000,
                extra={
                    "request_id": request_id,
                    "method": method,
                    "path": scope.get("path", ""),
                    "status_code": status_code,
                    "duration_ms": round(duration * 1000, 1),
                    "client_ip": client[0] if client else None,
                },
            )
            request_id_var.reset(token)


def install_observability(app, *, metrics: MetricsRegistry) -> None:
    app.add_middleware(ObservabilityMiddleware, metrics=metrics)
