"""
backend.scraper
===============

Social media and civic news scraper integrated with the AI Urban Intelligence Platform.
Links keywords and queries from scripts/collect_social.py to pull, normalize, and continuously
sync recent civic, transit, and incident reports.

Sources supported:
  * Google News RSS (linked via collect_social keywords, 100% token-free, no expiration)
  * YouTube (via Apify actor streamers/youtube-scraper using linked YOUTUBE_QUERIES)
  * Instagram (via Apify actor apify/instagram-scraper using linked INSTAGRAM_HASHTAGS)

Data format:
  Standardized JSONL records matching data/social/raw/posts.jsonl:
    {id, source, source_type, author_hash, text, posted_at, url, lang_hint, kw_hit}

Usage:
  # One-time sync of recent news updates with linked keywords:
  python backend/scraper.py --sync-news

  # Continuous auto-sync of recent news updates (every 5 minutes):
  python backend/scraper.py --continuous --interval 300

  # Continuous sync of news + YouTube:
  python backend/scraper.py --continuous --sources news youtube --interval 600

  # Test Apify scraper with a single keyword:
  python backend/scraper.py --keyword "chennai road accident infra" --sources youtube instagram
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Load .env file so os.getenv picks up secrets before anything else
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    with _env_file.open() as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

# Ensure project root is on sys.path so scripts and backend modules resolve cleanly
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------
# 1. LINKED KEYWORDS & QUERIES FROM scripts/collect_social.py
# ---------------------------------------------------------------------
from scripts.collect_social import (
    NEWS_QUERIES_EN,
    NEWS_QUERIES_TA,
    YOUTUBE_QUERIES,
    INSTAGRAM_HASHTAGS,
    KEYWORDS,
    OUT_DEFAULT,
    author_hash,
    clean,
    collect_rss,
    iso,
    make_record,
    save,
)

# Aliases for clarity and external imports
LINKED_NEWS_QUERIES_EN = NEWS_QUERIES_EN
LINKED_NEWS_QUERIES_TA = NEWS_QUERIES_TA
LINKED_YOUTUBE_QUERIES = YOUTUBE_QUERIES
LINKED_INSTAGRAM_HASHTAGS = INSTAGRAM_HASHTAGS
CIVIC_KEYWORDS = KEYWORDS

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------
# 2. FREE-TIER PROTECTOR & QUOTA GUARD
# ---------------------------------------------------------------------
class FreeTierProtector:
    """
    Prevents exhaustion of free-tier credits and API quotas across all services:
      * Apify: Capped at 5 runs/day by default (protects the $5/month free credit pool).
      * YouTube Data API: Capped at 20 searches/day (2,000 of 10,000 daily quota units).
      * Google News RSS: Unlimited (100% token-free, 0 credits, 0 exhaustion risk).
    """
    USAGE_FILE = PROJECT_ROOT / "outputs" / ".quota_usage.json"
    DEFAULT_LIMITS = {
        "apify": int(os.getenv("APIFY_DAILY_LIMIT", "5")),
        "youtube_api": int(os.getenv("YOUTUBE_DAILY_LIMIT", "20")),
        "x_api": int(os.getenv("X_DAILY_LIMIT", "3")),
    }

    @classmethod
    def _load_data(cls) -> dict:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if cls.USAGE_FILE.exists():
            try:
                with cls.USAGE_FILE.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("date") == today:
                        return data
            except Exception:
                pass
        return {"date": today, "counts": {}}

    @classmethod
    def _save_data(cls, data: dict):
        cls.USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            with cls.USAGE_FILE.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    @classmethod
    def can_proceed(cls, service: str, force: bool = False) -> tuple[bool, str]:
        if service in ("news", "rss"):
            return True, "Google News RSS is 100% free & unlimited."
        if force or os.getenv("BYPASS_QUOTA_GUARD", "0") == "1":
            return True, "Quota guard bypassed."

        limit = cls.DEFAULT_LIMITS.get(service, 5)
        data = cls._load_data()
        current = data.get("counts", {}).get(service, 0)

        if current >= limit:
            msg = (
                f"[FREE-TIER GUARD] Daily safety cap reached for '{service}' ({current}/{limit} calls today).\n"
                f"                  Execution skipped to protect your free credits from exhausting!\n"
                f"                  (Continuous News RSS sync remains active with 0 credit cost)."
            )
            return False, msg

        return True, f"Within safety limit ({current + 1}/{limit})"

    @classmethod
    def record_usage(cls, service: str):
        if service in ("news", "rss"):
            return
        data = cls._load_data()
        counts = data.setdefault("counts", {})
        counts[service] = counts.get(service, 0) + 1
        cls._save_data(data)

    @classmethod
    def get_status(cls) -> dict:
        data = cls._load_data()
        counts = data.get("counts", {})
        today = data.get("date", datetime.now(IST).strftime("%Y-%m-%d"))
        return {
            "date": today,
            "quota_guard_active": True,
            "services": {
                "news_rss": {
                    "provider": "Google News RSS",
                    "status": "active_unlimited",
                    "daily_used": counts.get("news", 0),
                    "daily_limit": "unlimited",
                    "exhaustion_risk": "none (0 tokens, 100% free)",
                    "recommended_for_continuous": True,
                },
                "apify": {
                    "provider": "Apify Actors (YouTube / Instagram)",
                    "status": "capped_for_safety",
                    "daily_used": counts.get("apify", 0),
                    "daily_limit": cls.DEFAULT_LIMITS.get("apify", 5),
                    "remaining_today": max(0, cls.DEFAULT_LIMITS.get("apify", 5) - counts.get("apify", 0)),
                    "exhaustion_risk": "protected ($5/mo free credits preserved)",
                    "recommended_for_continuous": False,
                },
                "youtube_api": {
                    "provider": "Google Cloud YouTube Data API v3",
                    "status": "capped_for_safety",
                    "daily_used": counts.get("youtube_api", 0),
                    "daily_limit": cls.DEFAULT_LIMITS.get("youtube_api", 20),
                    "remaining_today": max(0, cls.DEFAULT_LIMITS.get("youtube_api", 20) - counts.get("youtube_api", 0)),
                    "exhaustion_risk": "protected (10k daily units preserved)",
                    "recommended_for_continuous": False,
                },
            },
        }


# ---------------------------------------------------------------------
# 3. APIFY CLIENT CONFIGURATION
# ---------------------------------------------------------------------
APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
try:
    from apify_client import ApifyClient
    client: Optional[ApifyClient] = ApifyClient(APIFY_TOKEN)
except ImportError:
    client = None
    print("[scraper] warning: 'apify_client' is not installed (pip install apify-client)")


def get_dataset_items(run) -> list[dict]:
    """Safely extracts items from a completed Apify Actor run."""
    if not client:
        return []
    dataset_id = getattr(run, "default_dataset_id", None) or (run.get("defaultDatasetId") if isinstance(run, dict) else None)
    if not dataset_id:
        return []
    return list(client.dataset(dataset_id).iterate_items())


# ---------------------------------------------------------------------
# 4. APIFY SCRAPERS (YouTube & Instagram)
# ---------------------------------------------------------------------
def search_youtube(keyword: str, max_results: int = 5, force: bool = False) -> list[dict]:
    """
    Search YouTube videos via Apify Actor 'streamers/youtube-scraper'.
    Protected by FreeTierProtector to avoid exhausting monthly free credits.
    """
    if not client:
        raise RuntimeError("apify_client is not installed or initialized.")

    allowed, reason = FreeTierProtector.can_proceed("apify", force=force)
    if not allowed:
        print(f"\n{reason}")
        return []

    print(f"\n[YouTube/Apify] Searching for keyword: '{keyword}'...")
    try:
        run = client.actor("streamers/youtube-scraper").call(run_input={
            "searchKeywords": keyword,
            "maxResults": max_results,
        })
        FreeTierProtector.record_usage("apify")
        return get_dataset_items(run)
    except Exception as exc:
        print(f"[YouTube/Apify error] {exc}")
        return []


def search_instagram_hashtag(hashtag: str, max_results: int = 5, force: bool = False) -> list[dict]:
    """
    Search Instagram posts by hashtag via Apify Actor 'apify/instagram-scraper'.
    Protected by FreeTierProtector to avoid exhausting monthly free credits.
    """
    if not client:
        raise RuntimeError("apify_client is not installed or initialized.")

    allowed, reason = FreeTierProtector.can_proceed("apify", force=force)
    if not allowed:
        print(f"\n{reason}")
        return []

    clean_tag = hashtag.lstrip("#")
    print(f"\n[Instagram/Apify] Searching hashtag: #{clean_tag}...")
    try:
        run = client.actor("apify/instagram-scraper").call(run_input={
            "directUrls": [f"https://www.instagram.com/explore/tags/{clean_tag}/"],
            "resultsType": "posts",
            "resultsLimit": max_results,
        })
        FreeTierProtector.record_usage("apify")
        return get_dataset_items(run)
    except Exception as exc:
        print(f"[Instagram/Apify error] {exc}")
        return []


def scrape_youtube_records(
    queries: Optional[List[str]] = None,
    max_results: int = 3,
) -> List[Dict[str, Any]]:
    """
    Scrapes YouTube for linked queries and converts each into the platform's
    standardized record format with keyword matching.
    """
    if not client:
        print("[scraper] ApifyClient unavailable; skipping YouTube Apify scrape.")
        return []

    target_queries = queries if queries is not None else LINKED_YOUTUBE_QUERIES
    records = []

    for q in target_queries:
        try:
            items = search_youtube(q, max_results=max_results)
            for item in items:
                title = item.get("title", "")
                desc = item.get("description", "")
                text = f"{title}. {desc}".strip()
                url = item.get("url") or item.get("videoUrl") or ""
                channel = item.get("channelName") or item.get("channelTitle")

                posted_at = item.get("date") or item.get("uploadDate")
                when = None
                if posted_at:
                    try:
                        when = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
                    except Exception:
                        when = None

                rec = make_record(
                    source="youtube_apify",
                    source_type="community",
                    text=text,
                    posted_at=iso(when),
                    url=url,
                    author=channel,
                )
                if rec:
                    records.append(rec)
        except Exception as exc:
            print(f"[scraper] YouTube search for '{q}' failed: {exc}")

    return records


def scrape_instagram_records(
    hashtags: Optional[List[str]] = None,
    max_results: int = 3,
) -> List[Dict[str, Any]]:
    """
    Scrapes Instagram for linked hashtags and converts each into the platform's
    standardized record format with keyword matching.
    """
    if not client:
        print("[scraper] ApifyClient unavailable; skipping Instagram Apify scrape.")
        return []

    target_tags = hashtags if hashtags is not None else LINKED_INSTAGRAM_HASHTAGS
    records = []

    for tag in target_tags:
        try:
            items = search_instagram_hashtag(tag, max_results=max_results)
            for item in items:
                caption = item.get("caption") or ""
                url = item.get("url") or ""
                owner = item.get("ownerUsername") or ""
                timestamp = item.get("timestamp")

                when = None
                if timestamp:
                    try:
                        when = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    except Exception:
                        when = None

                rec = make_record(
                    source="instagram_apify",
                    source_type="community",
                    text=caption,
                    posted_at=iso(when),
                    url=url,
                    author=owner,
                )
                if rec:
                    records.append(rec)
        except Exception as exc:
            print(f"[scraper] Instagram search for '#{tag}' failed: {exc}")

    return records


# ---------------------------------------------------------------------
# 4. NEWS SCRAPER & LINKED SYNC
# ---------------------------------------------------------------------
def scrape_recent_news(
    queries_en: Optional[List[str]] = None,
    queries_ta: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Scrapes the most recent news updates using the linked keywords from
    collect_social.py. Uses Google News RSS, which requires NO API token
    and never expires.
    """
    q_en = queries_en if queries_en is not None else LINKED_NEWS_QUERIES_EN
    q_ta = queries_ta if queries_ta is not None else LINKED_NEWS_QUERIES_TA
    print(f"[scraper] Fetching latest news for {len(q_en)} EN and {len(q_ta)} TA linked queries...")
    return collect_rss(queries_en=q_en, queries_ta=q_ta)


def sync_recent_updates(
    sources: tuple[str, ...] | list[str] = ("news",),
    out_path: Path = OUT_DEFAULT,
    max_results_per_query: int = 5,
) -> Dict[str, Any]:
    """
    Pulls recent updates from requested sources using linked keywords,
    deduplicates against existing records, and appends new ones to out_path.

    Supported source options: 'news', 'youtube', 'instagram', 'all'
    """
    sources_set = set(sources)
    if "all" in sources_set:
        sources_set = {"news", "youtube", "instagram"}

    all_records: List[Dict[str, Any]] = []

    if "news" in sources_set:
        all_records.extend(scrape_recent_news())

    if "youtube" in sources_set:
        all_records.extend(scrape_youtube_records(max_results=max_results_per_query))

    if "instagram" in sources_set:
        all_records.extend(scrape_instagram_records(max_results=max_results_per_query))

    out_file = Path(out_path)
    if not out_file.is_absolute():
        out_file = PROJECT_ROOT / out_file

    added, skipped = save(all_records, out_file)
    hits = sum(1 for r in all_records if r.get("kw_hit"))

    summary = {
        "status": "ok",
        "timestamp": iso(datetime.now(IST)),
        "sources": list(sources_set),
        "total_scraped": len(all_records),
        "added": added,
        "skipped_duplicates": skipped,
        "keyword_hits": hits,
        "output_file": str(out_file),
    }
    return summary


# ---------------------------------------------------------------------
# 5. CONTINUOUS AUTO-SYNC
# ---------------------------------------------------------------------
def continuous_auto_sync(
    interval_seconds: int = 300,
    sources: tuple[str, ...] | list[str] = ("news",),
    out_path: Path = OUT_DEFAULT,
    max_cycles: Optional[int] = None,
    stop_event: Optional[threading.Event] = None,
    on_cycle_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> None:
    """
    Continuously synchronizes recent news and social media updates at regular
    intervals using the linked keywords.

    Runs in a loop until stop_event is set, max_cycles is reached, or Ctrl+C.
    """
    cycle = 0
    print("=" * 65)
    print("   AI URBAN INTELLIGENCE PLATFORM -- CONTINUOUS NEWS AUTO-SYNC   ")
    print("=" * 65)
    print(f"[*] Interval: {interval_seconds}s ({interval_seconds / 60:.1f} minutes)")
    print(f"[*] Sources: {', '.join(sources)}")
    print(f"[*] Output: {out_path}")
    print(f"[*] Linked keywords: {len(LINKED_NEWS_QUERIES_EN)} EN + {len(LINKED_NEWS_QUERIES_TA)} TA queries")
    print("[*] Press Ctrl+C to stop continuous sync.\n")

    try:
        while True:
            cycle += 1
            now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            print(f"--- [Auto-sync Cycle #{cycle} @ {now_str}] ---")

            try:
                result = sync_recent_updates(sources=sources, out_path=out_path)
                print(f"    Synced: +{result['added']} new articles, "
                      f"{result['skipped_duplicates']} duplicates skipped, "
                      f"{result['keyword_hits']} keyword hits")
                if on_cycle_callback:
                    on_cycle_callback(result)
            except Exception as exc:
                print(f"    [!] Error during sync cycle #{cycle}: {exc}")

            if max_cycles and cycle >= max_cycles:
                print(f"[*] Reached max cycles ({max_cycles}). Stopping auto-sync.")
                break

            print(f"[*] Sleeping for {interval_seconds}s until next sync...\n")
            if stop_event:
                if stop_event.wait(timeout=interval_seconds):
                    print("[*] Stop event triggered. Exiting continuous sync.")
                    break
            else:
                time.sleep(interval_seconds)

    except KeyboardInterrupt:
        print("\n[*] Continuous auto-sync terminated by user.")


def start_background_sync(
    interval_seconds: int = 300,
    sources: tuple[str, ...] | list[str] = ("news",),
    out_path: Path = OUT_DEFAULT,
) -> tuple[threading.Thread, threading.Event]:
    """
    Starts the continuous news auto-sync in a background daemon thread.
    Returns (thread, stop_event).
    """
    stop_event = threading.Event()
    thread = threading.Thread(
        target=continuous_auto_sync,
        kwargs={
            "interval_seconds": interval_seconds,
            "sources": sources,
            "out_path": out_path,
            "stop_event": stop_event,
        },
        daemon=True,
        name="NewsAutoSyncThread",
    )
    thread.start()
    print(f"[scraper] Background continuous news sync thread started (id={thread.ident})")
    return thread, stop_event


# ---------------------------------------------------------------------
# 6. COMMAND-LINE INTERFACE
# ---------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scrape and auto-sync civic news & social updates linked with collect_social.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--continuous", "-c", action="store_true",
        help="Run continuously in a loop syncing recent news updates"
    )
    parser.add_argument(
        "--interval", "-i", type=int, default=300,
        help="Polling interval in seconds for continuous mode (default: 300 / 5 min)"
    )
    parser.add_argument(
        "--sources", "-s", nargs="+", choices=["news", "youtube", "instagram", "all"],
        default=["news"], help="Sources to sync (default: news)"
    )
    parser.add_argument(
        "--sync-news", action="store_true",
        help="Perform a one-time sync of recent news updates with linked keywords"
    )
    parser.add_argument(
        "--keyword", "-k", type=str, default=None,
        help="Test search a specific keyword on Apify YouTube / Instagram"
    )
    parser.add_argument(
        "--max-results", type=int, default=5,
        help="Max results per search query (default: 5)"
    )
    parser.add_argument(
        "--out", type=Path, default=OUT_DEFAULT,
        help=f"Output JSONL file path (default: {OUT_DEFAULT})"
    )
    parser.add_argument(
        "--quota", action="store_true",
        help="Display current Free-Tier Protector quota usage, limits, and safety status"
    )
    parser.add_argument(
        "--bypass-quota", action="store_true",
        help="Bypass daily free-tier safety caps (use with caution to avoid credit exhaustion)"
    )
    args = parser.parse_args()

    if args.bypass_quota:
        os.environ["BYPASS_QUOTA_GUARD"] = "1"
        print("[!] Warning: Quota guard bypassed by user.")

    # Case 0: View Free-Tier Quota Status
    if args.quota:
        status = FreeTierProtector.get_status()
        print("=" * 70)
        print("   AI URBAN INTELLIGENCE PLATFORM -- FREE-TIER QUOTA STATUS")
        print("=" * 70)
        print(f"Date: {status['date']} | Guard Status: ACTIVE\n")
        for key, s in status["services"].items():
            print(f"• {s['provider']}")
            print(f"    Daily Calls Used:   {s['daily_used']} / {s['daily_limit']}")
            if 'remaining_today' in s:
                print(f"    Remaining Today:    {s['remaining_today']}")
            print(f"    Exhaustion Risk:    {s['exhaustion_risk']}")
            rec = "YES (Recommended for 24/7)" if s['recommended_for_continuous'] else "NO (Preserve free credits)"
            print(f"    Safe for Continuous:{rec}\n")
        print("=" * 70)
        return 0

    # Case 1: Specific keyword test on Apify
    if args.keyword:
        print("=" * 60)
        print(f"Urban Intelligence Scraper: Single Keyword Test '{args.keyword}'")
        print("=" * 60)
        if "youtube" in args.sources or "all" in args.sources:
            try:
                videos = search_youtube(args.keyword, max_results=args.max_results)
                print(f"\n[OK] Found {len(videos)} YouTube reports for '{args.keyword}':")
                for idx, v in enumerate(videos, 1):
                    title = v.get("title", "No Title")
                    channel = v.get("channelName", "Unknown")
                    print(f"  {idx}. {title} ({channel})")
            except Exception as e:
                print(f"[YouTube error] {e}")

        if "instagram" in args.sources or "all" in args.sources:
            try:
                posts = search_instagram_hashtag(args.keyword.replace(" ", ""), max_results=args.max_results)
                print(f"\n[OK] Found {len(posts)} Instagram posts for #{args.keyword}:")
                for idx, p in enumerate(posts, 1):
                    caption = (p.get("caption") or "")[:80].replace("\n", " ")
                    print(f"  {idx}. {caption}...")
            except Exception as e:
                print(f"[Instagram error] {e}")
        return 0

    # Case 2: Continuous auto-sync
    if args.continuous:
        continuous_auto_sync(
            interval_seconds=args.interval,
            sources=args.sources,
            out_path=args.out,
        )
        return 0

    # Case 3: One-off sync (default behavior)
    print("=" * 65)
    print("Urban Intelligence Platform: Syncing recent updates with linked keywords")
    print(f"Linked queries: {len(LINKED_NEWS_QUERIES_EN)} EN, {len(LINKED_NEWS_QUERIES_TA)} TA, {len(LINKED_YOUTUBE_QUERIES)} YouTube")
    print("=" * 65)
    res = sync_recent_updates(sources=args.sources, out_path=args.out, max_results_per_query=args.max_results)
    print("\n--- Sync Summary ---")
    print(f"  Status:               {res['status']}")
    print(f"  Sources:              {', '.join(res['sources'])}")
    print(f"  Total records read:   {res['total_scraped']}")
    print(f"  New records added:    {res['added']}")
    print(f"  Duplicates skipped:   {res['skipped_duplicates']}")
    print(f"  Keyword hits tagged:  {res['keyword_hits']}")
    print(f"  Saved to:             {res['output_file']}")
    print("=" * 65)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
