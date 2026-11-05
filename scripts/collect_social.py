#!/usr/bin/env python3
"""
collect_social.py -- pull public Chennai transport posts into one JSONL file.

Put it in  scripts/  of the repo and run from the project root:

    python scripts/collect_social.py rss                    # no key, start here
    python scripts/collect_social.py youtube                # needs YOUTUBE_API_KEY
    python scripts/collect_social.py x --max-posts 100      # needs X_BEARER_TOKEN, PAID
    python scripts/collect_social.py playstore --app-id <id>
    python scripts/collect_social.py all                    # rss + youtube (+ x if token set)

Every source is converted to the same record and appended to
data/social/raw/posts.jsonl (duplicates skipped):

    {id, source, source_type, author_hash, text, posted_at, url, lang_hint, kw_hit}

Notes
  * Usernames are never stored, only a short one-way hash.
  * RSS gives headline + snippet only, not full articles.
  * Keys come from environment variables, never from the code.
  * Only needs `feedparser` (pip install feedparser). Everything else is stdlib.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DEFAULT = Path("data/social/raw/posts.jsonl")

# Searches used for news and X.  Edit freely.
NEWS_QUERIES_EN = [
    "Chennai MTC bus", "Chennai metro", "Chennai suburban train",
    "Chennai waterlogging subway", "Chennai bus breakdown",
    "Chennai traffic jam", "Chennai One app", "CUMTA Chennai",
]
NEWS_QUERIES_TA = [
    "சென்னை மாநகர பேருந்து", "சென்னை மெட்ரோ", "சென்னை வெள்ளம் சுரங்கப்பாதை",
    "சென்னை போக்குவரத்து நெரிசல்",
]
YOUTUBE_QUERIES = [
    "Chennai bus", "Chennai metro", "Chennai flood", "Chennai traffic",
]
INSTAGRAM_HASHTAGS = [
    "chennaitraffic", "chennaimetro", "chennairains",
    "chennaifloods", "mtcchennai", "chennaicivic",
]
X_QUERY = (
    "(Chennai OR MTC OR CMRL OR #ChennaiRains) "
    "(bus OR metro OR train OR flood OR waterlogging OR traffic OR breakdown) "
    "-is:retweet"
)

# Cheap relevance tag so you can skip junk when labelling.  It does NOT delete.
KEYWORDS = re.compile(
    r"\b(bus|buses|mtc|metro|cmrl|train|suburban|mrts|flood|waterlog\w*|traffic|"
    r"breakdown|delay\w*|crowd\w*|accident|pothole|subway|signal|chennai one|cumta|"
    r"ticket|fare)\b|பேருந்து|மெட்ரோ|வெள்ளம்|நெரிசல்|விபத்து|ரயில்|தண்ணீர்|கூட்டம்",
    re.IGNORECASE,
)


# --------------------------------------------------------------------- utils
def author_hash(name: str | None) -> str:
    if not name:
        return ""
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]


def clean(text: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")           # strip HTML tags
    text = re.sub(r"&nbsp;|&amp;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def lang_hint(text: str) -> str:
    """Script-level guess only: tamil / english. Tanglish needs the NLP step."""
    tamil = sum(1 for c in text if "\u0b80" <= c <= "\u0bff")
    letters = sum(1 for c in text if c.isalpha()) or 1
    return "tamil" if tamil / letters > 0.3 else "latin"


def iso(dt: datetime | None) -> str:
    return (dt or datetime.now(IST)).astimezone(IST).isoformat(timespec="seconds")


def make_record(source, source_type, text, posted_at, url, author=None) -> dict | None:
    text = clean(text)
    if len(text) < 15:
        return None
    key = hashlib.sha1(f"{source}|{url or text[:200]}".encode("utf-8")).hexdigest()[:16]
    return {
        "id": f"{source}_{key}",
        "source": source,
        "source_type": source_type,       # news | community | review | official
        "author_hash": author_hash(author),
        "text": text,
        "posted_at": posted_at,
        "url": url,
        "lang_hint": lang_hint(text),
        "kw_hit": bool(KEYWORDS.search(text)),
    }


def http_json(url: str, headers: dict | None = None, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "chennai-mobility-research/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ------------------------------------------------------------------- sources
def rss_records_from_feed(feed) -> list[dict]:
    """feed = a parsed feedparser result (kept separate so it can be tested offline)."""
    out = []
    for e in feed.entries:
        try:
            when = parsedate_to_datetime(e.get("published", "")) if e.get("published") else None
        except (TypeError, ValueError):
            when = None
        text = f"{e.get('title', '')}. {e.get('summary', '')}"
        rec = make_record("news_rss", "news", text, iso(when), e.get("link"),
                          author=(e.get("source") or {}).get("title") if isinstance(e.get("source"), dict) else None)
        if rec:
            out.append(rec)
    return out


def parse_rss_xml(xml_bytes: bytes) -> list[dict]:
    """Fallback standard library parser for Google News RSS XML when feedparser is not installed."""
    import xml.etree.ElementTree as ET
    records = []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return []
    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        pub_str = item.findtext("pubDate") or ""
        desc = item.findtext("description") or ""
        src_el = item.find("source")
        src_name = src_el.text if src_el is not None else None

        when = None
        if pub_str:
            try:
                when = parsedate_to_datetime(pub_str)
            except Exception:
                when = None
        text = f"{title}. {clean(desc)}" if desc else title
        rec = make_record("news_rss", "news", text, iso(when), link, author=src_name)
        if rec:
            records.append(rec)
    return records


def collect_rss(queries_en: list[str] | None = None, queries_ta: list[str] | None = None) -> list[dict]:
    feedparser = None
    try:
        import feedparser as _fp
        feedparser = _fp
    except ImportError:
        feedparser = None

    q_en = queries_en if queries_en is not None else NEWS_QUERIES_EN
    q_ta = queries_ta if queries_ta is not None else NEWS_QUERIES_TA
    records = []
    jobs = [(q, "en-IN", "IN:en") for q in q_en] + [(q, "ta", "IN:ta") for q in q_ta]
    for query, hl, ceid in jobs:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query + " when:7d")
               + f"&hl={hl}&gl=IN&ceid={ceid}")
        try:
            if feedparser:
                feed = feedparser.parse(url)
                got = rss_records_from_feed(feed)
            else:
                req = urllib.request.Request(url, headers={"User-Agent": "chennai-mobility-research/0.1"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    got = parse_rss_xml(resp.read())
            print(f"  rss  {query!r:42} -> {len(got)}")
            records += got
        except Exception as exc:
            print(f"  rss  {query!r:42} -> error ({exc})")
        time.sleep(1)                      # be polite
    return records


def collect_youtube(max_videos: int = 6, max_comments: int = 100) -> list[dict]:
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        print("  youtube: set YOUTUBE_API_KEY (free, Google Cloud Console) -- skipped")
        return []

    try:
        from backend.scraper import FreeTierProtector
        allowed, reason = FreeTierProtector.can_proceed("youtube_api")
        if not allowed:
            print(f"  {reason}")
            return []
    except ImportError:
        pass

    records = []
    published_after = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for query in YOUTUBE_QUERIES:
        try:
            from backend.scraper import FreeTierProtector
            FreeTierProtector.record_usage("youtube_api")
        except ImportError:
            pass
        # search.list is the expensive call (about 100 quota units). Keep queries few.
        params = urllib.parse.urlencode({
            "part": "snippet", "q": query, "type": "video", "order": "date",
            "regionCode": "IN", "maxResults": max_videos,
            "publishedAfter": published_after, "key": key})
        try:
            videos = http_json("https://www.googleapis.com/youtube/v3/search?" + params).get("items", [])
        except urllib.error.HTTPError as exc:
            print(f"  youtube search failed ({exc.code}); quota or key problem")
            break
        for v in videos:
            vid = v["id"].get("videoId")
            if not vid:
                continue
            c_params = urllib.parse.urlencode({
                "part": "snippet", "videoId": vid, "maxResults": min(max_comments, 100),
                "order": "time", "textFormat": "plainText", "key": key})
            try:
                items = http_json("https://www.googleapis.com/youtube/v3/commentThreads?" + c_params).get("items", [])
            except urllib.error.HTTPError:
                continue                    # comments disabled on that video
            for it in items:
                s = it["snippet"]["topLevelComment"]["snippet"]
                when = datetime.fromisoformat(s["publishedAt"].replace("Z", "+00:00"))
                rec = make_record("youtube", "community", s.get("textDisplay", ""), iso(when),
                                  f"https://www.youtube.com/watch?v={vid}&lc={it['id']}",
                                  author=s.get("authorChannelId", {}).get("value"))
                if rec:
                    records.append(rec)
        print(f"  youtube {query!r:26} -> {len(records)} comments so far")
    return records


def collect_x(max_posts: int = 100) -> list[dict]:
    """PAID. Each post read costs money. max_posts is a hard cap on this run."""
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        print("  x: set X_BEARER_TOKEN (paid pay-per-use account) -- skipped")
        return []
    records, next_token, remaining = [], None, max_posts
    while remaining > 0:
        batch = max(10, min(100, remaining))       # API minimum per request is 10
        params = {"query": X_QUERY, "max_results": batch,
                  "tweet.fields": "created_at,lang,author_id"}
        if next_token:
            params["next_token"] = next_token
        try:
            data = http_json("https://api.x.com/2/tweets/search/recent?" + urllib.parse.urlencode(params),
                             headers={"Authorization": f"Bearer {token}"})
        except urllib.error.HTTPError as exc:
            print(f"  x request failed ({exc.code}): check credits and the endpoint in docs.x.com")
            break
        for t in data.get("data", []):
            when = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
            rec = make_record("x", "community", t["text"], iso(when),
                              f"https://x.com/i/status/{t['id']}", author=t.get("author_id"))
            if rec:
                records.append(rec)
        remaining -= batch
        next_token = data.get("meta", {}).get("next_token")
        if not next_token:
            break
    print(f"  x -> {len(records)} posts (stopped at your cap of {max_posts})")
    return records


def collect_playstore(app_id: str | None, count: int = 200) -> list[dict]:
    if not app_id:
        print("  playstore: pass --app-id (the text after 'id=' in the Play Store URL) -- skipped")
        return []
    try:
        from google_play_scraper import Sort, reviews
    except ImportError:
        print("  playstore: python -m pip install google-play-scraper -- skipped")
        return []
    got, _ = reviews(app_id, lang="en", country="in", sort=Sort.NEWEST, count=count)
    records = []
    for r in got:
        rec = make_record("playstore", "review", r.get("content", ""), iso(r.get("at")),
                          f"https://play.google.com/store/apps/details?id={app_id}",
                          author=r.get("userName"))
        if rec:
            rec["rating"] = r.get("score")
            records.append(rec)
    print(f"  playstore {app_id} -> {len(records)} reviews")
    return records


def collect_apify(max_results: int = 5) -> list[dict]:
    """Collect from Apify (YouTube & Instagram) via backend/scraper.py using linked keywords."""
    root_dir = Path(__file__).resolve().parent.parent
    if str(root_dir) not in sys.path:
        sys.path.insert(0, str(root_dir))
    try:
        from backend.scraper import scrape_youtube_records, scrape_instagram_records
    except Exception as exc:
        print(f"  apify: could not import backend.scraper ({exc}) -- skipped")
        return []

    records = []
    print("  apify: scraping YouTube with linked civic queries...")
    try:
        records += scrape_youtube_records(queries=YOUTUBE_QUERIES, max_results=max_results)
    except Exception as exc:
        print(f"  apify youtube failed: {exc}")

    print("  apify: scraping Instagram with linked civic hashtags...")
    try:
        records += scrape_instagram_records(hashtags=INSTAGRAM_HASHTAGS, max_results=max_results)
    except Exception as exc:
        print(f"  apify instagram failed: {exc}")

    return records


# ------------------------------------------------------------------- storage
def save(records: list[dict], out: Path) -> tuple[int, int]:
    out.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    if out.exists():
        with out.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    seen.add(json.loads(line)["id"])
                except (ValueError, KeyError):
                    pass
    new = [r for r in records if r["id"] not in seen]
    with out.open("a", encoding="utf-8") as fh:
        for r in new:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(new), len(records) - len(new)


def run_collection(args) -> list[dict]:
    records: list[dict] = []
    if args.source in ("rss", "all"):
        records += collect_rss()
    if args.source in ("youtube", "all"):
        records += collect_youtube()
    if args.source in ("apify", "all"):
        records += collect_apify(args.max_results)
    if args.source == "x" or (args.source == "all" and os.environ.get("X_BEARER_TOKEN")):
        records += collect_x(args.max_posts)
    if args.source == "playstore":
        records += collect_playstore(args.app_id)
    return records


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", choices=["rss", "youtube", "x", "playstore", "apify", "all"])
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--max-posts", type=int, default=100, help="hard cap for X (paid)")
    ap.add_argument("--max-results", type=int, default=5, help="max results per query for Apify / YouTube")
    ap.add_argument("--app-id", help="Play Store app id for playstore")
    ap.add_argument("--continuous", action="store_true", help="run continuously, polling on an interval")
    ap.add_argument("--interval", type=int, default=300, help="seconds between sync cycles in continuous mode (default: 300)")
    args = ap.parse_args()

    if args.continuous:
        print(f"[*] Starting continuous collector for '{args.source}' every {args.interval}s. Press Ctrl+C to stop.\n")
        try:
            while True:
                now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
                print(f"--- [Sync cycle started at {now_str}] ---")
                records = run_collection(args)
                added, skipped = save(records, args.out)
                hits = sum(1 for r in records if r.get("kw_hit"))
                print(f"[{now_str}] Done: saved {added} new, skipped {skipped} duplicates -> {args.out}")
                print(f"[{now_str}] Transport keyword hits: {hits}/{len(records)}")
                print(f"[*] Sleeping for {args.interval} seconds until next sync...\n")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[*] Continuous collector stopped by user.")
            return 0

    records = run_collection(args)
    added, skipped = save(records, args.out)
    hits = sum(1 for r in records if r["kw_hit"])
    print(f"\nsaved {added} new, skipped {skipped} duplicates -> {args.out}")
    print(f"{hits}/{len(records)} mention a transport keyword (kw_hit=true)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
