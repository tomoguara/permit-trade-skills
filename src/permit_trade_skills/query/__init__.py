"""Stage 04: query & retrieval.

- `service.QueryService`: the single implementation behind the REST API
  (`api.py`), the LLM tools (`tools.py`) and `pts query`.
- `nlu.py`: natural-language parsing via the `nlu.parser` LLM role.
- `retrieve.py`: hybrid retrieval (structured filters + trigram + vector, RRF).
- `echo.py`: template-rendered plain-English echo of an executed query.

Later stages (skills/, detail/, orchestration/) query data only through
`query.tools` (enforced by tests/unit/test_import_boundary.py).
"""
