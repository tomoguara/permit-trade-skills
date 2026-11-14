"""Stage 06 trade-skill inference: skill_inferences audit columns.

- prompt_hash, completion_hash: sha256 of the rendered prompt / raw completion
  (eval replay, Stage 08).
- context_hash, input_cache_key: the idempotency key. A repeated
  (snapshot, prompt_version, model, context) returns the stored row instead of
  calling the LLM again; UNIQUE (permit_id, input_cache_key) makes that hold
  under concurrent workers.
- degraded, substrate_hash, substrate_text: degraded inferences (no detail
  snapshot) cite catalog text, kept in substrate_text so the citations stay
  checkable.
- overall_confidence, licensing_notes, dropped_skills, warnings, signal_check.

Additive only.

Revision ID: 0005_skill_inference_audit
Revises: 0004_detail_extraction
Create Date: 2026-11-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0005_skill_inference_audit'
down_revision: Union[str, None] = '0004_detail_extraction'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
EMPTY_LIST = sa.text("'[]'::jsonb")

COLUMNS = ('prompt_hash', 'completion_hash', 'context_hash', 'input_cache_key', 'degraded', 'substrate_hash',
           'substrate_text', 'overall_confidence', 'licensing_notes', 'dropped_skills', 'warnings',
           'signal_check')


def upgrade() -> None:
    op.add_column('skill_inferences', sa.Column('prompt_hash', sa.String(), nullable=True))
    op.add_column('skill_inferences', sa.Column('completion_hash', sa.String(), nullable=True))
    op.add_column('skill_inferences', sa.Column('context_hash', sa.String(), nullable=True))
    op.add_column('skill_inferences', sa.Column('input_cache_key', sa.String(), nullable=True))
    op.add_column('skill_inferences', sa.Column('degraded', sa.Boolean(), server_default=sa.text('false'),
                                                nullable=False))
    op.add_column('skill_inferences', sa.Column('substrate_hash', sa.String(), nullable=True))
    op.add_column('skill_inferences', sa.Column('substrate_text', sa.Text(), nullable=True))
    op.add_column('skill_inferences', sa.Column('overall_confidence', sa.String(), nullable=True))
    op.add_column('skill_inferences', sa.Column('licensing_notes', JSONB, server_default=EMPTY_LIST,
                                                nullable=False))
    op.add_column('skill_inferences', sa.Column('dropped_skills', JSONB, server_default=EMPTY_LIST,
                                                nullable=False))
    op.add_column('skill_inferences', sa.Column('warnings', JSONB, server_default=EMPTY_LIST, nullable=False))
    op.add_column('skill_inferences', sa.Column('signal_check', JSONB, nullable=True))
    op.create_check_constraint(
        'ck_skill_inferences_overall_confidence', 'skill_inferences',
        "overall_confidence IS NULL OR overall_confidence IN ('high', 'medium', 'low')")
    op.create_index('uq_skill_inferences_permit_cache_key', 'skill_inferences',
                    ['permit_id', 'input_cache_key'], unique=True,
                    postgresql_where=sa.text('input_cache_key IS NOT NULL'))


def downgrade() -> None:
    op.drop_index('uq_skill_inferences_permit_cache_key', table_name='skill_inferences')
    op.drop_constraint('ck_skill_inferences_overall_confidence', 'skill_inferences', type_='check')
    for name in reversed(COLUMNS):
        op.drop_column('skill_inferences', name)
