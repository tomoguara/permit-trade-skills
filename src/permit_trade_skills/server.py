"""FastAPI app factory: `uvicorn --factory permit_trade_skills.server:create_app` or `pts api serve`."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from permit_trade_skills.query import api as query_api
from permit_trade_skills.query.service import QueryService

DESCRIPTION = """Building-permit query API (Stage 04).

Structured search with keyset paging, per-status counts, single-permit
detail, and a natural-language endpoint whose parse is echoed back
(template-rendered, never LLM-written). See docs/api.md for the cookbook.
"""


try:
    __version__ = version("permit-trade-skills")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0"


def create_app(service: Optional[QueryService] = None, *, db_url: Optional[str] = None,
               settings: Optional[Dict[str, Any]] = None, eval_store: Any = None) -> FastAPI:
    from permit_trade_skills.settings import load_settings

    settings = settings or load_settings()
    app = FastAPI(title="Permit Trade Skills API", version=__version__, description=DESCRIPTION)
    query_api.install(app, service or QueryService(db_url, settings=settings),
                      settings["api"]["rate_limit_per_minute"])

    @app.get("/health", tags=["ops"], summary="Liveness check")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", tags=["ops"], summary="Prometheus metrics (Stage 08)",
             response_class=PlainTextResponse)
    def metrics() -> str:
        from permit_trade_skills.eval.metrics import render
        from permit_trade_skills.wiring import eval_store as make_store

        store = eval_store or make_store(db_url)
        active = store.active_jurisdictions()
        return render(store, active[0] if len(active) == 1 else None)

    return app
