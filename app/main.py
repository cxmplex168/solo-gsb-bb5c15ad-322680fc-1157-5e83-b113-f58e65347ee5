"""FastAPI application: three-way merge API with SQLite persistence.

Endpoints
----------
POST   /merges                      Create a merge order (idempotent via
                                   the ``Idempotency-Key`` header)
GET    /merges/{merge_id}           Query merge status / working document
POST   /merges/{merge_id}/resolve   Submit a batch of conflict resolutions
GET    /merges/{merge_id}/result    Fetch the immutable final document
GET    /health                      Liveness probe
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Path
from fastapi.responses import JSONResponse

from .merge import (
    ResolutionError,
    apply_resolutions,
    no_markers,
    three_way_merge,
)
from .schemas import MergeCreateRequest, MergeResponse, ResolutionRequest

DB_PATH = os.environ.get(
    "MERGE_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "merges.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS merges (
    id               TEXT PRIMARY KEY,
    revision         INTEGER NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
    base_doc         TEXT NOT NULL,
    merged_doc       TEXT NOT NULL,
    conflicts        TEXT NOT NULL,
    result_doc       TEXT,
    idempotency_key  TEXT UNIQUE,
    request_hash     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merge_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    merge_id    TEXT NOT NULL REFERENCES merges(id),
    revision    INTEGER NOT NULL,
    event_type  TEXT NOT NULL,
    detail      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_merge_events_merge_id
    ON merge_events(merge_id);
"""


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def get_db():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    directory = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(directory, exist_ok=True)
    with get_db() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_hash(payload: Any) -> str:
    """Stable hash of the submitted base/left/right triple."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def conflict_error(
    code: str,
    message: str,
    *,
    merge_id: str | None = None,
    current_revision: int | None = None,
) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    if merge_id is not None:
        detail["merge_id"] = merge_id
    if current_revision is not None:
        detail["current_revision"] = current_revision
    return HTTPException(status_code=409, detail=detail)


def row_to_response(row: sqlite3.Row) -> dict:
    return {
        "merge_id": row["id"],
        "revision": row["revision"],
        "status": row["status"],
        "conflicts": json.loads(row["conflicts"]),
        "merged": json.loads(row["merged_doc"]),
        "result": json.loads(row["result_doc"]) if row["result_doc"] is not None else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def add_event(
    conn: sqlite3.Connection,
    merge_id: str,
    revision: int,
    event_type: str,
    detail: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO merge_events (merge_id, revision, event_type, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (merge_id, revision, event_type, json.dumps(detail, ensure_ascii=False), utc_now()),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Tables and dependencies are ready as soon as the service starts.
    init_db()
    yield


app = FastAPI(
    title="Three-Way Configuration Merge API",
    version="1.0.0",
    description=(
        "Submit base/left/right JSON documents and resolve conflicts by "
        "JSON Pointer with revision-based optimistic concurrency."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Create merge order
# ---------------------------------------------------------------------------

@app.post("/merges", response_model=MergeResponse, status_code=201)
def create_merge(
    payload: MergeCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=422,
            detail={
                "code": "missing_idempotency_key",
                "message": "Header 'Idempotency-Key' is required for POST /merges",
            },
        )
    idempotency_key = idempotency_key.strip()
    request_hash = canonical_hash(
        {"base": payload.base, "left": payload.left, "right": payload.right}
    )

    merged, conflicts = three_way_merge(payload.base, payload.left, payload.right)
    now = utc_now()
    merge_id = str(uuid.uuid4())
    status = "open" if conflicts else "resolved"

    with get_db() as conn:
        # Replay check first: the same key must never create a second order.
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM merges WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "idempotency_key_reuse",
                        "message": (
                            "Idempotency-Key was already used with a different "
                            "base/left/right payload"
                        ),
                        "merge_id": existing["id"],
                    },
                )
            conn.commit()
            # Same request retried: return the original order, no new row.
            return JSONResponse(status_code=200, content=row_to_response(existing))

        result_doc = None
        if status == "resolved":
            if not no_markers(merged):
                raise HTTPException(
                    status_code=500,
                    detail={"code": "merge_invariant", "message": "unexpected conflict marker"},
                )
            result_doc = merged

        try:
            conn.execute(
                "INSERT INTO merges (id, revision, status, base_doc, merged_doc, "
                "conflicts, result_doc, idempotency_key, request_hash, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    merge_id,
                    1,
                    status,
                    json.dumps(payload.base, ensure_ascii=False),
                    json.dumps(merged, ensure_ascii=False),
                    json.dumps(conflicts, ensure_ascii=False),
                    json.dumps(result_doc, ensure_ascii=False) if result_doc is not None else None,
                    idempotency_key,
                    request_hash,
                    now,
                    now,
                ),
            )
            add_event(
                conn,
                merge_id,
                1,
                "created",
                {
                    "status": status,
                    "conflict_count": len(conflicts),
                    "auto_resolved": status == "resolved",
                },
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Lost a race with a concurrent request carrying the same key.
            conn.rollback()
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM merges WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is None:
                raise HTTPException(status_code=500, detail="idempotency race")
            if existing["request_hash"] != request_hash:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "idempotency_key_reuse",
                        "message": "Idempotency-Key was already used with a different payload",
                        "merge_id": existing["id"],
                    },
                )
            conn.commit()
            return JSONResponse(status_code=200, content=row_to_response(existing))

        row = conn.execute("SELECT * FROM merges WHERE id = ?", (merge_id,)).fetchone()
        return row_to_response(row)


# ---------------------------------------------------------------------------
# Status query
# ---------------------------------------------------------------------------

@app.get("/merges/{merge_id}", response_model=MergeResponse)
def get_merge(merge_id: str = Path(..., min_length=1)):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM merges WHERE id = ?", (merge_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "merge not found"})
    return row_to_response(row)


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------

@app.post("/merges/{merge_id}/resolve", response_model=MergeResponse)
def resolve_merge(merge_id: str, payload: ResolutionRequest):
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM merges WHERE id = ?", (merge_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "merge not found"})

        if row["status"] == "resolved":
            raise conflict_error(
                "already_resolved",
                "This merge is already fully resolved; its result is immutable",
                merge_id=merge_id,
                current_revision=row["revision"],
            )

        # Optimistic concurrency: a stale client revision is rejected.
        if payload.revision != row["revision"]:
            raise conflict_error(
                "revision_mismatch",
                f"Expected revision {row['revision']} but client submitted {payload.revision}",
                merge_id=merge_id,
                current_revision=row["revision"],
            )

        merged = json.loads(row["merged_doc"])
        conflicts = json.loads(row["conflicts"])

        try:
            new_merged, remaining = apply_resolutions(
                merged, conflicts, payload.resolutions
            )
        except ResolutionError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_resolution", "message": str(exc)},
            )
        except ValueError as exc:
            # Malformed JSON Pointer.
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_pointer", "message": str(exc)},
            )

        new_revision = row["revision"] + 1
        now = utc_now()

        if remaining:
            new_status = "open"
            result_json = None
        else:
            new_status = "resolved"
            if not no_markers(new_merged):
                raise HTTPException(
                    status_code=500,
                    detail={"code": "merge_invariant", "message": "unexpected conflict marker remains"},
                )
            result_json = json.dumps(new_merged, ensure_ascii=False)

        conn.execute(
            "UPDATE merges SET revision = ?, status = ?, merged_doc = ?, "
            "conflicts = ?, result_doc = ?, updated_at = ? WHERE id = ?",
            (
                new_revision,
                new_status,
                json.dumps(new_merged, ensure_ascii=False),
                json.dumps(remaining, ensure_ascii=False),
                result_json,
                now,
                merge_id,
            ),
        )
        add_event(
            conn,
            merge_id,
            new_revision,
            "resolved_batch",
            {
                "resolved": list(payload.resolutions.keys()),
                "remaining": len(remaining),
                "final": new_status == "resolved",
            },
        )
        conn.commit()

        row = conn.execute("SELECT * FROM merges WHERE id = ?", (merge_id,)).fetchone()
        return row_to_response(row)


# ---------------------------------------------------------------------------
# Immutable result
# ---------------------------------------------------------------------------

@app.get("/merges/{merge_id}/result")
def get_result(merge_id: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT revision, status, result_doc, updated_at FROM merges WHERE id = ?",
            (merge_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "merge not found"})
    if row["status"] != "resolved":
        raise conflict_error(
            "not_resolved",
            f"Merge still has open conflicts (revision {row['revision']})",
            merge_id=merge_id,
            current_revision=row["revision"],
        )
    return {
        "merge_id": merge_id,
        "revision": row["revision"],
        "result": json.loads(row["result_doc"]),
        "resolved_at": row["updated_at"],
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {
        "service": "three-way-merge-api",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "endpoints": [
            "POST /merges",
            "GET /merges/{merge_id}",
            "POST /merges/{merge_id}/resolve",
            "GET /merges/{merge_id}/result",
            "GET /health",
        ],
    }
