"""Natural-language question -> structured PermitQuery parameters (LLM role `nlu.parser`).

The LLM only *parses*. It returns JSON matching `nlu_schema(known)`
(temperature 0, strict JSON-schema output), where permit_class/permit_type are
enums of the registered jurisdictions' known values. Everything after that is
deterministic code (`interpret`, then the service): jurisdiction resolution,
value validation, query execution and the echo text.

Parses are cached for 24h. The key is sha256(question, request jurisdiction,
schema version, prompt version, model, today's date). Today's date is in the
key because relative dates ("last 90 days") are resolved against it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence

from permit_trade_skills.llm import LLMError, LLMProvider
from permit_trade_skills.query.errors import NLU_UNAVAILABLE, QueryError
from permit_trade_skills.query.params import SORTS, STATUSES, parse_params
from permit_trade_skills.store.query import (
    CONFIDENCE_LEVELS,
    TRADE_MATCHES,
    PermitQuery,
)

SCHEMA_VERSION = "nlu-schema-v5"
PROMPT_VERSION = "nlu-parse-v7"

_NULLABLE_STR = {"type": ["string", "null"]}
_STATUS_LIST = {"type": ["array", "null"], "items": {"type": "string", "enum": STATUSES}}
_DATE = {"type": ["string", "null"], "description": "YYYY-MM-DD"}
_NUMBER = {"type": ["number", "null"]}

PERMIT_QUERY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": _STATUS_LIST,
        "statuses_exclude": _STATUS_LIST,
        "permit_class": _NULLABLE_STR,
        "permit_type": _NULLABLE_STR,
        "desc_keyword": _NULLABLE_STR,
        "trade": {"type": ["array", "null"], "items": {"type": "string"}},
        "trade_match": {"type": ["string", "null"], "enum": list(TRADE_MATCHES) + [None]},
        "trade_min_confidence": {"type": ["string", "null"], "enum": list(CONFIDENCE_LEVELS) + [None]},
        "city": _NULLABLE_STR,
        "zip": _NULLABLE_STR,
        "applied_after": _DATE,
        "applied_before": _DATE,
        "issued_after": _DATE,
        "issued_before": _DATE,
        "min_cost": _NUMBER,
        "max_cost": _NUMBER,
        "sort": {"type": ["string", "null"], "enum": SORTS + [None]},
        "limit": {"type": ["integer", "null"], "description": "Number of rows asked for (top N)"},
    },
    "required": ["status", "statuses_exclude", "permit_class", "permit_type", "desc_keyword", "trade", "trade_match",
                 "trade_min_confidence", "city",
                 "zip", "applied_after", "applied_before", "issued_after", "issued_before",
                 "min_cost", "max_cost", "sort", "limit"],
    "additionalProperties": False,
}

def nlu_schema(known: Optional[Dict[str, Sequence[str]]] = None) -> Dict[str, Any]:
    """NLU_SCHEMA with permit_class / permit_type / trade restricted to the known values (if any)."""
    import copy

    schema = copy.deepcopy(NLU_SCHEMA)
    for name in ("permit_class", "permit_type"):
        values = list((known or {}).get(name) or [])
        if values:
            schema["properties"]["permit_query"]["properties"][name] = {
                "type": ["string", "null"], "enum": values + [None]}
    trades = list((known or {}).get("trade") or [])
    if trades:
        schema["properties"]["permit_query"]["properties"]["trade"] = {
            "type": ["array", "null"], "items": {"type": "string", "enum": trades}}
    return schema


NLU_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "permit_query": PERMIT_QUERY_SCHEMA,
        "ambiguous_terms": {"type": "array", "items": {"type": "string"}},
        "unrecognized_entities": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "jurisdiction_in_question": _NULLABLE_STR,
        "core_requirement_unfulfillable": {"type": "boolean"},
        "clarification": _NULLABLE_STR,
    },
    "required": ["permit_query", "ambiguous_terms", "unrecognized_entities", "assumptions",
                 "jurisdiction_in_question", "core_requirement_unfulfillable", "clarification"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You translate a user's question about building permits into JSON search filters.
You only parse: you never answer the question and never invent permits, numbers or statuses.

Fill `permit_query` with ONLY the filters the question states or clearly implies; every other
field is null. Fields:
- status / statuses_exclude: canonical statuses. Map words as follows:
  issued, active, approved -> ISSUED; pending, under review, in review, applied for,
  awaiting, not yet issued -> PENDING; in inspection, under inspection -> IN_INSPECTION;
  completed, finished, finaled, closed -> COMPLETED (there is no CLOSED status: when the
  question says "closed", add the assumption "'closed' = status COMPLETED");
  expired -> EXPIRED; canceled, cancelled, withdrawn, denied -> CANCELED.
  "not canceled" etc. -> statuses_exclude. A status word anywhere in the question sets
  status, even when it also implies a date: "permits issued in the last 90 days" ->
  status ["ISSUED"] AND issued_after. A date window alone ("in the last 30 days", "since
  June") is not a status word: it sets issued_after/applied_after but no status.
- permit_class: use one of the known permit_class values given below when the question names
  one (e.g. "residential" -> "Residential", "commercial" -> "Commercial"). "residential" is
  ambiguous (permit class vs housing category): choose permit_class and add the
  ambiguous_terms entry "residential: interpreted as permit_class=Residential".
- permit_type: use one of the known permit_type values given below only when the question
  names that kind of permit (e.g. "demolition permits" -> "Demolition", "roof permits" ->
  "Roof"). "building permits" and "permits" mean permits in general: no permit_type.
- trade: when the question asks for permits by the trade or kind of trade work they need
  ("requiring carpentry work", "jobs for an electrician", "permits needing plumbing", "HVAC
  work"), set trade to the matching known trade value(s) given below, NOT desc_keyword.
  Trades are matched against the trade skills inferred from each permit's details, so they
  find work the description implies without naming the trade and skip trades the description
  says are done under a separate permit; a description keyword would get both wrong. Map
  occupations to trades: electrician -> Electrical, plumber -> Plumbing, carpenter ->
  Carpentry, framer -> Framing, roofer -> Roofing, painter -> Painting. Use several values only when the question names several
  trades, and then set trade_match: "all" when the permit must need every one ("electrical
  and carpentry work", "both plumbing and HVAC", "electrical and plumbing permits"); "any"
  when one is enough ("plumbing or electrical work", "either", "electrical/plumbing"). With
  one trade, trade_match is null. If "and" could also mean "either", choose "all" and add an
  ambiguous_terms entry. A permit kind that is a known permit_type ("roof permits",
  "demolition permits") stays permit_type, not trade.
- trade_min_confidence: null unless the question says how sure the trade must be: "with
  high confidence", "definitely" -> "high"; "any confidence", "possibly", "might need" ->
  "low". (Code applies a default when null.)
- desc_keyword: the single most distinctive word for the project or element described (not a
  trade), matched as a substring of the permit description: "solar panel permits" ->
  "solar", "basement remodel" -> "basement", "ADU construction" -> "ADU", "residential
  additions" -> "addition". Use the singular form. Do not repeat a status, class, type or
  trade here.
- dates are YYYY-MM-DD. Use issued_after/issued_before for issued permits and generally for
  permits by date; use applied_after/applied_before when the question is about applications or
  pending permits (pending permits have no issue date). "_after" is inclusive, "_before" is
  exclusive. Resolve relative dates against today's date given below:
  "last N days" -> today minus N days; "since <date>" -> <date>; "in <year>" -> after
  <year>-01-01 and before <year+1>-01-01; "this year" -> after Jan 1 of this year (no
  _before); "since <date>" and "last N days" also have no _before;
  "last month" -> the previous calendar month; "recent"/"recently" -> last 90 days (add the
  assumption "assumed 'recent' = last 90 days"). Record each resolved relative date in
  assumptions.
- min_cost / max_cost: estimated project cost in dollars ("over $50k" -> min_cost 50000,
  "under $10,000" -> max_cost 10000).
- zip: a 5-digit ZIP code. city: only a city name used to filter addresses that is NOT the
  jurisdiction itself; normally null.
- sort: "most expensive"/"largest" -> cost_desc; "newest applications" -> applied_desc;
  otherwise null.
- limit: the number of permits asked for: "top 5", "first 10", "5 most expensive" -> 5 / 10 / 5.
  Otherwise null. "top N" alone does not imply a sort; set sort only when the question says
  by what ("most expensive", "largest", "newest").

jurisdiction_in_question: the city or county the question names as the place the permits are
from (e.g. "Seattle", "Bellevue", "King County"), copied verbatim; null if none is named.

unrecognized_entities: copy verbatim every constraint you cannot express with the fields above:
neighborhoods, landmarks, streets, distances ("within 1 mile of the Space Needle"), contractor
or owner names, inspection results, etc. Never drop such a constraint silently.

core_requirement_unfulfillable: true only when the main point of the question cannot be
expressed with these filters (e.g. a proximity search); then set clarification to one short
sentence telling the user what they can ask instead. Otherwise false and clarification null.

assumptions: every interpretation you made. ambiguous_terms: terms with more than one plausible
meaning and the meaning you chose.

Examples (only the non-null permit_query fields are shown; every other field is null):
- "Expired building permits in Seattle" -> status ["EXPIRED"]; jurisdiction_in_question
  "Seattle". (No permit_type: "building permits" is generic. No city: Seattle is the
  jurisdiction.)
- "Grading permits issued this year" (today 2026-04-10) -> status ["ISSUED"],
  permit_type "Grading", issued_after "2026-01-01". (No issued_before.)
- "Deck permits that are not pending" -> desc_keyword "deck", statuses_exclude ["PENDING"].
  (No status: only exclusions were asked for.)
- "Kitchen remodels over $20k" -> desc_keyword "kitchen", min_cost 20000.
- "The 3 most expensive demolition permits" -> permit_type "Demolition", sort "cost_desc", limit 3.
- "Top 10 permits requiring carpentry work in the last 30 days" (today 2026-11-13) ->
  trade ["Carpentry"], issued_after "2026-10-14", limit 10. (No desc_keyword "carpentry".)
- "Issued permits that need an electrician" -> status ["ISSUED"], trade ["Electrical"].
- "Permits requiring electrical and carpentry work" -> trade ["Electrical", "Carpentry"],
  trade_match "all".
- "Permits that might need plumbing or HVAC work" -> trade ["Plumbing", "HVAC"],
  trade_match "any", trade_min_confidence "low".

Common mistakes to avoid: putting a trade word ("electrical", "carpentry", "plumbing") in
desc_keyword instead of trade; setting permit_type "Building" because the question says "building
permits" (that phrase means all permits); setting city to the jurisdiction's own name; adding
a status the question did not ask for (a date window such as "in the last 30 days" is
not a request for ISSUED); adding an _before date the question did not imply;
claiming in assumptions that you applied something no field expresses (put it in
unrecognized_entities instead).

Output only the JSON object."""


def build_user_prompt(question: str, today: date, known: Dict[str, Sequence[str]]) -> str:
    lines = [f"Today's date: {today.isoformat()}"]
    notes = {"permit_type": " (set permit_type only if the question names one of these kinds of "
                            "permit; never for the generic phrase \"building permits\")",
             "trade": " (set trade when the question asks for permits by the trade work they need)"}
    for name, values in known.items():
        if values:
            lines.append(f"Known {name} values: {', '.join(values)}{notes.get(name, '')}")
    lines.append(f"Question: {question}")
    return "\n".join(lines)


@dataclass
class NluParse:
    permit_query: Dict[str, Any]
    ambiguous_terms: List[str]
    unrecognized_entities: List[str]
    assumptions: List[str]
    jurisdiction_in_question: Optional[str]
    core_requirement_unfulfillable: bool
    clarification: Optional[str]
    model: str
    request_id: Optional[str] = None
    cached: bool = False

    @classmethod
    def from_payload(cls, payload: Dict[str, Any], *, model: str, request_id: Optional[str],
                     cached: bool) -> "NluParse":
        return cls(
            permit_query={k: v for k, v in (payload.get("permit_query") or {}).items() if v is not None},
            ambiguous_terms=list(payload.get("ambiguous_terms") or []),
            unrecognized_entities=list(payload.get("unrecognized_entities") or []),
            assumptions=list(payload.get("assumptions") or []),
            jurisdiction_in_question=payload.get("jurisdiction_in_question") or None,
            core_requirement_unfulfillable=bool(payload.get("core_requirement_unfulfillable")),
            clarification=payload.get("clarification") or None,
            model=model, request_id=request_id, cached=cached,
        )


class NluCache(Protocol):
    def get(self, key: str) -> Optional[dict]: ...

    def put(self, key: str, question: str, jurisdiction: Optional[str], schema_version: str,
            model: str, response: dict, ttl: timedelta) -> None: ...


class MemoryNluCache:
    """In-process cache with the NluCache interface (tests; TTL is not enforced)."""

    def __init__(self):
        self.entries: Dict[str, dict] = {}

    def get(self, key: str) -> Optional[dict]:
        return self.entries.get(key)

    def put(self, key, question, jurisdiction, schema_version, model, response, ttl) -> None:
        self.entries[key] = response


def cache_key(question: str, jurisdiction: Optional[str], model: str, today: date) -> str:
    material = "\n".join([" ".join(question.split()), jurisdiction or "", SCHEMA_VERSION,
                          PROMPT_VERSION, model, today.isoformat()])
    return hashlib.sha256(material.encode()).hexdigest()


class NLUParser:
    """Parses questions with an LLM provider, caching parses in `cache`."""

    def __init__(self, provider: LLMProvider, cache: Optional[NluCache] = None, *,
                 ttl: timedelta = timedelta(hours=24),
                 today: Callable[[], date] = date.today):
        self.provider = provider
        self.cache = cache
        self.ttl = ttl
        self.today = today

    @property
    def model(self) -> str:
        return getattr(self.provider, "model_id", self.provider.name)

    def parse(self, question: str, jurisdiction: Optional[str] = None,
              known: Optional[Dict[str, Sequence[str]]] = None) -> NluParse:
        today = self.today()
        key = cache_key(question, jurisdiction, self.model, today)
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                return NluParse.from_payload(hit["parse"], model=hit["model"],
                                             request_id=hit.get("request_id"), cached=True)
        try:
            completion = self.provider.complete(
                system=SYSTEM_PROMPT, user=build_user_prompt(question, today, known or {}),
                response_schema=nlu_schema(known), temperature=0.0, prompt_version=PROMPT_VERSION,
            )
        except LLMError as e:
            raise QueryError(NLU_UNAVAILABLE, f"The question parser is unavailable ({e}). "
                             "Retry later, or use GET /api/v1/permits with explicit filters.",
                             503, retryable=True) from e
        payload = completion.parsed
        if self.cache is not None:
            self.cache.put(key, question, jurisdiction, SCHEMA_VERSION, completion.model,
                           {"parse": payload, "model": completion.model,
                            "request_id": completion.request_id}, self.ttl)
        return NluParse.from_payload(payload, model=completion.model,
                                     request_id=completion.request_id, cached=False)


# ---- jurisdiction resolution (deterministic) ----------------------------------

_STOPWORDS = {"city", "of", "the", "county", "town", "permits", "permit"}


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def match_jurisdictions(named: str, jurisdictions: Sequence[Dict[str, Any]]) -> List[str]:
    """Slugs of the jurisdictions whose name/slug/county/state cover every word of `named`.

    "Seattle", "City of Seattle", "Seattle, WA" -> seattle-kingcounty-wa;
    "Bellevue" -> [] (not registered). "county" and "city" are ignored, so
    "King County" matches a jurisdiction whose county is "King County".
    """
    want = _tokens(named) - _STOPWORDS
    if not want:
        return []
    matches = []
    for j in jurisdictions:
        have = set()
        for part in (j.get("slug"), j.get("name"), j.get("county"), j.get("state")):
            have |= _tokens(part or "")
        have |= set((j.get("slug") or "").split("-"))
        if want <= have:
            matches.append(j["slug"])
    return matches


DEFAULT_TRADE_MIN_CONFIDENCE = "medium"


def _trade_defaults(params: Dict[str, Any], assumptions: List[str]) -> None:
    trades = params.get("trade")
    trades = [trades] if isinstance(trades, str) else list(trades or [])
    if not trades:
        params.pop("trade_match", None)
        params.pop("trade_min_confidence", None)
        return
    if params.get("trade_min_confidence") is None:
        params["trade_min_confidence"] = DEFAULT_TRADE_MIN_CONFIDENCE
        assumptions.append(f"trade skills must have {DEFAULT_TRADE_MIN_CONFIDENCE} or higher "
                           "confidence (default; ask for 'any confidence' to include low)")
    if len(trades) == 1:
        params.pop("trade_match", None)
    elif params.get("trade_match") is None:
        params["trade_match"] = "all"
        assumptions.append(f"permits must need all of {', '.join(trades)} (no 'and'/'or' was parsed)")


def interpret(parsed: NluParse, jurisdiction: Dict[str, Any],
              extra: Optional[Dict[str, Any]] = None) -> "tuple[PermitQuery, List[str], List[str]]":
    """Turn a parse into the PermitQuery to execute in `jurisdiction` (deterministic).

    Returns (query, problems, extra_assumptions). Values that fail validation
    become `problems` (reported as unfulfilled filters, never silently dropped).
    A `city` that only names the jurisdiction itself is not an address filter:
    it is removed and recorded as an assumption. A trade filter defaults to
    trade_min_confidence "medium" and, with several trades, trade_match "all"
    (each default is recorded as an assumption); without a trade both are dropped. `extra` (page_size, cursor)
    is applied as given and overrides a parsed `limit` ("top N" -> page_size N).
    """
    params = dict(parsed.permit_query)
    assumptions: List[str] = []
    limit = params.pop("limit", None)
    if limit is not None:
        params["page_size"] = limit   # "top N" -> first page of N rows (clamped to 1..200)
    city = params.get("city")
    if isinstance(city, str) and match_jurisdictions(city, [jurisdiction]):
        params.pop("city")
        assumptions.append(f"'{city}' is the jurisdiction ({jurisdiction['slug']}); "
                           "not applied as an address-city filter")
    _trade_defaults(params, assumptions)
    query, problems = parse_params(jurisdiction["slug"], {**params, **(extra or {})}, strict=False)
    return query, problems, assumptions
