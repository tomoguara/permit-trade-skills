"""Stage 04 query: NLU parse cache.

Adds `nlu_cache` (one row per cached natural-language parse, 24h TTL) with an
index on expires_at for `pts db prune`. Additive only.

Revision ID: 0003_nlu_cache
Revises: 0002_canonical_store
Create Date: 2026-11-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0003_nlu_cache'
down_revision: Union[str, None] = '0002_canonical_store'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UTC_NOW = sa.text("(now() AT TIME ZONE 'utc')")


def upgrade() -> None:
    op.create_table(
        'nlu_cache',
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('jurisdiction', sa.String(), nullable=True),
        sa.Column('schema_version', sa.String(), nullable=False),
        sa.Column('model', sa.String(), nullable=False),
        sa.Column('response', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('key', name='nlu_cache_pkey'),
    )
    op.create_index('idx_nlu_cache_expires', 'nlu_cache', ['expires_at'])


def downgrade() -> None:
    op.drop_index('idx_nlu_cache_expires', table_name='nlu_cache')
    op.drop_table('nlu_cache')
