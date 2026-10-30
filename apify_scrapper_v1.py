import argparse
import csv
import json
import logging
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, cast
from urllib.parse import urlparse

from apify_client import ApifyClient

# .env support -- narrowed to APIFY_API_TOKEN only (the one genuinely
# secret, per-machine value). Every other field that used to live in
# .env now lives in config.py instead, as real Python values -- see
# config.py's module docstring and implementation_plan_3.md. New
# dependency, not previously required by this script -- hard-fail with a
# clear install instruction rather than a bare ModuleNotFoundError
# traceback, since this is a solo-local Windows workflow where the fix
# is a single pip command.
try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit(
        "❌ Missing dependency: python-dotenv\n"
        "   Install it with: pip install python-dotenv --break-system-packages\n"
        "   (or: pip install python-dotenv, if you're not on a system-managed Python)"
    )

from playwright_engine import (
    PlaywrightWorker,
    resolve_social_links,
    ig_profile_regex,
)
import health_monitor
import pipeline_session

try:
    import config
except Exception as e:
    sys.exit(f"❌ Configuration error in config.py:\n {e}")

# All run_input/config values (search keywords, location targeting,
# category filter words, every actor's run_input, AD_ID_FIELDS,
# SESSION_BATCH_LIMIT, OUTPUT_BASE_NAME) live in config.py now -- see its
# module docstring for the full rationale. Importing it here also IS its
# validation step: a bad LOCATION_QUERY/CUSTOM_GEOLOCATION combination or
# a structurally broken polygon raises config.ConfigError right here,
# before .env is even loaded below, let alone before any Apify credit
# gets spent.

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Loads .env (if present) into the process environment before any of the
# os.environ.get() calls below run. Safe no-op if .env doesn't exist --
# real env vars set some other way (shell, CI, etc.) still work.
load_dotenv(override=True)


# =====================================================================
# ENV CONFIG HELPERS (Item 16 / Item 17)
#   Defined before CONFIGURATION & CONSTANTS below, since several of that
#   section's values are now sourced from .env via these helpers at
#   module-load time rather than hardcoded literals.
# =====================================================================
def _require_env(key: str) -> str:
    """Hard-fail with a clear, named-variable error if a required .env
    value is missing or blank. Applies ONLY to APIFY_API_TOKEN -- every
    other .env-sourced value in this script is individually optional
    (Item 16's revised validation scope)."""
    value = os.environ.get(key)
    if not value or not value.strip():
        sys.exit(
            f"❌ Missing required environment variable: {key}\n"
            f"   Set it in a .env file next to this script (copy .env.example "
            f"to .env and fill it in), or in your shell environment directly."
        )
    return value.strip()


# _parse_delimited_env_list(), _validate_location_config(), and
# _load_custom_geolocation() -- all removed. Superseded by config.py's
# own SEARCH_KEYWORDS / LOCATION_QUERY / CUSTOM_GEOLOCATION / 
# CATEGORY_FILTER_WORDS values and its own _validate_location_config() /
# _validate_custom_geolocation() (see config.py's module docstring and
# implementation_plan_3.md for the full redesign rationale). This
# script no longer does any .env list-parsing or GeoJSON-file loading
# at all -- config.py hands over already-validated Python values.
# =====================================================================
# CONFIGURATION & CONSTANTS
# =====================================================================
# Everything that used to be per-run or third-party-schema-shaped
# configuration (search keywords, location targeting, category filter
# words, every actor's run_input, AD_ID_FIELDS, SESSION_BATCH_LIMIT,
# OUTPUT_BASE_NAME) now lives in config.py -- see that file's module
# docstring for the full redesign rationale (implementation_plan_3.md).
# What's left here is genuinely local to this script: the one secret
# (.env-sourced, never committed), the output file extension (a code
# decision, not a config one -- changing it means swapping the CSV
# writer, not just a value), and this pipeline's own accumulated
# filtering vocabulary (additive-only, never at risk of a third party
# renaming it out from under us).
APIFY_API_TOKEN = _require_env("APIFY_API_TOKEN")

OUTPUT_EXT = ".csv"   # stays here -- see comment above.

# Pre-filtering rules — junk/institutional domains skipped before the
# browser ever opens, so they never reach playwright_engine.py at all.
# Item 5: this now runs directly against raw_website (a substring check
# needs no parsed URL) -- the engine owns all scheme/domain/path
# interpretation exclusively from here on.
SKIP_WEBSITE_KEYWORDS = [".gov", ".top", ".xyz", ".wtf", "ymca", "gotimmy", ".lol"]

# Instagram profile category/bio keywords that flag NGO or non-commercial
# pages — pipeline-level business filtering, unrelated to how the
# Instagram URL itself was found, so this stays here rather than moving
# to playwright_engine.py.
IG_BLOCK_KEYWORDS = ["ngo", "npo", "non-profit", "nonprofit", "charity", "government"]


# =====================================================================
# HELPER FUNCTIONS
# =====================================================================
def flush_dns_cache() -> None: # type: ignore[unreachable]
    """Flush OS DNS cache before run to avoid stale resolution (Windows only)."""
    try:
        if sys.platform == "win32":
            subprocess.run(["ipconfig", "/flushdns"], check=True, capture_output=True)
            logging.info("✅ DNS cache flushed.")
        else:
            logging.info("⏩ DNS flush skipped — non-Windows platform.") 
    except Exception as e:
        logging.warning(f"⚠️ DNS flush failed (non-critical): {e}")


# =====================================================================
# Item 12: Three-tier CSV sort helpers.
#   Tier 1 — fully processed to a definitive result (an "Other (...)" or
#            plain "No"/"No (Below 420 Followers)" counts as definitive,
#            just an unfavorable one -- same principle applied to both the
#            IG and Ads sides). Implemented as "not Tier 2 and not Tier 3"
#            rather than an explicit membership list, since the source
#            spec's own clarifying note ("Other(...) counts as Tier 1")
#            is broader than its literal enumerated set -- this exclusion
#            form is the reading that's actually consistent with every
#            example given, flag if that's not what was intended.
#   Tier 2 — Passed Filter Check or Running Active Ads landed on
#            "undefined" (handle/page was found, but the check itself
#            didn't produce a clean answer). Passed Filter Check is
#            matched by prefix, not exact equality, since it now carries
#            an inline parenthetical detail (Item 8b).
#   Tier 3 — no website, or no Instagram/Facebook URL discovered at all.
# =====================================================================
def _parse_instagram_timestamp(raw_timestamp: Any) -> Optional[datetime]:
    """Parse an Instagram post timestamp without allowing one bad value to
    abort the whole lead-qualification run."""
    if not isinstance(raw_timestamp, str) or not raw_timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logging.warning(f"[IG INVALID TIMESTAMP] Ignoring malformed timestamp: {raw_timestamp!r}")
        return None

    # An offset-less timestamp is ambiguous. Treat it as UTC so subtraction
    # against current_time (which is UTC) remains safe and deterministic.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _compute_tier(record: dict[str, Any]) -> int:
    if not record.get("Instagram URL") and not record.get("Facebook URL"):
        return 3

    passed_filter = str(record.get("Passed Filter Check", ""))
    running_ads   = str(record.get("Running Active Ads", ""))
    if passed_filter.startswith("undefined") or running_ads == "undefined":
        return 2

    return 1


def _tier_sort_key(record: dict[str, Any]) -> tuple[int, int, int, str]:
    """Sub-sort applied within Tier 1 (harmless no-op ordering for Tiers 2/3,
    since the spec doesn't define an internal order for those): ads=Yes
    first, then Instagram Followers descending, then Business Name
    alphabetically."""
    tier = _compute_tier(record)
    ads_is_yes = 0 if record.get("Running Active Ads") == "Yes" else 1
    followers = record.get("Instagram Followers")
    followers_desc = -followers if isinstance(followers, int) else 1  # non-numeric sorts last
    name = str(record.get("Business Name", "")).lower()
    return (tier, ads_is_yes, followers_desc, name)


class CircuitBreakerTriggered(RuntimeError):
    """Raised after a durable checkpoint when local health says to stop."""


_ACTIVE_SESSION: Optional[pipeline_session.PipelineSession] = None
_ACTIVE_LOG_ID: Optional[int] = None
_ACTIVE_LOG_HANDLER: Optional[logging.Handler] = None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find and qualify sports-academy leads with resumable checkpoints."
    )
    parser.add_argument("--resume", help="Path to an interrupted session database in sessions/active/.")
    parser.add_argument("--MapDatasetID", help="Rescue an unrecorded completed Maps dataset for --resume.")
    parser.add_argument("--igDatasetID", help="Rescue an unrecorded completed Instagram dataset for --resume.")
    return parser.parse_args()


def _configure_execution_log(session_id: str) -> str:
    """Add a per-invocation UTF-8 file log and return its relative name."""
    global _ACTIVE_LOG_HANDLER
    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"pipeline_{session_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logging.getLogger().addHandler(handler)
    _ACTIVE_LOG_HANDLER = handler
    return str(log_path)


def _check_health(health: health_monitor.PipelineHealthMonitor, stage: str) -> None:
    reason = health.check_health()
    if reason:
        raise CircuitBreakerTriggered(f"{stage}: {reason}")


def _finish_active_execution(reason: str) -> None:
    """Best-effort crash/circuit-breaker bookkeeping for the active session."""
    global _ACTIVE_SESSION, _ACTIVE_LOG_ID, _ACTIVE_LOG_HANDLER
    if _ACTIVE_SESSION is not None and _ACTIVE_LOG_ID is not None:
        try:
            _ACTIVE_SESSION.end_execution_log(_ACTIVE_LOG_ID, reason)
        except Exception as exc:
            logging.error(f"[SESSION] Could not close execution log: {exc}")
        try:
            _ACTIVE_SESSION.close()
        except Exception:
            pass
    if _ACTIVE_LOG_HANDLER is not None:
        logging.getLogger().removeHandler(_ACTIVE_LOG_HANDLER)
        _ACTIVE_LOG_HANDLER.close()
    _ACTIVE_SESSION = None
    _ACTIVE_LOG_ID = None
    _ACTIVE_LOG_HANDLER = None


# =====================================================================
# MAIN EXECUTION
# =====================================================================
def main() -> None:
    global _ACTIVE_SESSION, _ACTIVE_LOG_ID, _ACTIVE_LOG_HANDLER
    args = _parse_args()
    flush_dns_cache()
    client: Any = ApifyClient(APIFY_API_TOKEN)
    session = pipeline_session.PipelineSession.create_or_resume(
        resume_path=args.resume,
        rescue_map_dataset_id=args.MapDatasetID,
        rescue_ig_dataset_id=args.igDatasetID,
    )
    _ACTIVE_SESSION = session
    log_filename = _configure_execution_log(session.session_id)
    _ACTIVE_LOG_ID = session.start_execution_log(
        mode="resume" if args.resume else "fresh", log_filename=log_filename,
    )
    health = health_monitor.PipelineHealthMonitor(
        ram_enabled=config.HEALTH_MONITOR_KWARGS["ram_enabled"],
        ram_threshold_percent=config.HEALTH_MONITOR_KWARGS["ram_threshold_percent"],
        cpu_enabled=config.HEALTH_MONITOR_KWARGS["cpu_enabled"],
        cpu_threshold_percent=config.HEALTH_MONITOR_KWARGS["cpu_threshold_percent"],
        cpu_sustained_seconds=config.HEALTH_MONITOR_KWARGS["cpu_sustained_seconds"],
    )
    _check_health(health, "startup")
    
    # =====================================================================
    # Step 1: Google Maps — discover businesses matching target categories
    # across the configured MultiPolygon region(s).
    # =====================================================================
    logging.info("Step 1: Extracting sports academies from Google Maps...")
    
    # Base run_input (search keywords + every static cost-control flag)
    # comes straight from config.py -- copied, never mutated in place,
    # since config.MAPS_RUN_INPUT is a module-level value shared across
    # the whole process (see config.py's own docstring on this point).
    maps_run_input: dict[str, Any] = dict(config.MAPS_RUN_INPUT)
    
    # Exactly one of these two is already guaranteed set/unset correctly
    # by config.py's own import-time validation -- a plain if/else here
    # is sufficient without re-validating.
    if config.CUSTOM_GEOLOCATION is not None:
        maps_run_input["customGeolocation"] = config.CUSTOM_GEOLOCATION
    else:
        maps_run_input["locationQuery"] = config.LOCATION_QUERY
    
    # config.CATEGORY_FILTER_WORDS empty is a valid, non-error state --
    # omit the key entirely rather than sending categoryFilterWords: [].
    if config.CATEGORY_FILTER_WORDS:
        maps_run_input["categoryFilterWords"] = config.CATEGORY_FILTER_WORDS
    
    places = session.resolve_maps_dataset(client, maps_run_input)
    health.record_network_result(True)
    _check_health(health, "after Maps")
    logging.info(f"✅ Extracted {len(places)} records from Maps. Starting Playwright social-link crawler...")
    
    # =====================================================================
    # Step 2: Playwright — crawl each business website for Instagram and
    # Facebook URLs, via playwright_engine.py.
    #
    #   - Records are keyed by Maps' own "placeId", not business name, so
    #     franchise locations sharing the same name are kept as distinct
    #     leads instead of overwriting each other.
    #   - Junk/institutional domains are skipped before the browser opens.
    #   - Item 5: raw_website is passed into resolve_social_links() with zero
    #     parsing -- no `website` variable is computed here at all anymore.
    #     The engine owns 100% of URL interpretation (scheme-normalization,
    #     domain/path matching); pre-stripping to bare scheme+netloc here
    #     previously discarded the path component and made the engine's
    #     subpath-trust logic permanently inert.
    #   - All Playwright interaction — homepage scan, sitemap discovery,
    #     section-text fallback, watchdog — happens inside ONE call to
    #     resolve_social_links(). This script never touches a page/context/
    #     browser object directly; see playwright_engine.py for that logic.
    #   - PlaywrightWorker replaces the old context-dead recovery entirely: a
    #     watchdog timeout marks the worker dead, and it's discarded and
    #     replaced rather than repaired in place (a wedged call inside it
    #     can't be recovered — see PlaywrightWorker's own docstring for why).
    #   - Worker creation itself can fail (Chromium missing, transient launch
    #     hang, etc.) — retried with backoff up to WORKER_INIT_MAX_RETRIES
    #     before giving up on Step 2 entirely. If that happens, the run
    #     doesn't crash — remaining records are still written with an empty
    #     Playwright result, and Steps 3-5 still run on whatever was found.
    #   - Every call into the engine is logged going out and coming back
    #     (⇄ [ENGINE CALL] / ⇄ [ENGINE RETURN]), with elapsed time, so the
    #     main-script <-> module handoff is visible in the log on its own,
    #     separate from the engine's own internal step-by-step logs (scroll
    #     rounds, direct hits, watchdog warnings) which appear interleaved in
    #     the same stream since both files share one root logging config.
    #   - "Total Score" / "Reviews Count" / "Maps Place URL" (Item 16) are
    #     carried through unchanged from Maps. "Playwright Status" and
    #     "Fallback Status" are both purely for post-run correlation — no
    #     filtering logic depends on them.
    # =====================================================================
    WORKER_INIT_MAX_RETRIES   = 3
    WORKER_INIT_RETRY_BACKOFF = (10.0, 20.0)  # seconds; widens each retry via attempt multiplier
    
    current_time                 = datetime.now(timezone.utc)
    local_records: dict[str, dict[str, Any]] = {}
    instagram_urls_batch: list[str] = []
    pending_step2_ids = set(session.get_pending_place_ids(step=2))
    logging.info(f"[SESSION] Step 2 has {len(pending_step2_ids)} pending place(s).")
    processed_in_current_worker  = 0
    worker_generation            = 0   # increments every time a worker is (re)created — lets the
                                        # log trace exactly which worker instance handled which sites
    total_worker_restarts        = 0
    total_watchdog_timeouts      = 0
    step2_start_time             = time.monotonic()
    
    
    def _spin_up_worker(reason: str) -> Optional["PlaywrightWorker"]:
        """Create a new PlaywrightWorker with retry+backoff. Returns None (not
        an exception) if every attempt fails, so the caller can degrade Step 2
        gracefully instead of taking the whole pipeline down with it."""
        nonlocal worker_generation
        for attempt in range(1, WORKER_INIT_MAX_RETRIES + 1):
            try:
                w = PlaywrightWorker()
                worker_generation += 1
                logging.info(f"⚙️ [WORKER #{worker_generation}] Started ({reason}) — attempt {attempt}/{WORKER_INIT_MAX_RETRIES}.")
                return w
            except RuntimeError as e:
                logging.error(f"❌ [WORKER INIT FAILED] Attempt {attempt}/{WORKER_INIT_MAX_RETRIES}: {e}")
                if attempt < WORKER_INIT_MAX_RETRIES:
                    backoff = random.uniform(*WORKER_INIT_RETRY_BACKOFF) * attempt
                    logging.info(f"    ↳ Retrying in {backoff:.1f}s...")
                    time.sleep(backoff)
        return None
    
    
    worker = _spin_up_worker(reason="initial startup")
    if worker is None:
        logging.error(
            "❌ Could not start a Playwright browser after multiple attempts. "
            "Step 2 will be skipped for every record — check that "
            "`playwright install chromium` has been run, and that no leftover "
            "Chromium processes are hogging resources on this machine."
        )
    
    try:
        for idx, place in enumerate(places, 1):
            place_data: dict[str, Any] = place
            name: str = str(place_data.get("title", ""))
            raw_website: str = str(place_data.get("website", ""))
            total_score: Any = place_data.get("totalScore")
            reviews_count: Any = place_data.get("reviewsCount")
            maps_place_url: Any = place_data.get("url")  # Item 16
            # placeId uniquely identifies each Maps listing — safe key even
            # for franchises sharing an identical business name.
            place_id: str = str(
                place_data.get("placeId") or pipeline_session._stable_fallback_place_id(place_data)
            )
            if place_id not in pending_step2_ids:
                continue
    
            instagram_url     = None
            instagram_handle  = None
            facebook_url      = None
            playwright_status  = ""
            fallback_status    = ""
    
            # Item 14: permanently-closed places are skipped before Playwright
            # ever starts -- checked ahead of the website check itself, since
            # a closed business isn't a valid lead regardless of whether it
            # has a website. Label-only, not exclusion: the record still
            # flows through to local_records below (website=None-equivalent
            # path), same "include with a status label" convention already
            # used by "Skipped - No Website" / "Skipped - Institutional
            # Domain". temporarilyClosed is deliberately left unchecked --
            # those remain valid leads.
            if place_data.get("permanentlyClosed") is True:
                logging.info(f"  [{idx}/{len(places)}] 🚫 Skipping '{name}' — marked permanently closed.")
                playwright_status = "Skipped - Permanently Closed"
    
            elif not raw_website:
                logging.info(f"  [{idx}/{len(places)}] ⏩ Skipping '{name}' — no website.")
                playwright_status = "Skipped - No Website"
    
            elif worker is None:
                playwright_status = "Skipped - No Browser Available"
    
            # Item 5: pre-filter runs directly on raw_website -- no parsing.
            elif any(kw in raw_website.lower() for kw in SKIP_WEBSITE_KEYWORDS):
                logging.info(f"  [{idx}/{len(places)}] 🚫 Skipping institutional/junk domain: {raw_website}")
                playwright_status = "Skipped - Institutional Domain"
    
            else:
                logging.info(f"  [{idx}/{len(places)}] 🌐 [PLAYWRIGHT] Scraping: {raw_website}")
                logging.info(f"    ⇄ [ENGINE CALL] resolve_social_links(worker=#{worker_generation}, website='{raw_website}')")
                site_start = time.monotonic()
    
                try:
                    result = resolve_social_links(worker, raw_website)
                except Exception as e:
                    # Defensive boundary guard — resolve_social_links() is
                    # designed to always return a dict, never raise. If it
                    # somehow does anyway (a genuine bug in the engine, not
                    # an ordinary site-level failure, which the engine
                    # already turns into a status string), don't let that
                    # take the whole batch down — log with full context and
                    # keep going.
                    elapsed = time.monotonic() - site_start
                    logging.error(f"    ⇄ [ENGINE CALL FAILED] Unexpected exception after {elapsed:.1f}s: {e}")
                    result = {
                        "instagram_url": None, "instagram_handle": None, "facebook_url": None,
                        "playwright_status": f"Engine Error: {e}",
                        "fallback_status": "Not attempted - unexpected engine exception",
                        "watchdog_timed_out": False,
                    }
    
                elapsed = time.monotonic() - site_start
                instagram_url     = result["instagram_url"]
                instagram_handle  = result["instagram_handle"]
                facebook_url      = result["facebook_url"]
                playwright_status = result["playwright_status"]
                fallback_status   = result["fallback_status"]
                watchdog_fired    = result["watchdog_timed_out"]
    
                logging.info(
                    f"    ⇄ [ENGINE RETURN] {elapsed:.1f}s elapsed | status='{playwright_status}' | "
                    f"ig={'Y' if instagram_url else 'N'} fb={'Y' if facebook_url else 'N'} | "
                    f"watchdog={'YES' if watchdog_fired else 'no'}"
                )
    
                if instagram_url:
                    instagram_urls_batch.append(instagram_url)
                    logging.info(f"    ↳ 📸 [INSTAGRAM]: {instagram_handle}")
                if facebook_url:
                    logging.info(f"    ↳ 👤 [FACEBOOK]: {facebook_url}")
                if not instagram_url and not facebook_url:
                    logging.info(f"    ↳ ⏩ [NO LINKS] {fallback_status}")
    
                if watchdog_fired:
                    total_watchdog_timeouts += 1
    
                # ── Watchdog-triggered replacement ────────────────────
                # This entirely replaces the old is_context_dead() + inline
                # rebuild path. A dead worker can't be repaired (see
                # PlaywrightWorker docstring for why), so it's always
                # discarded and replaced, never reused.
                if worker.is_dead:
                    logging.warning(
                        f"    ↳ ♻️ [WORKER #{worker_generation} DEAD] Watchdog fired — replacing worker "
                        f"(this generation handled {processed_in_current_worker} site(s) before dying)."
                    )
                    worker.kill()
                    time.sleep(random.uniform(5.0, 10.0))
                    worker = _spin_up_worker(reason=f"replacing dead worker #{worker_generation}")
                    total_worker_restarts += 1
                    if worker is None:
                        logging.error(
                            "❌ Could not restart the Playwright browser after a watchdog timeout. "
                            "Remaining sites in this run will be skipped (no browser available)."
                        )
                    processed_in_current_worker = 0
                else:
                    processed_in_current_worker += 1
    
                time.sleep(random.uniform(1.5, 3.5))
    
                # ── Scheduled recycling for a fresh UA/viewport — a
                # different reason to rotate than watchdog death, so kept
                # as a separate trigger. ────────────────────────────────
                if worker is not None and processed_in_current_worker >= config.SESSION_BATCH_LIMIT:
                    cooldown = random.uniform(15.0, 30.0)
                    logging.info(
                        f"⚙️ [SESSION RESET] Recycling browser (new UA/viewport) after "
                        f"{processed_in_current_worker} sites. Sleeping {cooldown:.1f}s..."
                    )
                    worker.kill()
                    time.sleep(cooldown)
                    worker = _spin_up_worker(reason="scheduled session recycle")
                    total_worker_restarts += 1
                    if worker is None:
                        logging.error(
                            "❌ Could not restart the Playwright browser during scheduled recycling. "
                            "Remaining sites in this run will be skipped (no browser available)."
                        )
                    processed_in_current_worker = 0
    
            local_records[place_id] = {
                "Business Name":         name,
                "Website":               raw_website,
                "Maps Place URL":        maps_place_url,   # Item 16
                "Total Score":           total_score,
                "Reviews Count":         reviews_count,
                "Instagram URL":         instagram_url,
                "Instagram Handle":      instagram_handle,
                "Facebook URL":          facebook_url,
                "Instagram Followers":   None,
                "Last Post Date":        "Unknown",
                "Passed Filter Check":   "No",
                "Running Active Ads":    "Not Checked",
                "Ad ID (Manual Verify)": "",
                "Playwright Status":     playwright_status,
                # Same "passive CSV correlation column" spirit as Total
                # Score/Reviews Count — not used by any filtering logic, just
                # useful to eyeball post-run to see which sites resolved via
                # sitemap vs section-text vs nothing.
                "Fallback Status":       fallback_status,
            }
            session.record_step2_result(place_id, {
                "instagram_url": instagram_url,
                "instagram_handle": instagram_handle,
                "facebook_url": facebook_url,
                "playwright_status": playwright_status,
                "fallback_status": fallback_status,
            })
            _check_health(health, f"after Step 2 checkpoint for {name!r}")
    
    finally:
        if worker is not None:
            logging.info("⚙️ Cleaning up browser processes...")
            worker.kill()
    
        step2_elapsed = time.monotonic() - step2_start_time
        sites_attempted = sum(
            1 for r in local_records.values()
            if r["Playwright Status"] not in ("Skipped - No Website", "Skipped - Institutional Domain", "Skipped - No Browser Available")
        )
        logging.info(
            f"\n📊 [STEP 2 SUMMARY] {step2_elapsed/60:.1f} min total | {len(local_records)} record(s) | "
            f"{sites_attempted} site(s) actually crawled | {worker_generation} worker generation(s) used | "
            f"{total_worker_restarts} restart(s) | {total_watchdog_timeouts} watchdog timeout(s)"
        )
        if sites_attempted:
            logging.info(f"   Avg engine time/crawled site: {step2_elapsed / sites_attempted:.1f}s")

    # A4a: the CSV and later stages must see both this invocation's records
    # and every checkpoint rehydrated from an interrupted prior invocation.
    local_records = session.rehydrate_local_records()
    instagram_urls_batch = [
        str(record["Instagram URL"])
        for record in local_records.values()
        if record.get("Instagram URL")
    ]
    
    # =====================================================================
    # Step 3: Instagram batch call — one actor run for all discovered
    # profiles. Profiles flagged as NGO/charity are dropped here so they
    # never reach the ads check in Step 4.
    #
    #   Items 6 / 7 / 8 / 8b:
    #   - `instagram_status_overrides[handle]` records a definitive,
    #     non-quantitative reason a handle can't be scored on follower/
    #     recency data at all: "Other (Not Found)", "Other (Private)",
    #     "Other (Restricted)", or an "undefined (...)" catch-all. Step 4
    #     checks this map BEFORE the normal follower/recency branch, for
    #     every handle it covers.
    #   - Handle resolution prefers the URL-based extraction (matches how
    #     instagram_urls_batch itself was built), falling back to the raw
    #     "username" field when inputUrl is absent — confirmed populated
    #     even for not_found responses, where inputUrl is null and the old
    #     `if not url: continue` guard silently dropped the item entirely
    #     with no record anything was ever attempted (Item 6).
    #   - private/Restricted-profile responses both have inputUrl populated,
    #     so without an explicit check here they'd silently fall into the
    #     generic <420-follower branch in Step 4 and read as a genuinely
    #     small business rather than an inaccessible one (Item 7).
    #   - Any other named error, or an item with no error and no usable
    #     url/username at all, is Item 8b case 1 (unrecognized shape) — kept
    #     as "undefined (<= 120 char detail>)" for manual review, same
    #     truncation spirit as the Ads "Ad ID (Manual Verify)" column. A
    #     defensive try/except wraps the whole per-item block so one
    #     malformed item can never crash the batch loop.
    #   - Item 8b case 2 (silent batch drop — a handle sent to the actor that
    #     never appears in the response array in any form) is reconciled once,
    #     right after this loop, via a set-difference check.
    # =====================================================================
    instagram_profile_map: dict[str, dict[str, Any]] = {}
    instagram_status_overrides: dict[str, str] = {}
    
    if instagram_urls_batch:
        instagram_urls_batch = list(set(instagram_urls_batch))
        logging.info(f"\nStep 3: Batch Instagram call for {len(instagram_urls_batch)} unique profiles...")
        try:
            # PipelineSession owns directUrls/resultsLimit construction,
            # start+persist+wait, and missing-handle reconciliation.
            ig_results = session.resolve_instagram_batch(client, config.INSTAGRAM_RUN_INPUT)
            health.record_network_result(True)
            _check_health(health, "after Instagram batch")
    
            for profile in ig_results:
                try:
                    error = profile.get("error")
                    url   = profile.get("inputUrl")
    
                    # Resolve a handle/key for this item — prefer the
                    # URL-based extraction, fall back to the raw "username"
                    # field when inputUrl is absent (Item 6).
                    handle: Optional[str] = None
                    if isinstance(url, str) and url:
                        match = ig_profile_regex.search(url)
                        if match:
                            handle = match.group(1).split('?')[0].strip("/").lower()
                    if not handle:
                        raw_username = profile.get("username")
                        if isinstance(raw_username, str) and raw_username:
                            handle = raw_username.split('?')[0].strip("/").lower()
    
                    if not handle:
                        # No usable key at all — can't attribute this to any
                        # specific business record. Vanishingly rare; nothing
                        # left to do but skip.
                        logging.warning(f"    ⚠️ [IG] Unattributable response, no url or username: {str(profile)[:120]}")
                        continue
    
                    # Items 7 / 8: private/restricted/not_found all get an
                    # explicit Other (...) status, checked BEFORE any
                    # follower/recency data is trusted.
                    if profile.get("private") is True:
                        instagram_status_overrides[handle] = "Other (Private)"
                    elif profile.get("error") == "Restricted profile":
                        instagram_status_overrides[handle] = "Other (Restricted)"
                    elif profile.get("error") == "not_found":
                        instagram_status_overrides[handle] = "Other (Not Found)"
                    elif error:
                        # Some other, unrecognized error shape (Item 8b case 1)
                        instagram_status_overrides[handle] = f"undefined ({str(error)[:120]})"
                    elif not url:
                        # No error field, but no usable inputUrl either —
                        # also an unrecognized shape (Item 8b case 1)
                        instagram_status_overrides[handle] = f"undefined ({str(profile)[:120]})"
                    else:
                        category = (profile.get("businessCategoryName") or "").lower()
                        bio      = (profile.get("biography") or "").lower()
                        if any(kw in category or kw in bio for kw in IG_BLOCK_KEYWORDS):
                            logging.info(f"    🚫 [FILTERED] Dropping '{handle}' — NGO/NPO detected.")
                            instagram_status_overrides[handle] = "Other (NGO/Non-Profit)"
                            continue
                        instagram_profile_map[handle] = profile
    
                except Exception as item_e:
                    # Defensive catch-all (Item 8b case 1) — a single
                    # malformed item must never take down the whole batch loop.
                    logging.warning(f"    ⚠️ [IG UNRECOGNIZED SHAPE] {item_e} — raw: {str(profile)[:200]}")
    
            # Item 8b case 2: batch reconciliation — a handle sent to the
            # actor that never appears in the response array at all, in any
            # form. Mechanically different from case 1 (a set-difference
            # check, not a per-item field check), but classified the same way.
            requested_handles = {
                g1.split('?')[0].strip('/').lower()
                for u in instagram_urls_batch
                if (m := ig_profile_regex.search(u)) and (g1 := m.group(1)) is not None
            }
    
            accounted_handles = set(instagram_profile_map.keys()) | set(instagram_status_overrides.keys())
            missing_handles = requested_handles - accounted_handles
            for handle in missing_handles:
                if handle is not None:
                    instagram_status_overrides[handle] = "undefined (missing from batch response)"
            if missing_handles:
                logging.warning(f"    ⚠️ [IG BATCH RECONCILIATION] {len(missing_handles)} handle(s) never appeared in the response at all.")

            # Additional sanity check (2026-07-22, post-resultsLimit-bugfix):
            # purely observational, changes no record's classification and
            # never discards or retries anything. Item 8b's per-handle
            # reconciliation above already catches every individual missing
            # handle -- this check exists only to make ONE specific failure
            # shape loud and immediately legible in the log: a large,
            # uniform shortfall consistent with a dataset-level truncation
            # (e.g. resultsLimit silently capping the whole batch again, or
            # any future Apify-side change to how resultsLimit/directUrls
            # interact), as opposed to a handful of individually-unresolvable
            # handles (genuinely private/restricted/deleted accounts, which
            # is normal and expected noise in any real batch). Deliberately
            # does NOT set any threshold-based branching, does NOT retry,
            # and does NOT fall back to per-profile calls -- it only logs,
            # so a real but small number of missing handles is never
            # mistaken for something requiring intervention.
            actual_dataset_size = len(ig_results)
            requested_count = len(instagram_urls_batch)
            if actual_dataset_size < requested_count:
                shortfall = requested_count - actual_dataset_size
                logging.warning(
                    f"    ⚠️ [IG DATASET SIZE] Requested {requested_count} profile(s), dataset "
                    f"returned {actual_dataset_size} — {shortfall} short. If this shortfall is "
                    f"much larger than the {len(missing_handles)} handle(s) flagged above by name, "
                    f"that's consistent with a batch-level truncation (e.g. a resultsLimit "
                    f"misconfiguration) rather than ordinary per-handle failures — worth checking "
                    f"config.INSTAGRAM_RUN_INPUT and the resultsLimit override in this script if so."
                )

        except CircuitBreakerTriggered:
            raise  # let a real trip propagate, let the outer try/finally handle the durable checkpoint and exit
        except Exception as e:
            health.record_network_result(False)  # this IS the failure signal
            logging.error(f"❌ Batch Instagram call failed: {e}")
            # The whole batch is unusable at this point -- every handle that
            # would have been checked needs an explicit status, or it falls
            # through to Step 4's untouched defaults ("No" / "Not Checked")
            # and reads as a definitively-processed Tier 1 result instead of
            # "we never actually got an answer."
            for u in instagram_urls_batch:
                m = ig_profile_regex.search(u)
                if m:
                    h = m.group(1).split('?')[0].strip('/').lower()
                    instagram_status_overrides.setdefault(h, "undefined (instagram batch call failed)")
    
    # =====================================================================
    # Step 4: Filter + Ads check.
    #   Follower/recency tiering:
    #     < 420 followers          → skipped, no ads check
    #     420–90,000 followers     → must have posted within 210 days
    #     > 90,000 followers       → auto-pass (recency not required),
    #                                 distinct label (Item 9)
    #   Instagram status overrides (Items 7/8/8b) are checked FIRST, before
    #   any follower/recency logic runs.
    #   Ad result classification (Item 11 — clean-message mapping):
    #     "Yes"                             → ad_archive_id present in response
    #     "No (Page Exists, No Active Ads)" → actor returned ADS_NOT_FOUND sentinel
    #     "Other (PAGE_NOT_FOUND)"          → actor returned PAGE_NOT_FOUND (confirmed
    #                                          real cases: Facebook Group links)
    #     "Other (PAGE_PRIVATE)"            → actor returned PAGE_PRIVATE (confirmed
    #                                          real case: a moved/deleted business Page)
    #     "undefined"                       → empty dataset OR unrecognized response
    #                                          shape — both collapsed into one honest
    #                                          catch-all rather than two separate
    #                                          ambiguous labels (see implementation_plan.md)
    #   Item 10: Facebook Group links are pre-filtered before the ads actor is
    #   ever called — the API can't process them and returns PAGE_NOT_FOUND
    #   either way, indistinguishable after the fact from a genuinely deleted
    #   Page, so catching it locally preserves that distinction and saves the
    #   wasted call. The link itself stays visible in the "Facebook URL"
    #   column — only the ads-check step is skipped.
    # =====================================================================
    logging.info("\nStep 4: Running filter thresholds and Meta Ads checks...")
    pending_step4_ids = set(session.get_pending_place_ids(step=4))
    logging.info(f"[SESSION] Step 4 has {len(pending_step4_ids)} pending place(s).")

    def _checkpoint_step4(place_id: str, record: dict[str, Any]) -> None:
        session.record_step4_result(place_id, {
            "passed_filter_check": record["Passed Filter Check"],
            "running_active_ads": record["Running Active Ads"],
            "instagram_followers": record["Instagram Followers"],
            "last_post_date": record["Last Post Date"],
            "ad_id": record.get("Ad ID (Manual Verify)", ""),
        })
        _check_health(health, f"after Step 4 checkpoint for {record['Business Name']!r}")
    
    for place_id, record in local_records.items():
        if place_id not in pending_step4_ids:
            continue
        name              = record["Business Name"]
        handle_key        = record["Instagram Handle"]
        fb_link           = record["Facebook URL"]
        passed_shortlist  = False
        record["Ad ID (Manual Verify)"] = ""
    
        # Items 7/8/8b: a definitive non-quantitative signal already exists
        # for this handle — skip the follower/recency branch entirely rather
        # than let it silently read as a real business with near-zero
        # followers (or a clean unattempted check).
        if handle_key and handle_key in instagram_status_overrides:
            record["Passed Filter Check"] = instagram_status_overrides[handle_key]
            record["Running Active Ads"]  = "Skipped (No Usable IG Data)"
            _checkpoint_step4(place_id, record)
            continue
    
        if handle_key and handle_key in instagram_profile_map:
            profile_data = instagram_profile_map[handle_key]
            followers: int = int(profile_data.get("followersCount", 0) or 0)
            latest_posts = cast(list[dict[str, Any]], profile_data.get("latestPosts") or [])
            record["Instagram Followers"] = followers
    
            if followers < 420:
                record["Passed Filter Check"] = "No (Below 420 Followers)"
                record["Running Active Ads"]  = "Skipped (Low Follower Count)"
                _checkpoint_step4(place_id, record)
                continue
    
            if followers >= 90000:
                # Item 9 (revised): 90K+ is now a disqualifying ceiling, not
                # a recency bypass -- symmetric with the <420 branch above.
                # Original framing was "big enough that recency doesn't
                # matter"; the retargeted framing is "big enough to read as
                # an established/franchise-scale operation rather than the
                # small-business lead this pipeline is actually hunting
                # for." passed_shortlist is deliberately NOT set True here
                # anymore, and this branch now `continue`s immediately,
                # exactly like the <420 branch -- no ads check is run, and
                # latest_posts is never read for this branch (same as
                # before the Item 9 revision, just now for a disqualifying
                # reason instead of a bypass reason).
                record["Passed Filter Check"] = "No (Above 90K Followers)"
                record["Running Active Ads"]  = "Skipped (High Follower Count)"
                _checkpoint_step4(place_id, record)
                continue
            else:
                # Item 19: zero posts is its own standalone terminal status --
                # there's no recency verdict to compute at all, so this
                # doesn't fall through to the Yes/No branch below. Scoped to
                # this branch only (420 <= followers < 90000) -- the 90K+
                # branch above never reads latest_posts at all, by design
                # (see Item 9 revision note above).
                if not latest_posts:
                    record["Last Post Date"]      = "No Posts Yet"
                    record["Passed Filter Check"] = "No Posts Yet"
                    record["Running Active Ads"]  = "Skipped (No Posts Yet)"
                    # passed_shortlist stays False -- falls through to the
                    # existing ads-check-skip logic below unchanged.
                else:
                    # Item 15: scan ALL returned posts for recency, not just
                    # latestPosts[0] -- a pinned post can make index 0 an old
                    # timestamp that doesn't reflect true upload chronology.
                    # Uses the existing _parse_instagram_timestamp() helper
                    # (not a bare inline try/except) so UTC-naive
                    # normalization and malformed-timestamp logging stay
                    # consistent with the rest of this script.
                    parsed_dates: list[datetime] = []
                    for post in latest_posts:
                        parsed = _parse_instagram_timestamp(post.get("timestamp"))
                        if parsed is not None:
                            parsed_dates.append(parsed)

                    if parsed_dates:
                        most_recent = max(parsed_dates)
                        days_since_post = (current_time - most_recent).days
                        record["Last Post Date"] = most_recent.strftime('%Y-%m-%d')
                        passed_shortlist = days_since_post <= 210
                    else:
                        record["Last Post Date"] = "Unknown"

                    # Item 19: <12-posts annotation -- purely informational,
                    # composed onto the Yes/No verdict already computed
                    # above. Does not alter passed_shortlist or any
                    # downstream ads-check behavior.
                    post_count = len(latest_posts)
                    base_label = "Yes" if passed_shortlist else "No"
                    if post_count < 12:
                        record["Passed Filter Check"] = f"{base_label} (Only {post_count} Posts Found)"
                    else:
                        record["Passed Filter Check"] = base_label
    
        if passed_shortlist:
            logging.info(f"  -> [ADS] Checking: '{name}'")
    
            if not fb_link:
                record["Running Active Ads"] = "No Facebook URL Found"
    
            elif "/groups/" in urlparse(fb_link).path.lower():
                # Item 10: Facebook Groups can't be processed by the Ads
                # Library API — pre-filtered locally rather than spending a
                # call to get back an ambiguous PAGE_NOT_FOUND. Link itself
                # stays in the CSV; only the ads check is skipped.
                record["Running Active Ads"] = "Skipped (Facebook Group, Not a Page)"
                logging.info(f"    ↳ [FB GROUP] {name} — skipping ads check, link is a Group not a Page.")
    
            else:
                try:
                    ad_results = session.resolve_ads_call(
                        client, place_id, fb_link, config.ADS_RUN_INPUT,
                    )
                    health.record_network_result(True)
                    _check_health(health, f"after Ads call for {name!r}")

                    if ad_results is None:
                        record["Running Active Ads"]    = "undefined"
                        record["Ad ID (Manual Verify)"] = "No dataset ID returned"
                        logging.warning(f"    ↳ ⚠️ [UNDEFINED] {name} — no dataset ID returned.")

                    elif not ad_results:
                        # Item 11: empty dataset is NOT a confident "dead page"
                        # signal — often an Apify-side dataset/retry quirk or a
                        # profile.php-redirect artifact — so it's collapsed
                        # into the same honest "undefined" catch-all as an
                        # unrecognized response shape, rather than a
                        # confident-sounding "Dead or Unreachable" label.
                        record["Running Active Ads"]    = "undefined"
                        record["Ad ID (Manual Verify)"] = "Empty dataset returned"
                        logging.info(f"    ↳ [UNDEFINED] {name} — empty dataset returned.")
    
                    elif ad_results[0].get("errorCode") == "ADS_NOT_FOUND":
                        # Confident, explicit signal — page is valid but has no active ads
                        record["Running Active Ads"] = "No (Page Exists, No Active Ads)"
                        logging.info(f"    ↳ [ADS = No] {name}")
    
                    elif ad_results[0].get("errorCode") == "PAGE_NOT_FOUND":
                        record["Running Active Ads"] = "Other (PAGE_NOT_FOUND)"
                        logging.info(f"    ↳ [PAGE NOT FOUND] {name}")
    
                    elif ad_results[0].get("errorCode") == "PAGE_PRIVATE":
                        record["Running Active Ads"] = "Other (PAGE_PRIVATE)"
                        logging.info(f"    ↳ [PAGE PRIVATE] {name}")
    
                    else:
                        # Real ad items — require a known ID field to confirm
                        actual_ads = [item for item in ad_results if any(item.get(f) for f in config.AD_ID_FIELDS)]
                        if actual_ads:
                            ad_id = next((actual_ads[0].get(f) for f in config.AD_ID_FIELDS if actual_ads[0].get(f)), "")
                            record["Running Active Ads"]    = "Yes"
                            record["Ad ID (Manual Verify)"] = str(ad_id)
                            logging.info(f"    ↳ [ADS = Yes] {name} — Ad ID: {ad_id}")
                        else:
                            # Non-empty, no recognized error code, no
                            # recognized ID field — genuinely unrecognized shape
                            record["Running Active Ads"]    = "undefined"
                            record["Ad ID (Manual Verify)"] = json.dumps(ad_results[0])[:300]
                            logging.warning(f"    ↳ ⚠️ [UNDEFINED] {name} — unrecognized shape, raw: {record['Ad ID (Manual Verify)']}")
    
                except CircuitBreakerTriggered:
                    raise
                except Exception as e:
                    health.record_network_result(False)  #this IS the failure signal
                    logging.error(f"    ↳ ❌ Ad check failed for '{name}': {e}")
                    record["Running Active Ads"] = "Error"
    
        elif record["Instagram URL"]:
            if "Skipped" not in str(record["Running Active Ads"]):
                record["Running Active Ads"] = "Skipped (Failed IG Filter)"

        _checkpoint_step4(place_id, record)
    
    # =====================================================================
    # Step 5: Export — filename auto-increments if it already exists
    # (filtered_sports_academies.csv → filtered_sports_academies(1).csv etc.)
    # so no run ever silently overwrites a previous result.
    #
    # Item 12: rows are sorted into three completeness tiers before writing
    # (see _compute_tier / _tier_sort_key above) rather than written in raw
    # Maps insertion order, so the most actionable leads surface first
    # without manual sorting.
    # =====================================================================
    # A4a: rehydrate once more after Step 4 so the CSV is derived solely
    # from durable state, including records completed by an earlier resume.
    local_records = session.rehydrate_local_records()
    sorted_records = sorted(local_records.values(), key=_tier_sort_key)
    
    csv_file = f"{config.OUTPUT_BASE_NAME}{OUTPUT_EXT}"
    counter  = 1
    while os.path.exists(csv_file):
        csv_file = f"{config.OUTPUT_BASE_NAME}({counter}){OUTPUT_EXT}"
        counter += 1
    
    with open(csv_file, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Business Name", "Website", "Maps Place URL", "Total Score", "Reviews Count",
            "Instagram URL", "Facebook URL",
            "Instagram Followers", "Last Post Date",
            "Passed Filter Check", "Running Active Ads", "Ad ID (Manual Verify)",
            "Playwright Status", "Fallback Status",
        ])
        for data in sorted_records:
            writer.writerow([
                data["Business Name"],
                data["Website"],
                data.get("Maps Place URL", ""),
                data["Total Score"],
                data["Reviews Count"],
                data["Instagram URL"],
                data["Facebook URL"],
                data["Instagram Followers"],
                data["Last Post Date"],
                data["Passed Filter Check"],
                data["Running Active Ads"],
                data.get("Ad ID (Manual Verify)", ""),
                data.get("Playwright Status", ""),
                data.get("Fallback Status", ""),
            ])

    session.end_execution_log(_ACTIVE_LOG_ID, reason="complete")
    _ACTIVE_LOG_ID = None
    session.finalize(output_csv_filename=csv_file)
    _ACTIVE_SESSION = None
    if _ACTIVE_LOG_HANDLER is not None:
        logging.getLogger().removeHandler(_ACTIVE_LOG_HANDLER)
        _ACTIVE_LOG_HANDLER.close()
        _ACTIVE_LOG_HANDLER = None
    
    logging.info(f"\n🎉 Pipeline complete. Report saved: {csv_file}")
    logging.info(f"   Total records                     : {len(local_records)}")
    logging.info("   --- Running Active Ads breakdown ---")
    logging.info(f"   Yes                                : {sum(1 for d in local_records.values() if d['Running Active Ads'] == 'Yes')}")
    logging.info(f"   No (Page Exists, No Active Ads)    : {sum(1 for d in local_records.values() if d['Running Active Ads'] == 'No (Page Exists, No Active Ads)')}")
    logging.info(f"   Other (PAGE_NOT_FOUND)             : {sum(1 for d in local_records.values() if d['Running Active Ads'] == 'Other (PAGE_NOT_FOUND)')}")
    logging.info(f"   Other (PAGE_PRIVATE)               : {sum(1 for d in local_records.values() if d['Running Active Ads'] == 'Other (PAGE_PRIVATE)')}")
    logging.info(f"   undefined                          : {sum(1 for d in local_records.values() if d['Running Active Ads'] == 'undefined')}")
    logging.info(f"   No Facebook URL Found              : {sum(1 for d in local_records.values() if d['Running Active Ads'] == 'No Facebook URL Found')}")
    logging.info(f"   Skipped (Facebook Group)           : {sum(1 for d in local_records.values() if d['Running Active Ads'] == 'Skipped (Facebook Group, Not a Page)')}")
    logging.info(f"   Skipped (No Usable IG Data)        : {sum(1 for d in local_records.values() if d['Running Active Ads'] == 'Skipped (No Usable IG Data)')}")
    logging.info(f"   Skipped (Failed IG Filter)         : {sum(1 for d in local_records.values() if d['Running Active Ads'] == 'Skipped (Failed IG Filter)')}")
    logging.info(f"   Skipped (Low Follower Count)       : {sum(1 for d in local_records.values() if d['Running Active Ads'] == 'Skipped (Low Follower Count)')}")
    logging.info(f"   Skipped (High Follower Count)      : {sum(1 for d in local_records.values() if d['Running Active Ads'] == 'Skipped (High Follower Count)')}")
    logging.info(f"   Skipped (No Posts Yet)             : {sum(1 for d in local_records.values() if d['Running Active Ads'] == 'Skipped (No Posts Yet)')}")
    logging.info(f"   Error                              : {sum(1 for d in local_records.values() if d['Running Active Ads'] == 'Error')}")
    logging.info("   --- Passed Filter Check breakdown ---")
    logging.info(f"   Yes                                : {sum(1 for d in local_records.values() if d['Passed Filter Check'] == 'Yes')}")
    logging.info(f"   No                                 : {sum(1 for d in local_records.values() if d['Passed Filter Check'] == 'No')}")
    logging.info(f"   No (Below 420 Followers)           : {sum(1 for d in local_records.values() if d['Passed Filter Check'] == 'No (Below 420 Followers)')}")
    logging.info(f"   No (Above 90K Followers)           : {sum(1 for d in local_records.values() if d['Passed Filter Check'] == 'No (Above 90K Followers)')}")
    logging.info(f"   No Posts Yet                       : {sum(1 for d in local_records.values() if d['Passed Filter Check'] == 'No Posts Yet')}")
    logging.info(f"   Yes/No (<12 Posts Found, Item 19)  : {sum(1 for d in local_records.values() if '(Only ' in str(d['Passed Filter Check']) and 'Posts Found)' in str(d['Passed Filter Check']))}")
    logging.info(f"   Other (Not Found)                  : {sum(1 for d in local_records.values() if d['Passed Filter Check'] == 'Other (Not Found)')}")
    logging.info(f"   Other (Private)                    : {sum(1 for d in local_records.values() if d['Passed Filter Check'] == 'Other (Private)')}")
    logging.info(f"   Other (Restricted)                 : {sum(1 for d in local_records.values() if d['Passed Filter Check'] == 'Other (Restricted)')}")
    logging.info(f"   Other (NGO/Non-Profit)             : {sum(1 for d in local_records.values() if d['Passed Filter Check'] == 'Other (NGO/Non-Profit)')}")
    logging.info(f"   undefined (...)                    : {sum(1 for d in local_records.values() if str(d['Passed Filter Check']).startswith('undefined'))}")
    logging.info("   --- CSV tier breakdown (Item 12) ---")
    logging.info(f"   Tier 1 (definitive)                : {sum(1 for d in local_records.values() if _compute_tier(d) == 1)}")
    logging.info(f"   Tier 2 (ambiguous/undefined)       : {sum(1 for d in local_records.values() if _compute_tier(d) == 2)}")
    logging.info(f"   Tier 3 (no social data)            : {sum(1 for d in local_records.values() if _compute_tier(d) == 3)}")


if __name__ == "__main__":
    try:
        main()
    except CircuitBreakerTriggered as exc:
        logging.error(f"🛑 [CIRCUIT BREAKER] Pipeline paused safely: {exc}")
        _finish_active_execution(f"circuit_breaker: {exc}")
        sys.exit(2)
    except BaseException:
        _finish_active_execution("crashed")
        raise
