# Vector Embeddings and Hybrid Search Guide

This guide explains how to use vector embeddings for semantic search in the permit-trade-skills system. With embeddings, you can find permits with similar descriptions even when they use different keywords.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Setting Up Embeddings](#setting-up-embeddings)
- [Using the Search CLI](#using-the-search-cli)
- [Example Workflows](#example-workflows)
- [Troubleshooting](#troubleshooting)

---

## Overview

Permit-trade-skills uses **hybrid search** that combines multiple search methods:

1. **Structured Search** - SQL filters for specific fields (status, class, cost, etc.)
2. **Text Search** - PostgreSQL full-text search with trigram similarity
3. **Vector Search** - Semantic similarity using embeddings

### How It Works

1. **Embedding Generation** - During sync, permit descriptions are converted to 384-dimensional vectors using the `all-MiniLM-L6-v2` model
2. **Vector Storage** - Embeddings are stored in the `description_embedding` column (no vector index; see [Index](#index))
3. **Hybrid Search** - The text and vector rankings, each run inside the structured filters, are combined using Reciprocal Rank Fusion (RRF)

### Example Use Cases

| Scenario | Best Search Type |
|----------|-----------------|
| Find all roof permits in Seattle | Structured (status + jurisdiction) |
| Find permits similar to "deck construction" | Vector (semantic) |
| Find permits with "remodel" in description | Text (trigram) |
| Find high-cost permits with specific description | Hybrid (structured + vector) |

---

## Architecture

### High-Level Component Diagram

The following diagram shows the Query, NL Understanding & Hybrid Retrieval API stage architecture:

![Query Stage Component Diagram](./diagrams/query-component.png)

### Database Schema

The `permits` table includes:

| Column | Type | Description |
|--------|------|-------------|
| `description` | TEXT | Raw permit description |
| `description_embedding` | VECTOR(384) | Semantic embedding (computed) |
| `jurisdiction_id` | STRING | Foreign key to jurisdiction |

### Index

There is **no vector index**. Vector search is an exact (brute-force) cosine
scan, ~0.4 s at 193k rows. Stage 04 kept it on purpose: the vector ranking runs
inside the structured filters, where an approximate index (ivfflat/HNSW) would
drop matches. See [schema.md](schema.md) query 6.

### Embedding Model

- **Model**: `all-MiniLM-L6-v2` (from sentence-transformers)
- **Dimension**: 384
- **License**: Apache 2.0
- **Performance**: ~100-200ms per description on CPU

---

## Prerequisites

### 1. Install Dependencies

```bash
# sentence-transformers and pgvector are project dependencies
uv sync
```

### 2. Database Setup

PostgreSQL with the pgvector extension (`./script/pgvector.sh --start` runs
`pgvector/pgvector:pg18`). `pts db migrate` creates the `vector` and `pg_trgm`
extensions; to verify:

```sql
SELECT * FROM pg_extension WHERE extname = 'vector';
```

### 3. CLI

```bash
uv run pts --help
```

---

## Setting Up Embeddings

### Step 1: Create the Embedding Column

Run the migration to add the `description_embedding` column:

```bash
# Check migration status
pts db status --db-url "postgresql://user:password@localhost:5432/permit_trade_skills"

# Run migrations (creates the vector extension and the embedding column)
pts db migrate --db-url "postgresql://user:password@localhost:5432/permit_trade_skills"
```

**What This Creates:**

1. `description_embedding` column (VECTOR(384)); no index (see [Index](#index))

### Step 2: Compute Embeddings for Existing Data

To add embeddings to permits that were ingested before the migration:

```bash
# Method 1: Use sync to compute embeddings during re-ingestion
# (stores an embedding wherever one is missing; the Socrata API is down, so use the export)
pts sync sync --jurisdiction seattle-kingcounty-wa --local-file data/wa-seattle-building-permits.json

# Method 2: Direct computation (Python)
python << 'PYEOF'
from permit_trade_skills.discovery.registry import Registry
from permit_trade_skills.embeddings import EmbeddingService

# Initialize
registry = Registry(database_url="postgresql://user:password@localhost:5432/permit_trade_skills")
session = registry.get_session()
embedding_service = EmbeddingService()

# Get permits without embeddings
permits = session.query(PermitTable).filter(
    PermitTable.description_embedding.is_(None),
    PermitTable.description.isnot(None)
).limit(100).all()

# Compute and store embeddings
for permit in permits:
    embedding = embedding_service.compute_embedding(permit.description)
    permit.description_embedding = embedding

session.commit()
session.close()
print(f"Computed embeddings for {len(permits)} permits")
PYEOF
```

### Step 3: Verify Embeddings Are Being Computed

After sync completes, verify embeddings are being stored:

```bash
# Check if embeddings column exists
pts db status

# Count permits with embeddings
python << 'PYEOF'
from permit_trade_skills.discovery.registry import Registry
from sqlalchemy import text
r = Registry(database_url="postgresql://user:password@localhost:5432/permit_trade_skills")
s = r.get_session()
result = s.execute(text('''
    SELECT COUNT(*) as total,
           COUNT(description_embedding) as with_embedding
    FROM permits
''')).fetchone()
print(f"Total permits: {result[0]}")
print(f"With embeddings: {result[1]}")
s.close()
PYEOF
```

---

## Using the Search CLI

The `pts search` command provides four search modes:

```bash
pts search [COMMAND] [OPTIONS]

Commands:
  hybrid   - Combined structured + text + vector search
  vector   - Pure semantic vector search
  text     - Full-text trigram search
  similar  - Find permits similar to a given permit
```

### Command Options

| Option | Description | Example |
|--------|-------------|---------|
| `--jurisdiction` | Jurisdiction ID | `--jurisdiction seattle-kingcounty-wa` |
| `--limit` | Number of results | `--limit 10` |
| `--query` | Search query | `--query "roof replacement"` |
| `--permit-id` | `similar` only: permit number (or internal numeric row id) | `--permit-id 7140363-RR` |
| `--structured` | All four commands: JSON filters; every result must match all of them | `--structured '{"status": "ISSUED"}'` |

**`--structured` filter keys:**

| Key | Value | Example |
|-----|-------|---------|
| `status` | Canonical status, string or list (case-insensitive): `PENDING`, `ISSUED`, `IN_INSPECTION`, `COMPLETED`, `CANCELED`, `EXPIRED`, `UNKNOWN` | `"status": ["ISSUED", "COMPLETED"]` |
| `permit_class` | Raw class (e.g. `Commercial`, `Single Family/Duplex`) or mapped class (`Residential`, `Non-Residential`), string or list (case-insensitive) | `"permit_class": "RESIDENTIAL"` |
| `min_cost` / `max_cost` | Estimated project cost bounds | `"min_cost": 10000` |
| `issued_after` | ISO date | `"issued_after": "2024-01-01"` |

Unknown keys and invalid JSON are rejected with an error.

---

## Example Workflows

### Workflow 1: Hybrid Search (Recommended for Most Cases)

Find permits that match both your query AND have specific attributes:

```bash
# Find roof permits with high cost
pts search hybrid "roof replacement" \
  --jurisdiction seattle-kingcounty-wa \
  --structured '{"status": "ISSUED", "min_cost": 10000}' \
  --limit 20

# Find deck construction permits
pts search hybrid "deck addition" \
  --jurisdiction seattle-kingcounty-wa \
  --structured '{"permit_class": "RESIDENTIAL"}' \
  --limit 15

# Find roof replacements with issued permits and for commercial buildings
pts search hybrid "roof replacement" \
  --jurisdiction seattle-kingcounty-wa \
  --structured '{"permit_class": "COMMERCIAL", "status": "ISSUED"}' \
  --limit 15

# Find high-cost roof replacements for residential buildings with issued permits
pts search hybrid "roof replacement" \
  --jurisdiction seattle-kingcounty-wa \
  --structured '{"permit_class": "RESIDENTIAL", "status": "ISSUED", "min_cost":15000}' \
  --limit 15

# Find high-cost roof replacements for residential buildings with issued permits after 2026-06-01
pts search hybrid "roof replacement" \
  --jurisdiction seattle-kingcounty-wa \
  --structured '{"permit_class": "RESIDENTIAL", "status": "ISSUED", "min_cost":15000, "issued_after": "2026-06-01"  }' \
  --limit 15
```

**Result:** Combines keyword matching with semantic similarity and structured filters.

---

### Workflow 2: Pure Vector Search (Semantic Similarity)

Find permits with conceptually similar descriptions:

```bash
# Find anything related to "exterior remodeling"
pts search vector "exterior home renovation" \
  --jurisdiction seattle-kingcounty-wa \
  --limit 25

# Find permits about "solar panel installation"
pts search vector "photovoltaic system" \
  --jurisdiction seattle-kingcounty-wa \
  --limit 20
```

**Result:** Finds permits using different terminology but similar meaning.

---

### Workflow 3: Text Search (Keyword Matching)

Traditional keyword search with trigram similarity:

```bash
# Find permits containing "roof" or similar terms
pts search text "roof" \
  --jurisdiction seattle-kingcounty-wa \
  --limit 30

# Find permits about "windows"
pts search text "window" \
  --jurisdiction seattle-kingcounty-wa \
  --limit 20
```

**Result:** Fast keyword-based search with fuzzy matching.

---

### Workflow 4: Find Similar Permits

Find permits similar to a specific permit:

```bash
# Find permits similar to permit 7140363-RR
pts search similar --permit-id 7140363-RR \
  --jurisdiction seattle-kingcounty-wa \
  --limit 10

# Only similar permits that have been issued
pts search similar --permit-id 7140363-RR \
  --jurisdiction seattle-kingcounty-wa \
  --structured '{"status": "ISSUED"}' \
  --limit 10
```

Filters apply to the similar permits returned, not to the source permit.

**Use Cases:**
- "What other permits are like this one?"
- Recommendation engine
- Duplicate detection

---

### Workflow 5: Filter by Multiple Criteria

Combine structured filters with semantic search:

```bash
# Find high-cost residential projects
pts search hybrid "renovation" \
  --jurisdiction seattle-kingcounty-wa \
  --structured '{
    "status": ["ISSUED", "COMPLETED"],
    "permit_class": "RESIDENTIAL",
    "min_cost": 50000,
    "max_cost": 500000
  }' \
  --limit 50

# Find commercial permits issued recently
pts search hybrid "commercial construction" \
  --jurisdiction seattle-kingcounty-wa \
  --structured '{
    "permit_class": "COMMERCIAL",
    "status": "ISSUED"
  }' \
  --limit 25
```

---

### Workflow 6: Search Across Jurisdictions

Search multiple jurisdictions:

```bash
# Note: Each search is jurisdiction-specific
# You'll need to run separate searches for each jurisdiction

pts search hybrid "solar installation" \
  --jurisdiction seattle-kingcounty-wa \
  --limit 20

pts search hybrid "solar installation" \
  --jurisdiction portland-or \
  --limit 20
```

---

## Advanced Usage

### Python API for Custom Searches

```python
from permit_trade_skills.discovery.registry import Registry
from permit_trade_skills.embeddings import compute_embedding

# Initialize
registry = Registry(database_url="postgresql://user:password@localhost:5432/permit_trade_skills")
session = registry.get_session()

# Get a permit's embedding
permit = session.query(PermitTable).filter_by(id="PERMIT-12345").first()
embedding = permit.description_embedding

# Find similar permits
similar = session.execute(
    text('''
        SELECT *,
               1 - (description_embedding <=> :embedding) as similarity
        FROM permits
        WHERE jurisdiction_id = :jurisdiction_id
          AND id != :permit_id
        ORDER BY similarity DESC
        LIMIT 10
    '''),
    {
        'embedding': embedding,
        'jurisdiction_id': permit.jurisdiction_id,
        'permit_id': permit.id
    }
).fetchall()

for permit, similarity in similar:
    print(f"{permit.permit_number}: {similarity:.3f} similarity")
```

### Batch Embedding Computation

```python
from permit_trade_skills.embeddings import EmbeddingService
from permit_trade_skills.discovery.registry import Registry

embedding_service = EmbeddingService()
registry = Registry(database_url="postgresql://user:password@localhost:5432/permit_trade_skills")
session = registry.get_session()

# Get all permits without embeddings
permits = session.query(PermitTable).filter(
    PermitTable.description_embedding.is_(None),
    PermitTable.description.isnot(None)
).all()

# Batch compute embeddings
for permit in permits:
    try:
        embedding = embedding_service.compute_embedding(permit.description)
        permit.description_embedding = embedding
    except Exception as e:
        print(f"Failed to compute embedding for {permit.id}: {e}")

session.commit()
print(f"Computed embeddings for {len(permits)} permits")
```

---

## Performance Optimization

### Vector search cost

There is no vector index, so every vector query is an exact scan over the rows
that pass the structured filters (~0.4 s for all 193k Seattle permits, run as a
parallel sequential scan). Narrower filters (status, class, cost, date) make it
faster. An approximate index (HNSW with `hnsw.iterative_scan`) is the option to
revisit if volumes grow.

```sql
-- The shape of the vector ranking (exact cosine distance)
SELECT permit_number FROM permits
WHERE jurisdiction_id = 'seattle-kingcounty-wa' AND status = 'ISSUED'
ORDER BY description_embedding <=> '[0.1, 0.2, ...]'
LIMIT 20;
```

---

## Troubleshooting

### Issue: "embedding column does not exist"

**Solution:** Run the migration:

```bash
pts db migrate --db-url "postgresql://user:password@localhost:5432/permit_trade_skills"
```

### Issue: "pgvector extension not found"

**Solution:** Enable the extension:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

### Issue: Poor search results

**Possible Causes:**

1. **No embeddings computed** - Run sync to compute embeddings
2. **Low-quality descriptions** - Embeddings need good text input
3. **Index not built** - Rebuild the vector index

**Debug:**

```bash
# Check if embeddings exist
pts db status

# Check permit count
python << 'PYEOF'
from permit_trade_skills.discovery.registry import Registry
from sqlalchemy import text
r = Registry(database_url="postgresql://user:password@localhost:5432/permit_trade_skills")
s = r.get_session()
result = s.execute(text('''
    SELECT COUNT(*) as total,
           COUNT(description_embedding) as with_embedding,
           COUNT(description) as with_description
    FROM permits
''')).fetchone()
print(f"Total: {result[0]}, With embedding: {result[1]}, With description: {result[2]}")
s.close()
PYEOF
```

### Issue: Slow vector search

Vector search is an exact scan (no index), ~0.4 s at 193k rows.

**Solutions:**

1. Add structured filters (`--structured '{"status": "ISSUED"}'`); the scan only
   covers matching rows.
2. Check that `ANALYZE permits` has run, so the planner uses parallel workers.
3. For much larger volumes, consider an approximate index (HNSW with
   `hnsw.iterative_scan`), accepting that it can drop matches under tight filters.

---

## Related Documentation

- [Ingestion How-To Guide](./ingest-how-to-guide.md) - Data ingestion details
- [Database schema](./schema.md) - Query cookbook, including the vector query plan

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-11-05 | Initial release with vector embeddings support |
