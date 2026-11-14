"""Evaluation settings (config/settings.yaml `eval:`), thresholds, dataset paths, ids and hashing helpers."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SUITES = ("H1", "H2", "H3", "H4", "H5", "H6", "H7")
JUDGE_SUITE = "judge"
REPO_ROOT = Path(__file__).resolve().parents[3]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{utcnow():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def cache_key(suite: str, input_hash: str, model: str, prompt_version: str) -> str:
    return sha256("\n".join([suite, input_hash, model, prompt_version]))


def eval_settings(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if settings is None:
        from permit_trade_skills.settings import load_settings

        settings = load_settings()
    return settings["eval"] if "eval" in settings else settings


@dataclass(frozen=True)
class Threshold:
    criterion: str          # "H2.recall"
    min: Optional[float] = None
    max: Optional[float] = None
    hard: bool = False

    @property
    def suite(self) -> str:
        return self.criterion.split(".", 1)[0]

    @property
    def metric(self) -> str:
        return self.criterion.split(".", 1)[1]

    def passes(self, value: Optional[float]) -> Optional[bool]:
        if value is None:
            return None
        if self.min is not None and value < self.min:
            return False
        if self.max is not None and value > self.max:
            return False
        return True

    def describe(self) -> str:
        return " and ".join(p for p in ((f">= {self.min:g}" if self.min is not None else ""),
                                         (f"<= {self.max:g}" if self.max is not None else "")) if p)


def thresholds(settings: Optional[Dict[str, Any]] = None) -> List[Threshold]:
    raw = eval_settings(settings).get("thresholds") or {}
    out = []
    for criterion, spec in raw.items():
        spec = spec or {}
        out.append(Threshold(criterion, min=spec.get("min"), max=spec.get("max"), hard=bool(spec.get("hard"))))
    return out


def dataset_dir(jurisdiction: str, settings: Optional[Dict[str, Any]] = None, *, root: Optional[Path] = None) -> Path:
    cfg = eval_settings(settings)
    name = (cfg.get("dataset_dirs") or {}).get(jurisdiction) or jurisdiction
    base = Path(root or cfg.get("golden_root") or "evals/golden")
    if not base.is_absolute():
        base = REPO_ROOT / base
    return base / name


def evidence_dir(jurisdiction: str, settings: Optional[Dict[str, Any]] = None) -> Path:
    cfg = eval_settings(settings)
    name = (cfg.get("dataset_dirs") or {}).get(jurisdiction) or jurisdiction
    base = Path(cfg.get("evidence_root") or "evals/evidence")
    if not base.is_absolute():
        base = REPO_ROOT / base
    return base / name / "eval"


DATASET_FILES = {"trade_set": "trade-set.jsonl", "nl_query": "nl-query.jsonl",
                 "extraction_checklist": "extraction.jsonl"}


def default_build_id() -> str:
    """The short git commit (with `-dirty` for uncommitted changes), else `local`."""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True,
                             text=True, timeout=5, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO_ROOT,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return sha + ("-dirty" if dirty else "") if sha else "local"
    except Exception:  # noqa: BLE001 - no git: an explicit --build is still possible
        return "local"


def parse_window(value: str) -> "tuple[float, str]":
    """'24h' / '7d' / '90m' -> seconds."""

    value = value.strip().lower()
    units = {"m": 60, "h": 3600, "d": 86400}
    if not value or value[-1] not in units:
        raise ValueError(f"window must look like 24h, 7d or 90m (got {value!r})")
    return float(value[:-1]) * units[value[-1]], value


def window_delta(value: str):
    from datetime import timedelta

    return timedelta(seconds=parse_window(value)[0])


def display_path(path: Path) -> str:
    """A path relative to the repository when inside it (evidence files must not carry local paths)."""
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except ValueError:
        return Path(path).name
