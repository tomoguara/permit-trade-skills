"""Shared fakes for the Stage 04 query tests (no database, no LLM)."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from permit_trade_skills.llm.fake import FakeProvider
from permit_trade_skills.query.nlu import PERMIT_QUERY_SCHEMA, MemoryNluCache, NLUParser
from permit_trade_skills.query.service import QueryService

TODAY = date(2026, 11, 6)
SEATTLE = {"slug": "seattle-kingcounty-wa", "name": "Seattle King County", "county": "King County",
           "state": "WA"}
TACOMA = {"slug": "tacoma-piercecounty-wa", "name": "Tacoma", "county": "Pierce County", "state": "WA"}


def nlu_payload(*, unrecognized: Optional[List[str]] = None, assumptions: Optional[List[str]] = None,
                ambiguous: Optional[List[str]] = None, jurisdiction: Optional[str] = None,
                unfulfillable: bool = False, clarification: Optional[str] = None,
                **permit_query: Any) -> Dict[str, Any]:
    """A schema-valid NLU response; permit_query fields default to null."""
    pq = {name: None for name in PERMIT_QUERY_SCHEMA["properties"]}
    pq.update(permit_query)
    return {"permit_query": pq, "ambiguous_terms": ambiguous or [],
            "unrecognized_entities": unrecognized or [], "assumptions": assumptions or [],
            "jurisdiction_in_question": jurisdiction,
            "core_requirement_unfulfillable": unfulfillable, "clarification": clarification}


def fake_row(n: int, status: str = "ISSUED") -> Dict[str, Any]:
    return {"permit_number": f"{7000000 + n}-CN", "status": status}


class StubService(QueryService):
    """QueryService with the database replaced by in-memory stand-ins.

    The NLU, jurisdiction resolution, parameter validation and echo logic
    under test are the real ones; only DB reads are stubbed.
    """

    def __init__(self, provider: FakeProvider, jurisdictions=(SEATTLE,), rows=None, cache=None):
        self._nlu = NLUParser(provider, cache if cache is not None else MemoryNluCache(),
                              today=lambda: TODAY)
        self._settings = None
        self._known = None
        self.embedder = None
        self.jurisdictions = list(jurisdictions)
        self.rows = list(rows or [])
        self.executed = []

    def _active_jurisdictions(self):
        return self.jurisdictions

    def known_values(self, slugs):
        return {"permit_class": ["Non-Residential", "Residential"], "permit_type": ["Building", "Demolition"],
                "trade": ["Carpentry", "Electrical", "HVAC", "Plumbing"]}

    def execute(self, q):
        self.executed.append(q)
        return {"jurisdiction": q.jurisdiction, "query": q.to_dict(), "total_estimate": len(self.rows),
                "count": len(self.rows), "next_cursor": None, "rows": self.rows, "trade_coverage": None}
