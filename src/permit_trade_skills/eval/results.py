"""Suite results and how they are stored in `eval_results`.

A harness returns a `SuiteResult`: per-item dicts (each with `key`,
`passed`, `score`, `reason`), aggregate `metrics` (the gate reads these by
name, e.g. H2 `recall`), counts and the worst items. `save()` writes:

- one `item` row per item (value = score, passed, payload = the item, plus
  `cache_key` / `input_hash` / `model` / `prompt_version` when the item came
  from an LLM call, so a re-run can reuse it),
- one row per aggregate metric (metric = its name, value),
- one `summary` row (payload = the whole result without items).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from permit_trade_skills.eval.ports import EvalStore

WORST_N = 5


@dataclass
class SuiteResult:
    suite: str
    run_id: str
    jurisdiction: str
    build_id: Optional[str] = None
    metrics: Dict[str, Optional[float]] = field(default_factory=dict)
    counts: Dict[str, Any] = field(default_factory=dict)
    items: List[Dict[str, Any]] = field(default_factory=list)
    info: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    llm_calls: int = 0
    cache_hits: int = 0
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    @property
    def worst(self) -> List[Dict[str, Any]]:
        failed = [i for i in self.items if not i.get("passed")]
        failed.sort(key=lambda i: (i.get("score") if i.get("score") is not None else -1, str(i.get("key"))))
        return [{"key": i.get("key"), "score": i.get("score"), "reason": i.get("reason")} for i in failed[:WORST_N]]

    def summary(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("items")
        d["items_total"] = len(self.items)
        d["items_failed"] = sum(1 for i in self.items if not i.get("passed"))
        d["worst"] = self.worst
        return d

    def to_dict(self) -> Dict[str, Any]:
        return {**self.summary(), "items": self.items}


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


def save(store: EvalStore, result: SuiteResult) -> None:
    base = {"run_id": result.run_id, "suite": result.suite, "build_id": result.build_id}
    rows: List[Dict[str, Any]] = []
    for item in result.items:
        rows.append({**base, "metric": "item", "item_key": str(item.get("key")), "permit_id": item.get("permit_id"),
                     "value": item.get("score"), "passed": item.get("passed"),
                     "cache_key": item.get("cache_key"), "input_hash": item.get("input_hash"),
                     "model": item.get("model"), "prompt_version": item.get("prompt_version"),
                     "payload": _jsonable(item)})
    for name, value in result.metrics.items():
        rows.append({**base, "metric": name, "value": value})
    rows.append({**base, "metric": "summary", "model": result.model, "prompt_version": result.prompt_version,
                 "payload": _jsonable(result.summary())})
    store.save_results(rows)


def ratio(num: float, den: float) -> Optional[float]:
    return round(num / den, 4) if den else None


def mean(values: List[float]) -> Optional[float]:
    return round(sum(values) / len(values), 4) if values else None
