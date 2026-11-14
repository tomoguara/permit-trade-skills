"""Schema management on top of Alembic.

Alembic migrations in ``permit_trade_skills/db/migrations`` are the only way the
schema changes. This module wraps them for the CLI (`pts db ...`) and for
``Registry.create_tables()``:

- ``ensure_schema``: upgrade to head; a pre-Alembic database (tables present,
  no ``alembic_version``) is first stamped at the ``0001_initial`` baseline.
- Destructive-change protocol: a revision whose docstring contains a line that
  is exactly ``--destructive`` is refused unless ``allow_destructive=True``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import Script, ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"
BASELINE_REVISION = "0001_initial"
DESTRUCTIVE_MARKER = "--destructive"


class DestructiveMigrationError(RuntimeError):
    """Raised when a pending revision is destructive and wasn't explicitly allowed."""

    def __init__(self, revisions: List[str]):
        self.revisions = revisions
        super().__init__(
            "Refusing destructive migration(s): "
            + ", ".join(revisions)
            + ". Back up first (`pts db backup`), then run `pts db migrate --destructive`."
        )


@dataclass
class SchemaStatus:
    current: Optional[str]
    head: str
    pending: List[str]
    destructive_pending: List[str]
    pre_alembic: bool  # tables exist but no alembic_version (needs baseline stamp)


def _engine(target: Union[str, Engine]) -> Engine:
    return target if isinstance(target, Engine) else create_engine(target)


def alembic_config(db_url: Optional[str] = None) -> Config:
    """An in-memory Alembic Config pointing at our migrations."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["configure_logger"] = False
    if db_url:
        cfg.attributes["db_url"] = db_url
    return cfg


def script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(alembic_config())


def is_destructive(script: Script) -> bool:
    doc = script.module.__doc__ or ""
    return any(line.strip() == DESTRUCTIVE_MARKER for line in doc.splitlines())


def _current_revision(engine: Engine) -> Optional[str]:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _pending(current: Optional[str], target: str = "head") -> List[Script]:
    scripts = script_directory()
    # walk_revisions yields newest -> oldest between the two points
    revs = list(scripts.walk_revisions(base=current or "base", head=target))
    return [r for r in reversed(revs) if r.revision != current]


def status(target: Union[str, Engine]) -> SchemaStatus:
    engine = _engine(target)
    current = _current_revision(engine)
    tables = set(inspect(engine).get_table_names())
    pre_alembic = current is None and "permits" in tables
    pending = _pending(BASELINE_REVISION if pre_alembic else current)
    return SchemaStatus(
        current=current,
        head=script_directory().get_current_head(),
        pending=[s.revision for s in pending],
        destructive_pending=[s.revision for s in pending if is_destructive(s)],
        pre_alembic=pre_alembic,
    )


def _run(engine: Engine, fn, *args, **kwargs) -> None:
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        fn(cfg, *args, **kwargs)


def stamp_baseline_if_needed(target: Union[str, Engine]) -> bool:
    """Stamp a pre-Alembic database at the baseline. Returns True if stamped."""
    engine = _engine(target)
    if status(engine).pre_alembic:
        _run(engine, command.stamp, BASELINE_REVISION)
        return True
    return False


def upgrade(target: Union[str, Engine], revision: str = "head",
            allow_destructive: bool = False) -> List[str]:
    """Upgrade to `revision`. Returns the revisions that were applied."""
    engine = _engine(target)
    stamp_baseline_if_needed(engine)
    current = _current_revision(engine)
    pending = _pending(current, revision)
    destructive = [s.revision for s in pending if is_destructive(s)]
    if destructive and not allow_destructive:
        raise DestructiveMigrationError(destructive)
    if pending:
        _run(engine, command.upgrade, revision)
    return [s.revision for s in pending]


def downgrade(target: Union[str, Engine], revision: str = "-1") -> None:
    _run(_engine(target), command.downgrade, revision)


def ensure_schema(target: Union[str, Engine]) -> List[str]:
    """Idempotently bring the schema to head (never applies destructive revisions)."""
    return upgrade(target, "head", allow_destructive=False)


def heads() -> List[str]:
    return list(script_directory().get_heads())


def new_revision(db_url: str, message: str, autogenerate: bool = False,
                 rev_id: Optional[str] = None) -> Script:
    """Create a revision file (autogenerate diffs models vs the database at db_url)."""
    cfg = alembic_config(db_url)
    return command.revision(cfg, message=message, autogenerate=autogenerate, rev_id=rev_id)
