"""Sync progress counter output (no database)."""

import io

from permit_trade_skills.ingest.progress import Progress


def test_piped_output_prints_every_10_percent():
    out = io.StringIO()
    p = Progress("Upserting permits", 1000, stream=out, interactive=False)
    for i in range(1, 1001):
        p.update(i)
    p.close()
    lines = out.getvalue().splitlines()
    assert len(lines) == 10  # 10%..90% + final line
    assert lines[0].startswith("  Upserting permits: 100 / 1,000 (10.0%)")
    assert "1,000 / 1,000 (100.0%)" in lines[-1]


def test_interactive_redraws_in_place():
    out = io.StringIO()
    p = Progress("Embedding descriptions", 10, stream=out, interactive=True, min_interval=0)
    for i in range(1, 11):
        p.update(i)
    p.close()
    text = out.getvalue()
    assert text.count("\n") == 1 and text.endswith("\n")  # one line, redrawn with \r
    assert "\r" in text and "10 / 10 (100.0%)" in text
