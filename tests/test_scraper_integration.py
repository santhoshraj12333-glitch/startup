"""
tests.test_scraper_integration
==============================

Unit and integration tests for scraper.py, collect_social.py, and FreeTierProtector.
Verifies keyword linking, record schema normalization, deduplication, and quota safety guards.
"""

import json
import tempfile
from pathlib import Path

from backend.scraper import (
    FreeTierProtector,
    LINKED_NEWS_QUERIES_EN,
    LINKED_NEWS_QUERIES_TA,
    LINKED_YOUTUBE_QUERIES,
    CIVIC_KEYWORDS,
    make_record,
    save,
)


def test_linked_keywords_loaded():
    assert len(LINKED_NEWS_QUERIES_EN) > 0
    assert len(LINKED_NEWS_QUERIES_TA) > 0
    assert len(LINKED_YOUTUBE_QUERIES) > 0
    assert "Chennai MTC bus" in LINKED_NEWS_QUERIES_EN
    assert CIVIC_KEYWORDS.search("Heavy traffic near Vadapalani metro station") is not None


def test_make_record_schema():
    rec = make_record(
        source="news_rss",
        source_type="news",
        text="Waterlogging reported near T Nagar subway causing bus breakdown",
        posted_at="2026-11-05T15:00:00+05:30",
        url="https://news.example.com/item1",
        author="The Hindu",
    )
    assert rec is not None
    assert rec["id"].startswith("news_rss_")
    assert rec["source"] == "news_rss"
    assert rec["source_type"] == "news"
    assert rec["kw_hit"] is True
    assert rec["lang_hint"] == "latin"


def test_save_deduplication():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        out_file = Path(tmp) / "posts.jsonl"

        rec1 = make_record("news_rss", "news", "Severe Chennai metro delay after power fault", "2026-11-05T15:00:00+05:30", "https://news.example.com/1")
        rec2 = make_record("news_rss", "news", "New bus routes announced by MTC in North Chennai", "2026-11-05T15:05:00+05:30", "https://news.example.com/2")

        added, skipped = save([rec1, rec2], out_file)
        assert added == 2
        assert skipped == 0

        # Adding same records again must be detected as duplicates
        added2, skipped2 = save([rec1, rec2], out_file)
        assert added2 == 0
        assert skipped2 == 2


def test_free_tier_protector_allows_news():
    allowed, reason = FreeTierProtector.can_proceed("news")
    assert allowed is True
    assert "free" in reason.lower() or "unlimited" in reason.lower()


def test_free_tier_protector_status_structure():
    status = FreeTierProtector.get_status()
    assert status["quota_guard_active"] is True
    assert "news_rss" in status["services"]
    assert "apify" in status["services"]
    assert "youtube_api" in status["services"]
    assert status["services"]["news_rss"]["recommended_for_continuous"] is True
    assert status["services"]["apify"]["recommended_for_continuous"] is False

