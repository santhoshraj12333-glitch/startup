import json
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional

from backend.schemas.social import SocialPost, ProcessedEvent
from backend.db import Database

router = APIRouter()

def get_db() -> Database:
    # This dependency will be resolved by FastAPI when the app includes the router
    # It expects the FastAPI app to have `state.db` set (see backend/app.py)
    raise RuntimeError("Database dependency not configured – include router via backend/app.py")

# ---------------------------------------------------------------------------
# GET /social/health – simple health check (wrapper around existing /health)
@router.get("/health", summary="Social API health check")
def health_check(db: Database = Depends(get_db)):
    # Reuse existing database health logic if needed
    counts = db.count_events()
    return {
        "status": "ok",
        "events": sum(counts.values()),
        "events_by_type": counts,
    }

# ---------------------------------------------------------------------------
# GET /social/posts – list normalized posts with pagination / filters
@router.get("/posts", response_model=List[SocialPost], summary="List normalized social posts")
def list_posts(
    source: Optional[str] = Query(None, description="Filter by source name"),
    limit: int = Query(100, le=500, description="Maximum number of records"),
    offset: int = Query(0, description="Offset for pagination"),
    db: Database = Depends(get_db),
):
    query = "SELECT * FROM normalized_posts"
    params: List = []
    conditions: List[str] = []
    if source:
        conditions.append("source_id = (SELECT source_id FROM sources WHERE name = ?)")
        params.append(source)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = db.query(query, tuple(params))
    # Convert rows to SocialPost models
    posts = []
    for r in rows:
        posts.append(
            SocialPost(
                source=r["source_id"],  # placeholder – real implementation would join source name
                source_post_id=r["source_post_id"],
                text=r["text"],
                published_at=r["published_at"],
                url=r["url"],
                author_id=r["author_id"],
                language=r["language"],
                collection_timestamp=r["collection_timestamp"],
                location_id=r["location_id"],
                resolved_status=r["resolved_status"],
            )
        )
    return posts

# ---------------------------------------------------------------------------
# GET /social/events – list processed events
@router.get("/events", response_model=List[ProcessedEvent], summary="List processed mobility events")
def list_events(
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
    db: Database = Depends(get_db),
):
    query = "SELECT * FROM processed_events"
    params: List = []
    if event_type:
        query += " WHERE event_type = ?"
        params.append(event_type)
    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = db.query(query, tuple(params))
    events = []
    for r in rows:
        events.append(
            ProcessedEvent(
                event_id=r["event_id"],
                source_post_id=r["normalized_post_id"],  # placeholder mapping
                event_type=r["event_type"],
                severity=r["severity"],
                confidence=r["confidence"],
                location_id=r["location_id"],
                details=json.loads(r["details_json"]) if r["details_json"] else None,
                processed_at=r["processed_at"],
            )
        )
    return events

# ---------------------------------------------------------------------------
# GET /social/sources – list all configured data sources
@router.get("/sources", summary="List data sources")
def list_sources(db: Database = Depends(get_db)):
    rows = db.query("SELECT source_id, name, type, config_json, created_at FROM sources")
    return [dict(row) for row in rows]

# ---------------------------------------------------------------------------
# GET /social/summary – basic aggregate statistics (placeholder)
@router.get("/summary", summary="Aggregate statistics for social data")
def summary(db: Database = Depends(get_db)):
    total_posts = db.query_one("SELECT COUNT(*) as cnt FROM normalized_posts")["cnt"]
    total_events = db.query_one("SELECT COUNT(*) as cnt FROM processed_events")["cnt"]
    return {"total_posts": total_posts, "total_events": total_events}

