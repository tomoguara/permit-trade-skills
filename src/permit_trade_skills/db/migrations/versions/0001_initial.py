"""Baseline: the schema as built by Stages 1-2 (before Stage 3 additions).

This revision reproduces exactly what SQLAlchemy create_all() produced for the
models before Alembic became the source of truth, so databases created that way
can be stamped at 0001_initial without any data change (see
permit_trade_skills.store.schema.ensure_schema). Never edit it; add a new
revision instead.

Order: extensions -> jurisdictions -> permits -> status_history / sync_metadata
-> discovery tables -> indexes.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy  # noqa: F401  (autogenerate renders Vector columns via this module)
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0001_initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # (a)-(b) Extensions must precede any vector column / trigram index.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table('jurisdictions',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('state', sa.String(), nullable=False),
    sa.Column('county', sa.String(), nullable=True),
    sa.Column('platform_type', sa.String(), nullable=False),
    sa.Column('base_url', sa.String(), nullable=False),
    sa.Column('api_endpoint', sa.String(), nullable=True),
    sa.Column('api_key', sa.String(), nullable=True),
    sa.Column('dataset_id', sa.String(), nullable=True),
    sa.Column('dataset_name', sa.String(), nullable=True),
    sa.Column('config_version', sa.Integer(), nullable=False),
    sa.Column('field_map', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status_map', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('detail_strategy', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('permits',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('jurisdiction_id', sa.String(), nullable=False),
    sa.Column('jurisdiction_slug', sa.String(), nullable=False),
    sa.Column('permit_number', sa.String(), nullable=False),
    sa.Column('permit_class', sa.String(), nullable=True),
    sa.Column('permit_class_mapped', sa.String(), nullable=True),
    sa.Column('permit_type_mapped', sa.String(), nullable=True),
    sa.Column('permit_type_desc', sa.String(), nullable=True),
    sa.Column('status_raw', sa.String(), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('issue_date', sa.DateTime(), nullable=True),
    sa.Column('issued_at', sa.DateTime(), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('address', sa.String(), nullable=True),
    sa.Column('city', sa.String(), nullable=True),
    sa.Column('state', sa.String(), nullable=True),
    sa.Column('zip_code', sa.String(), nullable=True),
    sa.Column('latitude', sa.Float(), nullable=True),
    sa.Column('longitude', sa.Float(), nullable=True),
    sa.Column('valuation', sa.Float(), nullable=True),
    sa.Column('work_class', sa.String(), nullable=True),
    sa.Column('building_count', sa.Integer(), nullable=True),
    sa.Column('housing_units', sa.Integer(), nullable=True),
    sa.Column('status_map_key', sa.String(), nullable=True),
    sa.Column('description_embedding', pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=True),
    sa.Column('raw_row', sa.JSON(), nullable=False),
    sa.Column('raw_hash', sa.String(), nullable=False),
    sa.Column('source_updated_at', sa.DateTime(), nullable=True),
    sa.Column('source_synced_at', sa.DateTime(), nullable=True),
    sa.Column('detail_url', sa.String(), nullable=True),
    sa.Column('detail_strategy', sa.String(), nullable=True),
    sa.Column('detail_fetch_type', sa.String(), nullable=True),
    sa.Column('run_id', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('synced_at', sa.DateTime(), nullable=True),
    sa.Column('processing_state', sa.String(), nullable=False),
    sa.ForeignKeyConstraint(['jurisdiction_id'], ['jurisdictions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('status_history',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('permit_id', sa.BigInteger(), nullable=False),
    sa.Column('jurisdiction_id', sa.String(), nullable=False),
    sa.Column('from_status', sa.String(), nullable=True),
    sa.Column('to_status', sa.String(), nullable=False),
    sa.Column('changed_at', sa.DateTime(), nullable=False),
    sa.Column('changed_by', sa.String(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['permit_id'], ['permits.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('sync_metadata',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('jurisdiction_id', sa.String(), nullable=False),
    sa.Column('jurisdiction_slug', sa.String(), nullable=False),
    sa.Column('run_id', sa.String(), nullable=False),
    sa.Column('config_version', sa.Integer(), nullable=False),
    sa.Column('pull_count', sa.Integer(), nullable=False),
    sa.Column('inserted', sa.Integer(), nullable=False),
    sa.Column('updated', sa.Integer(), nullable=False),
    sa.Column('unchanged', sa.Integer(), nullable=False),
    sa.Column('dropped', sa.Integer(), nullable=False),
    sa.Column('warnings', sa.JSON(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('cursor_before', sa.String(), nullable=True),
    sa.Column('cursor_after', sa.String(), nullable=True),
    sa.Column('error', sa.String(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('completed_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('discovery_runs',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('jurisdiction_id', sa.String(), nullable=False),
    sa.Column('search_query', sa.Text(), nullable=False),
    sa.Column('search_parameters', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('total_results', sa.Integer(), nullable=True),
    sa.Column('candidates_found', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['jurisdiction_id'], ['jurisdictions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('probes',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('jurisdiction_id', sa.String(), nullable=False),
    sa.Column('probe_name', sa.String(), nullable=False),
    sa.Column('success', sa.Boolean(), nullable=False),
    sa.Column('detected_platform', sa.String(), nullable=True),
    sa.Column('confidence', sa.Integer(), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('executed_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['jurisdiction_id'], ['jurisdictions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('status_maps',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('jurisdiction_id', sa.String(), nullable=False),
    sa.Column('source_status', sa.String(), nullable=False),
    sa.Column('target_status', sa.String(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['jurisdiction_id'], ['jurisdictions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )

    # Indexes last
    op.create_index('idx_permit_jurisdiction_number', 'permits', ['jurisdiction_id', 'permit_number'], unique=True)


def downgrade() -> None:
    # Extensions are left in place: other objects may depend on them.
    op.drop_index('idx_permit_jurisdiction_number', table_name='permits')
    op.drop_table('status_maps')
    op.drop_table('probes')
    op.drop_table('discovery_runs')
    op.drop_table('sync_metadata')
    op.drop_table('status_history')
    op.drop_table('permits')
    op.drop_table('jurisdictions')
