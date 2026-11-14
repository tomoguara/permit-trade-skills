"""Alembic environment for database migrations.

The SQLAlchemy models in ``permit_trade_skills.discovery.registry`` are the
single source of schema truth; ``target_metadata`` points at them so
``pts db revision --autogenerate`` can draft migrations.

Database URL precedence:
1. ``config.attributes["db_url"]`` (set programmatically by ``pts db ...``),
2. ``alembic -x db_url=...`` on the command line,
3. ``PERMIT_TRADE_SKILLS_DB_URL`` from the shell or ``.env``, else the local
   default (``permit_trade_skills.config.get_db_url``).
The ``sqlalchemy.url`` in alembic.ini is deliberately ignored.
"""

from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context

from permit_trade_skills.config import get_db_url
from permit_trade_skills.discovery.registry import Base

config = context.config

# Only configure logging when invoked via the alembic CLI with an ini file;
# `pts db` passes its own Config and keeps the application's logging.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _db_url() -> str:
    return (
        config.attributes.get("db_url")
        or context.get_x_argument(as_dictionary=True).get("db_url")
        or get_db_url()
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a connection)."""
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = create_engine(_db_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        _run(connection)


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
