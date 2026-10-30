"""
config.py -- single source of truth for every Apify run_input this
pipeline sends, across all three actors (Google Maps, Instagram, Facebook
Ads Library). Read this docstring before editing anything below.

=============================================================================
WHY THIS FILE EXISTS (see implementation_plan_3.md for the full discussion)
=============================================================================
This file replaces an earlier `.env` + `target_polygon.json` setup
(implementation_plan_2.md's Items 16/17). That setup worked and was fully
tested, but turned out to have a real, not cosmetic, problem once it was
actually lived in:

  1. Configuration was split across THREE places with THREE unrelated
     syntaxes: `.env` (flat key=value strings) for search/location/category
     fields, a separate `.json` file for the target polygon (GeoJSON's own
     nesting and coordinate-order rules), and main_apify.py itself
     for every Instagram/Ads actor field. Changing "what this run does"
     routinely meant touching two or three files with no shared editing
     model.

  2. `.env`'s flat-string format can't natively hold a list, so a
     list-shaped field (like category filter words) needed its own
     delimiter-and-escaping scheme just to become a Python list at all.
     That parsing layer had a real, confirmed bug history worth knowing
     about (see the cross-reference note further down) -- none of that
     complexity was inherent to the actual problem ("store a list of
     strings that changes often"); it was a byproduct of forcing a list
     into a format that can't hold one.

  3. JSON can't hold comments, which is a genuine usability loss for a
     coordinates file -- you can't leave yourself a note like "SF metro,
     drawn 2026-07" next to the polygon it describes without hacking in a
     throwaway key that then has to be stripped back out before it leaks
     into the real Apify payload.

  4. The deciding factor: Apify actors are third-party and externally
     maintained, and their input schemas can change without notice
     (renamed fields, deprecated fields, new required fields -- this
     project already hit exactly this kind of question once, over the
     Google Maps actor's timeout parameter). Before this file existed, a
     schema change to anything NOT in `.env`/JSON -- i.e. any Instagram or
     Ads actor field, or any of the Maps actor's static cost-control flags
     -- required editing main_apify.py directly: the same file that
     also holds all the retry/watchdog/tiering/classification logic. A
     third party renaming one field shouldn't force a diff against that
     file at all.

=============================================================================
WHAT THIS FILE OWNS
=============================================================================
The COMPLETE run_input payload for every actor call in the pipeline --
Google Maps (compass/crawler-google-places), Instagram
(apify/instagram-scraper), and Facebook Ads Library
(curious_coder/facebook-ads-library-scraper) -- with NO distinction between
"fields that change every run" and "fields that basically never change".
Both categories live here. A cost-control flag like `includeWebResults`
is just as much at risk of being renamed by Apify's devs as
`categoryFilterWords` is, so it gets the same protection: change it here,
not by hunting through pipeline code.

The concrete line drawn, if you're wondering whether some future new
constant belongs here: does this value describe the SHAPE OF A REQUEST TO
(or a RESPONSE FIELD FROM) a specific third-party Apify actor -- something
that actor's own maintainers could rename, restructure, or remove without
warning? If yes, it belongs in this file, regardless of how rarely you'd
actually touch it. If it's this pipeline's OWN vocabulary instead --
something that only ever grows over time and is never at risk of being
renamed out from under you by someone else (a junk-domain keyword list, an
NGO-detection keyword list) -- it stays in main_apify.py next to the
logic that uses it. That's why AD_ID_FIELDS lives here (it's a list of
field names from the ADS actor's OWN response shape -- the same
upstream-could-rename-this risk as an input field, just on the response
side) while SKIP_WEBSITE_KEYWORDS and IG_BLOCK_KEYWORDS do not (they're
this project's own accumulated filtering vocabulary).

=============================================================================
WHAT THIS FILE IS NOT
=============================================================================
This is a PURE-DATA MODULE BY CONVENTION, not by any language-enforced
guarantee. Python has no true "data-only, cannot execute code" file mode --
nothing stops a future edit from adding an import or a function call up
here. The two validation functions below are the one deliberate exception,
and even they only ever check this file's OWN values against each other
(do LOCATION_QUERY and CUSTOM_GEOLOCATION conflict? does this polygon's
data actually make sense?) -- they never touch Playwright, the Apify
client, the CSV writer, or any other pipeline behavior. That's the
distinction that matters: config self-consistency checks are still "just
data" in spirit, even though they're technically functions. Anything that
touches the REST of the pipeline's behavior belongs in
main_apify.py, which is meant to shrink to pure orchestration --
read these dicts, call the actors, run the classification/tiering logic,
write the CSV, nothing else.

=============================================================================
WHAT STAYS OUTSIDE THIS FILE, AND WHY
=============================================================================
  - `.env` still exists, narrowed to hold ONLY `APIFY_API_TOKEN`. That's
    the one genuinely secret, per-machine value that must never be
    committed to git -- everything else that used to live in `.env` for
    per-run configuration now lives here instead, as real Python values.
    This file has no secrets in it, so unlike `.env` it's meant to be
    committed to git as-is, in full -- no `config.example.py` needed.

  - main_apify.py keeps `SKIP_WEBSITE_KEYWORDS` and
    `IG_BLOCK_KEYWORDS` (this pipeline's own filtering vocabulary -- see
    the line-drawing rule above), and `OUTPUT_EXT` (changing the output
    file's extension isn't a config change, it's a code change -- the CSV
    writer would have to become a different writer entirely, so exposing
    it as if it were freely configurable here would be misleading).

=============================================================================
A NOTE ON LIST-SHAPED VALUES (why they're plain Python lists, not .env
strings, and why that's not going back)
=============================================================================
`SEARCH_KEYWORDS` and `CATEGORY_FILTER_WORDS` are plain Python lists here
specifically because their previous home (`.env`, semicolon-delimited
strings) exists as a direct historical fix for a real, confirmed bug: an
earlier comma-delimited version collided with python-dotenv's OWN
quote-parsing and silently dropped or corrupted a real category value
("golf, mini") -- full details in implementation_plan_2.md's Item 16
history. If a future edit is ever tempted to move a list-shaped value back
into `.env` "for consistency" or "to keep everything in one env file,"
that failure mode resurfaces immediately with no memory of why it was
avoided the first time. It isn't being avoided here because `.env` is bad
in general -- it's avoided because `.env` cannot natively hold a list, and
every attempt to make it do so adds a parsing layer that is itself a
source of bugs. A Python list literal doesn't have this problem: it
already is a list, with no delimiter or escaping scheme standing between
the value you type and the value the actor receives.

=============================================================================
HOW main_apify.py IS EXPECTED TO CONSUME THIS FILE
=============================================================================
  - `import config` near its other imports. Importing this module IS the
    validation step -- if LOCATION_QUERY/CUSTOM_GEOLOCATION conflict, or
    CUSTOM_GEOLOCATION is structurally broken, the import itself raises
    a `ConfigError` with a clear message, before Step 1 ever spends an
    Apify credit. There is no second step to remember to call, and no
    message-passing protocol between this file and the orchestration
    script -- `import config` succeeding IS the config being valid.

  - Every *_RUN_INPUT dict below should be copied (`dict(config.X)`)
    before use, never mutated in place, since these are module-level
    values shared across the whole process.

  - `INSTAGRAM_RUN_INPUT` and `ADS_RUN_INPUT` deliberately do NOT include
    `directUrls` / `urls` -- those are DISCOVERED DATA (the Instagram URLs
    Playwright actually found this run; the specific business's Facebook
    link), not configuration, and get merged into a copy of the relevant
    dict at call time:
        run_input = dict(config.INSTAGRAM_RUN_INPUT)
        run_input["directUrls"] = instagram_urls_batch
    and similarly for ADS_RUN_INPUT + "urls".

  - `LOCATION_QUERY` / `CUSTOM_GEOLOCATION` and `CATEGORY_FILTER_WORDS`
    are exposed as standalone values (not pre-merged into
    `MAPS_RUN_INPUT`) because whether/how they get added to the actual
    request is a per-call construction decision, not configuration data
    itself -- exactly one of `locationQuery`/`customGeolocation` must be
    chosen, and `categoryFilterWords` is meant to be OMITTED entirely
    (not sent as an empty list) when unset. That decision-making stays in
    main_apify.py's Step 1, same as before this redesign -- this
    file only guarantees the two location fields aren't BOTH set or BOTH
    unset when you're done editing it.

  - Construct the health circuit breaker with
    `health_monitor.PipelineHealthMonitor(**config.HEALTH_MONITOR_KWARGS)`.
    The keys intentionally mirror that constructor's public parameters, so
    their meaning is visible here and no thresholds are hidden in pipeline
    orchestration code.

=============================================================================
SWITCHING LOCATION MODE (locationQuery vs. customGeolocation)
=============================================================================
Exactly one of LOCATION_QUERY / CUSTOM_GEOLOCATION must be set below --
both filled in, or both left as None/empty, is a configuration error and
`import config` will refuse to proceed.

The actual polygon coordinates live in TARGET_MULTIPOLYGON, which is
ALWAYS defined (never commented out) regardless of which mode is active --
this is deliberate, so going back and forth between the two modes across
different runs is a two-line edit, not a comment/uncomment-a-whole-block
edit every time:

    LOCATION_QUERY: Optional[str] = None
    CUSTOM_GEOLOCATION: Optional[dict[str, Any]] = TARGET_MULTIPOLYGON

To switch back to plain location-query targeting, just reverse it:

    LOCATION_QUERY: Optional[str] = "New York, US"
    CUSTOM_GEOLOCATION: Optional[dict[str, Any]] = None

TARGET_MULTIPOLYGON's structure is validated unconditionally at import
time regardless of which mode is currently active, so a coordinate mistake
surfaces the moment you save this file -- not only once you actually
switch CUSTOM_GEOLOCATION over to use it.
"""

from typing import Any, Optional, TypedDict


class ConfigError(Exception):
    """Raised when this file's own values are internally inconsistent
    (e.g. LOCATION_QUERY and CUSTOM_GEOLOCATION both set, or a polygon
    that doesn't structurally close). A plain Exception subclass rather
    than sys.exit() -- a validation failure is still fatal for a real
    pipeline run (import config unconditionally validates at import
    time, same as before), but sys.exit() kills the interpreter itself,
    which makes the failure path untestable: a test can't assert "bad
    config raises X" against a call that terminates the process. A test
    (or any other caller) can now do `pytest.raises(config.ConfigError)`
    instead. Uncaught, this still surfaces as a normal traceback ending
    in a readable `config.ConfigError: <message>` line -- no behavior
    is lost for the plain "just run the script" case."""

# =====================================================================
# GOOGLE MAPS (compass/crawler-google-places)
# =====================================================================

# ---- Search keywords ---------------------------------------------------
# What businesses to search for. Exposed as its own name (not just inlined
# into MAPS_RUN_INPUT below) so it's easy to find and so other code/tests
# can reference config.SEARCH_KEYWORDS directly if needed.
SEARCH_KEYWORDS: list[str] = [
    "tennis academy",
    "cricket academy",
    "football academy",
    "martial arts",
    "golf"
]

# ---- Location targeting (EXACTLY ONE of these two must be set) --------
# LOCATION_QUERY: a plain place name Compass understands (city, state,
# country, etc.). Set this OR CUSTOM_GEOLOCATION below, not both.
LOCATION_QUERY: Optional[str] = "Cambridge, UK"

# Human-readable, index-matched descriptions of TARGET_MULTIPOLYGON's
# polygons. These labels appear in a session snapshot only when this exact
# object is the active custom geolocation; they never affect Apify input or
# drift decisions. Add, remove, or reorder labels whenever the polygons
# below change so each entry continues to describe the polygon at its index.
TARGET_MULTIPOLYGON_LABELS: list[str] = [
    "Cambridge area",
    
]

'''
# TARGET_MULTIPOLYGON: the actual polygon structure, always defined here
# regardless of which location mode is currently active -- so switching
# modes is a one-line toggle (see CUSTOM_GEOLOCATION below) instead of
# commenting/uncommenting a whole multi-line dict every time you want to
# go back and forth between locationQuery and customGeolocation targeting.
# Placeholder bounding boxes for the SF and LA metro areas below -- NOT
# real target coordinates, replace with your actual polygon(s). Coordinate
# order is [longitude, latitude] -- reversed from how the Google Maps UI
# normally displays it -- and each ring must close (first and last
# coordinate pairs identical). Draw one at https://geojson.io (Apify's own
# docs recommend this tool) and paste the coordinates array in below.
# Structurally validated unconditionally at the bottom of this file,
# regardless of whether it's the currently active mode, so a coordinate
# mistake surfaces the moment you save this file rather than only once you
# switch CUSTOM_GEOLOCATION over to actually use it.
'''
TARGET_MULTIPOLYGON: dict[str, Any] = {
    "type": "MultiPolygon",
    "coordinates": [
        # Coordinates 1 (London area)
        [[
           # Currently Empty as Location Query is used.
        ]],
        # Coordinates 2 (...)
    ]
}

# CUSTOM_GEOLOCATION: whichever polygon structure is ACTUALLY active this
# run -- None by default. To switch to polygon-based targeting:
# set LOCATION_QUERY above to None, and
# set this to TARGET_MULTIPOLYGON. That's the whole switch -- two
# single-line edits, no need to touch the polygon structure itself:
#
#   LOCATION_QUERY: Optional[str] = None
#   CUSTOM_GEOLOCATION: Optional[dict[str, Any]] = TARGET_MULTIPOLYGON

CUSTOM_GEOLOCATION: Optional[dict[str, Any]] = None

# ---- Category filter words ---------------------------------------------
'''
# categoryFilterWords entries are CASE-SENSITIVE against Google's own
# category taxonomy (confirmed via implementation_plan_2.md's Item 13
# research) -- double-check case when adding new entries here. Empty list
# is valid; main_apify.py omits the categoryFilterWords key
# entirely rather than sending an empty list when this is empty.
'''
CATEGORY_FILTER_WORDS: list[str] = [
    "sports club", "sports complex", "sports school", "athletic club", "training center",
    "coaching center", "martial arts club", "martial arts school", "golf club" , "golf course",
    "golf instructor", "indoor golf course", "miniature golf course", "private golf course", "volleyball club", 
    "volleyball instructor", "beach volleyball club", "football club", "karate club", "karate school", 
    "taekwondo school", "jujitsu school", "boxing club", "boxing gym", "kickboxing school", 
    "muay thai boxing gym", "judo club", "judo school", "sports activity location", "aerobics instructor", 
    "ice skating instructor", "personal trainer", "training centre", "training provider", "swimming competition", 
    "swimming facility", "baby swimming school", "indoor swimming pool", "tennis club", "tennis instructor", 
    "table tennis club", "table tennis facility", "racquetball club", "badminton club", "badminton complex", 
    "squash club", "soccer club", "basketball club", "baseball club", "softball club", 
    "rugby club", "rugby league club", "ice hockey club", "ice skating club", "curling club", 
    "rowing club", "soccer practice", "rugby", "cricket club", "football field"
]

# ---- Full Maps run_input base -------------------------------------------
'''
# Everything else Compass's input schema accepts -- static cost-control
# flags included, per this file's "everything, all actors" scope decision.
# main_apify.py starts from a COPY of this dict, then conditionally
# adds locationQuery/customGeolocation and categoryFilterWords (see the
# module docstring above for why those three stay separate from this
# dict rather than being pre-merged in here).
'''
MAPS_RUN_INPUT: dict[str, Any] = {
    "searchStringsArray": SEARCH_KEYWORDS,
    #"maxCrawledPlacesPerSearch": 100,
    # Cost control -- all enrichment add-ons disabled
    "includeWebResults":              False,
    "maximumLeadsEnrichmentRecords":  0,
    "scrapeContacts":                 False,
    "scrapeDirectories":              False,
    "scrapeImageAuthors":             False,
    "scrapeOrderOnline":              False,
    "scrapePlaceDetailPage":          False,
    "scrapeReviewsPersonalData":      False,
    "scrapeTableReservationProvider": False,
    "scrapeSocialMediaProfiles": {
        "facebooks": False, "instagrams": False,
        "tiktoks":   False, "twitters":   False, "youtubes": False,
    },
}


# =====================================================================
# INSTAGRAM (apify/instagram-scraper)
# =====================================================================
# "directUrls" is intentionally NOT included here -- it's discovered data
# (the Instagram URLs Playwright found this run), not configuration. See
# the module docstring's "HOW main_apify.py IS EXPECTED TO CONSUME
# THIS FILE" section for the exact merge pattern.
INSTAGRAM_RUN_INPUT: dict[str, Any] = {
    "resultsType":  "details",
    # NOTE (2026-07-22 bugfix): "resultsLimit" here is a placeholder, NOT
    # the real value used at runtime. For apify/instagram-scraper in
    # "details" mode with a multi-URL directUrls batch, resultsLimit caps
    # the number of PROFILES returned in the whole dataset -- it is NOT
    # "posts per profile" (that's latestPosts, which the actor returns up
    # to 12 of per profile regardless of this field). A fixed value of 1
    # here previously caused only 1 profile out of an entire batch to ever
    # come back, with every other profile silently vanishing into Item
    # 8b's "missing from batch response" catch-all. main_apify.py
    # overrides this at call time to len(instagram_urls_batch), since the
    # correct value is derived from discovered data (how many URLs this
    # run actually found), not a fixed config constant. Left at 1 here
    # only so the key exists as a documented placeholder for standalone/
    # single-URL testing -- never rely on this literal value in the real
    # pipeline run.
    "resultsLimit": 1,
}


# =====================================================================
# FACEBOOK ADS LIBRARY (curious_coder/facebook-ads-library-scraper)
# =====================================================================
# "urls" is intentionally NOT included here -- it's the specific
# business's Facebook URL, discovered per-record, not configuration. Same
# merge pattern as INSTAGRAM_RUN_INPUT's "directUrls" above.
ADS_RUN_INPUT: dict[str, Any] = {
    "limitPerSource":             1,
    "scrapePageAds.activeStatus": "active",
    "scrapePageAds.countryCode":  "US",
}


# =====================================================================
# NON-run_input CONSTANTS
#   Third-party-shaped values that live here even though they aren't
#   literally inside a run_input dict -- see the module docstring's line-
#   drawing rule. SKIP_WEBSITE_KEYWORDS / IG_BLOCK_KEYWORDS / OUTPUT_EXT
#   are deliberately NOT here -- see "WHAT STAYS OUTSIDE THIS FILE" above.
# =====================================================================

# Confirmed real ad-ID field names from live curious_coder datasets
# (2026-06-30) -- this is the Ads actor's OWN response shape, the same
# upstream-could-rename-this risk as an input field, just on the response
# side.
AD_ID_FIELDS: list[str] = ["ad_archive_id", "adArchiveID", "adId", "id"]

# Recycle the Playwright browser every N sites processed. A per-machine/
# per-run tunable, not third-party-shaped, but the same "small number
# you'll plausibly want to change without touching pipeline code"
# category the rest of this file exists to serve.
SESSION_BATCH_LIMIT: int = 10

class HealthMonitorKwargs(TypedDict):
    ram_enabled: bool
    ram_threshold_percent: float
    cpu_enabled: bool
    cpu_threshold_percent: float
    cpu_sustained_seconds: float


# Local-machine health circuit breaker. These are pipeline safety settings,
# not Apify actor input, so they are intentionally separate from the three
# *_RUN_INPUT dictionaries above. Keep the resource detectors opt-in until
# their thresholds have been confirmed for the machine running the pipeline.
#
# Consumption:
#   health_monitor.PipelineHealthMonitor(**HEALTH_MONITOR_KWARGS)
HEALTH_MONITOR_KWARGS: HealthMonitorKwargs = {
    "ram_enabled": False,
    "ram_threshold_percent": 90.0,
    "cpu_enabled": False,
    "cpu_threshold_percent": 95.0,
    "cpu_sustained_seconds": 8.0,
}

# Output CSV filename base -- auto-increments if the file already exists
# (filtered_sports_academies.csv -> filtered_sports_academies(1).csv,
# etc.). OUTPUT_EXT stays in main_apify.py -- see module docstring.
OUTPUT_BASE_NAME: str = "london_sports_academies"


# =====================================================================
# SELF-VALIDATION -- runs automatically at import time (see module
# docstring's "HOW main_apify.py IS EXPECTED TO CONSUME THIS
# FILE"). Config self-consistency checks only -- neither function below
# touches Playwright, the Apify client, or any pipeline behavior; they
# only check this file's own values against each other.
# =====================================================================
def _validate_health_monitor_config(settings: HealthMonitorKwargs) -> None:
    """Keep health-monitor constructor arguments well-formed before a run
    starts. The values are deliberately supplied as a single kwargs-shaped
    mapping so the pipeline can pass them without translating names."""
    expected_keys = {
        "ram_enabled", "ram_threshold_percent", "cpu_enabled",
        "cpu_threshold_percent", "cpu_sustained_seconds",
    }
    if set(settings) != expected_keys:
        raise ConfigError(
            "❌ config.py: HEALTH_MONITOR_KWARGS must contain exactly: "
            + ", ".join(sorted(expected_keys))
        )

    for key in ("ram_enabled", "cpu_enabled"):
        if not isinstance(settings[key], bool):
            raise ConfigError(f"❌ config.py: HEALTH_MONITOR_KWARGS[{key!r}] must be True or False.")

    for key in ("ram_threshold_percent", "cpu_threshold_percent"):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 100:
            raise ConfigError(
                f"❌ config.py: HEALTH_MONITOR_KWARGS[{key!r}] must be a number in (0, 100]."
            )

    sustained_seconds = settings["cpu_sustained_seconds"]
    if (isinstance(sustained_seconds, bool)
            or not isinstance(sustained_seconds, (int, float))
            or sustained_seconds <= 0):
        raise ConfigError(
            "❌ config.py: HEALTH_MONITOR_KWARGS['cpu_sustained_seconds'] must be greater than zero."
        )


def _validate_location_config(
    location_query: Optional[str], custom_geolocation: Optional[dict[str, Any]]
) -> None:
    """Enforce the exactly-one-of-two rule between LOCATION_QUERY and
    CUSTOM_GEOLOCATION -- both set or both unset are configuration errors,
    rather than silently guessing which one should win. Carried over
    from implementation_plan_2.md's Item 17 -- the source of the two
    values changed (module-level Python values here, instead of parsed
    .env strings / a loaded JSON file), and this now takes them as
    parameters rather than reading module globals directly, matching
    _validate_custom_geolocation's existing shape below -- so a test (or
    anything else) can call `_validate_location_config("NYC", None)`
    directly with arbitrary values, with no need to monkeypatch this
    module's globals first."""
    has_location = bool(location_query and location_query.strip())
    has_geo = custom_geolocation is not None

    if has_location and has_geo:
        raise ConfigError(
            "❌ config.py: both LOCATION_QUERY and CUSTOM_GEOLOCATION are set -- "
            "set exactly one, not both, so the script doesn't have to silently "
            "guess which one should win this run. Set the one you're not using "
            "to None."
        )
    if not has_location and not has_geo:
        raise ConfigError(
            "❌ config.py: neither LOCATION_QUERY nor CUSTOM_GEOLOCATION is set -- "
            "set exactly one to tell the Google Maps actor where to search."
        )


def _validate_custom_geolocation(geo: dict[str, Any], label: str) -> None:
    """Lightweight structural sanity check for a GeoJSON Polygon/
    MultiPolygon dict -- the same checks implementation_plan_2.md's Item
    17 used to run against a loaded GeoJSON file, now operating on an
    in-memory dict literal instead since target_polygon.json is retired.
    NOT a full GeoJSON validator -- Apify's own actor performs true
    validation on submission; this is a fast local pre-check catching the
    two most common hand-editing mistakes before spending a run:
      (a) correct top-level "type" (Polygon or MultiPolygon)
      (b) every ring closes (first and last coordinate pairs identical)
      (c) every coordinate pair falls within valid [longitude, latitude]
          numeric ranges -- catches a lat/lng-order swap as a side effect,
          since a swapped pair is very likely to fall outside valid range
          for most real-world non-equatorial/non-prime-meridian locations.
    `label` is the variable name to use in error messages (e.g.
    "TARGET_MULTIPOLYGON" or "CUSTOM_GEOLOCATION") -- this function is
    called against both, and a message that always said "CUSTOM_GEOLOCATION"
    would be actively misleading when the actual problem is in
    TARGET_MULTIPOLYGON while CUSTOM_GEOLOCATION itself is still None
    (LOCATION_QUERY active instead)."""
    geo_type = geo.get("type") if isinstance(geo, dict) else None
    if geo_type not in ("Polygon", "MultiPolygon"):
        raise ConfigError(
            f"❌ config.py: {label} has an invalid \"type\": "
            f"{geo_type!r} -- must be \"Polygon\" or \"MultiPolygon\"."
        )

    coordinates = geo.get("coordinates")
    if not isinstance(coordinates, list) or not coordinates:
        raise ConfigError(f"❌ config.py: {label} has missing or empty \"coordinates\".")

    # Normalize nesting depth so the same validation loop covers both
    # shapes: MultiPolygon's coordinates is already a list of polygons
    # (each a list of rings); Polygon's coordinates IS a single polygon
    # (a list of rings), so wrap it once to match.
    polygons: list[Any] = coordinates if geo_type == "MultiPolygon" else [coordinates]

    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            raise ConfigError(f"❌ config.py: {label} contains an empty polygon.")
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 4:
                raise ConfigError(
                    f"❌ config.py: {label} has a ring with fewer than 4 "
                    f"coordinate pairs -- a closed ring needs at least 3 unique "
                    f"points plus the closing repeat."
                )
            if ring[0] != ring[-1]:
                raise ConfigError(
                    f"❌ config.py: {label} has a ring that doesn't "
                    f"close -- first coordinate pair {ring[0]} must match the "
                    f"last {ring[-1]}."
                )
            for pair in ring:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ConfigError(
                        f"❌ config.py: {label} has a malformed "
                        f"coordinate pair: {pair!r} -- expected [longitude, latitude]."
                    )
                lng, lat = pair
                if (not isinstance(lng, (int, float)) or not isinstance(lat, (int, float))
                        or not (-180 <= lng <= 180) or not (-90 <= lat <= 90)):
                    raise ConfigError(
                        f"❌ config.py: {label} has an out-of-range "
                        f"coordinate pair {pair!r} (expected [longitude, latitude] "
                        f"with lng in [-180, 180] and lat in [-90, 90]) -- check "
                        f"you haven't swapped latitude and longitude."
                )


def _validate_target_multipolygon_labels(
    geo: dict[str, Any], labels: list[str],
) -> None:
    """Ensure session-snapshot labels stay aligned with their polygons."""
    coordinates = geo["coordinates"]
    polygon_count = len(coordinates) if geo["type"] == "MultiPolygon" else 1
    if len(labels) != polygon_count:
        raise ConfigError(
            "❌ config.py: TARGET_MULTIPOLYGON_LABELS must contain exactly "
            f"{polygon_count} label(s), one for each TARGET_MULTIPOLYGON polygon."
        )
    if any(not isinstance(label, str) or not label.strip() for label in labels):
        raise ConfigError(
            "❌ config.py: every TARGET_MULTIPOLYGON_LABELS entry must be a non-empty string."
        )


# Runs unconditionally at import time -- `import config` succeeding IS the
# validation passing. No separate step for main_apify.py to
# remember to call, no message-passing protocol between the two files.
#
# TARGET_MULTIPOLYGON is validated unconditionally, regardless of which
# location mode is currently active, so a coordinate mistake surfaces the
# moment this file is saved/imported -- not only once CUSTOM_GEOLOCATION
# is actually switched over to use it.
_validate_custom_geolocation(TARGET_MULTIPOLYGON, label="TARGET_MULTIPOLYGON")
_validate_target_multipolygon_labels(TARGET_MULTIPOLYGON, TARGET_MULTIPOLYGON_LABELS)

_validate_health_monitor_config(HEALTH_MONITOR_KWARGS)

_validate_location_config(LOCATION_QUERY, CUSTOM_GEOLOCATION)
if CUSTOM_GEOLOCATION is not None:
    _validate_custom_geolocation(CUSTOM_GEOLOCATION, label="CUSTOM_GEOLOCATION")


# =====================================================================
# Standalone sanity check -- `python config.py` re-runs the exact same
# import-time validation above (nothing special needs to happen here for
# that) and, if it didn't already exit, confirms the active configuration
# in plain language. Lets anyone (a new contributor, future-Shiv on a new
# machine) verify their config is valid without needing apify_client,
# playwright, or any other pipeline dependency installed at all -- this
# file has none.
# =====================================================================
if __name__ == "__main__":
    print("✅ config.py loaded and validated successfully.\n")
    print(f"Search keywords       : {SEARCH_KEYWORDS}")
    if LOCATION_QUERY:
        print(f"Location mode         : locationQuery = {LOCATION_QUERY!r}")
    else:
        assert CUSTOM_GEOLOCATION is not None
        print(f"Location mode         : customGeolocation ({CUSTOM_GEOLOCATION['type']}, "
              f"{len(CUSTOM_GEOLOCATION['coordinates'])} polygon(s))")
    is_active = CUSTOM_GEOLOCATION is TARGET_MULTIPOLYGON
    print(f"TARGET_MULTIPOLYGON   : structurally valid, {len(TARGET_MULTIPOLYGON['coordinates'])} "
          f"polygon(s) ({'currently ACTIVE' if is_active else 'defined but not active -- LOCATION_QUERY is active instead'})")
    print(f"Polygon labels        : {TARGET_MULTIPOLYGON_LABELS}")
    print(f"Category filter words : {CATEGORY_FILTER_WORDS or '(none -- categoryFilterWords will be omitted)'}")
    print(f"Session batch limit   : {SESSION_BATCH_LIMIT}")
    print(f"Health monitor config : {HEALTH_MONITOR_KWARGS}")
    print(f"Output base name      : {OUTPUT_BASE_NAME}")
    print(f"Ad ID fields          : {AD_ID_FIELDS}")
