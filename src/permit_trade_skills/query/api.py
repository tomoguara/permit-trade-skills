"""Stage 04 REST API: /api/v1/{jurisdictions, permits, permits/{n}, status-counts, query}.

Every response carries `X-Trace-Id` (echoing the request's header when sent)
and, for /api routes, `X-RateLimit-Limit/-Remaining/-Reset` from an in-process
fixed-window limiter (per client IP per minute; `api.rate_limit_per_minute`, 0
disables). Production deployments rate-limit at the gateway. Errors are
`application/problem+json` bodies: {type, title, status, error_code, message,
trace_id, retryable, ...}.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from http import HTTPStatus
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from permit_trade_skills.query import errors as E
from permit_trade_skills.query.errors import QueryError
from permit_trade_skills.query.schemas import (
    JurisdictionsResponse,
    PermitDetailResponse,
    PermitsResponse,
    Problem,
    QueryRequest,
    QueryResponse,
    StatusCountsResponse,
)
from permit_trade_skills.query.service import DATE_FIELDS, QueryService, current_trace_id

log = logging.getLogger("pts.api")

PROBLEM = "application/problem+json"
ERROR_RESPONSES: Dict[int | str, Dict[str, Any]] = {
    code: {"model": Problem, "content": {PROBLEM: {}}} for code in (404, 422, 429, 500, 503)
}

router = APIRouter(prefix="/api/v1", responses=ERROR_RESPONSES)


def _service(request: Request) -> QueryService:
    return request.app.state.query_service


def _with_trace(body: Dict[str, Any]) -> Dict[str, Any]:
    body["trace_id"] = current_trace_id.get()
    return body


@router.get("/jurisdictions", response_model=JurisdictionsResponse, tags=["query"],
            summary="List active jurisdictions")
def list_jurisdictions(request: Request):
    return _with_trace(_service(request).list_jurisdictions())


@router.get("/permits", response_model=PermitsResponse, tags=["query"],
            summary="Structured permit search (keyset paging)")
def search_permits(
    request: Request,
    jurisdiction: str = Query(..., description="Jurisdiction slug, e.g. seattle-kingcounty-wa"),
    status: Optional[str] = Query(None, description="Canonical statuses, comma-separated (ISSUED,PENDING)"),
    statuses_exclude: Optional[str] = Query(None, description="Statuses to exclude, comma-separated"),
    permit_class: Optional[str] = Query(None, description="Raw or mapped class, case-insensitive, csv"),
    permit_type: Optional[str] = Query(None, description="Mapped type or type description, csv"),
    desc_keyword: Optional[str] = Query(None, description="Case-insensitive substring of description"),
    trade: Optional[str] = Query(None, description="Trades from the latest skill inference, csv, "
                                                   "case-insensitive (Carpentry,Electrical); "
                                                   "permits without an inference never match"),
    trade_min_confidence: Optional[str] = Query(None, description="low | medium | high: minimum "
                                                                  "confidence of the matching skill"),
    trade_match: Optional[str] = Query(None, description="any (default): a permit needs one of the "
                                                         "trades; all: it needs every one"),
    city: Optional[str] = Query(None, description="Case-insensitive substring of the address city"),
    zip: Optional[str] = Query(None, description="Exact ZIP code"),
    applied_after: Optional[str] = Query(None, description="ISO date, inclusive"),
    applied_before: Optional[str] = Query(None, description="ISO date, exclusive"),
    issued_after: Optional[str] = Query(None, description="ISO date, inclusive"),
    issued_before: Optional[str] = Query(None, description="ISO date, exclusive"),
    min_cost: Optional[float] = Query(None, description="Minimum valuation (est. project cost)"),
    max_cost: Optional[float] = Query(None, description="Maximum valuation (est. project cost)"),
    state: Optional[str] = Query(None, description="Processing lifecycle states, csv (ops)"),
    sort: Optional[str] = Query(None, description="issued_at_desc (default) | applied_desc | cost_desc | number_asc"),
    page_size: Optional[int] = Query(None, description="Rows per page, default 25, clamped to 1..200"),
    cursor: Optional[str] = Query(None, description="next_cursor from the previous page"),
):
    params = {k: v for k, v in locals().items() if k not in ("request", "jurisdiction")}
    return _with_trace(_service(request).search(jurisdiction, params))


@router.get("/permits/{permit_number}", response_model=PermitDetailResponse, tags=["query"],
            summary="One permit with status history, snapshot and inference")
def get_permit(request: Request, permit_number: str = Path(...),
               jurisdiction: str = Query(..., description="Jurisdiction slug")):
    return _with_trace(_service(request).get_permit(jurisdiction, permit_number))


@router.get("/status-counts", response_model=StatusCountsResponse, tags=["query"],
            summary="Permit counts per canonical status")
def status_counts(
    request: Request,
    jurisdiction: str = Query(...),
    since: Optional[str] = Query(None, description="ISO date, inclusive"),
    until: Optional[str] = Query(None, description="ISO date, exclusive"),
    date_field: str = Query("applied_at", description=f"Window column: {' | '.join(DATE_FIELDS)}"),
    permit_class: Optional[str] = Query(None),
    permit_type: Optional[str] = Query(None),
):
    return _with_trace(_service(request).status_counts(
        jurisdiction, since, until, date_field=date_field,
        permit_class=permit_class.split(",") if permit_class else None,
        permit_type=permit_type.split(",") if permit_type else None))


@router.post("/query", response_model=QueryResponse, tags=["query"],
             summary="Natural-language permit query")
def nl_query(request: Request, body: QueryRequest):
    return _with_trace(_service(request).ask(body.question, body.jurisdiction,
                                             page_size=body.page_size, cursor=body.cursor))


# ---- errors -------------------------------------------------------------------

def problem_response(status: int, error_code: str, message: str, *, retryable: bool = False,
                     extra: Optional[Dict[str, Any]] = None,
                     headers: Optional[Dict[str, str]] = None) -> JSONResponse:
    body = {"type": "about:blank", "title": HTTPStatus(status).phrase, "status": status,
            "error_code": error_code, "message": message, "trace_id": current_trace_id.get(),
            "retryable": retryable, **(extra or {})}
    return JSONResponse(body, status_code=status, media_type=PROBLEM, headers=headers)


async def _query_error(request: Request, exc: QueryError) -> JSONResponse:
    return problem_response(exc.status, exc.error_code, exc.message, retryable=exc.retryable,
                            extra=exc.extra)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = "; ".join(f"{'.'.join(str(p) for p in e['loc'] if p not in ('query', 'body'))}: {e['msg']}"
                        for e in exc.errors())
    return problem_response(422, E.INVALID_PARAMETER, details or "invalid request")


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error")
    return problem_response(500, E.INTERNAL_ERROR, "internal error", retryable=True)


# ---- middleware ---------------------------------------------------------------

class RateLimiter:
    """Fixed one-minute window per client key. limit <= 0 disables."""

    def __init__(self, limit_per_minute: int):
        self.limit = limit_per_minute
        self._windows: Dict[str, Tuple[int, int]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, now: Optional[float] = None) -> Tuple[bool, int, int]:
        """Record a request. Returns (allowed, remaining, reset_epoch_seconds)."""
        now = time.time() if now is None else now
        window = int(now // 60)
        with self._lock:
            start, count = self._windows.get(key, (window, 0))
            if start != window:
                start, count = window, 0
            count += 1
            self._windows[key] = (start, count)
            if len(self._windows) > 10_000:   # drop stale windows
                self._windows = {k: v for k, v in self._windows.items() if v[0] == window}
        return count <= self.limit, max(0, self.limit - count), (window + 1) * 60


class TraceAndLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limiter: RateLimiter):
        super().__init__(app)
        self.limiter = limiter

    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex
        token = current_trace_id.set(trace_id)
        started = time.monotonic()
        try:
            limit_headers: Dict[str, str] = {}
            if self.limiter.limit > 0 and request.url.path.startswith("/api/"):
                client = request.client.host if request.client else "unknown"
                allowed, remaining, reset = self.limiter.hit(client)
                limit_headers = {"X-RateLimit-Limit": str(self.limiter.limit),
                                 "X-RateLimit-Remaining": str(remaining),
                                 "X-RateLimit-Reset": str(reset)}
                if not allowed:
                    response = problem_response(
                        429, E.RATE_LIMITED, f"Rate limit of {self.limiter.limit} requests/minute exceeded.",
                        retryable=True, headers={**limit_headers,
                                                 "Retry-After": str(max(1, reset - int(time.time())))})
                    response.headers["X-Trace-Id"] = trace_id
                    return response
            response = await call_next(request)
            response.headers.update(limit_headers)
            response.headers["X-Trace-Id"] = trace_id
            log.info(json.dumps({"event": "http.request", "trace_id": trace_id, "method": request.method,
                                 "path": request.url.path, "query": str(request.url.query),
                                 "status": response.status_code,
                                 "duration_ms": int((time.monotonic() - started) * 1000)}))
            return response
        finally:
            current_trace_id.reset(token)


def install(app: FastAPI, service: QueryService, rate_limit_per_minute: int) -> None:
    """Wire the query router, error handlers and middleware into `app`."""
    app.state.query_service = service
    app.include_router(router)
    app.add_exception_handler(QueryError, _query_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unhandled)
    app.add_middleware(TraceAndLimitMiddleware, limiter=RateLimiter(rate_limit_per_minute))
