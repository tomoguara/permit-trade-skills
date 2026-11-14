"""Stage 3 canonical store: lifecycle, provenance, constraints, indexes, new tables.

Additive only (no data is dropped on upgrade):
- permits: applied/expires/completed dates, housing detail, contractor/applicant,
  Socrata row id/version, per-row warnings, lifecycle state_updated_at/state_error,
  generated `geo` point; CHECKs on status, processing_state, lat/lng;
  FILLFACTOR=90; ON DELETE CASCADE from jurisdictions.
- status_history: raw_status, run_id, (permit_id, changed_at DESC) index, cascade.
- sync_metadata: mode, status_transitions, FK + (jurisdiction_id, started_at DESC).
- jurisdictions: CHECK (config_version >= 1). status_maps: created_at.
- New tables: detail_snapshots, skill_inferences, eval_results.
- Indexes (last): hot path (jurisdiction_slug, status, issued_at DESC), BRIN on
  issued_at/applied_at, (slug, permit_class_mapped), (slug, zip_code),
  GIN trigram on description, GiST on geo.

Revision ID: 0002_canonical_store
Revises: 0001_initial
Create Date: 2026-11-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0002_canonical_store'
down_revision: Union[str, None] = '0001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UTC_NOW = sa.text("(now() AT TIME ZONE 'utc')")
STATUSES = ('PENDING', 'ISSUED', 'IN_INSPECTION', 'CANCELED', 'EXPIRED', 'COMPLETED', 'UNKNOWN')
STATES = ('SYNCED', 'DETAIL_READY', 'SKILLS_INFERRED', 'SKILLS_APPROVED', 'SKILLS_REJECTED',
          'DETAIL_FAILED', 'SKILLS_FAILED', 'SKILLS_SKIPPED')


def _in(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    # Extensions (idempotent; 0001 creates them, repeated for databases stamped at 0001)
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # --- jurisdictions / status_maps ---
    op.create_check_constraint('ck_jurisdictions_config_version', 'jurisdictions', 'config_version >= 1')
    op.add_column('status_maps', sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=True))

    # --- permits ---
    for name in ('applied_at', 'expires_at', 'completed_at'):
        op.add_column('permits', sa.Column(name, sa.DateTime(), nullable=True))
    op.add_column('permits', sa.Column('housing_units_added', sa.Integer(), nullable=True))
    op.add_column('permits', sa.Column('housing_units_removed', sa.Integer(), nullable=True))
    for name in ('housing_category', 'contractor_name', 'applicant_name',
                 'row_id_source', 'row_version_source'):
        op.add_column('permits', sa.Column(name, sa.String(), nullable=True))
    op.add_column('permits', sa.Column('warnings', postgresql.JSONB(astext_type=sa.Text()),
                                       server_default=sa.text("'[]'::jsonb"), nullable=False))
    op.add_column('permits', sa.Column('state_updated_at', sa.DateTime(), server_default=UTC_NOW, nullable=False))
    op.add_column('permits', sa.Column('state_error', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    # Generated point (built-in Postgres geometry; no PostGIS)
    op.execute(
        "ALTER TABLE permits ADD COLUMN geo point GENERATED ALWAYS AS "
        "(CASE WHEN latitude IS NOT NULL AND longitude IS NOT NULL "
        "THEN point(longitude, latitude) END) STORED"
    )
    op.create_check_constraint('ck_permits_latitude', 'permits',
                               'latitude IS NULL OR (latitude >= -90 AND latitude <= 90)')
    op.create_check_constraint('ck_permits_longitude', 'permits',
                               'longitude IS NULL OR (longitude >= -180 AND longitude <= 180)')
    op.create_check_constraint('ck_permits_status', 'permits', _in('status', STATUSES))
    op.create_check_constraint('ck_permits_processing_state', 'permits', _in('processing_state', STATES))
    op.drop_constraint('permits_jurisdiction_id_fkey', 'permits', type_='foreignkey')
    op.create_foreign_key('permits_jurisdiction_id_fkey', 'permits', 'jurisdictions',
                          ['jurisdiction_id'], ['id'], ondelete='CASCADE')
    # Status/lifecycle updates churn rows; leave 10% free space per page for HOT updates.
    # Applies to newly written pages; VACUUM FULL rewrites existing ones (see docs/ops.md).
    op.execute("ALTER TABLE permits SET (fillfactor = 90)")

    # --- status_history ---
    op.add_column('status_history', sa.Column('raw_status', sa.String(), nullable=True))
    op.add_column('status_history', sa.Column('run_id', sa.String(), nullable=True))
    op.alter_column('status_history', 'changed_at', server_default=UTC_NOW)
    op.drop_constraint('status_history_permit_id_fkey', 'status_history', type_='foreignkey')
    op.create_foreign_key('status_history_permit_id_fkey', 'status_history', 'permits',
                          ['permit_id'], ['id'], ondelete='CASCADE')

    # --- sync_metadata ---
    op.add_column('sync_metadata', sa.Column('mode', sa.String(), server_default='full', nullable=False))
    op.add_column('sync_metadata', sa.Column('status_transitions', postgresql.JSONB(astext_type=sa.Text()),
                                             server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.create_check_constraint('ck_sync_metadata_mode', 'sync_metadata',
                               "mode IN ('full', 'incremental', 'dry_run')")
    op.create_foreign_key('sync_metadata_jurisdiction_id_fkey', 'sync_metadata', 'jurisdictions',
                          ['jurisdiction_id'], ['id'], ondelete='CASCADE')

    # --- new tables (FK order: permits -> detail_snapshots -> skill_inferences) ---
    op.create_table('detail_snapshots',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('permit_id', sa.BigInteger(), nullable=False),
        sa.Column('final_url', sa.Text(), nullable=False),
        sa.Column('fetch_strategy', sa.String(), nullable=False),
        sa.Column('content_text', sa.Text(), nullable=False),
        sa.Column('raw_html', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.String(), nullable=False),
        sa.Column('fetched_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.CheckConstraint("fetch_strategy IN ('soda_row', 'redirect_chase', 'playwright')",
                           name='ck_detail_snapshots_fetch_strategy'),
        sa.ForeignKeyConstraint(['permit_id'], ['permits.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('permit_id', 'content_hash', name='uq_detail_snapshots_permit_hash'),
    )
    op.create_table('skill_inferences',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('permit_id', sa.BigInteger(), nullable=False),
        sa.Column('prompt_version', sa.String(), nullable=False),
        sa.Column('model', sa.String(), nullable=False),
        sa.Column('request_id', sa.String(), nullable=True),
        sa.Column('skills', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('assumptions', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('input_snapshot_id', sa.BigInteger(), nullable=True),
        sa.Column('review_status', sa.String(), server_default='PENDING', nullable=False),
        sa.Column('reviewer', sa.String(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('review_note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.CheckConstraint("review_status IN ('PENDING', 'APPROVED', 'REJECTED', 'NEEDS_HUMAN')",
                           name='ck_skill_inferences_review_status'),
        sa.ForeignKeyConstraint(['input_snapshot_id'], ['detail_snapshots.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['permit_id'], ['permits.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table('eval_results',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(), nullable=False),
        sa.Column('permit_id', sa.BigInteger(), nullable=True),
        sa.Column('metric', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('value', sa.Numeric(), nullable=True),
        sa.Column('threshold', sa.Numeric(), nullable=True),
        sa.Column('passed', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(['permit_id'], ['permits.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    # --- indexes last ---
    op.create_index('idx_permits_slug_status_issued', 'permits',
                    ['jurisdiction_slug', 'status', sa.literal_column('issued_at DESC')])
    op.create_index('brin_permits_issued_at', 'permits', ['issued_at'], postgresql_using='brin')
    op.create_index('brin_permits_applied_at', 'permits', ['applied_at'], postgresql_using='brin')
    op.create_index('idx_permits_slug_class', 'permits', ['jurisdiction_slug', 'permit_class_mapped'])
    op.create_index('idx_permits_slug_zip', 'permits', ['jurisdiction_slug', 'zip_code'])
    op.create_index('gin_permits_description_trgm', 'permits', ['description'],
                    postgresql_using='gin', postgresql_ops={'description': 'gin_trgm_ops'})
    op.create_index('gist_permits_geo', 'permits', ['geo'], postgresql_using='gist')
    op.create_index('idx_status_history_permit_changed', 'status_history',
                    ['permit_id', sa.literal_column('changed_at DESC')])
    op.create_index('idx_sync_metadata_jurisdiction_started', 'sync_metadata',
                    ['jurisdiction_id', sa.literal_column('started_at DESC')])
    op.create_index('idx_detail_snapshots_permit_fetched', 'detail_snapshots',
                    ['permit_id', sa.literal_column('fetched_at DESC')])
    op.create_index('idx_skill_inferences_permit_created', 'skill_inferences',
                    ['permit_id', sa.literal_column('created_at DESC')])
    op.create_index('idx_skill_inferences_pending', 'skill_inferences', ['created_at'],
                    postgresql_where=sa.text("review_status = 'PENDING'"))
    op.create_index('idx_eval_results_run', 'eval_results', ['run_id'])


def downgrade() -> None:
    # Reverse order. Drops the Stage 3 columns/tables (and their data): take a
    # backup first (`pts db backup`).
    for name, table in (
        ('idx_eval_results_run', 'eval_results'),
        ('idx_skill_inferences_pending', 'skill_inferences'),
        ('idx_skill_inferences_permit_created', 'skill_inferences'),
        ('idx_detail_snapshots_permit_fetched', 'detail_snapshots'),
        ('idx_sync_metadata_jurisdiction_started', 'sync_metadata'),
        ('idx_status_history_permit_changed', 'status_history'),
        ('gist_permits_geo', 'permits'),
        ('gin_permits_description_trgm', 'permits'),
        ('idx_permits_slug_zip', 'permits'),
        ('idx_permits_slug_class', 'permits'),
        ('brin_permits_applied_at', 'permits'),
        ('brin_permits_issued_at', 'permits'),
        ('idx_permits_slug_status_issued', 'permits'),
    ):
        op.drop_index(name, table_name=table)

    op.drop_table('eval_results')
    op.drop_table('skill_inferences')
    op.drop_table('detail_snapshots')

    op.drop_constraint('sync_metadata_jurisdiction_id_fkey', 'sync_metadata', type_='foreignkey')
    op.drop_constraint('ck_sync_metadata_mode', 'sync_metadata', type_='check')
    op.drop_column('sync_metadata', 'status_transitions')
    op.drop_column('sync_metadata', 'mode')

    op.drop_constraint('status_history_permit_id_fkey', 'status_history', type_='foreignkey')
    op.create_foreign_key('status_history_permit_id_fkey', 'status_history', 'permits',
                          ['permit_id'], ['id'])
    op.alter_column('status_history', 'changed_at', server_default=None)
    op.drop_column('status_history', 'run_id')
    op.drop_column('status_history', 'raw_status')

    op.execute("ALTER TABLE permits RESET (fillfactor)")
    op.drop_constraint('permits_jurisdiction_id_fkey', 'permits', type_='foreignkey')
    op.create_foreign_key('permits_jurisdiction_id_fkey', 'permits', 'jurisdictions',
                          ['jurisdiction_id'], ['id'])
    for name in ('ck_permits_processing_state', 'ck_permits_status',
                 'ck_permits_longitude', 'ck_permits_latitude'):
        op.drop_constraint(name, 'permits', type_='check')
    for name in ('geo', 'state_error', 'state_updated_at', 'warnings', 'row_version_source',
                 'row_id_source', 'applicant_name', 'contractor_name', 'housing_category',
                 'housing_units_removed', 'housing_units_added', 'completed_at',
                 'expires_at', 'applied_at'):
        op.drop_column('permits', name)

    op.drop_column('status_maps', 'created_at')
    op.drop_constraint('ck_jurisdictions_config_version', 'jurisdictions', type_='check')
