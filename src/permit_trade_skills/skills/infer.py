"""K3: `infer_skills(permit, detail) -> SkillInference`, the Stage 06 entry point.

Steps:

1. Gate: permit.status must be in `allowed_statuses` (default ISSUED), else
   NotEligible (no LLM call, no state change).
2. Context (context.py). The detail text must name the permit number (else it
   is another permit's detail: GROUNDING_FAILED, no LLM call). With a store, an
   existing row for the same input_cache_key = sha256(snapshot, prompt_version,
   model, context_hash) is returned with `cached=True`: no second LLM call.
3. LLM call (role skills.infer, JSON schema, temperature from settings,
   <= 0.2). Provider failure -> InferenceFailed(PROVIDER_UNAVAILABLE,
   retryable); never a fallback answer.
4. Validation (Pydantic). Invalid -> one retry with the error appended;
   invalid again -> InferenceFailed(SCHEMA_INVALID, retryable=True). An empty
   skills list -> InferenceFailed(NO_SKILLS, retryable=False).
5. Grounding gate (grounding.py). No survivor -> GROUNDING_FAILED.
6. Signal cross-check (signals.py), advisory: a high signal with no skill adds
   an assumption and lowers confidence one step; skills are never added.
7. Confidence (K4) and persistence (persist.py): SKILLS_INFERRED and
   review_status PENDING; failures -> SKILLS_FAILED with state_error.

Without a store, steps 2 (cache) and 7 (persistence) are skipped: the function
is then pure apart from the LLM call (evals, tests).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

from pydantic import ValidationError

from permit_trade_skills.detail.models import DetailDocument
from permit_trade_skills.llm import LLMError, LLMProvider, LLMResponseInvalid, LLMUnavailable
from permit_trade_skills.skills import grounding as G
from permit_trade_skills.skills import persist
from permit_trade_skills.skills import signals as S
from permit_trade_skills.skills.context import (
    DEGRADED_ASSUMPTION,
    PermitInput,
    SkillContext,
    build_context,
    cache_key,
    render,
    sha256,
)
from permit_trade_skills.skills.schemas import (
    GROUNDING_FAILED,
    NO_SKILLS,
    PROMPT_VERSION,
    PROVIDER_UNAVAILABLE,
    SCHEMA_INVALID,
    InferenceFailed,
    NotEligible,
    SkillInference,
    SkillOutput,
    lower,
    provider_schema,
    step_down,
)

log = logging.getLogger("pts.skills")

MAX_TEMPERATURE = 0.2
DEFAULTS = {"temperature": 0.1, "max_tokens": 4000, "max_context_chars": 8000, "allowed_statuses": ["ISSUED"]}


def skill_settings(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if settings is None:
        from permit_trade_skills.settings import load_settings

        settings = load_settings().get("skills") or {}
    merged = {**DEFAULTS, **settings}
    if float(merged["temperature"]) > MAX_TEMPERATURE:
        raise ValueError(f"skills.temperature must be <= {MAX_TEMPERATURE} (got {merged['temperature']})")
    return merged


def model_id(provider: LLMProvider) -> str:
    return getattr(provider, "model_id", None) or provider.name


def _missing_signal_assumption(trade: str, signal: S.TradeSignal) -> str:
    words = ", ".join(signal.keywords)
    return (f"detail mentions {words} ({trade}) but no skill was inferred — likely omitted by the model")


def _call(provider: LLMProvider, ctx: SkillContext, cfg: Dict[str, Any], prompt_version: str):
    """Steps 3-4: one call, plus one corrective retry on invalid output. Returns (output, completion, user, tokens)."""
    system, user = render(ctx, prompt_version)
    schema = provider_schema(ctx.source_url)
    tokens = {"input": 0, "output": 0}
    cost = 0.0
    prompt = user
    last_error = ""
    for attempt in (1, 2):
        try:
            completion = provider.complete(system=system, user=prompt, response_schema=schema,
                                           temperature=float(cfg["temperature"]),
                                           max_tokens=int(cfg["max_tokens"]) or None,
                                           prompt_version=prompt_version)
        except LLMResponseInvalid as e:
            last_error = str(e)
        except LLMUnavailable as e:
            raise InferenceFailed(ctx.permit.permit_number, PROVIDER_UNAVAILABLE, str(e), retryable=True,
                                  details={"request_id": e.request_id}) from e
        except LLMError as e:
            raise InferenceFailed(ctx.permit.permit_number, PROVIDER_UNAVAILABLE, str(e),
                                  retryable=e.retryable, details={"request_id": e.request_id}) from e
        else:
            for k in tokens:
                tokens[k] += int(completion.tokens.get(k, 0))
            cost = cost + completion.cost if completion.cost is not None and cost is not None else None
            parsed = completion.parsed if isinstance(completion.parsed, dict) else {}
            if parsed.get("skills") == []:
                raise InferenceFailed(ctx.permit.permit_number, NO_SKILLS,
                                      "the model found no skill supported by the detail text", retryable=False,
                                      details={"assumptions": parsed.get("assumptions") or [],
                                               "request_id": completion.request_id,
                                               "completion_hash": sha256(completion.text)})
            try:
                output = SkillOutput.model_validate(parsed)
            except ValidationError as e:
                last_error = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:5])
            else:
                return output, completion, system, prompt, tokens, cost
        if attempt == 1:
            log.info(json.dumps({"event": "skills.retry", "permit": ctx.permit.permit_number,
                                 "error": last_error[:300]}))
            prompt = (f"{user}\n\nYour last output was invalid: {last_error[:500]}; "
                      f"output EXACTLY the schema.")
    raise InferenceFailed(ctx.permit.permit_number, SCHEMA_INVALID,
                          f"invalid output twice: {last_error[:300]}", retryable=True)


def _confidence(output: SkillOutput, accepted, dropped, missing_high, ctx: SkillContext) -> str:
    overall = output.overall_confidence
    for skill in accepted:
        overall = lower(overall, skill["confidence"])
    if dropped:
        overall = lower(overall, "medium")
    if missing_high:
        overall = step_down(overall)
    if ctx.detail_degraded:
        overall = lower(overall, "medium")
    if ctx.degraded:
        overall = "low"
    return overall


def infer_skills(permit: PermitInput, detail: Optional[DetailDocument], *, provider: LLMProvider,
                 store: Optional[persist.SkillStore] = None,
                 allowed_statuses: Optional[Sequence[str]] = None, allow_degraded: bool = False,
                 settings: Optional[Dict[str, Any]] = None,
                 prompt_version: str = PROMPT_VERSION) -> SkillInference:
    """Infer the trade skills for one permit. See the module docstring for the steps."""
    cfg = skill_settings(settings)
    statuses = tuple(allowed_statuses or cfg["allowed_statuses"])

    # 1. gate
    if (permit.status or "").upper() not in statuses:
        raise NotEligible(permit.permit_number, f"status {permit.status!r} is not in {list(statuses)}")

    # 2. context, document anchor, cache
    ctx = build_context(permit, detail, max_chars=int(cfg["max_context_chars"]), allow_degraded=allow_degraded)
    log.info(json.dumps({"event": "skills.context", **ctx.to_log()}))
    try:
        if not G.mentions_permit(ctx.substrate, permit.permit_number):
            raise InferenceFailed(permit.permit_number, GROUNDING_FAILED,
                                  "the detail text does not mention this permit number (wrong-permit detail)",
                                  retryable=False, details={"input_snapshot_id": ctx.input_snapshot_id})
        model = model_id(provider)
        key = cache_key(ctx.input_snapshot_id, prompt_version, model, ctx.context_hash)
        if store is not None:
            existing = store.find_inference(permit.permit_id, key)
            if existing is not None:
                log.info(json.dumps({"event": "skills.cache_hit", "permit": permit.permit_number,
                                     "inference_id": existing.get("id")}))
                return persist.from_row(existing, permit_number=permit.permit_number,
                                        jurisdiction=permit.jurisdiction)

        # 3-4. LLM call + validation
        output, completion, system, user, tokens, cost = _call(provider, ctx, cfg, prompt_version)
        completion_hash = sha256(completion.text)

        # 5. grounding gate
        report = G.gate([s.model_dump() for s in output.skills], ctx.substrate, ctx.source_url)
        if not report.passed:
            raise InferenceFailed(permit.permit_number, GROUNDING_FAILED,
                                  f"none of {report.checked} skill(s) passed the grounding gate", retryable=False,
                                  details={"dropped_skills": report.dropped, "request_id": completion.request_id,
                                           "completion_hash": completion_hash,
                                           "input_snapshot_id": ctx.input_snapshot_id})
    except InferenceFailed as failure:
        if store is not None:
            persist.mark_failed(store, permit.permit_id, failure)
        raise

    # 6. signal cross-check (advisory)
    check = S.cross_check(ctx.signals, report.accepted)
    check["first_pass_rate"] = round(report.first_pass_rate, 4)
    missing_high = check["missing_high_signals"]

    # 7. assumptions, warnings, confidence
    assumptions = list(output.assumptions)
    if ctx.degraded and DEGRADED_ASSUMPTION not in assumptions:
        assumptions.insert(0, DEGRADED_ASSUMPTION)
    if ctx.detail_degraded:
        assumptions.append("The stored detail snapshot is marked degraded"
                           + (f" ({'; '.join(ctx.detail_notes)})" if ctx.detail_notes else "") + ".")
    for trade in missing_high:
        assumptions.append(_missing_signal_assumption(trade, ctx.signals[trade]))
    warnings = list(report.warnings)
    if report.dropped:
        warnings.append(f"dropped {len(report.dropped)} of {report.checked} skill(s) that failed grounding")
    if any(s["confidence"] == "low" for s in report.accepted) and not output.assumptions:
        warnings.append("low-confidence skill(s) without an assumption explaining them")
    licensing_notes = [n for n in output.licensing_notes if G.licensing_note_grounded(n, ctx.substrate)]
    warnings += [f"dropped licensing note (neither quoted from the detail nor marked 'assumed:'): {n[:80]!r}"
                 for n in output.licensing_notes if n not in licensing_notes]
    if ctx.excerpt_note:
        warnings.append(ctx.excerpt_note.strip("()\n"))

    inference = SkillInference(
        permit_number=permit.permit_number, jurisdiction=permit.jurisdiction, model=model,
        prompt_version=prompt_version, skills=report.accepted, assumptions=assumptions,
        overall_confidence=_confidence(output, report.accepted, report.dropped, missing_high, ctx),
        evidence_source=ctx.source_url, degraded=ctx.degraded, licensing_notes=licensing_notes,
        dropped_skills=report.dropped, warnings=warnings, signal_check=check, request_id=completion.request_id,
        prompt_hash=sha256(system + "\n\n" + user), completion_hash=completion_hash,
        context_hash=ctx.context_hash, input_cache_key=key, input_snapshot_id=ctx.input_snapshot_id,
        substrate_hash=ctx.substrate_hash,
        generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        tokens=tokens, cost=cost,
    )
    if store is not None:
        inference = persist.save_accepted(store, permit.permit_id, inference, report,
                                          substrate_text=ctx.substrate if ctx.degraded else None)
    return inference
