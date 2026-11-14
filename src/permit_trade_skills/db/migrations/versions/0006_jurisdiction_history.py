"""Stage 01 registry: config history and unlinked discovery runs.

- jurisdiction_versions: a jurisdiction's config as it was before each in-place
  update (`pts discovery register --replace`, `pts seeds seattle`), so the
  config a past sync used (sync_metadata.config_version) can be recovered.
- discovery_runs.jurisdiction_id becomes nullable: `pts discovery discover`
  records its run before the jurisdiction is registered; `register` links it.

Additive. Downgrade deletes discovery runs that were never linked to a
jurisdiction (they cannot satisfy NOT NULL) and drops the version history.

Revision ID: 0006_jurisdiction_history
Revises: 0005_skill_inference_audit
Create Date: 2026-11-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0006_jurisdiction_history'
down_revision: Union[str, None] = '0005_skill_inference_audit'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UTC_NOW = sa.text("(now() AT TIME ZONE 'utc')")


def upgrade() -> None:
    op.create_table(
        'jurisdiction_versions',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('jurisdiction_id', sa.String(), nullable=False),
        sa.Column('config_version', sa.Integer(), nullable=False),
        sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('replaced_by', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(['jurisdiction_id'], ['jurisdictions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('jurisdiction_id', 'config_version', name='uq_jurisdiction_versions_version'),
    )
    op.alter_column('discovery_runs', 'jurisdiction_id', existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM discovery_runs WHERE jurisdiction_id IS NULL")
    op.alter_column('discovery_runs', 'jurisdiction_id', existing_type=sa.String(), nullable=False)
    op.drop_table('jurisdiction_versions')
