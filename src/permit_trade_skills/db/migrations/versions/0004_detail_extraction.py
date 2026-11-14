"""Stage 05 detail extraction: snapshot provenance columns + run ledger.

- detail_snapshots: fields, degraded, notes, signals, source_hash,
  raw_responses, run_id.
- New tables: detail_fetch_runs (one row per run), detail_fetch_results
  (one row per permit per run; UNIQUE (run_id, permit_id) for --resume).

Additive only.

Revision ID: 0004_detail_extraction
Revises: 0003_nlu_cache
Create Date: 2026-11-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0004_detail_extraction'
down_revision: Union[str, None] = '0003_nlu_cache'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UTC_NOW = sa.text("(now() AT TIME ZONE 'utc')")
JSONB = postgresql.JSONB(astext_type=sa.Text())
STRATEGIES = "('soda_row', 'redirect_chase', 'playwright')"

SNAPSHOT_COLUMNS = ('fields', 'degraded', 'notes', 'signals', 'source_hash', 'raw_responses', 'run_id')


def upgrade() -> None:
    op.add_column('detail_snapshots', sa.Column('fields', JSONB, server_default=sa.text("'{}'::jsonb"),
                                                nullable=False))
    op.add_column('detail_snapshots', sa.Column('degraded', sa.Boolean(), server_default=sa.text('false'),
                                                nullable=False))
    op.add_column('detail_snapshots', sa.Column('notes', JSONB, server_default=sa.text("'[]'::jsonb"),
                                                nullable=False))
    op.add_column('detail_snapshots', sa.Column('signals', JSONB, nullable=True))
    op.add_column('detail_snapshots', sa.Column('source_hash', sa.String(), nullable=True))
    op.add_column('detail_snapshots', sa.Column('raw_responses', JSONB, nullable=True))
    op.add_column('detail_snapshots', sa.Column('run_id', sa.String(), nullable=True))

    op.create_table(
        'detail_fetch_runs',
        sa.Column('run_id', sa.String(), nullable=False),
        sa.Column('jurisdiction_id', sa.String(), nullable=False),
        sa.Column('trigger', sa.String(), server_default='cli', nullable=False),
        sa.Column('params', JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('status', sa.String(), server_default='running', nullable=False),
        sa.Column('total', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('ok', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('failed', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('skipped', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('running', 'completed', 'failed')", name='ck_detail_fetch_runs_status'),
        sa.ForeignKeyConstraint(['jurisdiction_id'], ['jurisdictions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('run_id'),
    )
    op.create_table(
        'detail_fetch_results',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(), nullable=False),
        sa.Column('permit_id', sa.BigInteger(), nullable=False),
        sa.Column('permit_number', sa.String(), nullable=False),
        sa.Column('outcome', sa.String(), nullable=False),
        sa.Column('fetch_strategy', sa.String(), nullable=True),
        sa.Column('final_url', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.String(), nullable=True),
        sa.Column('snapshot_id', sa.BigInteger(), nullable=True),
        sa.Column('new_snapshot', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('degraded', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('retryable', sa.Boolean(), nullable=True),
        sa.Column('attempts', JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('duration_ms', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.CheckConstraint("outcome IN ('ok', 'failed', 'skipped')", name='ck_detail_fetch_results_outcome'),
        sa.CheckConstraint(f"fetch_strategy IS NULL OR fetch_strategy IN {STRATEGIES}",
                           name='ck_detail_fetch_results_fetch_strategy'),
        sa.ForeignKeyConstraint(['run_id'], ['detail_fetch_runs.run_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['permit_id'], ['permits.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['snapshot_id'], ['detail_snapshots.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('run_id', 'permit_id', name='uq_detail_fetch_results_run_permit'),
    )
    op.create_index('idx_detail_fetch_runs_jurisdiction_started', 'detail_fetch_runs',
                    ['jurisdiction_id', sa.literal_column('started_at DESC')])


def downgrade() -> None:
    op.drop_index('idx_detail_fetch_runs_jurisdiction_started', table_name='detail_fetch_runs')
    op.drop_table('detail_fetch_results')
    op.drop_table('detail_fetch_runs')
    for name in reversed(SNAPSHOT_COLUMNS):
        op.drop_column('detail_snapshots', name)
