"""LLM call metering: a provider wrapper that counts calls, tokens and cost.

The pipeline hands Stage 06 a `MeteredProvider` around the `skills.infer`
provider, so a run report can show `llm_calls`, tokens and `est_cost`
without Stage 06 reporting them. Cost comes from the provider profile's
price table (`llm.providers.<profile>.prices`, USD per 1M tokens); a call
whose model has no price leaves `est_cost` incomplete (`cost_known: false`).
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional


class MeteredProvider:
    def __init__(self, inner: Any = None, *, factory: Optional[Callable[[], Any]] = None):
        if inner is None and factory is None:
            raise ValueError("MeteredProvider needs a provider or a factory")
        self._inner = inner
        self._factory = factory
        self._lock = threading.Lock()
        self.calls = 0
        self.failures = 0
        self.tokens = {"input": 0, "output": 0}
        self.cost = 0.0
        self.cost_known = True

    @property
    def inner(self) -> Any:
        if self._inner is None:
            with self._lock:
                if self._inner is None:
                    self._inner = self._factory()  # type: ignore[misc]
        return self._inner

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def model_id(self) -> str:
        return getattr(self.inner, "model_id", None) or self.inner.name

    def complete(self, **kw: Any):
        try:
            completion = self.inner.complete(**kw)
        except Exception:
            with self._lock:
                self.calls += 1
                self.failures += 1
            raise
        with self._lock:
            self.calls += 1
            for k in ("input", "output"):
                self.tokens[k] += int((completion.tokens or {}).get(k, 0))
            if completion.cost is None:
                self.cost_known = False
            else:
                self.cost += float(completion.cost)
        return completion

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"calls": self.calls, "failures": self.failures, "tokens": dict(self.tokens),
                    "est_cost": round(self.cost, 6), "cost_known": self.cost_known}


def delta(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """Usage between two snapshots."""
    return {"calls": after["calls"] - before["calls"], "failures": after["failures"] - before["failures"],
            "tokens": {k: after["tokens"][k] - before["tokens"][k] for k in ("input", "output")},
            "est_cost": round(after["est_cost"] - before["est_cost"], 6), "cost_known": after["cost_known"]}
