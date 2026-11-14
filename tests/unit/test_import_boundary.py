"""Module boundary: SQLAlchemy is imported only by the store, ingestion, and an
explicit allowlist of modules that predate the store's repository API.

New code must go through permit_trade_skills.store (repos / session factory).
Shrink ALLOWLIST as legacy modules move onto the repositories; never grow it
without a comment next to the new entry explaining why.
"""

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "src" / "permit_trade_skills"
ALLOWED_DIRS = ("store/", "ingest/", "db/migrations/")
ALLOWLIST = {
    "discovery/registry.py",   # ORM models + Registry (schema source of truth)
    "cli/search.py",           # search/retrieval queries (Stage 4 surface)
    "cli/db/commands.py",      # schema tooling (create_engine for pts db)
}
IMPORT_RE = re.compile(r"^\s*(from|import)\s+sqlalchemy\b", re.M)


def test_no_sqlalchemy_imports_outside_store():
    offenders = []
    for path in PKG.rglob("*.py"):
        rel = path.relative_to(PKG).as_posix()
        if rel.startswith(ALLOWED_DIRS) or rel in ALLOWLIST:
            continue
        if IMPORT_RE.search(path.read_text()):
            offenders.append(rel)
    assert not offenders, f"SQLAlchemy imported outside store/ingest: {offenders}"


def test_allowlist_entries_still_exist():
    missing = [p for p in ALLOWLIST if not (PKG / p).exists()]
    assert not missing, f"stale allowlist entries: {missing}"


# Stage 04 Q4: later stages query data only through the query tools
# (permit_trade_skills.query.tools), never the store directly.
TOOL_ONLY_PACKAGES = ("skills/", "detail/", "orchestration/", "eval/")
STORE_IMPORT_RE = re.compile(
    r"^\s*(from\s+permit_trade_skills\.store\b|import\s+permit_trade_skills\.store\b"
    r"|from\s+permit_trade_skills\s+import\s+store\b)", re.M)


def test_later_stages_use_query_tools_not_the_store():
    offenders = [path.relative_to(PKG).as_posix() for path in PKG.rglob("*.py")
                 if path.relative_to(PKG).as_posix().startswith(TOOL_ONLY_PACKAGES)
                 and STORE_IMPORT_RE.search(path.read_text())]
    assert not offenders, f"import permit_trade_skills.query.tools instead of the store: {offenders}"


def test_store_import_pattern_matches():
    assert STORE_IMPORT_RE.search("from permit_trade_skills.store.repos import PermitRepository")
    assert STORE_IMPORT_RE.search("import permit_trade_skills.store")
    assert not STORE_IMPORT_RE.search("from permit_trade_skills.query.tools import call_tool")


# Stage 06 definition of done: the grounding gate is the only path to an
# accepted skill_inferences row, and Stage 07 uses only `skills.infer`.
SAVE_RE = re.compile(r"\.save_inference\(")
SAVE_ACCEPTED_RE = re.compile(r"\bsave_accepted\(")
APPROVE_RE = re.compile(r"review_status\s*[=:]\s*['\"](APPROVED|REJECTED)['\"]|['\"]APPROVED['\"]")
SKILLS_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+permit_trade_skills\.skills(\.\w+)?", re.M)


def _sources(prefix=""):
    for path in PKG.rglob("*.py"):
        rel = path.relative_to(PKG).as_posix()
        if rel.startswith(prefix):
            yield rel, path.read_text()


def test_only_persist_writes_skill_inferences():
    writers = sorted(rel for rel, src in _sources() if SAVE_RE.search(src) and not rel.startswith("store/"))
    assert writers == ["skills/persist.py"], writers
    callers = sorted(rel for rel, src in _sources() if SAVE_ACCEPTED_RE.search(src)
                     and rel != "skills/persist.py")
    assert callers == ["skills/infer.py"], callers
    infer_src = (PKG / "skills" / "infer.py").read_text()
    assert infer_src.index("G.gate(") < infer_src.index("persist.save_accepted(")


# Stage 07: the review workflow (orchestration/review.py) is the only code that may
# write an approve/reject review_status.
REVIEW_WRITERS = ("discovery/registry.py", "orchestration/review.py")
# Stage 08: read-only queries that look for approve/reject decisions made without a human (H7).
REVIEW_READERS = ("store/eval.py",)


def test_no_code_approves_or_rejects_inferences():
    offenders = [rel for rel, src in _sources() if APPROVE_RE.search(src)
                 and rel not in REVIEW_WRITERS + REVIEW_READERS and not rel.startswith("db/migrations/")]
    assert not offenders, f"review decisions belong to the Stage 07 review workflow: {offenders}"
    assert APPROVE_RE.search((PKG / "orchestration" / "review.py").read_text())


def test_orchestration_uses_only_skills_infer():
    imports = {m.group(0).split()[-1] for rel, src in _sources("orchestration/")
               for m in SKILLS_IMPORT_RE.finditer(src)}
    assert imports <= {"permit_trade_skills.skills.infer", "permit_trade_skills.skills"}, imports


# Stage 07 AC5: orchestration is jurisdiction-parameterized: no jurisdiction ids,
# names or hosts in its code or prompts (tests and docs may name Seattle).
HARDCODED_RE = re.compile(r"seattle|king\s*county|kingcounty|\bwa\b|-wa\b", re.I)


def test_orchestration_has_no_jurisdiction_hardcoding_ac5():
    files = [p for p in (PKG / "orchestration").rglob("*") if p.suffix in (".py", ".md")]
    assert any(p.name == "planner_v1.md" for p in files)
    offenders = [f"{p.relative_to(PKG)}:{i}" for p in files
                 for i, line in enumerate(p.read_text().splitlines(), 1) if HARDCODED_RE.search(line)]
    assert not offenders, offenders


def test_hardcoding_pattern_matches():
    for text in ("seattle-kingcounty-wa", "Seattle", "King County", "state = 'WA'", "foo-wa"):
        assert HARDCODED_RE.search(text), text
    for text in ("await", "toward", "wall clock", "waiting", "aware"):
        assert not HARDCODED_RE.search(text), text


# Stage 08: evaluation is jurisdiction-parameterized too (datasets and evidence dirs come from config).
def test_eval_has_no_jurisdiction_hardcoding():
    files = [p for p in (PKG / "eval").rglob("*") if p.suffix in (".py", ".md")]
    assert any(p.name == "judge_v1.md" for p in files)
    offenders = [f"{p.relative_to(PKG)}:{i}" for p in files
                 for i, line in enumerate(p.read_text().splitlines(), 1) if HARDCODED_RE.search(line)]
    assert not offenders, offenders


def test_eval_flags_through_the_review_proposal_path_only():
    """The judge/H1 never decide a review: no `decide(`, no PTS_REVIEWER, no review_status writes in eval/."""
    for rel, src in _sources("eval/"):
        assert ".decide(" not in src and "PTS_REVIEWER" not in src, rel
        assert not re.search(r"review_status\s*=\s*['\"]", src), rel
