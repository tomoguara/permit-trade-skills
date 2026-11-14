"""Runtime settings from config/settings.yaml (LLM providers, API limits, NLU cache, detail fetch,
skill inference, orchestration, evaluation).

Lookup: $PTS_SETTINGS > the nearest config/settings.yaml at or above the
working directory > the repository's config/settings.yaml > built-in defaults.
The file is deep-merged over DEFAULTS, so a partial file is fine.

Environment overrides (shell or .env):
- PTS_USE_LOCAL_OLLAMA_MODEL=true|false  -> llm.use_local_ollama_model
- PTS_RATE_LIMIT_PER_MINUTE=<int>        -> api.rate_limit_per_minute
- PTS_INGEST_USER_AGENT=<str>            -> ingest.user_agent
- PTS_INGEST_RATE_LIMIT_PER_MINUTE=<num> -> ingest.rate_limit_per_minute
- PTS_ALERT_WEBHOOK_URL=<url>            -> eval.drift.webhook_url
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from permit_trade_skills.config import load_env

DEFAULTS: Dict[str, Any] = {
    "llm": {
        "use_local_ollama_model": True,
        "timeout_s": 120,
        "max_retries": 2,
        "providers": {
            "openai": {
                "base_url": "https://api.openai.com",
                "api_key_env": "OPENAI_API_KEY",
                "models": {"nlu.parser": "gpt-4.1-mini", "skills.infer": "gpt-4.1",
                           "skills.judge": "gpt-4.1-mini", "agent.planner": "gpt-4.1",
                           "eval.judge": "gpt-4.1-mini"},
                "prices": {"gpt-4.1": {"input": 2.00, "output": 8.00},
                           "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
                           "gpt-4o-mini": {"input": 0.15, "output": 0.60}},
            },
            "ollama": {
                "base_url": "http://localhost:11434",
                "api_key_env": "OLLAMA_API_KEY",
                "models": {"nlu.parser": "qwen3.8:latest", "skills.infer": "qwen3.8:latest",
                           "skills.judge": "qwen3.8:latest", "agent.planner": "qwen3.8:latest",
                           "eval.judge": "gemma4:31b"},
                "reasoning_effort": "none",
            },
        },
    },
    "api": {"rate_limit_per_minute": 60},
    "ingest": {
        "user_agent": "PermitTradeSkills/1.0 (public permit records sync; read-only)",
        "rate_limit_per_minute": 60,
    },
    "nlu": {"cache_ttl_hours": 24},
    "detail": {
        "judge_enabled": False,
        "min_snapshot_age_hours": 24,
        "concurrency": 3,
        "max_concurrency": 8,
        "user_agent": "PermitTradeSkills/0.1 (public permit record lookup; read-only)",
        "nav_timeout_s": 20,
        "settle_timeout_s": 8,
        "total_budget_s": 60,
        "retries": 2,
        "backoff_s": [1, 3],
        "max_retry_after_s": 30,
        "rate_limit_per_minute": 20,
        "human_gate_cooldown_s": 3600,
        "cache_size": 512,
    },
    "skills": {
        "temperature": 0.1,
        "max_tokens": 4000,
        "max_context_chars": 8000,
        "allowed_statuses": ["ISSUED"],
    },
    "orchestration": {
        "pipeline": {
            "workers": 4,
            "max_workers": 16,
            "max_minutes": 60,
            "limit": 25,
            "max_attempts": 3,
            "attempts_per_run": 2,
            "backoff_s": [30, 120],
            "detail_timeout_s": 120,
            "infer_timeout_s": 120,
            "drain_s": 30,
        },
        "agent": {
            "temperature": 0.0,
            "max_tokens": 4000,
            "max_iterations": 12,
            "max_tool_calls": 10,
            "max_expensive_calls": 4,
            "max_wall_s": 300,
            "max_result_chars": 4000,
        },
    },
    "eval": {
        "golden_root": "evals/golden",
        "evidence_root": "evals/evidence",
        "dataset_dirs": {"seattle-kingcounty-wa": "seattle"},
        "trade_set_statuses": ["ISSUED", "COMPLETED"],
        "judge": {"temperature": 0.0, "max_tokens": 2000, "sample_rate": 0.30},
        "h4_live_sample": 15,
        "thresholds": {
            "H1.faithfulness": {"min": 1.0, "hard": True},
            "H2.recall": {"min": 0.80},
            "H2.trap_hallucination_rate": {"max": 0.10},
            "H2.degraded_recall": {"min": 0.50},
            "H3.full_match": {"min": 0.90},
            "H4.must_appear": {"min": 0.90},
            "H4.catalog_grounding": {"min": 1.0, "hard": True},
            "H5.fabrications": {"max": 0, "hard": True},
            "H6.loss": {"max": 0.0, "hard": True},
            "H7.violations": {"max": 0, "hard": True},
        },
        "drift": {
            "window_hours": 24,
            "baseline_days": 7,
            "min_samples": 10,
            "ks_alpha": 0.01,
            "unknown_status_jump_pts": 2.0,
            "degraded_jump_pts": 20.0,
            "h2_recall_min": 0.80,
            "webhook_url": "",
        },
        "slo": {"inference_within_hours": 24, "inference_target": 0.95, "p95_ready_hours": 72},
    },
}

_REPO_SETTINGS = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def settings_path() -> Optional[Path]:
    """The settings file in use, or None (defaults only)."""
    load_env()
    if os.environ.get("PTS_SETTINGS"):
        return Path(os.environ["PTS_SETTINGS"])
    here = Path.cwd().resolve()
    for folder in (here, *here.parents):
        candidate = folder / "config" / "settings.yaml"
        if candidate.is_file():
            return candidate
    return _REPO_SETTINGS if _REPO_SETTINGS.is_file() else None


def _as_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"expected true/false, got {value!r}")


def load_settings(path: Optional[Path] = None) -> Dict[str, Any]:
    """Settings dict: file (if any) over DEFAULTS, then environment overrides."""
    path = path or settings_path()
    data: Dict[str, Any] = {}
    if path is not None:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    settings = _merge(DEFAULTS, data)
    if os.environ.get("PTS_USE_LOCAL_OLLAMA_MODEL"):
        settings["llm"]["use_local_ollama_model"] = _as_bool(os.environ["PTS_USE_LOCAL_OLLAMA_MODEL"])
    if os.environ.get("PTS_RATE_LIMIT_PER_MINUTE"):
        settings["api"]["rate_limit_per_minute"] = int(os.environ["PTS_RATE_LIMIT_PER_MINUTE"])
    if os.environ.get("PTS_INGEST_USER_AGENT"):
        settings["ingest"]["user_agent"] = os.environ["PTS_INGEST_USER_AGENT"]
    if os.environ.get("PTS_INGEST_RATE_LIMIT_PER_MINUTE"):
        settings["ingest"]["rate_limit_per_minute"] = float(os.environ["PTS_INGEST_RATE_LIMIT_PER_MINUTE"])
    if os.environ.get("PTS_ALERT_WEBHOOK_URL"):
        settings["eval"]["drift"]["webhook_url"] = os.environ["PTS_ALERT_WEBHOOK_URL"]
    return settings
