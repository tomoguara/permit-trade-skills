"""Hybrid retrieval (Stage 04 Q3): RRF math, determinism, dedup, tie-break, degraded mode."""

import pytest

from permit_trade_skills.query.retrieve import retrieval_id, retrieve, rrf_fuse
from permit_trade_skills.store.query import PermitQuery

Q = PermitQuery(jurisdiction="seattle-kingcounty-wa", status="ISSUED")


def row(n):
    return {"permit_number": n}


class FakeBackend:
    def __init__(self, structured=(), text=(), vector=(), embeddings=True):
        self._structured = [row(n) for n in structured]
        self._text = [(row(n), 1.0) for n in text]
        self._vector = [(row(n), 0.9) for n in vector]
        self.embeddings = embeddings
        self.calls = []

    def structured(self, q, limit):
        self.calls.append(("structured", limit))
        return self._structured[:limit]

    def text_ranked(self, q, text, limit):
        self.calls.append(("text", text, limit))
        return self._text[:limit]

    def vector_ranked(self, q, embedding, limit):
        self.calls.append(("vector", tuple(embedding), limit))
        return self._vector[:limit]

    def has_embeddings(self, jurisdiction):
        return self.embeddings


def embed(text):
    return [0.1, 0.2, 0.3]


def test_rrf_math_hand_computed():
    fused = rrf_fuse({"text": ["A", "B", "C"], "vector": ["C", "A", "D"]}, rrf_k=60)
    scores = {key: score for key, score, *_ in fused}
    assert scores["A"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["C"] == pytest.approx(1 / 63 + 1 / 61)
    assert scores["B"] == pytest.approx(1 / 62)
    assert scores["D"] == pytest.approx(1 / 63)
    assert [key for key, *_ in fused] == ["A", "C", "B", "D"]
    by_key = {key: (matched, ranks) for key, _, matched, ranks in fused}
    assert by_key["A"] == (["text", "vector"], {"text": 1, "vector": 2})
    assert by_key["D"] == (["vector"], {"vector": 3})


def test_three_mode_fusion():
    backend = FakeBackend(structured=["S1", "X"], text=["X", "T1"], vector=["X", "V1"])
    result = retrieve(Q, "deck", k=10, modes=("structured", "text", "vector"), backend=backend,
                      embedder=embed)
    assert result.fused_order[0] == "X"   # in all three lists
    assert result.rows[0].matched_by == ["structured", "text", "vector"]
    assert result.rows[0].score == pytest.approx(1 / 62 + 1 / 61 + 1 / 61)
    assert result.candidate_counts == {"structured": 2, "text": 2, "vector": 2}
    assert set(result.fused_order) == {"X", "S1", "T1", "V1"}
    assert result.degraded is False


def test_tie_break_by_permit_number():
    # B and A have identical scores (rank 1 in one list each) -> ordered by permit_number.
    fused = rrf_fuse({"text": ["B"], "vector": ["A"]})
    assert [key for key, *_ in fused] == ["A", "B"]


def test_dedup_within_and_across_modes():
    backend = FakeBackend(text=["A", "A", "B"], vector=["B", "A"])
    result = retrieve(Q, "roof", k=10, backend=backend, embedder=embed)
    # A: text 1 + vector 2 = 1/61 + 1/62; B: text 3 (position kept) + vector 1 = 1/63 + 1/61
    assert result.fused_order == ["A", "B"]
    a = next(r for r in result.rows if r.permit_number == "A")
    assert a.ranks == {"text": 1, "vector": 2}


def test_determinism_same_inputs_same_output_and_id():
    backend = FakeBackend(text=["C", "A", "B"], vector=["B", "D"])
    first = retrieve(Q, "kitchen", k=3, backend=backend, embedder=embed).to_dict()
    second = retrieve(Q, "kitchen", k=3, backend=backend, embedder=embed).to_dict()
    assert first == second
    assert first["retrieval_id"] == retrieval_id(Q, "kitchen", 3, ["text", "vector"])
    assert first["retrieval_id"] != retrieval_id(Q, "kitchen", 4, ["text", "vector"])
    assert len(first["rows"]) == 3


def test_candidate_limit_is_twice_k():
    backend = FakeBackend(text=[f"P{i}" for i in range(50)], vector=[])
    retrieve(Q, "x", k=5, backend=backend, embedder=embed)
    assert ("text", "x", 10) in backend.calls


def test_degraded_without_embeddings_matches_non_vector_fusion():
    backend = FakeBackend(text=["C", "A", "B"], vector=["Z", "Y"], embeddings=False)
    degraded = retrieve(Q, "solar", k=10, modes=("text", "vector"), backend=backend, embedder=embed)
    text_only = retrieve(Q, "solar", k=10, modes=("text",), backend=backend, embedder=embed)
    assert degraded.degraded is True
    assert degraded.modes_used == ["text"]
    assert any("no embeddings" in n for n in degraded.notes)
    assert [r.permit_number for r in degraded.rows] == [r.permit_number for r in text_only.rows]
    assert [r.score for r in degraded.rows] == [r.score for r in text_only.rows]
    assert not any(c[0] == "vector" for c in backend.calls)


def test_degraded_when_embedding_model_unavailable():
    backend = FakeBackend(text=["A"], vector=["B"])
    result = retrieve(Q, "solar", backend=backend, embedder=lambda text: [])
    assert result.degraded and "embedding model is unavailable" in result.notes[0]
    assert result.fused_order == ["A"]


def test_without_text_only_structured_runs():
    backend = FakeBackend(structured=["S2", "S1"], text=["T"], vector=["V"])
    result = retrieve(Q, None, k=10, backend=backend, embedder=embed)
    assert result.modes_used == ["structured"]
    assert result.fused_order == ["S2", "S1"]   # query sort order preserved
    assert result.degraded is False and result.notes


@pytest.mark.parametrize("modes", [(), ("bm25",)])
def test_invalid_modes_rejected(modes):
    with pytest.raises(ValueError):
        retrieve(Q, "x", modes=modes, backend=FakeBackend())
