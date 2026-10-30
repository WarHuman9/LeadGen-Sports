"""
pipeline_session.py -- crash-resilient session persistence and resumable
Apify call tracking for the sports-academy lead-generation pipeline
(implementation_plan_4.md, Part A).

=============================================================================
A NOTE ON SCOPE: THIS FILE USED TO ALSO OWN HEALTH MONITORING
=============================================================================
Session-resume (this file) and pipeline health / circuit-breaking
(health_monitor.py) were originally drafted together in one file, then
split apart on request once that combined file grew large enough to be
genuinely hard to maintain and test as a single unit. The split also
tracks a real difference in how finished each half is: this file (schema,
reconciliation, resumable Apify calls) is mostly settled, with only minor
fixes left; health_monitor.py still has real design work ahead of it (B4
Phase 2's resource detectors, B6's other candidates). This file has ZERO
import of, or dependency on, health_monitor.py -- composition of the two
happens one level up, in apify_scrapper_v1.py, which constructs one
PipelineSession and one health_monitor.PipelineHealthMonitor independently
and calls into each on its own terms. See health_monitor.py's own
docstring for the full reasoning and the expected usage shape.

=============================================================================
WHY THIS FILE EXISTS (implementation_plan_4.md, Part A)
=============================================================================
Every prior plan (implementation_plan.md through implementation_plan_3.md)
assumed a run either succeeds or fails cleanly. In practice, on a solo local
machine (8GB RAM ceiling, Windows), a run can also just *stop* -- power
loss, a network drop mid-.call(), a Chromium process eating enough RAM that
Windows starts killing things. None of that is a bug in the pipeline's own
classification/tiering/Playwright-fallback logic (Items 1-19, already
implemented and tested elsewhere) -- it's an environment risk the pipeline
had zero defense against before this module. Right now, without this file,
a crash at record 280 of 319 means: the Maps data is gone (never persisted
anywhere but memory), the Instagram batch call -- if it already ran -- gets
silently re-paid for on the next attempt, and every already-processed
Playwright result is lost. This module's job is to make "the process died"
a recoverable event instead of a wasted run.

=============================================================================
WHAT THIS FILE OWNS (and what it deliberately does NOT own)
=============================================================================
Owns: the SQLite session schema, read/write mechanics for it, the
start()+persist+wait_for_finish() resumable-call wrapper (A5), config-drift
snapshot + hash comparison (A6), dataset-vs-DB reconciliation (A4b),
manual rescue-flag handling (A4c), execution-log capture (A7), and
archiving (A8).

Does NOT own, and never should: pipeline health / circuit-breaking (now
health_monitor.py, entirely -- see the note above), Playwright
orchestration, Instagram/Ads response *classification* (private/
restricted/not_found/NGO detection, follower-tier logic, ad-status-code
mapping -- Items 6/7/8/8b/9/11/19 all stay in apify_scrapper_v1.py), or CSV
writing/tiering (Item 12). The dividing line: this file manages *durable
session state and resumable-call survival*; apify_scrapper_v1.py still
owns every decision about what a result *means*.

The resource profiler that produced B4's real observational data
(apify_scrapper_v1_profiler.py) was explicitly a throwaway test harness for
that observation phase -- this module has no dependency on it and none of
its code was reused here.

=============================================================================
HOW apify_scrapper_v1.py IS EXPECTED TO CONSUME THIS FILE
=============================================================================
    import pipeline_session
    import health_monitor  # separate, independently-constructed object --
                            # see health_monitor.py's own docstring

    session = pipeline_session.PipelineSession.create_or_resume(
        resume_path=args.resume,
        rescue_map_dataset_id=args.MapDatasetID,
        rescue_ig_dataset_id=args.igDatasetID,
    )
    health = health_monitor.PipelineHealthMonitor()
    log_id = session.start_execution_log(mode="resume" if args.resume else "fresh",
                                          log_filename=log_filename)

    # Step 1
    places = session.resolve_maps_dataset(client, maps_run_input)

    # Step 2 -- only iterate what's actually pending; rehydrate the rest later
    for place_id in session.get_pending_place_ids(step=2):
        ...
        session.record_step2_result(place_id, result)
        reason = health.check_health()  # own object, own call -- not session's job
        if reason:
            ...

    # Step 3
    ig_results = session.resolve_instagram_batch(client, config.INSTAGRAM_RUN_INPUT)

    # Step 4 -- per record
    for place_id in session.get_pending_place_ids(step=4):
        ...
        ad_results = session.resolve_ads_call(client, place_id, fb_link, config.ADS_RUN_INPUT)
        # classify ad_results exactly as today, then:
        session.record_step4_result(place_id, result)

    # A4a -- mandatory, not optional. Reload EVERY completed record, not
    # just the ones this invocation itself processed, or a resumed run's
    # CSV silently contains only the delta.
    local_records = session.rehydrate_local_records()

    # ... write CSV as today ...
    session.finalize(output_csv_filename=csv_file)
    session.end_execution_log(log_id, reason="complete")

Every *_RUN_INPUT dict passed in should already be a caller-owned copy
(config.py's own docstring makes the same point) -- this module never
mutates a caller's dict in place, but it also doesn't re-copy defensively
beyond what it needs to merge in discovered data (directUrls / urls),
so passing in a shared mutable reference from a hot loop is still the
caller's responsibility to avoid.

=============================================================================
ONE FLAGGED, NOT-YET-MADE DEPENDENCY: config.py's additive A6 change
=============================================================================
A6 calls for an additive `TARGET_MULTIPOLYGON_LABELS` list in config.py
(human-readable notes per polygon, for the descriptive config_snapshot_json
-- never the actual drift gate, which is hash-based). config.py as it
currently stands does NOT have this yet. `_build_config_snapshot()` below
reads it via `getattr(config, "TARGET_MULTIPOLYGON_LABELS", None)` so this
module doesn't hard-fail against the current config.py -- it just records
`None` for that field until the config.py addition lands. This is the one
explicitly-flagged cross-file dependency called out in the plan itself,
not something slipped in silently.

=============================================================================
TWO DESIGN DECISIONS MADE WHILE DRAFTING THIS FILE, NOT EXPLICITLY SPELLED
OUT IN implementation_plan_4.md ITSELF -- flagged here rather than buried
=============================================================================
1. `places.ads_run_id` -- a nullable column ADDED beyond A3's literal SQL.
   A5 asks for the resumable start()+persist+wait_for_finish() pattern to
   apply to Step 4's per-record Ads call, but A3's schema as written has
   nowhere to persist that run's ID. Resolved (in prior discussion, not
   just assumed here): Ads is naturally 1:1 with `places` -- one business,
   at most one in-flight Ads call -- unlike Instagram's one-batch-covers-
   many-handles shape, which is why `instagram_batch` gets its own table
   but Ads just gets one more nullable column on the row that already
   exists.

2. Instagram's `--igDatasetID` rescue flag needed a real answer for "what
   counts as already-rescued," and the plan doesn't spell this out as
   explicitly as it does for Maps. Two genuinely different crash shapes
   can produce Instagram batch state: (a) a request row exists with
   `run_id` already persisted, but the wait/fetch never completed -- a
   normal in-flight resume, no flag needed, handled automatically; (b) a
   request row exists but `run_id` itself never got persisted -- the exact
   A5 crash window the flag exists for. This module treats ONLY shape (b)
   ("orphaned" rows, `run_id IS NULL AND raw_results_json IS NULL`) as
   `--igDatasetID`'s target. If no such row exists but the flag was given,
   or such a row exists but was already resolved, it's a hard error
   (`sys.exit`), same "narrow window only" philosophy as `--MapDatasetID`.
   If such a row exists and the flag is NOT given, this module does NOT
   error -- it logs a warning and lets the row sit unresolved; those
   handles simply get re-requested as part of the normal missing-handle
   reconciliation below, since we can't tell from local state alone
   whether Apify ever actually started that request. Worst case is one
   avoidable extra batch call, not silent data loss or a duplicate
   double-charge -- the safer default in the absence of the flag.

=============================================================================
WHAT'S STILL UNVERIFIED (flagged in-line where it matters, not hidden)
=============================================================================
- A5's .start() / .wait_for_finish() call shapes: apify_scrapper_v1.py's
  existing, tested code only ever calls the combined `.call()` -- it has
  never exercised `.start()` or a standalone `.wait_for_finish()` against
  real Apify traffic. `_extract_run_id()`/`_extract_dataset_id()` below
  mirror the existing proven attribute-style access
  (`getattr(obj, "default_dataset_id", None)`) with a dict-style fallback,
  rather than asserting a shape as fact. Worth a real-run confirmation
  before fully trusting this path -- same standard applied to health_monitor.py's
  NLM syntax (see that file's own docstring), which is unverified for the
  same reason (authored in a non-Windows environment) but is no longer
  this file's concern to track since the split.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import config
from playwright_engine import ig_profile_regex

logger = logging.getLogger(__name__)

# =====================================================================
# A2: session file location
# =====================================================================
SESSIONS_ROOT = Path("sessions")
SESSIONS_ACTIVE_DIR = SESSIONS_ROOT / "active"
SESSIONS_ARCHIVE_DIR = SESSIONS_ROOT / "archive"


def _now() -> str:
    """UTC ISO-8601 timestamp -- consistent with the rest of this project's
    timezone-aware datetime handling (see apify_scrapper_v1.py's own
    current_time = datetime.now(timezone.utc))."""
    return datetime.now(timezone.utc).isoformat()


# =====================================================================
# A6: the real config-drift gate. A raised, catchable exception -- NOT a
# sys.exit() -- deliberately distinct from A4c's rescue-flag misuse errors
# (which ARE sys.exit(), matching config.py's own convention for plain
# user/config mistakes). This one gets its own exception class because,
# unlike a CLI-flag misuse with no resume-loop context to catch it in, a
# config-drift failure is exactly the kind of thing a caller might
# reasonably want to catch and react to (e.g. a future wrapper script
# offering "start a fresh session instead?"), same testability-first
# reasoning config.py's own errors follow.
# =====================================================================
class SessionConfigMismatchError(Exception):
    """Raised when INSTAGRAM_RUN_INPUT or ADS_RUN_INPUT has changed since
    this session's corresponding work began, per A6. Maps config is
    deliberately NEVER a source of this error -- see
    PipelineSession._validate_config_drift()'s docstring for why."""


# =====================================================================
# A3: schema. Matches implementation_plan_4.md's SQL verbatim, with ONE
# addition flagged in this module's own docstring above: `ads_run_id` on
# `places`, needed for A5's resumable pattern applied to Step 4's
# per-record Ads call.
# =====================================================================
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    session_id              TEXT PRIMARY KEY,
    created_at               TEXT NOT NULL,
    config_snapshot_json      TEXT NOT NULL,
    ig_config_hash             TEXT NOT NULL,
    ads_config_hash             TEXT NOT NULL,
    health_config_hash          TEXT NOT NULL,
    maps_run_id                  TEXT,
    maps_dataset_id               TEXT,
    maps_places_fetched_at         TEXT,
    maps_data_origin                 TEXT,
    instagram_dataset_origin          TEXT,
    status                             TEXT NOT NULL DEFAULT 'in_progress',
    output_csv_filename                 TEXT,
    completed_at                          TEXT
);

CREATE TABLE IF NOT EXISTS places (
    place_id             TEXT PRIMARY KEY,
    raw_maps_json          TEXT NOT NULL,
    instagram_url            TEXT,
    instagram_handle           TEXT,
    facebook_url                 TEXT,
    playwright_status              TEXT,
    fallback_status                  TEXT,
    step2_completed_at                 TEXT,
    passed_filter_check                  TEXT,
    running_active_ads                     TEXT,
    instagram_followers                      INTEGER,
    last_post_date                             TEXT,
    ad_id                                        TEXT,
    ads_run_id                                     TEXT,
    step4_completed_at                               TEXT
);

CREATE TABLE IF NOT EXISTS instagram_batch (
    id                       INTEGER PRIMARY KEY,
    run_id                    TEXT,
    dataset_id                  TEXT,
    requested_handles_json        TEXT NOT NULL,
    raw_results_json                 TEXT,
    fetched_at                         TEXT,
    is_supplemental                      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS execution_log (
    id            INTEGER PRIMARY KEY,
    started_at     TEXT NOT NULL,
    ended_at         TEXT,
    log_filename       TEXT NOT NULL,
    mode                 TEXT NOT NULL,
    ended_reason           TEXT
);
"""


# =====================================================================
# SCHEMA VERSIONING -- closes a real gap: `CREATE TABLE IF NOT EXISTS`
# only ever helps a BRAND-NEW file. It does nothing to an EXISTING
# table's structure when a later code change adds/removes/renames a
# column -- exactly what already happened once with `ads_run_id` above
# (see this module's docstring), just before any real session file
# existed yet to expose the gap. Without this guard, resuming an
# out-of-date session file surfaces as a confusing IndexError (a
# sqlite3.Row missing a column some new method expects) or
# OperationalError ("no such column") buried deep inside whatever
# method first touches the missing column -- not a clear message at the
# top of resume, where it actually belongs.
#
# Uses SQLite's own `PRAGMA user_version` (an integer baked into the
# file's header) rather than a `meta` table column deliberately: it's
# readable and writable before we've committed to trusting `meta`'s own
# shape at all, which matters if a *future* breaking change ever touches
# `meta` itself rather than `places`/`instagram_batch`.
#
# SCHEMA_VERSION 1 IS today's SCHEMA_SQL above, verbatim -- including
# ads_run_id. Every real session file that exists right now was created
# by code that already matches this exact structure, so there is
# nothing to actually migrate for the 0 -> 1 step; a SQLite file that
# has never had `user_version` set at all reads back as 0 by default,
# which is why this bootstraps at 0 rather than starting the ladder at
# 1. The 0 -> 1 entry below is registered explicitly (not just skipped)
# so it's visible in the migration history, and so the ladder's own
# "no migration registered" error stays reserved for a genuine future
# gap rather than being silently bypassed for this one.
#
# Only ADDITIVE changes (a new nullable column) are expected to ever
# earn a real migration entry here, matching this project's own
# established convention (config.py, implementation_plan_4.md) of
# favoring additive change. A genuinely non-additive change (a renamed
# or dropped column, a restructured table) should still bump
# SCHEMA_VERSION, but deliberately NOT get an entry in _MIGRATIONS --
# see _migrate_schema()'s error message for that case; that kind of
# change isn't safely auto-migratable and shouldn't pretend to be.
SCHEMA_VERSION = 2


def _migration_0_to_1(conn: sqlite3.Connection) -> None:
    """Bootstrap entry only -- see the SCHEMA VERSIONING note above.
    Structural no-op: version 1 already matches every real session
    file's actual on-disk structure (ads_run_id included), so there is
    nothing to ALTER here, only a version stamp to make future
    migrations possible at all."""
    pass  # no-op -- see docstring


def _migration_1_to_2(conn: sqlite3.Connection) -> None:
    """Add the audit hash for local health-monitor settings.

    Existing sessions have no historical value for this new setting, so the
    additive column is nullable for migrated rows; fresh sessions store it.
    """
    conn.execute("ALTER TABLE meta ADD COLUMN health_config_hash TEXT")


# Next additive migration goes here, keyed by the version it migrates
# FROM, e.g.:
#   1: lambda conn: conn.execute(
#       "ALTER TABLE places ADD COLUMN google_maps_place_type TEXT"
#   ),
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    0: _migration_0_to_1,
    1: _migration_1_to_2,
}


def _extract_run_id(run_obj: Any) -> Optional[str]:
    """See this module's docstring, 'What's still unverified' -- mirrors
    apify_scrapper_v1.py's proven getattr-based access, with a dict
    fallback since .start()'s exact return shape hasn't been exercised
    against real Apify traffic yet."""
    run_id = getattr(run_obj, "id", None)
    if run_id:
        return run_id
    if isinstance(run_obj, dict):
        return run_obj.get("id")
    return None


def _extract_dataset_id(run_obj: Any) -> Optional[str]:
    """Same caveat as _extract_run_id() -- mirrors apify_scrapper_v1.py's
    existing, tested `getattr(maps_run, "default_dataset_id", None)`
    pattern exactly, so this module doesn't introduce a second, unverified
    access convention alongside the one that's already live and proven."""
    dataset_id = getattr(run_obj, "default_dataset_id", None)
    if dataset_id:
        return dataset_id
    if isinstance(run_obj, dict):
        return run_obj.get("defaultDatasetId") or run_obj.get("default_dataset_id")
    return None


def _start_and_track(
    client: Any,
    actor_id: str,
    run_input: Optional[dict[str, Any]],
    existing_run_id: Optional[str],
    persist_run_id: Callable[[str], None],
) -> Any:
    """A5: start() + persist + wait_for_finish(), resumable -- the one
    shared helper backing all three call sites (Maps Step 1, Instagram
    Step 3, Ads Step 4). If existing_run_id is given (a crash happened
    after Apify confirmed the run started but before the ID reached disk
    -- the exact narrow window this pattern exists to close), this skips
    straight to waiting on that run instead of starting a duplicate;
    run_input/actor_id become unused on that path but are still required
    parameters for a uniform call signature across all three sites.

    Deliberately NOT wrapped in this module's own try/except -- a genuine
    Apify-side failure here (auth, network, quota) should propagate to the
    caller as a real exception, same as apify_scrapper_v1.py's existing
    per-call try/except blocks already handle today. This helper only
    owns the resumable-persistence mechanics, not failure handling policy.
    """
    if existing_run_id:
        run_id = existing_run_id
        logger.info(
            f"[SESSION] Resuming in-flight {actor_id} run {run_id} instead of "
            f"starting a new one."
        )
    else:
        run = client.actor(actor_id).start(run_input=run_input)
        run_id = _extract_run_id(run)
        if not run_id:
            raise RuntimeError(
                f"[SESSION] Could not extract a run ID from {actor_id}.start()'s "
                f"return value: {run!r}"
            )
        persist_run_id(run_id)
        logger.info(f"[SESSION] Started {actor_id} run {run_id}, persisted before waiting.")

    return client.run(run_id).wait_for_finish()


def _stable_fallback_place_id(item: dict[str, Any]) -> str:
    """Used only when Maps' own `placeId` is absent -- rare, but
    apify_scrapper_v1.py already explicitly handles this case today (its
    own fallback: `f"no_id_{idx}_{name}"`). A purely positional or
    name-based fallback is NOT safe for THIS module's purposes
    specifically: _reconcile_places() can re-fetch the same dataset_id on
    a later, separate resume attempt, and Apify doesn't guarantee
    iterate_items() returns items in the same order every time for a
    dataset that may still be settling. A content hash is stable
    regardless of fetch order or position, since it's derived purely from
    the item's own data. This never surfaces in the CSV (place_id is an
    internal identity key only), so it doesn't need to match
    apify_scrapper_v1.py's own fallback format -- only to be stable across
    repeated fetches within this module."""
    digest = hashlib.sha256(
        json.dumps(item, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"no_id_{digest}"


def _extract_ig_handle(item: dict[str, Any]) -> Optional[str]:
    """Identity-key extraction ONLY -- mirrors apify_scrapper_v1.py's own
    Step 3 handle resolution (Items 6/7/8b) so the two files never
    disagree about what 'the same handle' means, but intentionally stops
    there: classification (private/restricted/not_found/NGO detection)
    stays entirely in apify_scrapper_v1.py, per this module's scope
    boundary. This function only needs to answer 'have we already seen
    this handle in a completed batch,' not what that handle's business
    classification should be."""
    url = item.get("inputUrl")
    if isinstance(url, str) and url:
        match = ig_profile_regex.search(url)
        if match:
            return match.group(1).split("?")[0].strip("/").lower()
    raw_username = item.get("username")
    if isinstance(raw_username, str) and raw_username:
        return raw_username.split("?")[0].strip("/").lower()
    return None


# =====================================================================
# PipelineSession -- the module's main object. Owns one sqlite3
# connection for the lifetime of a run.
# =====================================================================
class PipelineSession:
    def __init__(
        self,
        conn: sqlite3.Connection,
        db_path: Path,
        session_id: str,
        is_resume: bool,
        rescue_map_dataset_id: Optional[str] = None,
        rescue_ig_dataset_id: Optional[str] = None,
    ) -> None:
        self._conn = conn
        self.db_path = db_path
        self.session_id = session_id
        self._is_resume = is_resume
        self._rescue_map_dataset_id = rescue_map_dataset_id
        self._rescue_ig_dataset_id = rescue_ig_dataset_id

    # -----------------------------------------------------------------
    # Construction: fresh vs. resume (A2, A10)
    # -----------------------------------------------------------------
    @classmethod
    def create_or_resume(
        cls,
        resume_path: Optional[str] = None,
        rescue_map_dataset_id: Optional[str] = None,
        rescue_ig_dataset_id: Optional[str] = None,
    ) -> "PipelineSession":
        if (rescue_map_dataset_id or rescue_ig_dataset_id) and not resume_path:
            sys.exit(
                "❌ --MapDatasetID/--igDatasetID were given without --resume -- these "
                "rescue flags only make sense against an existing session file."
            )

        if resume_path:
            return cls._open_resume(Path(resume_path), rescue_map_dataset_id, rescue_ig_dataset_id)
        return cls._create_fresh()

    @classmethod
    def _create_fresh(cls) -> "PipelineSession":
        SESSIONS_ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
        SESSIONS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        db_path = SESSIONS_ACTIVE_DIR / f"session_{session_id}.db"

        conn = sqlite3.connect(str(db_path))
        cls._apply_pragmas(conn)
        cls._create_schema(conn)

        snapshot = cls._build_config_snapshot()
        ig_hash = cls._hash_run_input(config.INSTAGRAM_RUN_INPUT)
        ads_hash = cls._hash_run_input(config.ADS_RUN_INPUT)
        health_hash = cls._hash_run_input(config.HEALTH_MONITOR_KWARGS)
        conn.execute(
            "INSERT INTO meta (session_id, created_at, config_snapshot_json, "
            "ig_config_hash, ads_config_hash, health_config_hash, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'in_progress')",
            (session_id, _now(), json.dumps(snapshot), ig_hash, ads_hash, health_hash),
        )
        conn.commit()

        session = cls(conn, db_path, session_id, is_resume=False)
        print(f"🆕 New session created: {db_path}")
        print(f"   If this run is interrupted, resume it with: --resume {db_path}")
        return session

    @classmethod
    def _open_resume(
        cls,
        db_path: Path,
        rescue_map_dataset_id: Optional[str],
        rescue_ig_dataset_id: Optional[str],
    ) -> "PipelineSession":
        if not db_path.exists():
            sys.exit(f"❌ --resume path does not exist: {db_path}")

        conn = sqlite3.connect(str(db_path))
        cls._apply_pragmas(conn)
        # Schema-versioning guard -- runs before EVEN the config-drift
        # check below, let alone before `meta`/`places` are read. A
        # version mismatch needs to surface as one clear message right
        # here, not as a confusing IndexError/OperationalError deep
        # inside whatever method first touches a column that turns out
        # not to exist on disk (see the SCHEMA VERSIONING block above
        # SCHEMA_SQL for the full rationale).
        cls._migrate_schema(conn, db_path)
        session_id = db_path.stem.replace("session_", "", 1)

        session = cls(
            conn, db_path, session_id, is_resume=True,
            rescue_map_dataset_id=rescue_map_dataset_id,
            rescue_ig_dataset_id=rescue_ig_dataset_id,
        )
        # A6: the real gate. Runs FIRST, before any other resume logic --
        # before places are loaded, before Step 1/3/4 methods are ever
        # called -- so a config-drift problem halts the run before a
        # single Apify credit is spent this attempt.
        session._validate_config_drift()
        print(f"♻️  Resuming session: {db_path}")
        return session

    # -----------------------------------------------------------------
    # A9a: PRAGMAs -- once per connection, at construction, not per-query.
    # -----------------------------------------------------------------
    @staticmethod
    def _apply_pragmas(conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # synchronous=FULL, not NORMAL -- this project's own stated
        # motivation explicitly includes power loss, not just "the script
        # crashed"; NORMAL is only safe against the latter in WAL mode.
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA_SQL)
        # Stamp this brand-new file with the CURRENT schema version --
        # see the SCHEMA VERSIONING block above. A fresh file always
        # matches SCHEMA_SQL exactly, by construction, so there's never
        # a migration to run here; this stamp only exists so a LATER
        # resume of this same file can tell what it was created as.
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection, db_path: Path) -> None:
        """Schema-versioning guard -- checked immediately on every resume,
        before ANY other resume logic (including config-drift validation)
        touches a single table. See the SCHEMA VERSIONING block above
        SCHEMA_SQL for the full rationale; this is the enforcement side of
        it."""
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current == SCHEMA_VERSION:
            return

        if current > SCHEMA_VERSION:
            sys.exit(
                f"❌ {db_path} was created by a NEWER version of this "
                f"pipeline (schema version {current}) than the one "
                f"currently running (schema version {SCHEMA_VERSION}). "
                f"Refusing to resume an older codebase against a newer "
                f"session file -- update this code to match, or point "
                f"--resume at a session created by this exact version."
            )

        version = current
        while version < SCHEMA_VERSION:
            migration = _MIGRATIONS.get(version)
            if migration is None:
                sys.exit(
                    f"❌ {db_path} is schema version {version}, but this "
                    f"code is version {SCHEMA_VERSION} and has no "
                    f"registered migration from {version} -> {version + 1}. "
                    f"This session predates a non-additive schema change "
                    f"and can't be safely resumed automatically. Either "
                    f"revert to whichever code version created this "
                    f"session and let it finish there, or abandon this "
                    f"session and start a fresh one."
                )
            logger.info(
                f"[SESSION] Migrating {db_path} schema {version} -> {version + 1}..."
            )
            migration(conn)
            version += 1
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()

    # -----------------------------------------------------------------
    # A6: config snapshot (descriptive) + hashing (the real gate)
    # -----------------------------------------------------------------
    @staticmethod
    def _hash_run_input(run_input: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(run_input, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _build_config_snapshot() -> dict[str, Any]:
        """Human-readable record of what this run was configured with --
        NEVER the actual drift gate (ig_config_hash/ads_config_hash are).
        Maps fields are historical record only: Step 1 runs at most once
        per session, and a resumed session never re-reads Maps config
        regardless of what config.py says by the time it resumes, so
        there's nothing to gate there -- see _validate_config_drift().

        polygon_hash/polygon_labels describe whichever polygon is
        ACTUALLY active (config.CUSTOM_GEOLOCATION), not unconditionally
        config.TARGET_MULTIPOLYGON. Nothing stops CUSTOM_GEOLOCATION from
        being set to a different, independently-valid polygon instead of
        TARGET_MULTIPOLYGON -- config.py's own _validate_custom_geolocation
        checks it structurally on its own, with no requirement that it
        match TARGET_MULTIPOLYGON. If that ever happens, hashing
        TARGET_MULTIPOLYGON unconditionally (the original approach) would
        silently record the wrong, unused polygon's shape as if it were
        the one that actually drove this run -- exactly the kind of
        "the audit record can lie" gap this snapshot exists to avoid.
        TARGET_MULTIPOLYGON_LABELS has the same problem one level up: it's
        only ever written to describe TARGET_MULTIPOLYGON's own
        coordinates (index-matched to it by convention), so it's only
        attached when the active polygon actually IS that object -- same
        `is` identity check config.py's own __main__ block already uses
        for the same reason. In query mode, or when CUSTOM_GEOLOCATION is
        a different polygon than TARGET_MULTIPOLYGON, both fields fall
        back to None rather than silently describing something inert."""
        active_polygon: Optional[dict[str, Any]] = config.CUSTOM_GEOLOCATION
        location_mode = "polygon" if active_polygon is not None else "query"

        if active_polygon is None:
            polygon_labels = None
            polygon_hash = None
        else:
            polygon_hash = "sha256:" + hashlib.sha256(
                json.dumps(active_polygon, sort_keys=True).encode("utf-8")
            ).hexdigest()
            polygon_labels = (
                getattr(config, "TARGET_MULTIPOLYGON_LABELS", None)
                if active_polygon is config.TARGET_MULTIPOLYGON
                else None
            )

        return {
            "search_keywords": config.SEARCH_KEYWORDS,
            "category_filter_words": config.CATEGORY_FILTER_WORDS,
            "location_mode": location_mode,
            "location_query": config.LOCATION_QUERY,
            "polygon_labels": polygon_labels,
            "polygon_hash": polygon_hash,
            "session_batch_limit": config.SESSION_BATCH_LIMIT,
            "health_monitor": dict(config.HEALTH_MONITOR_KWARGS),
            "output_base_name": config.OUTPUT_BASE_NAME,
        }

    def _validate_config_drift(self) -> None:
        """A6: Maps fields are snapshotted but NEVER gated here -- Step 1
        is consumed at most once per session (either it already ran, in
        which case config.py's current Maps values are irrelevant to this
        resume at all, or it didn't, in which case whatever config.py says
        *right now* simply becomes what this attempt uses, same as any
        fresh run). IG/Ads fields get independent hash columns and are
        gated ONLY once committed progress already exists for that step --
        an empty instagram_batch table, or zero step4_completed_at rows,
        means there's nothing yet that could have been computed under a
        different config, so nothing to protect. No --force override, by
        design -- see implementation_plan_4.md's A6 for the reasoning."""
        row = self._get_meta_row()

        has_ig_progress = self._instagram_batch_row_count() > 0
        has_ads_progress = self._any_step4_completed()

        # Health settings can change when it is safe to continue on this
        # machine; they do not change actor output or lead classification.
        # Keep their hash for auditability, but do not block a valid resume
        # merely because an operator tuned a local threshold.
        stored_health_hash = row.get("health_config_hash")
        current_health_hash = self._hash_run_input(config.HEALTH_MONITOR_KWARGS)
        if stored_health_hash and current_health_hash != stored_health_hash:
            logger.info(
                "[SESSION] Health-monitor configuration changed since session creation; "
                "continuing because it does not affect lead-result semantics."
            )

        if has_ig_progress:
            current_ig_hash = self._hash_run_input(config.INSTAGRAM_RUN_INPUT)
            if current_ig_hash != row["ig_config_hash"]:
                raise SessionConfigMismatchError(
                    "config.INSTAGRAM_RUN_INPUT has changed since this session's "
                    "Instagram work began (ig_config_hash mismatch). Revert config.py "
                    "to match this session, or start a fresh session instead of "
                    "resuming this one."
                )

        if has_ads_progress:
            current_ads_hash = self._hash_run_input(config.ADS_RUN_INPUT)
            if current_ads_hash != row["ads_config_hash"]:
                raise SessionConfigMismatchError(
                    "config.ADS_RUN_INPUT has changed since this session's Ads work "
                    "began (ads_config_hash mismatch). Revert config.py to match this "
                    "session, or start a fresh session instead of resuming this one."
                )

    # -----------------------------------------------------------------
    # meta table helpers
    # -----------------------------------------------------------------
    # maps_run_id/maps_dataset_id were previously protected only by ad-hoc
    # `if not row[...]:` guards inside resolve_maps_dataset() -- correct in
    # the current control flow, but with no backstop at the storage layer
    # itself if a future edit ever removed one of those guards by mistake.
    # Adding them here is purely additive: in every existing code path,
    # _set_meta() is already never called for these fields unless they're
    # still None, so this doesn't change any current behavior -- it just
    # makes that invariant enforced centrally instead of scattered across
    # call sites, matching how maps_data_origin/instagram_dataset_origin
    # already work.
    _META_WRITE_ONCE_FIELDS = frozenset({
        "maps_data_origin", "instagram_dataset_origin",
        "maps_run_id", "maps_dataset_id",
    })

    def _get_meta_row(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM meta WHERE session_id=?", (self.session_id,)
        ).fetchone()
        return dict(row)

    def _set_meta(self, **kwargs: Any) -> None:
        if not kwargs:
            return
        row = self._get_meta_row()
        for key in kwargs:
            if key in self._META_WRITE_ONCE_FIELDS and row.get(key) is not None:
                raise RuntimeError(
                    f"[SESSION] Refusing to overwrite write-once meta field "
                    f"'{key}' (already '{row[key]}')."
                )
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        self._conn.execute(
            f"UPDATE meta SET {set_clause} WHERE session_id = ?",
            (*kwargs.values(), self.session_id),
        )
        self._conn.commit()

    # -----------------------------------------------------------------
    # A7: execution log
    # -----------------------------------------------------------------
    def start_execution_log(self, mode: str, log_filename: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO execution_log (started_at, log_filename, mode) VALUES (?, ?, ?)",
            (_now(), log_filename, mode),
        )
        self._conn.commit()
        lastrowid = cur.lastrowid
        if lastrowid is None:
            raise RuntimeError("[SESSION] INSERT into execution_log did not return a row id.")
        return lastrowid

    def end_execution_log(self, log_id: int, reason: str) -> None:
        self._conn.execute(
            "UPDATE execution_log SET ended_at=?, ended_reason=? WHERE id=?",
            (_now(), reason, log_id),
        )
        self._conn.commit()

    # -----------------------------------------------------------------
    # places: low-level helpers
    # -----------------------------------------------------------------
    def _places_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM places").fetchone()
        return row["c"]

    def _existing_place_ids(self) -> set[str]:
        rows = self._conn.execute("SELECT place_id FROM places").fetchall()
        return {r["place_id"] for r in rows}

    def _insert_place_raw(self, place_id: str, raw_dict: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO places (place_id, raw_maps_json) VALUES (?, ?)",
            (place_id, json.dumps(raw_dict)),
        )

    def _load_places_raw(self) -> list[dict[str, Any]]:
        """Returns the same shape apify_scrapper_v1.py's existing
        _get_dataset_items() already returns -- a plain list of Maps
        place_data dicts -- so Step 2's existing loop body over `places`
        needs zero changes to consume either source interchangeably."""
        rows = self._conn.execute(
            "SELECT raw_maps_json FROM places ORDER BY rowid"
        ).fetchall()
        return [json.loads(r["raw_maps_json"]) for r in rows]

    # -----------------------------------------------------------------
    # A4b: identity-based, symmetric, union-only reconciliation --
    # dataset has place_ids the DB doesn't -> append; DB has place_ids the
    # dataset doesn't -> keep, never discarded on the dataset's say-so.
    # Runs unconditionally any time a Maps dataset is fetched, not just in
    # the A4c rescue-flag path -- drift (Apify retention, a crash mid-
    # fetch on a prior attempt) can happen without any rescue flag at all.
    # -----------------------------------------------------------------
    def _reconcile_places(self, client: Any, dataset_id: str) -> list[dict[str, Any]]:
        dataset_items = list(client.dataset(dataset_id).iterate_items())
        dataset_by_id: dict[str, dict[str, Any]] = {}
        for item in dataset_items:
            # A positional fallback (no_id_{index}) would NOT be safe here
            # specifically: reconciliation can re-fetch this same
            # dataset_id on a later, separate resume attempt, and Apify
            # doesn't guarantee iterate_items() returns identical order
            # across separate fetches. A placeId-less record could
            # silently look like a brand-new entry (duplicate row) if its
            # position shifts between fetches. A content hash is stable
            # regardless of fetch order -- caught during self-review, not
            # part of the original design.
            pid = str(item.get("placeId") or _stable_fallback_place_id(item))
            dataset_by_id[pid] = item

        existing_ids = self._existing_place_ids()
        new_ids = set(dataset_by_id) - existing_ids
        if new_ids:
            logger.info(
                f"[SESSION] Maps reconciliation: dataset has {len(new_ids)} place(s) "
                f"not yet in `places` -- appending."
            )
            for pid in new_ids:
                self._insert_place_raw(pid, dataset_by_id[pid])
            self._conn.commit()

        missing_from_dataset = existing_ids - set(dataset_by_id)
        if missing_from_dataset:
            logger.warning(
                f"[SESSION] Maps reconciliation: {len(missing_from_dataset)} place(s) "
                f"already committed locally no longer appear in the dataset (Apify "
                f"retention drift?) -- keeping local rows as-is, neither side is "
                f"unconditionally the truth (A4b)."
            )

        return self._load_places_raw()

    # -----------------------------------------------------------------
    # Step 1 / Maps -- A4, A4c, A5 all converge here.
    # -----------------------------------------------------------------
    def resolve_maps_dataset(self, client: Any, run_input: dict[str, Any]) -> list[dict[str, Any]]:
        row = self._get_meta_row()

        if row["maps_places_fetched_at"]:
            if self._rescue_map_dataset_id:
                sys.exit(
                    "❌ --MapDatasetID was given, but Step 1 already completed for "
                    "this session (meta.maps_places_fetched_at is set) -- there's "
                    "nothing left for this flag to rescue. Remove --MapDatasetID and "
                    "resume normally."
                )
            logger.info(
                "[SESSION] Step 1 already complete for this session -- loading from "
                "`places`, no Apify call."
            )
            return self._load_places_raw()

        if self._rescue_map_dataset_id:
            if row["maps_dataset_id"]:
                sys.exit(
                    "❌ --MapDatasetID was given, but this session's "
                    "meta.maps_dataset_id is already set -- the crash window this "
                    "flag exists for has already passed for this session. Remove "
                    "--MapDatasetID and resume normally."
                )
            dataset_id = self._rescue_map_dataset_id
            client.dataset(dataset_id).get()  # existence check -- fails loudly if bad
            self._set_meta(maps_dataset_id=dataset_id, maps_data_origin="rescued")
            places = self._reconcile_places(client, dataset_id)
            self._set_meta(maps_places_fetched_at=_now())
            return places

        # Case 4: this is a resume, no run_id was ever recorded, and
        # `places` is empty -- the session died before Python had even
        # called the Maps API. Reuse this file, run Step 1 fresh inside
        # it, tag the origin so it's auditable later (A4, "Case 4").
        is_empty_resume = self._is_resume and not row["maps_run_id"] and self._places_count() == 0

        # A prior "initial" label collapsed two different histories: a
        # genuinely fresh Step 1 call, vs. a resume that's reattaching to
        # a Maps run still in flight on Apify's side from a previous
        # attempt (the process died while waiting on wait_for_finish(),
        # per A5). The trigger is simply whether maps_run_id was already
        # set on entry into THIS call -- the exact same condition
        # _start_and_track() below uses (via existing_run_id) to decide
        # whether to skip .start() and reattach instead of starting a new
        # run. Mutually exclusive with is_empty_resume by construction:
        # is_empty_resume requires `not row["maps_run_id"]`, so a truthy
        # maps_run_id here always means is_empty_resume is False.
        is_resumed_in_flight = bool(row["maps_run_id"])

        finished = _start_and_track(
            client, "compass/crawler-google-places", run_input,
            existing_run_id=row["maps_run_id"],
            persist_run_id=lambda rid: self._set_meta(maps_run_id=rid),
        )
        dataset_id = _extract_dataset_id(finished)
        if not dataset_id:
            raise RuntimeError(
                f"[SESSION] Maps run finished but no dataset ID could be extracted: {finished!r}"
            )
        if not row["maps_dataset_id"]:
            self._set_meta(maps_dataset_id=dataset_id)

        places = self._reconcile_places(client, dataset_id)

        if is_empty_resume:
            origin = "resumed_empty"
        elif is_resumed_in_flight:
            origin = "resumed_in_flight"
        else:
            origin = "initial"

        # Guard against re-writing an already-set, write-once
        # maps_data_origin: if a PRIOR attempt crashed mid-rescue (after
        # _reconcile_places above had already run for that attempt, but
        # before that attempt reached its own maps_places_fetched_at
        # write), `row["maps_data_origin"]` would already be "rescued"
        # here. Without this guard, the _set_meta() call below would hit
        # _META_WRITE_ONCE_FIELDS and raise, turning an already-recoverable
        # situation into a crash. Found while implementing this fix, not
        # one of the four originally flagged items -- kept in since it's
        # directly adjacent to code already being touched here.
        if row["maps_data_origin"] is None:
            self._set_meta(maps_places_fetched_at=_now(), maps_data_origin=origin)
        else:
            self._set_meta(maps_places_fetched_at=_now())
        return places

    # -----------------------------------------------------------------
    # Step 2 / Step 4 -- pending-record queries + checkpoint writers
    # -----------------------------------------------------------------
    def get_pending_place_ids(self, step: int) -> list[str]:
        if step == 2:
            col = "step2_completed_at"
        elif step == 4:
            col = "step4_completed_at"
        else:
            raise ValueError(f"get_pending_place_ids: step must be 2 or 4, got {step!r}")
        rows = self._conn.execute(f"SELECT place_id FROM places WHERE {col} IS NULL").fetchall()
        return [r["place_id"] for r in rows]

    def record_step2_result(self, place_id: str, result: dict[str, Any]) -> None:
        """A1: one atomic checkpoint write, in full, exactly once per
        place -- a site-level failure (HTTP 403, DNS error, whatever) is
        still `step2_completed_at`-set, same as today; nothing here
        retries a legitimately-failed site automatically."""
        self._conn.execute(
            "UPDATE places SET instagram_url=?, instagram_handle=?, facebook_url=?, "
            "playwright_status=?, fallback_status=?, step2_completed_at=? WHERE place_id=?",
            (
                result.get("instagram_url"), result.get("instagram_handle"),
                result.get("facebook_url"), result.get("playwright_status"),
                result.get("fallback_status"), _now(), place_id,
            ),
        )
        self._conn.commit()

    def record_step4_result(self, place_id: str, result: dict[str, Any]) -> None:
        self._conn.execute(
            "UPDATE places SET passed_filter_check=?, running_active_ads=?, "
            "instagram_followers=?, last_post_date=?, ad_id=?, step4_completed_at=? "
            "WHERE place_id=?",
            (
                result.get("passed_filter_check"), result.get("running_active_ads"),
                result.get("instagram_followers"), result.get("last_post_date"),
                result.get("ad_id"), _now(), place_id,
            ),
        )
        self._conn.commit()

    # -----------------------------------------------------------------
    # Step 3 / Instagram batch -- A4 (identity-based reconciliation,
    # replacing the earlier, now-abandoned 80/20 threshold idea entirely),
    # A4c (--igDatasetID, narrow "orphaned row" window only), A5.
    # -----------------------------------------------------------------
    def _expected_instagram_handles(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT instagram_handle FROM places "
            "WHERE step2_completed_at IS NOT NULL AND instagram_handle IS NOT NULL"
        ).fetchall()
        return {r["instagram_handle"] for r in rows if r["instagram_handle"]}

    def _processed_instagram_handles(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT raw_results_json FROM instagram_batch WHERE raw_results_json IS NOT NULL"
        ).fetchall()
        processed: set[str] = set()
        for r in rows:
            for item in json.loads(r["raw_results_json"]):
                handle = _extract_ig_handle(item)
                if handle:
                    processed.add(handle)
        return processed

    def _instagram_batch_row_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM instagram_batch").fetchone()
        return row["c"]

    def _any_step4_completed(self) -> bool:
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM places WHERE step4_completed_at IS NOT NULL"
        ).fetchone()
        return row["c"] > 0

    def _get_in_flight_instagram_batch_row(self) -> Optional[dict[str, Any]]:
        """A run_id was persisted, but the wait/fetch never completed --
        a normal, unambiguous A5 resume, no rescue flag needed. Ordered
        oldest-first (ASC) for consistency with
        _get_all_orphaned_instagram_batch_rows() below, though in practice
        this should never have more than one match at a time: the flow in
        resolve_instagram_batch() always resolves an existing in-flight
        row before ever creating a new request row, so a second one
        appearing here would itself indicate a bug elsewhere -- ASC is
        just the safer default if that invariant is ever violated."""
        row = self._conn.execute(
            "SELECT * FROM instagram_batch WHERE run_id IS NOT NULL "
            "AND raw_results_json IS NULL ORDER BY id ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def _get_all_orphaned_instagram_batch_rows(self) -> list[dict[str, Any]]:
        """Every request row where we'd decided which handles to send but
        run_id itself was never persisted -- oldest first. Caught during
        self-review: unlike the in-flight case above, orphaned rows are
        NOT guaranteed to number at most one -- if --igDatasetID isn't
        given on a resume, resolve_instagram_batch() deliberately leaves
        an orphaned row unresolved (see this module's docstring, decision
        2) and moves on, so repeated crashes-before-persist across
        separate resume attempts can leave more than one behind over time.
        --igDatasetID targets a single dataset ID, so it can only ever
        rescue ONE row per invocation -- resolving several requires one
        --igDatasetID resume per row, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM instagram_batch WHERE run_id IS NULL "
            "AND raw_results_json IS NULL ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def _insert_instagram_batch_request(self, handles: list[str], is_supplemental: int) -> int:
        cur = self._conn.execute(
            "INSERT INTO instagram_batch (requested_handles_json, is_supplemental) "
            "VALUES (?, ?)",
            (json.dumps(handles), is_supplemental),
        )
        self._conn.commit()
        lastrowid = cur.lastrowid
        if lastrowid is None:
            raise RuntimeError("[SESSION] INSERT into instagram_batch did not return a row id.")
        return lastrowid

    def _set_instagram_batch_run_id(self, row_id: int, run_id: str) -> None:
        """Same write-once protection as meta's _META_WRITE_ONCE_FIELDS,
        needed as its own guard here since instagram_batch isn't part of
        the meta table -- currently only ever called via _start_and_track's
        persist_run_id callback, which only fires when existing_run_id was
        None going in, so this is additive safety, not a behavior change."""
        existing = self._conn.execute(
            "SELECT run_id FROM instagram_batch WHERE id=?", (row_id,)
        ).fetchone()
        if existing and existing["run_id"] is not None:
            raise RuntimeError(
                f"[SESSION] Refusing to overwrite already-set run_id for "
                f"instagram_batch row {row_id} (already '{existing['run_id']}')."
            )
        self._conn.execute(
            "UPDATE instagram_batch SET run_id=? WHERE id=?", (run_id, row_id)
        )
        self._conn.commit()

    def _complete_instagram_batch_row(self, row_id: int, dataset_id: str, client: Any) -> None:
        items = list(client.dataset(dataset_id).iterate_items())
        self._conn.execute(
            "UPDATE instagram_batch SET dataset_id=?, raw_results_json=?, fetched_at=? "
            "WHERE id=?",
            (dataset_id, json.dumps(items), _now(), row_id),
        )
        self._conn.commit()

    def _load_all_instagram_results(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT raw_results_json FROM instagram_batch WHERE raw_results_json IS NOT NULL"
        ).fetchall()
        all_items: list[dict[str, Any]] = []
        for r in rows:
            all_items.extend(json.loads(r["raw_results_json"]))
        return all_items

    def resolve_instagram_batch(
        self, client: Any, run_input_base: dict[str, Any]
    ) -> list[dict[str, Any]]:
        expected = self._expected_instagram_handles()
        if not expected:
            return []

        # Normal in-flight resume -- unambiguous, no flag needed.
        in_flight = self._get_in_flight_instagram_batch_row()
        if in_flight:
            logger.info(
                f"[SESSION] Resuming in-flight Instagram batch (row {in_flight['id']}, "
                f"run {in_flight['run_id']})."
            )
            finished = _start_and_track(
                client, "apify/instagram-scraper", run_input=None,
                existing_run_id=in_flight["run_id"], persist_run_id=lambda rid: None,
            )
            dataset_id = _extract_dataset_id(finished)
            if dataset_id:
                self._complete_instagram_batch_row(in_flight["id"], dataset_id, client)

        # A4c's narrower window -- see this module's docstring, decision 2.
        # Caught during self-review: multiple orphaned rows can in
        # principle exist (repeated crashes-before-persist across separate
        # resume attempts) -- --igDatasetID can only ever target one
        # dataset ID, so it rescues the OLDEST orphaned row per invocation;
        # any remaining ones are reported honestly rather than silently
        # dropped.
        orphaned_rows = self._get_all_orphaned_instagram_batch_rows()
        if orphaned_rows and self._rescue_ig_dataset_id:
            oldest = orphaned_rows[0]
            dataset_id = self._rescue_ig_dataset_id
            client.dataset(dataset_id).get()
            self._complete_instagram_batch_row(oldest["id"], dataset_id, client)
            row = self._get_meta_row()
            if not row["instagram_dataset_origin"]:
                self._set_meta(instagram_dataset_origin="rescued")
            if len(orphaned_rows) > 1:
                logger.warning(
                    f"[SESSION] Rescued the oldest orphaned Instagram batch row "
                    f"(id {oldest['id']}) via --igDatasetID, but "
                    f"{len(orphaned_rows) - 1} more orphaned row(s) still remain -- "
                    f"resolve them one at a time with another --igDatasetID resume "
                    f"each, or let them fall through to normal (re-paid) "
                    f"reconciliation below."
                )
        elif orphaned_rows and not self._rescue_ig_dataset_id:
            total_handles = sum(
                len(json.loads(r["requested_handles_json"])) for r in orphaned_rows
            )
            logger.warning(
                f"[SESSION] {len(orphaned_rows)} Instagram batch request row(s) have "
                f"no recorded run_id ({total_handles} handle(s) total) -- treating as "
                f"never confirmed and re-requesting those handles as part of normal "
                f"reconciliation below. If you've confirmed a real run exists for the "
                f"oldest one via Apify's own console, re-run with --igDatasetID <id> "
                f"to rescue it (repeat for any additional ones) instead of paying "
                f"for it twice."
            )
        elif self._rescue_ig_dataset_id and not orphaned_rows:
            sys.exit(
                "❌ --igDatasetID was given, but no Instagram batch request row in "
                "this session is missing a run_id -- there's nothing for this flag "
                "to rescue. Remove --igDatasetID and resume normally."
            )

        # A4: identity-based reconciliation, unconditional -- replaces the
        # earlier, abandoned 80/20 percentage-threshold idea entirely.
        # Whatever's missing gets requested, whether that's a handful of
        # handles or the entire expected set.
        already_processed = self._processed_instagram_handles()
        missing = expected - already_processed
        if missing:
            is_supplemental = 1 if already_processed else 0
            sorted_missing = sorted(missing)
            row_id = self._insert_instagram_batch_request(sorted_missing, is_supplemental)

            run_input = dict(run_input_base)
            run_input["directUrls"] = [f"https://www.instagram.com/{h}/" for h in sorted_missing]
            # BUGFIX (mirrors apify_scrapper_v1.py's own 2026-07-22 fix --
            # carried over here so it isn't silently reintroduced).
            # config.INSTAGRAM_RUN_INPUT's "resultsLimit" is a documented
            # placeholder (see config.py's own comment on that field) --
            # for apify/instagram-scraper in "details" mode with a
            # multi-URL directUrls batch, resultsLimit caps the number of
            # PROFILES returned for the WHOLE dataset, not "posts per
            # profile". Left at run_input_base's placeholder value, only
            # 1 of however many handles are in sorted_missing would ever
            # come back -- silently reintroducing the exact bug
            # apify_scrapper_v1.py already fixed once, and reintroducing
            # it on EVERY future supplemental/resumed batch too, not just
            # the first call, since this one block is what both the
            # initial (is_supplemental=0) and supplemental
            # (is_supplemental=1) paths share. Must be recomputed HERE,
            # scaled to THIS call's own sorted_missing -- a value copied
            # from run_input_base or set once anywhere else can never be
            # correct across multiple calls of different sizes over a
            # session's lifetime.
            run_input["resultsLimit"] = len(sorted_missing)

            finished = _start_and_track(
                client, "apify/instagram-scraper", run_input,
                existing_run_id=None,
                persist_run_id=lambda rid, row_id=row_id: self._set_instagram_batch_run_id(row_id, rid),
            )
            dataset_id = _extract_dataset_id(finished)
            if not dataset_id:
                raise RuntimeError(
                    f"[SESSION] Instagram batch run finished but no dataset ID could "
                    f"be extracted: {finished!r}"
                )
            self._complete_instagram_batch_row(row_id, dataset_id, client)

            row = self._get_meta_row()
            if not row["instagram_dataset_origin"]:
                self._set_meta(instagram_dataset_origin="initial")

        return self._load_all_instagram_results()

    # -----------------------------------------------------------------
    # Step 4 / Ads -- A5 applied per-record. `ads_run_id` lives directly
    # on `places` (this module's own flagged schema addition -- see the
    # module docstring's decision 1) rather than a side table, since one
    # place has at most one in-flight Ads call at a time.
    # -----------------------------------------------------------------
    def _get_place_ads_run_id(self, place_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT ads_run_id FROM places WHERE place_id=?", (place_id,)
        ).fetchone()
        return row["ads_run_id"] if row else None

    def _set_place_ads_run_id(self, place_id: str, run_id: str) -> None:
        """Same write-once protection as _set_instagram_batch_run_id --
        see that method's docstring for the reasoning. Currently only
        ever called via _start_and_track's persist_run_id callback, which
        only fires when existing_run_id was None going in, so this is
        additive safety, not a behavior change."""
        existing = self._get_place_ads_run_id(place_id)
        if existing is not None:
            raise RuntimeError(
                f"[SESSION] Refusing to overwrite already-set ads_run_id for place "
                f"{place_id!r} (already '{existing}')."
            )
        self._conn.execute(
            "UPDATE places SET ads_run_id=? WHERE place_id=?", (run_id, place_id)
        )
        self._conn.commit()

    def resolve_ads_call(
        self, client: Any, place_id: str, fb_link: str, run_input_base: dict[str, Any]
    ) -> Optional[list[dict[str, Any]]]:
        """Returns the raw ad_results list (or None if no dataset could be
        resolved), the exact shape apify_scrapper_v1.py's existing Step 4
        classification logic (Item 11) already expects -- that
        classification stays entirely in apify_scrapper_v1.py; this
        method only owns the crash-safe call mechanics."""
        existing_run_id = self._get_place_ads_run_id(place_id)
        run_input = dict(run_input_base)
        run_input["urls"] = [{"url": fb_link}]

        finished = _start_and_track(
            client, "curious_coder/facebook-ads-library-scraper", run_input,
            existing_run_id=existing_run_id,
            persist_run_id=lambda rid: self._set_place_ads_run_id(place_id, rid),
        )
        dataset_id = _extract_dataset_id(finished)
        if not dataset_id:
            return None
        return list(client.dataset(dataset_id).iterate_items())

    # -----------------------------------------------------------------
    # A4a: rehydration -- MANDATORY on resume, not optional. Reconstructs
    # the exact local_records dict shape apify_scrapper_v1.py's Step 5
    # already expects, so a resumed run's CSV never silently contains only
    # the delta this invocation itself processed.
    # -----------------------------------------------------------------
    def rehydrate_local_records(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM places").fetchall()
        records: dict[str, dict[str, Any]] = {}
        for row in rows:
            place_data = json.loads(row["raw_maps_json"])
            records[row["place_id"]] = {
                "Business Name":         str(place_data.get("title", "")),
                "Website":               str(place_data.get("website", "")),
                "Maps Place URL":        place_data.get("url"),
                "Total Score":           place_data.get("totalScore"),
                "Reviews Count":         place_data.get("reviewsCount"),
                "Instagram URL":         row["instagram_url"],
                "Instagram Handle":      row["instagram_handle"],
                "Facebook URL":          row["facebook_url"],
                "Instagram Followers":   row["instagram_followers"],
                "Last Post Date":        row["last_post_date"] if row["last_post_date"] is not None else "Unknown",
                "Passed Filter Check":   row["passed_filter_check"] if row["passed_filter_check"] is not None else "No",
                "Running Active Ads":    row["running_active_ads"] if row["running_active_ads"] is not None else "Not Checked",
                "Ad ID (Manual Verify)": row["ad_id"] if row["ad_id"] is not None else "",
                "Playwright Status":     row["playwright_status"] if row["playwright_status"] is not None else "",
                "Fallback Status":       row["fallback_status"] if row["fallback_status"] is not None else "",
            }
        return records

    # -----------------------------------------------------------------
    # A8: archiving
    # -----------------------------------------------------------------
    def finalize(self, output_csv_filename: str) -> None:
        self._set_meta(status="complete", output_csv_filename=output_csv_filename, completed_at=_now())

        # WAL mode (A9a) can leave -wal/-shm sidecar files alongside the
        # main .db file. Closing the last connection normally triggers
        # SQLite's own auto-checkpoint, but forcing an explicit full
        # checkpoint here removes any doubt before archiving -- otherwise
        # the move() below only relocates the main file, silently leaving
        # sidecar files orphaned in sessions/active/. Caught during
        # self-review, not part of the original design.
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            logger.warning(f"[SESSION] WAL checkpoint before archiving failed ({e}) -- continuing anyway.")

        self._conn.close()

        SESSIONS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        archive_path = SESSIONS_ARCHIVE_DIR / self.db_path.name
        shutil.move(str(self.db_path), str(archive_path))

        # Finishing the thought started in the comment above: the
        # wal_checkpoint(TRUNCATE) call reduces the odds of a lingering
        # -wal/-shm file, but doesn't GUARANTEE a full drain (rare, but
        # not impossible) -- and shutil.move() only ever relocates the
        # main .db file. Anything left behind here would sit orphaned in
        # sessions/active/ forever, with nothing left pointing at it.
        # Explicitly check for both known SQLite sidecar suffixes, after
        # the connection is closed, and relocate whatever's still there.
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.db_path) + suffix)
            if not sidecar.exists():
                continue
            sidecar_archive_path = Path(str(archive_path) + suffix)
            try:
                shutil.move(str(sidecar), str(sidecar_archive_path))
                logger.info(
                    f"[SESSION] Relocated lingering sidecar file: "
                    f"{sidecar} -> {sidecar_archive_path}"
                )
            except Exception as e:
                logger.warning(
                    f"[SESSION] Could not relocate lingering sidecar file "
                    f"{sidecar} ({e}) -- it may still be sitting in "
                    f"sessions/active/; safe to delete manually once you've "
                    f"confirmed {archive_path} opens cleanly and is intact."
                )

        logger.info(f"[SESSION] Run complete -- archived {self.db_path} -> {archive_path}")
        self.db_path = archive_path

    # -----------------------------------------------------------------
    # Cleanup
    # -----------------------------------------------------------------
    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> "PipelineSession":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


# =====================================================================
# Standalone smoke test -- `python pipeline_session.py` creates a
# throwaway fresh session and confirms the schema/config-hashing machinery
# runs without error. Mirrors config.py's own __main__ convention for the
# same reason: lets anyone confirm basic health without needing
# apify_client or playwright installed at all.
#
# Deliberately does NOT import or exercise health_monitor.py here -- see
# `python health_monitor.py` for that. Keeping the two smoke tests
# separate is itself part of what this split was for: each file should be
# confirmable in total isolation from the other.
# =====================================================================
if __name__ == "__main__":
    print("Running pipeline_session.py smoke test (creates a throwaway session)...\n")
    test_session = PipelineSession.create_or_resume()
    print(f"\n✅ Schema created, config hashes computed, meta row inserted.")
    print(f"   session_id       : {test_session.session_id}")
    print(f"   db_path          : {test_session.db_path}")
    row = test_session._get_meta_row()
    print(f"   ig_config_hash   : {row['ig_config_hash'][:16]}...")
    print(f"   ads_config_hash  : {row['ads_config_hash'][:16]}...")
    health_hash = row.get("health_config_hash")
    print(f"   health_config_hash: {health_hash[:16] + '...' if health_hash else '(not recorded)'}")
    schema_version_on_disk = test_session._conn.execute("PRAGMA user_version").fetchone()[0]
    print(f"   schema_version   : {schema_version_on_disk} (code expects {SCHEMA_VERSION})")
    snapshot = json.loads(row["config_snapshot_json"])
    print(f"   location_mode    : {snapshot['location_mode']}")
    print(f"   polygon_hash     : {snapshot['polygon_hash']}")
    print(f"   polygon_labels   : {snapshot['polygon_labels']}")
    test_session.close()
    print(
        f"\n(This was a throwaway smoke-test session at {test_session.db_path} -- "
        f"delete it manually if you don't want it lingering in sessions/active/.)"
    )
