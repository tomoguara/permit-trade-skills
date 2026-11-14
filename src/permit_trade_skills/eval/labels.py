"""E1: golden-set labels, the labeling workflow (`pts label`) and the golden-file loader.

Three kinds (labels.kind):

- trade_set (Dataset A): for one permit + its detail snapshot,
  `required_trades` (recall truth), `optional_trades` (defensible extras,
  the precision anchor), `unrequired_trades` (precision traps),
  `defensible_skills` ({trade: [capability phrases]}), `difficulty`, `notes`.
  Two labelers (role `labeler`), then an adjudicator (role `adjudicator`)
  writes the gold row: required = the union of the labelers' required trades,
  minus any trade the adjudicator drops with a note; disagreements are kept.
  A single labeler + adjudicator is allowed with `single_labeler` (Phase 1
  staffing), and recorded.
- nl_query (Dataset B): a question, the expected PermitQuery parameters
  (relative-date tokens resolved against `as_of`), the expected unfulfilled
  filters and the gold row count of the expected query at label time.
- extraction_checklist (Dataset C): 3-5 lines that must appear verbatim in
  the permit's detail `content_text`.

Every label carries `labeled_by`, `label_version`, `created_at` and
`pinned_input_hash`, the hash of the exact input it was made against. A label
is never overwritten: a new version supersedes the old row. A label whose
input changed is SUPERSEDED and is not used for scoring.

Golden files (`<golden_root>/<dataset dir>/{trade-set,nl-query,extraction}.jsonl`)
are exported from the adjudicated labels. A trade-set row embeds its input
(catalog fields + the snapshot's `content_text`), so H2 replays it without
the database; `load_golden` re-hashes every row and marks a tampered one
SUPERSEDED.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from permit_trade_skills.eval import trades as TR
from permit_trade_skills.eval.config import canonical_json, sha256, utcnow
from permit_trade_skills.eval.ports import EvalStore

KINDS = ("trade_set", "nl_query", "extraction_checklist")
TRADE_SET, NL_QUERY, CHECKLIST = KINDS
DIFFICULTIES = ("easy", "medium", "hard")
LABELER, ADJUDICATOR = "labeler", "adjudicator"
ACTIVE, SUPERSEDED = "ACTIVE", "SUPERSEDED"
PERMIT_FIELDS = ("permit_number", "status", "permit_class", "permit_class_mapped", "permit_type_mapped",
                 "permit_type_desc", "valuation", "issued_at", "address", "city", "state", "zip_code",
                 "description", "detail_url")
SNAPSHOT_FIELDS = ("id", "final_url", "fetch_strategy", "content_hash", "degraded", "fetched_at")


class LabelError(ValueError):
    pass


# ---- inputs and hashes --------------------------------------------------------

def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (datetime, date)) else value


def trade_input(permit: Dict[str, Any], snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """The exact input a trade-set label is made against: catalog fields + the detail snapshot."""
    inp = {k: _iso(permit.get(k)) for k in PERMIT_FIELDS}
    if inp.get("valuation") is not None:
        inp["valuation"] = float(inp["valuation"])
    inp["snapshot"] = {**{k: _iso(snapshot.get(k)) for k in SNAPSHOT_FIELDS},
                       "content_text": snapshot["content_text"]}
    return inp


def input_hash(kind: str, inp: Dict[str, Any]) -> str:
    if kind == TRADE_SET:
        snap = {k: v for k, v in (inp.get("snapshot") or {}).items() if k not in ("content_text", "fetched_at")}
        return sha256(canonical_json({**{k: v for k, v in inp.items() if k != "snapshot"}, "snapshot": snap}))
    if kind == CHECKLIST:
        return sha256(f"{inp['permit_number']}\n{inp['content_hash']}")
    if kind == NL_QUERY:
        return sha256(canonical_json({k: inp.get(k) for k in ("question", "expected", "as_of")}))
    raise LabelError(f"unknown label kind {kind!r}")


def verify_trade_input(inp: Dict[str, Any], pinned: str) -> Optional[str]:
    """None when the embedded input still binds to the pinned hash, else the reason it does not."""
    snap = inp.get("snapshot") or {}
    if sha256(snap.get("content_text") or "") != snap.get("content_hash"):
        return "content_text does not match its content_hash"
    if input_hash(TRADE_SET, inp) != pinned:
        return "input does not match pinned_input_hash"
    return None


# ---- relative dates (Dataset B) -------------------------------------------------

def resolve_token(value: Any, today: date) -> Any:
    """@today-<N>d, @year-start, @month-start, @last-month-start -> ISO date (same tokens as Stage 04)."""
    if not isinstance(value, str) or not value.startswith("@"):
        return value
    if m := re.fullmatch(r"@today-(\d+)d", value):
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    month_start = today.replace(day=1)
    tokens = {"@year-start": today.replace(month=1, day=1), "@month-start": month_start,
              "@last-month-start": (month_start - timedelta(days=1)).replace(day=1)}
    if value not in tokens:
        raise LabelError(f"unknown date token {value!r}")
    return tokens[value].isoformat()


def resolve_expected(expected: Dict[str, Any], as_of: date) -> Dict[str, Any]:
    return {k: resolve_token(v, as_of) for k, v in expected.items()}


def expected_query(jurisdiction: str, expected: Dict[str, Any], as_of: date):
    from permit_trade_skills.query.params import parse_params

    return parse_params(jurisdiction, resolve_expected(expected, as_of))[0]


# ---- payload validation -----------------------------------------------------------

def _trade_list(values: Iterable[str], field: str) -> List[str]:
    out = []
    for v in values or []:
        name = TR.canonical_name(str(v))
        if name not in TR.TRADES:
            raise LabelError(f"{field}: {v!r} is not a canonical trade ({', '.join(TR.TRADES)})")
        if name not in out:
            out.append(name)
    return out


def validate_payload(kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if kind == TRADE_SET:
        required = _trade_list(payload.get("required_trades"), "required_trades")
        if not required:
            raise LabelError("required_trades: at least one trade")
        optional = [t for t in _trade_list(payload.get("optional_trades"), "optional_trades") if t not in required]
        unrequired = _trade_list(payload.get("unrequired_trades"), "unrequired_trades")
        overlap = sorted(set(unrequired) & (set(required) | set(optional)))
        if overlap:
            raise LabelError(f"unrequired_trades overlaps required/optional: {overlap}")
        skills: Dict[str, List[str]] = {}
        for trade, phrases in (payload.get("defensible_skills") or {}).items():
            name = TR.canonical_name(trade)
            if name not in required and name not in optional:
                raise LabelError(f"defensible_skills: {trade!r} is not a required or optional trade")
            skills[name] = [p.strip() for p in ([phrases] if isinstance(phrases, str) else phrases) if p.strip()]
        difficulty = payload.get("difficulty") or "medium"
        if difficulty not in DIFFICULTIES:
            raise LabelError(f"difficulty must be one of {DIFFICULTIES}")
        out = {"required_trades": required, "optional_trades": optional, "unrequired_trades": unrequired,
               "defensible_skills": skills, "difficulty": difficulty, "notes": payload.get("notes") or ""}
        for key in ("labelers", "disagreements", "dropped", "adjudication_note", "single_labeler"):
            if key in payload:
                out[key] = payload[key]
        return out
    if kind == CHECKLIST:
        lines = [str(x).strip() for x in payload.get("must_appear") or [] if str(x).strip()]
        if not 3 <= len(lines) <= 5:
            raise LabelError("must_appear: 3 to 5 lines")
        return {"must_appear": lines, "notes": payload.get("notes") or ""}
    if kind == NL_QUERY:
        for key in ("question", "expected", "as_of"):
            if not payload.get(key) and payload.get(key) != {}:
                raise LabelError(f"nl_query: {key} is required")
        gold = payload.get("gold_total")
        if gold is not None and (not isinstance(gold, int) or gold < 0):
            raise LabelError("gold_total must be a non-negative integer")
        return {"question": " ".join(str(payload["question"]).split()), "expected": dict(payload["expected"]),
                "as_of": str(payload["as_of"]), "gold_total": gold,
                "expected_unfulfilled": list(payload.get("expected_unfulfilled") or []),
                "expect_unfulfilled": bool(payload.get("expect_unfulfilled")
                                           or payload.get("expected_unfulfilled")),
                "notes": payload.get("notes") or ""}
    raise LabelError(f"unknown label kind {kind!r}")


# ---- the labeling workflow --------------------------------------------------------------

class LabelService:
    """`pts label ...` over the EvalStore. `jurisdiction` is the registry id."""

    def __init__(self, store: EvalStore, jurisdiction: str, *, count_query=None):
        self.store = store
        self.jurisdiction = jurisdiction
        self._count = count_query or store.count_query

    # inputs
    def permit_input(self, kind: str, permit_number: str) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
        """(permit row, input, hash) for a permit-bound kind, from the latest stored snapshot."""
        permit = self.store.permit(self.jurisdiction, permit_number)
        if permit is None:
            raise LabelError(f"permit {permit_number!r} not found in {self.jurisdiction}")
        snap = self.store.latest_snapshot(permit["id"])
        if snap is None:
            raise LabelError(f"{permit_number}: no detail snapshot (run `pts detail fetch -p {permit_number}`)")
        if kind == TRADE_SET:
            inp = trade_input(permit, snap)
        else:
            inp = {"permit_number": permit_number, "content_hash": snap["content_hash"], "id": snap["id"],
                   "final_url": snap["final_url"], "fetch_strategy": snap["fetch_strategy"]}
        return permit, inp, input_hash(kind, inp)

    def nl_input(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        inp = {k: payload.get(k) for k in ("question", "expected", "as_of")}
        return inp, input_hash(NL_QUERY, inp)

    def _check_separation(self, permit: Optional[Dict[str, Any]], name: str) -> None:
        if permit is not None and name in self.store.reviewers(permit["id"]):
            raise LabelError(f"{name} reviewed this permit's inference; a labeler never labels their own "
                             "review-queue permits (separation of duties)")

    def annotate(self, kind: str, item_key: str, payload: Dict[str, Any], labeled_by: str, *,
                 role: str = LABELER) -> Dict[str, Any]:
        name = (labeled_by or "").strip()
        if not name:
            raise LabelError("labeled_by is required (--labeler or PTS_LABELER)")
        payload = validate_payload(kind, payload)
        permit = None
        if kind == NL_QUERY:
            if payload.get("gold_total") is None:
                payload["gold_total"] = self.gold_total(payload)
            _, pinned = self.nl_input(payload)
        else:
            permit, _, pinned = self.permit_input(kind, item_key)
            self._check_separation(permit, name)
        current = [r for r in self.store.current_labels(self.jurisdiction, kind, item_key, role)
                   if r["labeled_by"] == name]
        old = current[0] if current else None
        row = {"jurisdiction_id": self.jurisdiction, "permit_id": permit["id"] if permit else None, "kind": kind,
               "item_key": item_key, "role": role, "payload": payload, "labeled_by": name,
               "label_version": (old["label_version"] + 1) if old else 1, "pinned_input_hash": pinned}
        row["id"] = self.store.add_label(row, replaces=old["id"] if old else None)
        return row

    def gold_total(self, payload: Dict[str, Any]) -> int:
        as_of = date.fromisoformat(str(payload["as_of"]))
        return int(self._count(expected_query(self.jurisdiction, payload["expected"], as_of)))

    def current(self, kind: str, item_key: str, role: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """(labels bound to the current input, labels made against another input -> superseded now)."""
        rows = self.store.current_labels(self.jurisdiction, kind, item_key, role)
        if kind == NL_QUERY:
            return rows, []
        _, _, pinned = self.permit_input(kind, item_key)
        live = [r for r in rows if r["pinned_input_hash"] == pinned]
        stale = [r for r in rows if r["pinned_input_hash"] != pinned]
        for r in stale:
            self.store.supersede_label(r["id"], None)
        return live, stale

    def adjudicate(self, permit_number: str, adjudicator: str, note: str, *,
                   drop: Optional[Dict[str, str]] = None, single_labeler: bool = False,
                   difficulty: Optional[str] = None) -> Dict[str, Any]:
        """Write the gold trade set: the union of the labelers' required trades, minus noted drops."""
        name = (adjudicator or "").strip()
        if not name or not (note or "").strip():
            raise LabelError("adjudicator and an adjudication note are required")
        labels, stale = self.current(TRADE_SET, permit_number, LABELER)
        if stale:
            note += f" [{len(stale)} label(s) made against an older input were superseded]"
        if any(r["labeled_by"] == name for r in labels):
            raise LabelError(f"{name} labeled this permit; the adjudicator must be someone else")
        if len(labels) < 2 and not (single_labeler and len(labels) == 1):
            raise LabelError(f"{permit_number}: {len(labels)} current label(s); two labelers are needed "
                             "(or one with --single-labeler)")
        drop = {TR.canonical_name(k): v for k, v in (drop or {}).items()}
        missing_notes = [t for t, v in drop.items() if not (v or "").strip()]
        if missing_notes:
            raise LabelError(f"each dropped trade needs a note: {missing_notes}")
        payloads = [r["payload"] for r in labels]
        union = [t for p in payloads for t in p["required_trades"]]
        required = [t for t in dict.fromkeys(union) if t not in drop]
        disagreements = {t: sorted(r["labeled_by"] for r in labels if t in r["payload"]["required_trades"])
                         for t in dict.fromkeys(union) if sum(t in p["required_trades"] for p in payloads) < len(payloads)}
        optional = [t for p in payloads for t in p["optional_trades"] if t not in required and t not in drop]
        unrequired = [t for p in payloads for t in p["unrequired_trades"]
                      if t not in required and t not in optional]
        skills: Dict[str, List[str]] = {}
        for p in payloads:
            for trade, phrases in p["defensible_skills"].items():
                if trade in required or trade in optional:
                    skills.setdefault(trade, [])
                    skills[trade] += [x for x in phrases if x not in skills[trade]]
        levels = [p["difficulty"] for p in payloads] + ([difficulty] if difficulty else [])
        gold = {"required_trades": required, "optional_trades": list(dict.fromkeys(optional)),
                "unrequired_trades": list(dict.fromkeys(unrequired)), "defensible_skills": skills,
                "difficulty": difficulty or max(levels, key=DIFFICULTIES.index),
                "notes": " | ".join(p["notes"] for p in payloads if p.get("notes")),
                "labelers": [r["labeled_by"] for r in labels], "disagreements": disagreements,
                "dropped": [{"trade": t, "note": v} for t, v in drop.items()],
                "adjudication_note": note.strip(), "single_labeler": len(labels) == 1}
        return self.annotate(TRADE_SET, permit_number, gold, name, role=ADJUDICATOR)

    # queue
    def queue(self, kind: str = TRADE_SET, permit_numbers: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        """Label status per item: labelers, adjudicated or not."""
        rows = self.store.current_labels(self.jurisdiction, kind)
        keys = list(dict.fromkeys(list(permit_numbers or []) + [r["item_key"] for r in rows]))
        out = []
        for key in keys:
            mine = [r for r in rows if r["item_key"] == key]
            out.append({"item_key": key,
                        "labelers": sorted(r["labeled_by"] for r in mine if r["role"] == LABELER),
                        "adjudicated_by": next((r["labeled_by"] for r in mine if r["role"] == ADJUDICATOR), None),
                        "drafts": sum(1 for r in mine if r["labeled_by"].startswith("draft-"))})
        return out

    # export / import
    def export(self, kind: str, path: Path, *, provisional: bool = False) -> Dict[str, Any]:
        """Write the golden JSONL for `kind`; returns {written, skipped: [{item_key, reason}]}.

        `provisional` (trade_set only): export the newest *labeler* row per permit
        instead of the adjudicated gold, marked `provisional: true` and
        `adjudicated_by: null`, for a baseline before adjudication. Never write
        it over the gold file.
        """
        role = ADJUDICATOR if kind == TRADE_SET and not provisional else None
        rows = self.store.current_labels(self.jurisdiction, kind, role=role)
        if provisional:
            rows = [r for r in rows if r["role"] == LABELER]
        if kind != TRADE_SET or provisional:   # newest per item (an adjudicator row wins over a labeler row)
            best: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                cur = best.get(r["item_key"])
                if cur is None or (r["role"] == ADJUDICATOR, r["created_at"]) > (cur["role"] == ADJUDICATOR,
                                                                                   cur["created_at"]):
                    best[r["item_key"]] = r
            rows = list(best.values())
        written, skipped = [], []
        for r in sorted(rows, key=lambda r: r["item_key"]):
            try:
                row = self._golden_row(kind, r)
                if provisional:
                    row.update(labelers=[r["labeled_by"]], adjudicated_by=None, provisional=True)
                written.append(row)
            except LabelError as e:
                skipped.append({"item_key": r["item_key"], "reason": str(e)})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(w, ensure_ascii=False, default=str) + "\n" for w in written))
        return {"path": str(path), "written": len(written), "skipped": skipped}

    def _golden_row(self, kind: str, r: Dict[str, Any]) -> Dict[str, Any]:
        meta = {"labeled_by": r["labeled_by"], "label_version": r["label_version"],
                "labeled_at": _iso(r["created_at"]), "pinned_input_hash": r["pinned_input_hash"]}
        if kind == NL_QUERY:
            return {"id": r["item_key"], "kind": kind, "jurisdiction": self.jurisdiction, **r["payload"], **meta}
        permit, inp, pinned = self.permit_input(kind, r["item_key"])
        if pinned != r["pinned_input_hash"]:
            self.store.supersede_label(r["id"], None)
            raise LabelError("the permit's input changed since it was labeled (label superseded)")
        if kind == CHECKLIST:
            return {"id": r["item_key"], "kind": kind, "jurisdiction": self.jurisdiction,
                    "permit_number": r["item_key"], **r["payload"], "snapshot": inp, **meta}
        payload = dict(r["payload"])
        return {"id": r["item_key"], "kind": kind, "jurisdiction": self.jurisdiction, "permit_number": r["item_key"],
                "input": inp, "label": {k: v for k, v in payload.items() if k not in ("labelers",)},
                "labelers": payload.get("labelers") or [], "adjudicated_by": r["labeled_by"], **meta}

    def import_rows(self, rows: Iterable[Dict[str, Any]], *, labeled_by: Optional[str] = None,
                    role: str = LABELER) -> Dict[str, Any]:
        """Import label drafts: JSONL rows {kind, item_key|permit_number|id, payload|label fields, labeled_by}."""
        done, errors = [], []
        for row in rows:
            kind = row.get("kind") or TRADE_SET
            key = row.get("item_key") or row.get("permit_number") or row.get("id")
            payload = row.get("payload") or row.get("label") or {k: v for k, v in row.items() if k not in (
                "kind", "item_key", "permit_number", "id", "labeled_by", "jurisdiction")}
            try:
                done.append(self.annotate(kind, key, payload, labeled_by or row.get("labeled_by") or "", role=role))
            except LabelError as e:
                errors.append({"item_key": key, "error": str(e)})
        return {"imported": len(done), "errors": errors}


# ---- golden-file loader ----------------------------------------------------------------

def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_golden(path: Path, kind: str, *, store: Optional[EvalStore] = None,
                jurisdiction: Optional[str] = None) -> List[Dict[str, Any]]:
    """Golden rows with `status` ACTIVE or SUPERSEDED (+ `status_reason`). Only ACTIVE rows are scored.

    trade_set: the embedded input is re-hashed (content_text -> content_hash
    -> pinned_input_hash). extraction_checklist: with a store, the pinned
    snapshot must still exist with the same content hash. nl_query: the
    question/expected/as_of must hash to the pinned value.
    """
    out = []
    for row in read_jsonl(path):
        reason = None
        if kind == TRADE_SET:
            reason = verify_trade_input(row.get("input") or {}, row.get("pinned_input_hash") or "")
        elif kind == NL_QUERY:
            if input_hash(NL_QUERY, row) != row.get("pinned_input_hash"):
                reason = "question/expected/as_of do not match pinned_input_hash"
        elif kind == CHECKLIST:
            snap = row.get("snapshot") or {}
            if input_hash(CHECKLIST, {"permit_number": row.get("permit_number"),
                                      "content_hash": snap.get("content_hash")}) != row.get("pinned_input_hash"):
                reason = "checklist snapshot does not match pinned_input_hash"
            elif store is not None and snap.get("id") is not None:
                stored = store.snapshot(int(snap["id"]))
                if stored is None or stored.get("content_hash") != snap.get("content_hash"):
                    reason = "pinned snapshot missing or changed in the store"
        if jurisdiction and row.get("jurisdiction") not in (None, jurisdiction):
            continue
        out.append({**row, "status": SUPERSEDED if reason else ACTIVE, "status_reason": reason})
    return out


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows))


def now_iso() -> str:
    return utcnow().replace(microsecond=0).isoformat() + "Z"
