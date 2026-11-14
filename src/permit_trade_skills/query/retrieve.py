"""Hybrid retrieval: structured filters + trigram text + pgvector, fused with RRF.

Semantics (same as `pts search hybrid`):
- The PermitQuery's structured filters constrain *every* mode, so every
  returned row satisfies every filter.
- `text` ranks permits whose description contains the search text by trigram
  `word_similarity`; `vector` ranks embedded permits by cosine similarity
  between the search-text embedding and `permits.description_embedding`.
- `structured` (opt-in) contributes the filtered rows in the query's sort order
  as a third ranked list. Without search text, only `structured` can run.
- Each mode contributes up to 2*k candidates; lists are fused with Reciprocal
  Rank Fusion, score = sum over modes of 1 / (rrf_k + rank), rrf_k = 60. Ties are
  broken by permit_number, so the order is deterministic.
- If `vector` is requested but the jurisdiction has no embeddings, or the
  embedding model is unavailable, vector is dropped, `degraded` is true and
  `notes` says why. The query does not fail.

`retrieve()` is a pure function of its inputs plus the backend. Tests pass a
fake backend; `RepoBackend` (query/service.py) reads the store.
`retrieval_id` = sha256 of the inputs, for eval replay.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from permit_trade_skills.store.query import PermitQuery

MODES = ("structured", "text", "vector")
DEFAULT_MODES = ("text", "vector")
RRF_K = 60

Row = Dict[str, Any]          # serialized permit; must contain "permit_number"
Ranked = List[Tuple[Row, float]]


class RetrievalBackend(Protocol):
    def structured(self, q: PermitQuery, limit: int) -> List[Row]: ...
    def text_ranked(self, q: PermitQuery, text: str, limit: int) -> Ranked: ...
    def vector_ranked(self, q: PermitQuery, embedding: Sequence[float], limit: int) -> Ranked: ...
    def has_embeddings(self, jurisdiction: str) -> bool: ...


Embedder = Callable[[str], Sequence[float]]   # returns [] when the model is unavailable


@dataclass
class RetrievedRow:
    permit_number: str
    score: float
    matched_by: List[str]
    ranks: Dict[str, int]
    permit: Row


@dataclass
class RetrievalResult:
    retrieval_id: str
    query: Dict[str, Any]
    text: Optional[str]
    k: int
    modes_requested: List[str]
    modes_used: List[str]
    degraded: bool
    notes: List[str]
    candidate_counts: Dict[str, int]
    fused_order: List[str]
    rows: List[RetrievedRow] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def rrf_fuse(ranked: Dict[str, List[str]], rrf_k: int = RRF_K
             ) -> List[Tuple[str, float, List[str], Dict[str, int]]]:
    """Fuse ranked key lists. Returns [(key, score, matched_by, ranks)] best first.

    Ranks are 1-based; a key repeated within one list counts at its best rank.
    Ties on score are broken by key ascending.
    """
    scores: Dict[str, float] = {}
    matched: Dict[str, List[str]] = {}
    ranks: Dict[str, Dict[str, int]] = {}
    for mode, keys in ranked.items():
        seen = set()
        for rank, key in enumerate(keys, 1):
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank)
            matched.setdefault(key, []).append(mode)
            ranks.setdefault(key, {})[mode] = rank
    order = sorted(scores, key=lambda key: (-scores[key], key))
    return [(key, scores[key], matched[key], ranks[key]) for key in order]


def retrieval_id(query: PermitQuery, text: Optional[str], k: int, modes: Sequence[str],
                 rrf_k: int = RRF_K) -> str:
    payload = {"query": query.to_dict(), "text": text, "k": k, "modes": list(modes), "rrf_k": rrf_k}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def retrieve(query: PermitQuery, text: Optional[str] = None, k: int = 10,
             modes: Sequence[str] = DEFAULT_MODES, *, backend: RetrievalBackend,
             embedder: Optional[Embedder] = None, rrf_k: int = RRF_K) -> RetrievalResult:
    modes = list(dict.fromkeys(modes))
    unknown = [m for m in modes if m not in MODES]
    if unknown or not modes:
        raise ValueError(f"modes must be a non-empty subset of {MODES}, got {modes}")
    if k < 1:
        raise ValueError("k must be >= 1")
    text = (text or "").strip() or None
    notes: List[str] = []
    degraded = False
    used = list(modes)

    if text is None:
        dropped = [m for m in used if m != "structured"]
        if dropped:
            notes.append(f"no search text: {', '.join(dropped)} skipped; structured filters only")
        used = ["structured"]

    embedding: Sequence[float] = []
    if "vector" in used:
        reason = None
        if not backend.has_embeddings(query.jurisdiction):
            reason = f"no embeddings exist for {query.jurisdiction}"
        else:
            embedding = (embedder(text) if embedder else []) or []
            if not embedding:
                reason = "the embedding model is unavailable"
        if reason:
            used.remove("vector")
            degraded = True
            notes.append(f"vector mode unavailable ({reason}); results use {', '.join(used) or 'nothing'}")

    limit = 2 * k
    lists: Dict[str, List[str]] = {}
    rows: Dict[str, Row] = {}
    for mode in used:
        if mode == "structured":
            ranked = [(row, 0.0) for row in backend.structured(query, limit)]
        elif mode == "text":
            ranked = backend.text_ranked(query, text, limit)
        else:
            ranked = backend.vector_ranked(query, embedding, limit)
        lists[mode] = [row["permit_number"] for row, _ in ranked]
        for row, _ in ranked:
            rows.setdefault(row["permit_number"], row)

    fused = rrf_fuse(lists, rrf_k)[:k]
    return RetrievalResult(
        retrieval_id=retrieval_id(query, text, k, modes, rrf_k),
        query=query.to_dict(), text=text, k=k, modes_requested=modes, modes_used=used,
        degraded=degraded, notes=notes,
        candidate_counts={mode: len(keys) for mode, keys in lists.items()},
        fused_order=[key for key, *_ in fused],
        rows=[RetrievedRow(key, round(score, 8), matched, rk, rows[key])
              for key, score, matched, rk in fused],
    )
