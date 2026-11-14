"""Tool surface (Stage 04 Q4): JSON schemas, dry runs, error envelope, framework adapters."""

import re

import jsonschema
import pytest
from jsonschema import Draft202012Validator

from permit_trade_skills.query.errors import QueryError
from permit_trade_skills.query.tools import (
    TOOL_NAMES,
    TOOL_SCHEMAS,
    call_tool,
    to_anthropic_tools,
    to_openai_tools,
)

DRY_RUN_ARGS = {
    "search_permits": {"jurisdiction": "seattle-kingcounty-wa", "status": ["ISSUED"],
                       "permit_class": "Residential", "issued_after": "2026-08-08", "min_cost": 50000},
    "get_permit": {"permit_number": "7083363-CN"},
    "permit_status_counts": {"since": "2026-01-01", "until": "2026-09-27"},
    "list_jurisdictions": {},
}


def test_four_tools():
    assert TOOL_NAMES == ("search_permits", "get_permit", "permit_status_counts", "list_jurisdictions")


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_schemas_are_valid_json_schema(name):
    Draft202012Validator.check_schema(TOOL_SCHEMAS[name]["parameters"])
    Draft202012Validator.check_schema(TOOL_SCHEMAS[name]["returns"])
    description = TOOL_SCHEMAS[name]["description"]
    assert "\n" not in description and 40 < len(description) < 300


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_dry_run_needs_no_database_and_matches_return_schema(name):
    result = call_tool(name, {**DRY_RUN_ARGS[name], "dry_run": True}, service=None)
    assert result["ok"] is True and result["dry_run"] is True
    jsonschema.validate(result, TOOL_SCHEMAS[name]["returns"])


def test_search_dry_run_shows_query_and_echo():
    result = call_tool("search_permits", {**DRY_RUN_ARGS["search_permits"], "dry_run": True}, None)
    would = result["data"]["would_execute"]
    assert would["query"]["status"] == ["ISSUED"] and would["query"]["min_cost"] == 50000.0
    assert would["echo"].startswith("Showing ISSUED permits, Residential class")


@pytest.mark.parametrize("name,args,code", [
    ("search_permits", {"status": ["CLOSED"]}, "INVALID_PARAMETER"),          # not a canonical status
    ("search_permits", {"bogus": 1}, "INVALID_PARAMETER"),
    ("search_permits", {"issued_after": "2026-09-01", "issued_before": "2026-01-01", "dry_run": True},
     "INVALID_DATE_RANGE"),
    ("get_permit", {}, "INVALID_PARAMETER"),
    ("permit_status_counts", {"since": "2026-09-01", "until": "2026-01-01", "dry_run": True},
     "INVALID_DATE_RANGE"),
    ("nope", {}, "INVALID_PARAMETER"),
])
def test_errors_are_returned_not_raised(name, args, code):
    result = call_tool(name, args, service=None)
    assert result["ok"] is False and result["error_code"] == code
    assert set(result) >= {"message", "retryable"}
    jsonschema.validate(result, TOOL_SCHEMAS.get(name, TOOL_SCHEMAS["get_permit"])["returns"])


class RaisingService:
    def __init__(self, exc):
        self.exc = exc

    def resolve_default_jurisdiction(self, slug):
        raise self.exc

    def list_jurisdictions(self):
        raise self.exc


@pytest.mark.parametrize("exc,code,retryable", [
    (QueryError("JURISDICTION_REQUIRED", "pick one"), "JURISDICTION_REQUIRED", False),
    (RuntimeError("db down"), "INTERNAL_ERROR", True),
])
def test_service_failures_become_error_results(exc, code, retryable):
    for name in ("get_permit", "list_jurisdictions"):
        result = call_tool(name, DRY_RUN_ARGS[name], service=RaisingService(exc))
        assert (result["ok"], result["error_code"], result["retryable"]) == (False, code, retryable)


def test_openai_serialization():
    tools = to_openai_tools()
    assert [t["function"]["name"] for t in tools] == list(TOOL_NAMES)
    for t in tools:
        assert t["type"] == "function"
        assert set(t["function"]) == {"name", "description", "parameters"}
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", t["function"]["name"])
        assert t["function"]["parameters"]["type"] == "object"
        Draft202012Validator.check_schema(t["function"]["parameters"])


def test_anthropic_serialization():
    tools = to_anthropic_tools()
    assert [t["name"] for t in tools] == list(TOOL_NAMES)
    for t in tools:
        assert set(t) == {"name", "description", "input_schema"}
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", t["name"])
        assert t["input_schema"]["type"] == "object"
        Draft202012Validator.check_schema(t["input_schema"])
