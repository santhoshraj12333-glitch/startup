"""
backend.app
===========

FastAPI backend + the dashboard's static files.

There was no existing backend in any team member's zip, so this is the new
one -- and it is deliberately thin: the pipeline already writes the common
event schema into SQLite, so almost every endpoint here is one query.

Run it with::

    python run_backend.py            # http://127.0.0.1:8000

Endpoints
---------
GET  /health                    service + database status
POST /events                    ingest an event (used when backend.mode=http)
GET  /events                    filter by type/module/severity/bus/segment/since
GET  /events/{event_id}
GET  /road-defects              module 1
GET  /traffic                   module 2 (observations + congestion)
GET  /incidents                 module 4 (all incident types, filterable)
GET  /incidents/{incident_id}   one incident + state timeline + evidence
GET  /hit-and-run               hit-and-run alerts only
GET  /anpr                      every plate read, UNKNOWNs included
GET  /vehicles                  module 5 (ANPR identifications)
GET  /vehicles/{track_id}       one vehicle, with its incident + plate
GET  /issues                    module 6 persistent issues
GET  /issues/{issue_id}         one issue with its observation history
GET  /analytics                 counts, severities, departments, priorities
GET  /heatmap                   per-road-segment rows for the map
GET  /runs                      pipeline runs
GET  /evidence/{path}           evidence images / clips
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, HTTPException, Query                # noqa: E402
from fastapi.middleware.cors import CORSMiddleware               # noqa: E402
from fastapi.responses import FileResponse, JSONResponse          # noqa: E402
from fastapi.staticfiles import StaticFiles                       # noqa: E402

from core.config import load_config                               # noqa: E402
from core.event import Event                                      # noqa: E402
from core.logging_setup import get_logger, setup_logging          # noqa: E402
from backend.db import Database                                   # noqa: E402

INCIDENT_TYPES = [
    "collision", "possible_collision", "hit_and_run",
    "rash_driving", "abnormal_movement",
]
TRAFFIC_TYPES = ["traffic_observation", "traffic_congestion", "traffic_bottleneck"]


def create_app(config=None) -> FastAPI:
    config = config or load_config()
    setup_logging(config.get("platform.log_level", "INFO"))
    log = get_logger("backend")

    database = Database(config.path("backend.database")).initialise()
    evidence_dir = config.path("backend.evidence_dir")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    frontend_dir = PROJECT_ROOT / "frontend"

    app = FastAPI(
        title="AI Urban Intelligence Platform",
        version="1.0.0",
        description="Buses as mobile urban sensors -- integrated backend.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.db = database
    app.state.config = config

    # ---------------------------------------------------------------- health
# Import and include the social router
from backend.api.social import router as social_router
app.include_router(social_router, prefix="/social")

    @app.get("/health")
    def health() -> Dict[str, Any]:
        counts = database.count_events()
        return {
            "status": "ok",
            "database": str(config.path("backend.database")),
            "events": sum(counts.values()),
            "events_by_type": counts,
            "issues": len(database.all_issues()),
        }

    # ---------------------------------------------------------------- events

    @app.post("/events")
    def ingest(payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            event = Event.from_dict(payload)
        except Exception as exc:                                 # noqa: BLE001
            raise HTTPException(400, f"invalid event: {exc}") from exc
        database.insert_event(event)
        return {"ok": True, "event_id": event.event_id}

    @app.post("/events/batch")
    def ingest_batch(payload: List[Dict[str, Any]]) -> Dict[str, Any]:
        stored = 0
        for item in payload:
            try:
                database.insert_event(Event.from_dict(item))
                stored += 1
            except Exception as exc:                             # noqa: BLE001
                log.warning("skipped bad event: %s", exc)
        return {"ok": True, "stored": stored}

    @app.get("/events")
    def events(
        event_type: Optional[str] = None,
        source_module: Optional[str] = None,
        severity: Optional[str] = None,
        bus_id: Optional[str] = None,
        issue_id: Optional[str] = None,
        road_segment: Optional[str] = None,
        since: Optional[float] = None,
        include_duplicates: bool = True,
        limit: int = Query(100, le=2000),
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        return database.query_events(
            event_type=event_type, source_module=source_module,
            severity=severity, bus_id=bus_id, issue_id=issue_id,
            road_segment=road_segment, since=since,
            include_duplicates=include_duplicates, limit=limit, offset=offset,
        )

    @app.get("/events/{event_id}")
    def event_detail(event_id: str) -> Dict[str, Any]:
        event = database.get_event(event_id)
        if event is None:
            raise HTTPException(404, "event not found")
        return event

    # ------------------------------------------------------- per-module views

    @app.get("/road-defects")
    def road_defects(limit: int = Query(100, le=2000), include_duplicates: bool = True):
        return database.query_events(
            event_type="road_defect", limit=limit,
            include_duplicates=include_duplicates,
        )

    @app.get("/traffic")
    def traffic(limit: int = Query(200, le=2000), congestion_only: bool = False):
        types = ["traffic_congestion", "traffic_bottleneck"] if congestion_only else TRAFFIC_TYPES
        return database.query_events(event_types=types, limit=limit)

    @app.get("/incidents")
    def incidents(
        limit: int = Query(200, le=2000),
        event_type: Optional[str] = None,
        status: Optional[str] = None,
        min_confidence: float = 0.0,
    ):
        """The incident feed the dashboard's Incidents page reads.

        ``event_type`` accepts one canonical type, or the shorthand
        ``possible`` for everything that is suspected rather than confirmed.
        """
        if event_type == "possible":
            rows = database.query_events(
                event_types=["possible_collision", "abnormal_movement"], limit=limit
            )
        elif event_type:
            rows = database.query_events(event_type=event_type, limit=limit)
        else:
            rows = database.query_events(event_types=INCIDENT_TYPES, limit=limit)

        if status:
            rows = [
                r for r in rows
                if (r.get("metadata") or {}).get("status") == status
            ]
        if min_confidence:
            rows = [r for r in rows if (r.get("confidence") or 0) >= min_confidence]
        return rows

    @app.get("/incidents/{incident_id}")
    def incident_detail(incident_id: str):
        """One incident, fully assembled for the investigation view:
        state-machine timeline, evidence frames, the vehicle's other events
        and the ANPR result if identification was requested."""
        rows = database.query_events(event_types=INCIDENT_TYPES, limit=2000)
        record = next(
            (
                r for r in rows
                if (r.get("metadata") or {}).get("incident_id") == incident_id
                or r.get("event_id") == incident_id
            ),
            None,
        )
        if record is None:
            raise HTTPException(404, "incident not found")

        metadata = record.get("metadata") or {}
        track_id = metadata.get("vehicle_track_id") or record.get("track_id")

        related = [
            r for r in database.query_events(limit=2000)
            if r.get("track_id") == track_id
            and r.get("bus_id") == record.get("bus_id")
        ]
        identification = next(
            (r for r in related if r["event_type"] == "vehicle_identification"), None
        )
        return {
            "incident": record,
            "incident_id": incident_id,
            "timeline": metadata.get("timeline") or [],
            "evidence": metadata.get("evidence_frames") or {},
            "vehicle": database.get_vehicle(track_id, record.get("bus_id"))
            if track_id is not None else None,
            "identification": identification,
            "related_events": related,
        }

    @app.get("/hit-and-run")
    def hit_and_run(limit: int = Query(100, le=1000)):
        """Hit-and-run alerts, newest first -- the dedicated page's feed."""
        return database.query_events(event_type="hit_and_run", limit=limit)

    @app.get("/anpr")
    def anpr(limit: int = Query(100, le=1000)):
        """Every plate read, including the honest UNKNOWNs."""
        return database.query_events(
            event_type="vehicle_identification", limit=limit
        )

    @app.get("/vehicles")
    def vehicles(limit: int = Query(100, le=1000)):
        return database.list_vehicles(limit=limit)

    @app.get("/vehicles/{track_id}")
    def vehicle_detail(track_id: int, bus_id: Optional[str] = None):
        vehicle = database.get_vehicle(track_id, bus_id)
        if vehicle is None:
            raise HTTPException(404, "vehicle not found")
        related = database.query_events(limit=50)
        vehicle["events"] = [
            e for e in related
            if e.get("track_id") == track_id
            and (bus_id is None or e.get("bus_id") == bus_id)
        ]
        return vehicle

    # ------------------------------------------------------------- issues

    @app.get("/issues")
    def issues(
        status: Optional[str] = None,
        class_name: Optional[str] = None,
        department: Optional[str] = None,
        limit: int = Query(200, le=1000),
    ):
        return database.list_issues(
            status=status, class_name=class_name,
            department=department, limit=limit,
        )

    @app.get("/issues/{issue_id}")
    def issue_detail(issue_id: str):
        issue = database.get_issue(issue_id)
        if issue is None:
            raise HTTPException(404, "issue not found")
        issue["observations"] = database.issue_observations(issue_id)
        issue["events"] = database.query_events(issue_id=issue_id, limit=100)
        return issue

    # ---------------------------------------------------------- analytics

    @app.get("/analytics")
    def analytics() -> Dict[str, Any]:
        counts = database.count_events()
        all_issues = database.all_issues()

        by_status: Dict[str, int] = {}
        by_department: Dict[str, int] = {}
        by_band: Dict[str, int] = {}
        for issue in all_issues:
            by_status[issue.get("status") or "OPEN"] = by_status.get(issue.get("status") or "OPEN", 0) + 1
            department = issue.get("department") or "Unassigned"
            by_department[department] = by_department.get(department, 0) + 1
            band = issue.get("priority_band") or "LOW"
            by_band[band] = by_band.get(band, 0) + 1

        recent = database.query_events(limit=500)
        by_severity: Dict[str, int] = {}
        by_bus: Dict[str, int] = {}
        for event in recent:
            severity = event.get("severity") or "INFO"
            by_severity[severity] = by_severity.get(severity, 0) + 1
            bus = event.get("bus_id") or "unknown"
            by_bus[bus] = by_bus.get(bus, 0) + 1

        traffic_rows = database.query_events(
            event_type="traffic_observation", limit=200, order="asc"
        )
        timeline = [
            {
                "timestamp": row.get("timestamp"),
                "video_time_s": row.get("video_time_s"),
                "vehicle_count": (row.get("metadata") or {}).get("vehicle_count"),
                "density_percentage": (row.get("metadata") or {}).get("density_percentage"),
                "traffic_level": (row.get("metadata") or {}).get("traffic_level"),
                "average_speed_px_s": (row.get("metadata") or {}).get("average_speed_px_s"),
            }
            for row in traffic_rows
        ]

        return {
            "events_total": sum(counts.values()),
            "events_by_type": counts,
            "events_by_severity": by_severity,
            "events_by_bus": by_bus,
            "issues_total": len(all_issues),
            "issues_by_status": by_status,
            "issues_by_department": by_department,
            "issues_by_priority_band": by_band,
            "top_issues": sorted(
                all_issues, key=lambda i: (i.get("priority_score") or 0), reverse=True
            )[:10],
            "traffic_timeline": timeline,
            "segments": len(database.list_segments()),
        }

    @app.get("/heatmap")
    def heatmap() -> Dict[str, Any]:
        segments = database.list_segments()
        issues = database.all_issues()
        return {
            "segments": [s for s in segments if s.get("latitude") is not None],
            "issues": [
                {
                    "issue_id": i["issue_id"],
                    "class_name": i.get("class_name"),
                    "issue_type": i.get("issue_type"),
                    "status": i.get("status"),
                    "severity": i.get("severity"),
                    "latitude": i.get("latitude"),
                    "longitude": i.get("longitude"),
                    "priority_score": i.get("priority_score"),
                    "priority_band": i.get("priority_band"),
                    "department": i.get("department"),
                    "observation_count": i.get("observation_count"),
                    "bus_count": i.get("bus_count"),
                }
                for i in issues if i.get("latitude") is not None
            ],
        }

    @app.get("/runs")
    def runs(limit: int = Query(20, le=100)):
        return database.list_runs(limit=limit)

    @app.get("/videos")
    def videos() -> Dict[str, Any]:
        """Annotated videos the pipeline produced, for the Live Analysis page."""
        runs_dir = (config.path("output.events_jsonl") or Path("outputs/runs")).parent
        found = []
        if runs_dir.exists():
            for path in sorted(runs_dir.glob("*.mp4")):
                found.append({
                    "name": path.name,
                    "url": f"/videos/{path.name}",
                    "size_mb": round(path.stat().st_size / 1e6, 2),
                    "modified": path.stat().st_mtime,
                })
        return {"videos": found, "directory": str(runs_dir)}

    @app.get("/videos/{name}")
    def video_file(name: str):
        runs_dir = (config.path("output.events_jsonl") or Path("outputs/runs")).parent
        target = (runs_dir / name).resolve()
        if not str(target).startswith(str(runs_dir.resolve())):
            raise HTTPException(400, "invalid path")
        if not target.exists():
            raise HTTPException(404, "video not found")
        return FileResponse(target, media_type="video/mp4")

    # ------------------------------------------------------------ news / social

    @app.get("/news")
    @app.get("/social")
    def get_social_news(
        limit: int = Query(50, le=200),
        kw_only: bool = Query(False, description="Filter for posts matching transport keywords"),
        source: Optional[str] = Query(None, description="Filter by source, e.g. news_rss, youtube_apify, instagram_apify"),
        q: Optional[str] = Query(None, description="Search term in text"),
    ) -> Dict[str, Any]:
        """Returns the latest synced news and social posts from data/social/raw/posts.jsonl."""
        posts_path = PROJECT_ROOT / "data" / "social" / "raw" / "posts.jsonl"
        if not posts_path.exists():
            return {"posts": [], "total": 0, "path": str(posts_path)}

        items = []
        try:
            with posts_path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if kw_only and not rec.get("kw_hit"):
                            continue
                        if source and rec.get("source") != source:
                            continue
                        if q and q.lower() not in rec.get("text", "").lower():
                            continue
                        items.append(rec)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            raise HTTPException(500, f"Error reading posts: {e}")

        # newest first
        items.reverse()
        return {
            "posts": items[:limit],
            "total_matching": len(items),
            "limit": limit,
            "file": str(posts_path),
        }

    @app.post("/news/sync")
    def trigger_news_sync(
        sources: str = Query("news", description="Comma-separated sources: news, youtube, instagram, all")
    ) -> Dict[str, Any]:
        """Trigger an on-demand sync of recent news/social updates using linked keywords."""
        from backend.scraper import sync_recent_updates
        source_list = [s.strip() for s in sources.split(",") if s.strip()]
        result = sync_recent_updates(sources=source_list)
        return result

    @app.get("/quota")
    def get_quota_status() -> Dict[str, Any]:
        """Returns the free-tier usage status and active limits protecting against exhaustion."""
        from backend.scraper import FreeTierProtector
        return FreeTierProtector.get_status()

    # ------------------------------------------------------------ evidence

    @app.get("/evidence/{path:path}")
    def evidence(path: str):
        target = (evidence_dir / path).resolve()
        if not str(target).startswith(str(evidence_dir.resolve())):
            raise HTTPException(400, "invalid path")
        if not target.exists():
            raise HTTPException(404, "evidence not found")
        return FileResponse(target)

    # ------------------------------------------------------------ frontend

    if frontend_dir.exists():
        app.mount(
            "/app", StaticFiles(directory=str(frontend_dir), html=True), name="frontend"
        )

        @app.get("/")
        def index():
            return FileResponse(frontend_dir / "index.html")
    else:                                                        # pragma: no cover
        @app.get("/")
        def index():
            return JSONResponse({"service": "AI Urban Intelligence Platform"})

    if config.get("social_intelligence.auto_sync", False):
        try:
            from backend.scraper import start_background_sync
            interval = int(config.get("social_intelligence.interval_s", 300))
            sources = config.get("social_intelligence.sources", ["news"])
            start_background_sync(interval_seconds=interval, sources=sources)
            log.info("social_intelligence background sync started (interval=%ss, sources=%s)", interval, sources)
        except Exception as exc:
            log.warning("could not start social_intelligence auto_sync: %s", exc)

    log.info("backend ready -- database %s", config.path("backend.database"))
    return app


app = create_app()
