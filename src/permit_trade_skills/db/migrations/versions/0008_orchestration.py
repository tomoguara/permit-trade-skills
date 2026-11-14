"""Stage 07 orchestration: event trail, pipeline run reports, retry backlog, HITL proposals.

- orchestration_events: append-only trail of planner tool calls, pipeline
  steps, state transitions and review decisions (one run_id per pipeline run
  or agent goal); params_json/result_json feed `pts agent replay`.
- pipeline_runs: one report row per `pts pipeline run`.
- retry_backlog: per-permit cross-run retry state (attempts, next_retry_at,
  exhausted). With permits.processing_state it is the durable queue.
- skill_inferences.proposed_by / proposed_decision: an agent's review
  proposal is stored as review_status NEEDS_HUMAN with proposed_by 'agent'.

Additive only.

Revision ID: 0008_orchestration
Revises: 0007_sync_fingerprint
Create Date: 2026-11-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0008_orchestration'
down_revision: Union[str, None] = '0007_sync_fingerprint'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UTC_NOW = sa.text("(now() AT TIME ZONE 'utc')")
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column('skill_inferences', sa.Column('proposed_by', sa.String(), nullable=True))
    op.add_column('skill_inferences', sa.Column('proposed_decision', sa.String(), nullable=True))
    op.create_check_constraint('ck_skill_inferences_proposed_by', 'skill_inferences',
                               "proposed_by IS NULL OR proposed_by IN ('agent', 'human')")
    op.create_check_constraint('ck_skill_inferences_proposed_decision', 'skill_inferences',
                               "proposed_decision IS NULL OR proposed_decision IN "
                               "('approve', 'reject', 'needs_human')")

    op.create_table(
        'orchestration_events',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(), nullable=False),
        sa.Column('permit_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('event', sa.String(), nullable=False),
        sa.Column('from_state', sa.String(), nullable=True),
        sa.Column('to_state', sa.String(), nullable=True),
        sa.Column('tool', sa.String(), nullable=True),
        sa.Column('params_json', JSONB, nullable=True),
        sa.Column('result_json', JSONB, nullable=True),
        sa.Column('ok', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('error_code', sa.String(), nullable=True),
        sa.Column('retryable', sa.Boolean(), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['permit_id'], ['permits.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'pipeline_runs',
        sa.Column('run_id', sa.String(), nullable=False),
        sa.Column('jurisdiction_id', sa.String(), nullable=False),
        sa.Column('trigger', sa.String(), server_default='cli', nullable=False),
        sa.Column('params', JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('status', sa.String(), server_default='running', nullable=False),
        sa.Column('planned', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('completed', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('failed', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('skipped', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('queued', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('llm_calls', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('tokens', JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('est_cost', sa.Numeric(), nullable=True),
        sa.Column('started_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('running', 'completed', 'stopped', 'failed')",
                           name='ck_pipeline_runs_status'),
        sa.ForeignKeyConstraint(['jurisdiction_id'], ['jurisdictions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('run_id'),
    )
    op.create_table(
        'retry_backlog',
        sa.Column('permit_id', sa.BigInteger(), nullable=False),
        sa.Column('stage', sa.String(), nullable=False),
        sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('next_retry_at', sa.DateTime(), nullable=True),
        sa.Column('exhausted', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('last_error', JSONB, nullable=True),
        sa.Column('last_run_id', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.CheckConstraint("stage IN ('detail', 'skill_infer')", name='ck_retry_backlog_stage'),
        sa.ForeignKeyConstraint(['permit_id'], ['permits.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('permit_id'),
    )
    op.create_index('idx_orchestration_events_run', 'orchestration_events', ['run_id', 'id'])
    op.create_index('idx_orchestration_events_permit_created', 'orchestration_events', ['permit_id', 'created_at'])
    op.create_index('brin_orchestration_events_created_at', 'orchestration_events', ['created_at'],
                    postgresql_using='brin')
    op.create_index('idx_pipeline_runs_jurisdiction_started', 'pipeline_runs',
                    ['jurisdiction_id', sa.literal_column('started_at DESC')])
    op.create_index('idx_retry_backlog_next_retry', 'retry_backlog', ['next_retry_at'],
                    postgresql_where=sa.text('NOT exhausted'))


def downgrade() -> None:
    op.drop_index('idx_retry_backlog_next_retry', table_name='retry_backlog')
    op.drop_index('idx_pipeline_runs_jurisdiction_started', table_name='pipeline_runs')
    op.drop_index('brin_orchestration_events_created_at', table_name='orchestration_events')
    op.drop_index('idx_orchestration_events_permit_created', table_name='orchestration_events')
    op.drop_index('idx_orchestration_events_run', table_name='orchestration_events')
    op.drop_table('retry_backlog')
    op.drop_table('pipeline_runs')
    op.drop_table('orchestration_events')
    op.drop_constraint('ck_skill_inferences_proposed_decision', 'skill_inferences', type_='check')
    op.drop_constraint('ck_skill_inferences_proposed_by', 'skill_inferences', type_='check')
    op.drop_column('skill_inferences', 'proposed_decision')
    op.drop_column('skill_inferences', 'proposed_by')
