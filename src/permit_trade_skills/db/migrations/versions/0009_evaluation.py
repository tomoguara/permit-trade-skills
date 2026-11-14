"""Stage 08 evaluation: golden-set labels, eval results, build gates, drift alerts.

- labels: golden-set labels (trade sets, NL queries, extraction checklists),
  bound to the exact input by `pinned_input_hash`; a changed input supersedes
  a row (`superseded_by`), never overwrites it.
- eval_results (the 0002 stub, realized here): suite, build, item, cache and
  payload columns. Existing columns unchanged.
- build_gates: one `pts quality gate` report per evaluation, with sign-off.
- drift_alerts: drift detector breaches with a suspected cause.

Additive only.

Revision ID: 0009_evaluation
Revises: 0008_orchestration
Create Date: 2026-11-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0009_evaluation'
down_revision: Union[str, None] = '0008_orchestration'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UTC_NOW = sa.text("(now() AT TIME ZONE 'utc')")
JSONB = postgresql.JSONB(astext_type=sa.Text())
EVAL_COLUMNS = ('suite', 'build_id', 'item_key', 'input_hash', 'model', 'prompt_version', 'cache_key')


def upgrade() -> None:
    for name in EVAL_COLUMNS:
        op.add_column('eval_results', sa.Column(name, sa.String(), nullable=True))
    op.add_column('eval_results', sa.Column('payload', JSONB, nullable=True))
    op.create_index('idx_eval_results_suite_created', 'eval_results',
                    ['suite', sa.literal_column('created_at DESC')])
    op.create_index('idx_eval_results_build_suite', 'eval_results', ['build_id', 'suite'])
    op.create_index('idx_eval_results_cache_key', 'eval_results', ['cache_key'],
                    postgresql_where=sa.text('cache_key IS NOT NULL'))

    op.create_table(
        'labels',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('jurisdiction_id', sa.String(), nullable=False),
        sa.Column('permit_id', sa.BigInteger(), nullable=True),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('item_key', sa.String(), nullable=False),
        sa.Column('role', sa.String(), server_default='labeler', nullable=False),
        sa.Column('payload', JSONB, nullable=False),
        sa.Column('labeled_by', sa.String(), nullable=False),
        sa.Column('label_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('pinned_input_hash', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('superseded_by', sa.BigInteger(), nullable=True),
        sa.CheckConstraint("kind IN ('trade_set', 'nl_query', 'extraction_checklist')", name='ck_labels_kind'),
        sa.CheckConstraint("role IN ('labeler', 'adjudicator')", name='ck_labels_role'),
        sa.ForeignKeyConstraint(['jurisdiction_id'], ['jurisdictions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['permit_id'], ['permits.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['superseded_by'], ['labels.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_labels_kind_item', 'labels', ['kind', 'item_key', 'created_at'])
    op.create_index('uq_labels_current', 'labels', ['jurisdiction_id', 'kind', 'item_key', 'role', 'labeled_by'],
                    unique=True, postgresql_where=sa.text('superseded_by IS NULL'))

    op.create_table(
        'build_gates',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('build_id', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('report', JSONB, nullable=False),
        sa.Column('signed_off_by', sa.String(), nullable=True),
        sa.Column('signed_off_at', sa.DateTime(), nullable=True),
        sa.Column('sign_off_note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.CheckConstraint("status IN ('PASS', 'CONDITIONAL', 'FAIL')", name='ck_build_gates_status'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_build_gates_build_created', 'build_gates',
                    ['build_id', sa.literal_column('created_at DESC')])

    op.create_table(
        'drift_alerts',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('jurisdiction_id', sa.String(), nullable=True),
        sa.Column('detector', sa.String(), nullable=False),
        sa.Column('severity', sa.String(), server_default='warning', nullable=False),
        sa.Column('suspected_cause', sa.String(), nullable=True),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('measured', JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('baseline', JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('window_start', sa.DateTime(), nullable=True),
        sa.Column('window_end', sa.DateTime(), nullable=True),
        sa.Column('notified', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('acknowledged_by', sa.String(), nullable=True),
        sa.Column('acknowledged_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.CheckConstraint("severity IN ('info', 'warning', 'critical')", name='ck_drift_alerts_severity'),
        sa.ForeignKeyConstraint(['jurisdiction_id'], ['jurisdictions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_drift_alerts_created', 'drift_alerts', [sa.literal_column('created_at DESC')])


def downgrade() -> None:
    op.drop_index('idx_drift_alerts_created', table_name='drift_alerts')
    op.drop_table('drift_alerts')
    op.drop_index('idx_build_gates_build_created', table_name='build_gates')
    op.drop_table('build_gates')
    op.drop_index('uq_labels_current', table_name='labels')
    op.drop_index('idx_labels_kind_item', table_name='labels')
    op.drop_table('labels')
    op.drop_index('idx_eval_results_cache_key', table_name='eval_results')
    op.drop_index('idx_eval_results_build_suite', table_name='eval_results')
    op.drop_index('idx_eval_results_suite_created', table_name='eval_results')
    op.drop_column('eval_results', 'payload')
    for name in reversed(EVAL_COLUMNS):
        op.drop_column('eval_results', name)
