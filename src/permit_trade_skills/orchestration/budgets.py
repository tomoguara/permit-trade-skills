"""Hard planner budgets, enforced in code (the prompt only states them).

- iterations (planner LLM calls), default 12: the next iteration past the cap
  raises `BudgetExhausted` and the loop stops with a partial report;
- wall clock, default 300 s: checked at every iteration, same hard stop;
- tool calls, default 10, and `expensive_llm` tool calls, default 4: a call
  past either cap is refused (`BUDGET_EXHAUSTED`, nothing executed) so the
  planner can still write its final answer within the iteration cap.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

EXPENSIVE = "expensive_llm"


class BudgetExhausted(Exception):
    def __init__(self, reason: str, budget: "Budget"):
        super().__init__(f"budget exhausted: {reason}")
        self.reason, self.budget = reason, budget


@dataclass
class Budget:
    max_iterations: int = 12
    max_tool_calls: int = 10
    max_expensive_calls: int = 4
    max_wall_s: float = 300.0
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    iterations: int = 0
    tool_calls: int = 0
    expensive_calls: int = 0
    started: Optional[float] = None

    @classmethod
    def from_settings(cls, agent: Dict[str, Any], clock: Callable[[], float] = time.monotonic) -> "Budget":
        return cls(max_iterations=int(agent["max_iterations"]), max_tool_calls=int(agent["max_tool_calls"]),
                   max_expensive_calls=int(agent["max_expensive_calls"]),
                   max_wall_s=float(agent["max_wall_s"]), clock=clock)

    def start(self) -> None:
        self.started = self.clock()

    def elapsed_s(self) -> float:
        return 0.0 if self.started is None else self.clock() - self.started

    def next_iteration(self) -> int:
        """Count one planner iteration; raises BudgetExhausted past a hard cap."""
        if self.started is None:
            self.start()
        if self.elapsed_s() >= self.max_wall_s:
            raise BudgetExhausted("wall_clock", self)
        if self.iterations >= self.max_iterations:
            raise BudgetExhausted("iterations", self)
        self.iterations += 1
        return self.iterations

    def refusal(self, cost_class: str) -> Optional[str]:
        """Why a tool call of this class may not run now, or None."""
        if self.tool_calls >= self.max_tool_calls:
            return f"tool-call budget spent ({self.tool_calls}/{self.max_tool_calls})"
        if cost_class == EXPENSIVE and self.expensive_calls >= self.max_expensive_calls:
            return f"expensive_llm budget spent ({self.expensive_calls}/{self.max_expensive_calls})"
        return None

    def charge(self, cost_class: str) -> None:
        self.tool_calls += 1
        if cost_class == EXPENSIVE:
            self.expensive_calls += 1

    def to_dict(self) -> Dict[str, Any]:
        return {"iterations": self.iterations, "max_iterations": self.max_iterations,
                "tool_calls": self.tool_calls, "max_tool_calls": self.max_tool_calls,
                "expensive_calls": self.expensive_calls, "max_expensive_calls": self.max_expensive_calls,
                "elapsed_s": round(self.elapsed_s(), 3), "max_wall_s": self.max_wall_s}
