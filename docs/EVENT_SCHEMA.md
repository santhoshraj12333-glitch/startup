# The common event format

`core/event.py` defines one dataclass. Every module produces it, the database
stores it, the API serves it, the dashboard renders it. **Nothing else
crosses a module boundary** — modules do not import each other, they exchange
`Event` objects through the orchestrator.

Two rules shape it:

* A module is never forced to fill a field it does not have. Everything
  except `event_type` and `source_module` has a default.
* Anything module-specific lives in `metadata`, so the schema never grows a
  per-module column.

---

## Fields

| Field | Type | Filled by | Meaning |
|---|---|---|---|
| `event_id` | str | auto | uuid4 hex |
| `event_type` | str | **required** | see the vocabulary below |
| `source_module` | str | **required** | `road_detection` · `traffic` · `incident` · `anpr` · `intelligence` |
| `bus_id` | str? | orchestrator | e.g. `BUS-01` |
| `camera_id` | str? | orchestrator | e.g. `CAM-FRONT` |
| `timestamp` | str | auto | ISO-8601 UTC, millisecond precision |
| `gps` | object? | orchestrator | `{latitude, longitude, accuracy_m, speed_kmh, heading_deg}` |
| `confidence` | float | module | 0.0 – 1.0 |
| `severity` | str | module | `INFO` `LOW` `MEDIUM` `HIGH` `CRITICAL` |
| `track_id` | int? | module | BoT-SORT id — **the same id across all modules** |
| `class_name` | str? | module | canonical class, e.g. `pothole`, `car`, `truck_car` (a collision pair) |
| `bounding_box` | [int]? | module | `[x1, y1, x2, y2]` in pixels |
| `evidence_image` | str? | module | path relative to `outputs/evidence/` |
| `evidence_video` | str? | module | clip path, when one was saved |
| `frame_number` | int? | orchestrator | |
| `video_time_s` | float? | orchestrator | seconds into the clip |
| `road_segment` | str? | orchestrator | quantised segment id, e.g. `SEG-13.08490-80.27351` |
| `issue_id` | str? | **intelligence** | `POT-001` — null before Module 6 |
| `priority_score` | float? | **intelligence** | 0 – 100 |
| `department` | str? | **intelligence** | `Roads/PWD`, `Drainage`, `Traffic`, `Traffic Police`, `Transport` |
| `duplicate_of` | str? | **intelligence** | the event this one duplicates |
| `metadata` | object | module | everything module-specific |

`with_context(ctx)` stamps bus / camera / GPS / frame fields from the
`FrameContext` **without overwriting anything the module set deliberately**,
so a module can always override a default.

---

## Event types

| `event_type` | Source | Emitted when |
|---|---|---|
| `road_defect` | road_detection | a defect or infrastructure item is detected (or inferred missing) |
| `traffic_observation` | traffic | heartbeat every `observation_interval_s` — feeds heatmap and analytics |
| `traffic_congestion` | traffic | density/speed thresholds held for `minimum_duration_seconds` |
| `traffic_bottleneck` | traffic | a zone in the grid sustains bottleneck conditions |
| `rash_driving` | incident | speed spike, direction change, lane change or weaving |
| `possible_collision` | incident | partial contact evidence — reported as possible, not confirmed |
| `collision` | incident | transient overlap from a separated state, corroborated by approach or impact motion |
| `abnormal_movement` | incident | two suspicious motion signals, below the rash-driving bar |
| `hit_and_run` | incident | a collision whose offending track does not slow down |
| `vehicle_identification` | anpr | a plate was read for an incident track |
| `persistent_issue` | intelligence | an issue was opened, updated, reopened or resolved |

---

## Per-module `metadata`

Only the module-specific parts are listed; every event also has the common
fields above.

### `road_defect` — Person 1

```json
"metadata": {
  "raw_class_name": "Damaged road",
  "model": "road_defect_best.pt",
  "area_px": 18420,
  "relative_area": 0.031
}
```

An *inferred* absence (missing divider / missing zebra crossing) instead
carries:

```json
"metadata": {
  "inferred": true,
  "detector": "baseline",
  "reason": "segment SEG-13.08490-80.27351 showed a zebra crossing on 4 earlier passes, none on this one",
  "baseline_observations": 4
}
```

### `traffic_congestion` / `traffic_observation` — Person 2

```json
{
  "event_type": "traffic_congestion",
  "severity": "MEDIUM",
  "metadata": {
    "traffic_level": "HIGH",
    "vehicle_count": 60,
    "density_percentage": 71.2,
    "average_speed_px_s": 22.4,
    "moving_vehicles": 38,
    "slow_vehicles": 22,
    "congestion_status": "CONGESTED",
    "congestion_duration": 600,
    "bottleneck_status": "BOTTLENECK",
    "bottleneck_score": 0.87,
    "bottleneck_zone": "r1c2",
    "estimated_delay": 8,
    "counts_by_class": {"car": 41, "motorcycle": 12, "bus": 3, "truck": 4},
    "counted_by_class": {"car": 88, "motorcycle": 31},
    "counted_total": 137,
    "unique_vehicles": 164,
    "peak_vehicles": 71,
    "road_segment": "SEG-13.08490-80.27351"
  }
}
```

`road_segment` on these rows is what `GET /heatmap` aggregates.

### `hit_and_run` / `collision` / `possible_collision` — Person 4

```json
{
  "event_type": "hit_and_run",
  "track_id": 31,
  "class_name": "truck",
  "confidence": 0.72,
  "severity": "CRITICAL",
  "metadata": {
    "incident_id": "INC-000004",
    "incident_type": "hit_and_run",
    "status": "suspected",
    "state": "HIT_AND_RUN_CONFIRMED",
    "vehicle_track_id": 31,
    "other_track_id": 18,
    "collision_event_id": "3b90…",
    "collision_frame_id": 24,
    "incident_location": {"latitude": 13.0876, "longitude": 80.2764},
    "escape_distance_px": 232.35,
    "escape_speed_px_s": 232.35,
    "escape_direction": "right",
    "progress": 0.69,
    "timeline": [
      {"state": "SUSPICIOUS_INTERACTION",    "frame": 20, "video_time_s": 0.67, "note": "…"},
      {"state": "POSSIBLE_COLLISION",        "frame": 24, "video_time_s": 0.80, "note": "…"},
      {"state": "POST_COLLISION_MONITORING", "frame": 24, "video_time_s": 0.80, "note": "…"},
      {"state": "VEHICLE_LEAVING",           "frame": 33, "video_time_s": 1.10, "note": "…"},
      {"state": "HIT_AND_RUN_CONFIRMED",     "frame": 54, "video_time_s": 1.80, "note": "…"}
    ],
    "evidence_frames": {
      "frame_before":   "collision/BUS-01_000021_ab12.jpg",
      "incident_frame": "collision/BUS-01_000024_cd34.jpg",
      "frame_after":    "hit_and_run/BUS-01_000048_ef56.jpg",
      "vehicle_crop":   "hit_and_run/BUS-01_000054_78ab.jpg",
      "clip":           "hit_and_run/BUS-01_000048_90cd.mp4"
    },
    "detector": "person4.HitAndRunDetector",
    "requires_identification": true,
    "anpr_track_id": 31
  }
}
```

A `collision` carries the contact measurements instead:

```json
"metadata": {
  "status": "confirmed",
  "confirmation_tier": "strict",
  "vehicle_track_ids": [31, 18],
  "iou": 0.52, "approach": 1.0, "motion_change": 0.29,
  "impact_frame": 24,
  "detector": "person4.CollisionDetector",
  "requires_identification": false
}
```

`metadata.requires_identification` (a bool) plus `anpr_track_id` is the
**only** thing that makes ANPR run. A confirmed collision on its own does
**not** set it — only a hit-and-run, or a `HIGH`-severity `rash_driving`
event when `rash.trigger_anpr_on_high` is on.

A `synthetic: true` key means the record came from `--demo-incident` or
`scripts/demo_hit_and_run.py`. It is a demonstration, not a detection, and
the dashboard badges it as such.

### `vehicle_identification` — Person 5

```json
{
  "event_type": "vehicle_identification",
  "track_id": 24,
  "confidence": 0.94,
  "metadata": {
    "registration_number": "TN12AB1234",
    "plate_confidence": 0.94,
    "plate_valid_format": true,
    "vehicle_track_id": 24,
    "frames_used": 5,
    "votes": {"TN12AB1234": 4, "TN12A81234": 1},
    "raw_candidates": ["TN12AB1234", "TN12A81234"],
    "engine": "easyocr",
    "triggered_by_event_id": "9c218d70476a…",
    "triggered_by_event_type": "hit_and_run"
  }
}
```

### `persistent_issue` — Person 6

```json
{
  "event_type": "persistent_issue",
  "issue_id": "POT-001",
  "priority_score": 78.4,
  "department": "Roads/PWD",
  "metadata": {
    "issue_state": "OPENED",         // OPENED | UPDATED | REOPENED | RESOLVED
    "issue_id": "POT-001",
    "issue_type": "pothole",
    "first_seen": "2026-09-09T04:52:43.775+00:00",
    "last_seen":  "2026-09-11T08:14:02.110+00:00",
    "observations": 12,
    "buses": ["BUS-01", "BUS-02", "BUS-03", "BUS-07"],
    "bus_count": 4,
    "clean_passes": 0,
    "reopened_count": 1,
    "repair_observed": false,
    "department": "Roads/PWD",
    "priority_score": 78.4,
    "priority_band": "HIGH",
    "triggering_event_id": "9c218d70476a…"
  }
}
```

---

## Producing an event from a module

```python
from core.event import EventType, Severity, SourceModule, make_event

def process(self, ctx):
    return [
        make_event(
            EventType.ROAD_DEFECT,
            SourceModule.ROAD,
            class_name="pothole",
            confidence=0.87,
            severity=Severity.HIGH,
            bounding_box=[x1, y1, x2, y2],
            metadata={"raw_class": "Pothole", "area_px": 18420},
        )
    ]
```

`bus_id`, `camera_id`, `gps`, `frame_number`, `video_time_s` and
`road_segment` are stamped by the orchestrator — do not set them unless you
mean to override.

---

## Storage

`Event.to_dict()` is written to `outputs/runs/events.jsonl` (one JSON object
per line, append-only) and to the SQLite `events` table, whose columns mirror
the schema one-for-one with `gps` flattened to `latitude` / `longitude` and
`metadata` stored as a JSON blob. Consequence: `sqlite3` and `jq` both work
on the output with no extra tooling.
