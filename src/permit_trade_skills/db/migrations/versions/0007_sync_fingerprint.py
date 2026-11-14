"""Stage 02 incremental sync: high-water marks and a pulled-set fingerprint.

sync_metadata gains
- max_applied_at: latest applied date among the rows the run pulled,
- max_synced_at: latest source row timestamp (Socrata `:updated_at`) pulled,
- fingerprint: sha256 over (config_version, permit_number, raw_hash) of the pulled
  set. A run whose fingerprint equals the previous successful run's is a no-op.

Additive only.

Revision ID: 0007_sync_fingerprint
Revises: 0006_jurisdiction_history
Create Date: 2026-11-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0007_sync_fingerprint'
down_revision: Union[str, None] = '0006_jurisdiction_history'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sync_metadata', sa.Column('max_applied_at', sa.DateTime(), nullable=True))
    op.add_column('sync_metadata', sa.Column('max_synced_at', sa.DateTime(), nullable=True))
    op.add_column('sync_metadata', sa.Column('fingerprint', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('sync_metadata', 'fingerprint')
    op.drop_column('sync_metadata', 'max_synced_at')
    op.drop_column('sync_metadata', 'max_applied_at')
