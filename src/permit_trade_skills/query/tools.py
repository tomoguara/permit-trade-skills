"""LLM function-calling tools over the query service (bound by Stages 06/07).

Four tools: search_permits, get_permit, permit_status_counts,
list_jurisdictions. Each has a JSON Schema for its parameters and its result,
a one-line LLM-facing description, and a `dry_run` flag that returns the
would-be call without touching the database.

`call_tool` never raises. It returns `{"ok": true, "data": {...}}` or
`{"ok": false, "error_code", "message", "retryable"}`. Validation and error
codes are the REST API's (query/errors.py).

`to_openai_tools()` / `to_anthropic_tools()` serialize the same specs for
either framework.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import jsonschema

from permit_trade_skills.query import errors as E
from permit_trade_skills.query.echo import render_echo
from permit_trade_skills.query.errors import QueryError
from permit_trade_skills.query.params import SORTS, STATES, STATUSES
from permit_trade_skills.query.retrieve import MODES
from permit_trade_skills.query.service import (
    DATE_FIELDS,
    QueryService,
    build_query,
    build_status_counts_query,
)
from permit_trade_skills.store.query import CONFIDENCE_LEVELS, TRADE_MATCHES

log = logging.getLogger("pts.query.tools")

_STR_OR_LIST = {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}
_STATUS_LIST = {"type": "array", "items": {"type": "string", "enum": STATUSES}}
_DATE = {"type": "string", "description": "ISO date, e.g. 2026-01-31"}
_JURISDICTION = {"type": "string", "description": "Jurisdiction slug from list_jurisdictions; "
                                                  "optional when only one is registered"}
_DRY_RUN = {"type": "boolean", "description": "Validate and return the would-be call without querying"}

ERROR_RESULT = {
    "type": "object",
    "properties": {"ok": {"const": False}, "error_code": {"type": "string"},
                   "message": {"type": "string"}, "retryable": {"type": "boolean"}},
    "required": ["ok", "error_code", "message", "retryable"],
}


def _ok_result(data_schema: Dict[str, Any]) -> Dict[str, Any]:
    """{ok: true, data} | error. Dry runs return data.would_execute instead of results."""
    data_schema["properties"]["would_execute"] = {"type": "object"}
    return {"oneOf": [
        {"type": "object",
         "properties": {"ok": {"const": True}, "dry_run": {"type": "boolean"}, "data": data_schema},
         "required": ["ok", "data"]},
        ERROR_RESULT,
    ]}


_ROW = {"type": "object", "required": ["permit_number", "status"],
        "properties": {"permit_number": {"type": "string"}, "status": {"type": "string"}}}

SEARCH_PARAMS = {
    "type": "object",
    "properties": {
        "jurisdiction": _JURISDICTION,
        "status": _STATUS_LIST,
        "statuses_exclude": _STATUS_LIST,
        "permit_class": {**_STR_OR_LIST, "description": "e.g. Residential, Non-Residential, Commercial"},
        "permit_type": {**_STR_OR_LIST, "description": "e.g. Building, Demolition, Roof"},
        "desc_keyword": {"type": "string", "description": "Case-insensitive substring of the description"},
        "trade": {**_STR_OR_LIST, "description": ("Trade(s) the permit's work needs, from its latest "
                                                  "skill inference, e.g. Carpentry, Electrical; "
                                                  "permits without an inference never match (see "
                                                  "trade_coverage in the result)")},
        "trade_min_confidence": {"type": "string", "enum": list(CONFIDENCE_LEVELS)},
        "trade_match": {"type": "string", "enum": list(TRADE_MATCHES),
                        "description": "any (default): one of the trades; all: every trade"},
        "city": {"type": "string"},
        "zip": {"type": "string"},
        "applied_after": _DATE, "applied_before": _DATE,
        "issued_after": _DATE, "issued_before": _DATE,
        "min_cost": {"type": "number", "minimum": 0}, "max_cost": {"type": "number", "minimum": 0},
        "state": {"type": "array", "items": {"type": "string", "enum": STATES},
                  "description": "Processing lifecycle state (ops)"},
        "sort": {"type": "string", "enum": SORTS},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 200},
        "cursor": {"type": "string", "description": "next_cursor from a previous call"},
        "text": {"type": "string", "description": "Optional free text: ranks matches by hybrid "
                                                  "keyword + semantic relevance instead of sort"},
        "k": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Rows for hybrid ranking"},
        "modes": {"type": "array", "items": {"type": "string", "enum": list(MODES)}},
        "dry_run": _DRY_RUN,
    },
    "additionalProperties": False,
}

TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "search_permits": {
        "description": ("Search permits in one jurisdiction by status, class, type, applied/issued "
                        "dates, cost, ZIP, city, description keyword or needed trade (or rank by free "
                        "`text`); "
                        "returns up to page_size rows plus next_cursor; empty rows means no permit "
                        "matches, so never invent one."),
        "parameters": SEARCH_PARAMS,
        "returns": _ok_result({"type": "object", "required": ["jurisdiction"],
                               "properties": {"jurisdiction": {"type": "string"},
                                              "query": {"type": "object"},
                                              "rows": {"type": "array", "items": _ROW}}}),
    },
    "get_permit": {
        "description": ("Get one permit by permit_number with its status history, lifecycle state, "
                        "detail-snapshot presence and latest skill inference; returns "
                        "PERMIT_NOT_FOUND if it does not exist."),
        "parameters": {
            "type": "object",
            "properties": {"permit_number": {"type": "string", "description": "e.g. 7083363-CN"},
                           "jurisdiction": _JURISDICTION, "dry_run": _DRY_RUN},
            "required": ["permit_number"],
            "additionalProperties": False,
        },
        "returns": _ok_result({"type": "object", "required": ["jurisdiction"],
                               "properties": {"jurisdiction": {"type": "string"},
                                              "permit": _ROW,
                                              "status_history": {"type": "array"},
                                              "detail_snapshot": {"type": "object"},
                                              "skill_inference": {"type": ["object", "null"]}}}),
    },
    "permit_status_counts": {
        "description": ("Count permits per canonical status for a date window [since, until) on "
                        "applied_at (default) or issued_at; every status is listed, with 0 when "
                        "no permit matches."),
        "parameters": {
            "type": "object",
            "properties": {"since": _DATE, "until": _DATE,
                           "date_field": {"type": "string", "enum": list(DATE_FIELDS)},
                           "permit_class": _STR_OR_LIST, "permit_type": _STR_OR_LIST,
                           "jurisdiction": _JURISDICTION, "dry_run": _DRY_RUN},
            "additionalProperties": False,
        },
        "returns": _ok_result({"type": "object", "required": ["jurisdiction"],
                               "properties": {"jurisdiction": {"type": "string"},
                                              "counts": {"type": "object",
                                                         "additionalProperties": {"type": "integer"}},
                                              "total": {"type": "integer"}}}),
    },
    "list_jurisdictions": {
        "description": ("List the active jurisdictions that can be queried, with record counts and "
                        "last sync time; pass one of these slugs as `jurisdiction` to other tools."),
        "parameters": {"type": "object", "properties": {"dry_run": _DRY_RUN},
                       "additionalProperties": False},
        "returns": _ok_result({"type": "object", "required": ["jurisdictions"],
                               "properties": {"jurisdictions": {"type": "array"},
                                              "count": {"type": "integer"}}}),
    },
}

TOOL_NAMES = tuple(TOOL_SCHEMAS)


@dataclass(frozen=True)
class QueryTool:
    name: str
    description: str
    parameters: Dict[str, Any]
    returns: Dict[str, Any]


def tool_specs() -> List[QueryTool]:
    return [QueryTool(name, s["description"], s["parameters"], s["returns"])
            for name, s in TOOL_SCHEMAS.items()]


def to_openai_tools() -> List[Dict[str, Any]]:
    """Chat Completions / Responses-style function tools."""
    return [{"type": "function",
             "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in tool_specs()]


def to_anthropic_tools() -> List[Dict[str, Any]]:
    """Anthropic Messages API tools."""
    return [{"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tool_specs()]


# ---- dispatch -----------------------------------------------------------------

_SEARCH_ONLY = ("jurisdiction", "dry_run", "text", "k", "modes")


def _search(service: QueryService, args: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    params = {k: v for k, v in args.items() if k not in _SEARCH_ONLY}
    text = args.get("text")
    slug = args.get("jurisdiction") or ("(default)" if dry_run else None)
    if dry_run:
        q = build_query(slug, params)
        would = {"query": q.to_dict(), "echo": render_echo(q)}
        if text:
            would.update(text=text, k=args.get("k", 10), modes=args.get("modes", ["text", "vector"]))
        return {"jurisdiction": slug, "would_execute": would}
    slug = service.resolve_default_jurisdiction(args.get("jurisdiction"))
    if text:
        result = service.retrieve(slug, params, text, args.get("k", 10),
                                  args.get("modes") or ("text", "vector"))
        return {"jurisdiction": slug, "query": result.query, "retrieval": {
            k: v for k, v in result.to_dict().items() if k != "rows"},
            "rows": [{**r.permit, "score": r.score, "matched_by": r.matched_by} for r in result.rows]}
    return service.search(slug, params)


def _get_permit(service: QueryService, args: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    if dry_run:
        return {"jurisdiction": args.get("jurisdiction") or "(default)",
                "would_execute": {"permit_number": args["permit_number"]}}
    slug = service.resolve_default_jurisdiction(args.get("jurisdiction"))
    return service.get_permit(slug, args["permit_number"])


def _status_counts(service: QueryService, args: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    kwargs = {k: args.get(k) for k in ("since", "until", "permit_class", "permit_type")}
    date_field = args.get("date_field", "applied_at")
    if dry_run:
        slug = args.get("jurisdiction") or "(default)"
        q = build_status_counts_query(slug, date_field=date_field, **kwargs)
        return {"jurisdiction": slug, "would_execute": {"query": q.to_dict(), "group_by": "status"}}
    slug = service.resolve_default_jurisdiction(args.get("jurisdiction"))
    return service.status_counts(slug, date_field=date_field, **kwargs)


def _list_jurisdictions(service: QueryService, args: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    if dry_run:
        return {"jurisdictions": [], "count": 0, "would_execute": {"active_only": True}}
    return service.list_jurisdictions()


HANDLERS: Dict[str, Callable[[Optional[QueryService], Dict[str, Any], bool], Dict[str, Any]]] = {
    "search_permits": _search,
    "get_permit": _get_permit,
    "permit_status_counts": _status_counts,
    "list_jurisdictions": _list_jurisdictions,
}


def call_tool(name: str, args: Optional[Dict[str, Any]], service: Optional[QueryService]) -> Dict[str, Any]:
    """Run a tool. Never raises; errors come back as {ok: false, error_code, message, retryable}.

    `service` may be None for dry runs.
    """
    args = dict(args or {})
    if name not in HANDLERS:
        return {"ok": False, "error_code": E.INVALID_PARAMETER, "retryable": False,
                "message": f"unknown tool {name!r}; available: {', '.join(TOOL_NAMES)}"}
    try:
        jsonschema.validate(args, TOOL_SCHEMAS[name]["parameters"])
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path) or "arguments"
        return {"ok": False, "error_code": E.INVALID_PARAMETER, "retryable": False,
                "message": f"{where}: {e.message}"}
    dry_run = bool(args.pop("dry_run", False))
    if service is None and not dry_run:
        return {"ok": False, "error_code": E.INTERNAL_ERROR, "retryable": False,
                "message": "no query service configured"}
    try:
        data = HANDLERS[name](service, args, dry_run)
    except QueryError as e:
        return e.to_tool_result()
    except Exception as e:  # noqa: BLE001 - tools must not raise to the planner
        log.exception("tool %s failed", name)
        return {"ok": False, "error_code": E.INTERNAL_ERROR, "retryable": True,
                "message": f"{e.__class__.__name__}: {e}"}
    result = {"ok": True, "data": data}
    if dry_run:
        result["dry_run"] = True
    return result
