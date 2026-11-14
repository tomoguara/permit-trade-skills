"""Typed query errors shared by the REST API, the tools and the CLI.

The API renders them as problem+json (`{type, title, status, error_code,
message, trace_id, ...extra}`); tools return them as
`{ok: false, error_code, message, retryable}`; neither raises to the caller.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

JURISDICTION_NOT_FOUND = "JURISDICTION_NOT_FOUND"            # 404: slug unknown or inactive
JURISDICTION_NOT_REGISTERED = "JURISDICTION_NOT_REGISTERED"  # 422: question names an unregistered place
JURISDICTION_REQUIRED = "JURISDICTION_REQUIRED"              # 422: none given/named, several registered
JURISDICTION_CONFLICT = "JURISDICTION_CONFLICT"              # 422: question names X, request says Y
PERMIT_NOT_FOUND = "PERMIT_NOT_FOUND"                        # 404
INVALID_PARAMETER = "INVALID_PARAMETER"                      # 422: unknown/ill-typed parameter
INVALID_DATE_RANGE = "INVALID_DATE_RANGE"                    # 422: start >= end
INVALID_CURSOR = "INVALID_CURSOR"                            # 422
NLU_UNAVAILABLE = "NLU_UNAVAILABLE"                          # 503 (retryable)
RATE_LIMITED = "RATE_LIMITED"                                # 429 (retryable)
INTERNAL_ERROR = "INTERNAL_ERROR"                            # 500 (retryable)


class QueryError(Exception):
    def __init__(self, error_code: str, message: str, status: int = 422, *,
                 retryable: bool = False, extra: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.extra = extra or {}

    def to_tool_result(self) -> Dict[str, Any]:
        return {"ok": False, "error_code": self.error_code, "message": self.message,
                "retryable": self.retryable, **self.extra}
