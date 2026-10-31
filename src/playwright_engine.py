"""
playwright_engine.py

Playwright execution engine -- everything about actually driving a browser
against a business website lives here, separate from main_apify.py's
own pipeline shape (Maps discovery -> [this] -> Instagram batch -> Ads
check -> CSV export). Nothing in this file knows what a "sports academy
lead" is; it only knows how to find Instagram/Facebook links on a website.

CONTAINS
--------
- Anti-bot browser session setup (UA/viewport profiles, headers, init script)
- Resource-blocking router
- Scoped multi-selector social-link scanning (footer/header/nav/role/class)
- Sitemap discovery (sitemap.xml / sitemap_index.xml / wp-sitemap.xml / ...)
- Jittered scroll, with round/time logging
- A two-tier hard watchdog (PlaywrightWorker) that can actually recover
  from a wedged page.goto() -- Playwright's sync API only works from the
  thread that created it, so the whole browser lifecycle lives inside one
  persistent worker thread. On a real timeout we can't preempt that
  thread, so we force-kill the leaked process tree via psutil and replace
  the worker entirely.
- resolve_social_links(worker, website) -- the single entry point
  main_apify.py calls; runs the full fallback chain and returns a
  plain dict ready to merge into a record.

FALLBACK ORDER (confirmed 2026-07-11 -- guessed-path fully retired, not
just gated)
------------------------------------------------------------------------
  1. Homepage, scoped scan (footer/header/nav/role/class, all in one pass)
  2. Sitemap discovery -- matched urlset entries are tested, stopping at
     the first hit.
  3. Homepage section-text candidates (same-domain links whose visible
     text suggests contact/about/team) -- tried regardless of the sitemap
     outcome, since these were already extracted from the homepage visit
     at zero extra discovery cost. This is now the true last resort.

Guessed-path (/contact, /about, etc.) is gone entirely, not just
deprioritized -- section-text strictly dominates it. Where guessed-path
might have accidentally hit, section-text already covers that same ground
by reading the page's actual navigation instead of hoping a common slug
matches; where guessed-path was just going to 404 (the common case in the
2026-07-10 test run), section-text was never going to hit either since
there's genuinely nothing there to point to. No condition under which
guessed-path could find something section-text wouldn't.

NOTE: this also differs from sitemap_fallback_test.py's behavior on
purpose. That script deliberately tested *every* matched urlset slot (up
to 10) even after an early hit, purely to gather hit-rate research data.
This engine stops at the first success -- it's production code, not a
data-gathering exercise.

CONFIRMED BUG FIXES IN THIS VERSION (from the 2026-07-10 test run)
------------------------------------------------------------------
- Facebook `profile.php?id=...` links were being destroyed by an
  indiscriminate `.split('?')[0]` -- that URL shape has no identifier
  *except* in the query string. Now handled explicitly.
- A bare `facebook.com` domain link (no page path, no profile id) was
  being accepted as a valid match. Now rejected.

2026-07-12 ADDITIONS (from real-execution findings, not synthetic tests)
--------------------------------------------------------------------------
Running the integrated pipeline against real, live sites (not just the
standalone test script) surfaced failure modes only real data could show:

1. Sitemap size cap (MAX_SITEMAP_TOTAL_LOCS = 150). A real run against
   pypi.org's sitemap (22,564 URLs) proved that raw substring keyword
   matching against a large, unrelated sitemap produces confident-looking
   but meaningless matches (e.g. a package named "...-connect-db" matching
   the "connect" keyword). Manual review of real target sites puts normal
   usage at 30-50 urlsets; 150 is a safe margin above that, not the
   observed ceiling. Above it, urlset testing is skipped entirely in favor
   of section-text -- see MAX_SITEMAP_TOTAL_LOCS usage in
   resolve_social_links().

2. Input-URL subpath trust check (_should_trust_domain_sitemap). Some
   businesses -- franchisees, partners -- are Maps-listed under a path on
   a LARGER site (e.g. mainexample.com/partner) rather than their own
   domain. Naively requesting mainexample.com/partner/sitemap.xml either
   404s (no per-subpath sitemaps exist) or, worse, silently returns the
   PARENT site's sitemap -- answering for the wrong business entirely.
   Conversely, some legitimate single-business sites are just listed with
   a path anyway (example.com/about, example.com/home) for no deeper
   reason than the owner filled in Maps that way -- these should still get
   normal domain-root sitemap treatment. The function distinguishes the
   two: a single recognized path segment (checked via SECTION_KEYWORDS,
   first hyphen/underscore-split word only) keeps sitemap trust; anything
   else skips sitemap discovery entirely for that site.

3. discover_sitemap_urlsets() now always builds its request against the
   domain root (scheme + netloc), never the input URL's own path -- a
   necessary companion fix to (2), since sitemaps live at the domain root
   by convention regardless of what path a specific business page sits
   under.

4. Cross-stage URL deduplication (visited_urls set in
   resolve_social_links). Confirmed wasteful in the 2026-07-10 test run:
   alliogolf.com's /about page got visited twice -- once as a section-text
   candidate, once again as a guessed-path attempt -- for an identical,
   already-known result. Now any URL visited once in a given
   resolve_social_links() call (homepage, a sitemap slot, a section-text
   candidate) is tracked and skipped if it would be visited again.

5. "home" added to SECTION_KEYWORDS, covering Maps-listed URLs like
   example.com/home or example.com/us that don't literally say "about" or
   "contact" but are still clearly a single legitimate page name.

6. _describe_visit_failure() fixes a minor but real logging bug: a failed
   visit's log line previously read "-> HTTP None" whenever the failure
   was a genuine exception (network error, nav timeout) rather than a
   clean HTTP status, since http_status is None in that case and the old
   code never checked for a message in the "error" field instead.


"""

import logging
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, Optional, TypedDict
from urllib.parse import urlparse, parse_qs

import requests

try:
    import psutil  # type: ignore[import-not-found]
except ImportError:
    psutil = None  # type: ignore[assignment]

try:
    import playwright.sync_api as playwright_sync_api  # type: ignore[import-not-found]
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # type: ignore[import-not-found]

    sync_playwright = playwright_sync_api.sync_playwright
except Exception:  # pragma: no cover - fallback for environments without Playwright
    def sync_playwright() -> Any:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install playwright && playwright install chromium"
        )

    class PlaywrightTimeoutError(Exception):  # type: ignore[no-redef]
        pass

logger = logging.getLogger(__name__)


# =====================================================================
# CONSTANTS -- identical to main_apify.py where they overlap, so
# behavior doesn't silently drift between the two files. If you change
# FB_SKIP_PATHS / IG_SKIP_PATHS / USER_AGENT_PROFILES here, mirror it there
# until the main script is fully switched over to importing from this file.
# =====================================================================
BLOCK_DOMAINS = [
    "google-analytics.com", "googletagmanager.com", "facebook.net",
    "vimeo.com", "youtube.com", "maps.googleapis.com",
]
BLOCK_EXTENSIONS = re.compile(
    r"\.(png|jpg|jpeg|gif|webp|svg|ico|mp4|webm|avi|mov|mp3|ogg|wav|woff|woff2|ttf|otf|css|scss|pdf)$",
    re.IGNORECASE,
)

FB_SKIP_PATH_PREFIXES = ["/tr", "/sharer", "/share", "/plugins", "/policy", "/policies",
                          "/login", "/dialog", "/legal", "/help"]
FB_SKIP_SUBSTRINGS    = ["/wix", "/squarespace"]

IG_SKIP_PATH_PREFIXES = ["/p/", "/reel", "/tv/", "/stories/"]
IG_SKIP_SUBSTRINGS    = ["/embed", "/squarespace", "/wix"]


def _should_skip_social_href(href: str, path_prefixes: list[str], substrings: list[str]) -> bool:
    """Anchored path-prefix check for feature-page URLs + unanchored
    substring check for website-builder leftover links.  Fixes false-
    positives like '/tr' matching inside 'facebook.com/travissportsacademy'
    and '/help' matching inside 'facebook.com/helpfulgolf'."""
    href_lower = href.lower()
    if any(s in href_lower for s in substrings):
        return True
    try:
        path = urlparse(href).path.lower()
    except Exception:
        return False
    for p in path_prefixes:
        p_clean = p.rstrip("/")
        if path == p_clean or path.startswith(p_clean + "/"):
            return True
    return False


IG_SKIP_USERNAMES = ["accounts", "explore", "developer", "about", "legal", "wix", "squarespace", "blog", "linktree", "stories", "reel", "tv", "help", "press", "privacy", "terms"]

# Pre-filtering rules — junk/institutional domains skipped before the
# browser ever opens.  Moved here from main_apify.py so the main
# script stays a pure integration layer (implementation_plan_5 alignment).
# Dot-prefixed entries are TLD-anchored; non-dot entries are substring
# checks against the domain only (not the full URL).
SKIP_WEBSITE_KEYWORDS = [".gov", ".top", ".xyz", ".wtf", "ymca", "gotimmy", ".lol"]


def _is_skippable_website(raw_website: str, skip_keywords: Optional[list[str]] = None) -> bool:
    """Anchor dot-prefixed keywords to the actual TLD position
    (domain.endswith(kw)); leave non-dot-prefixed keywords as substring
    checks against the domain.  Fixes a confirmed false-positive from a
    real run: 'topshottennis.net' was wrongly skipped because '.top'
    matched the literal characters 't-o-p' inside the business name."""
    if skip_keywords is None:
        skip_keywords = SKIP_WEBSITE_KEYWORDS
    lowered = raw_website.lower()
    domain = urlparse(lowered if "://" in lowered else f"//{lowered}").netloc
    domain = domain.split(":")[0]  # strip a port if present

    for kw in skip_keywords:
        if kw.startswith("."):
            if domain.endswith(kw):
                return True
        else:
            if kw in domain:
                return True
    return False

ig_profile_regex = re.compile(r"instagram\.com/([a-zA-Z0-9_\.]+)")


class ViewportProfile(TypedDict):
    user_agent: str
    viewport: dict[str, int]


USER_AGENT_PROFILES: list[ViewportProfile] = [
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "viewport":   {"width": 1920, "height": 1080},
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "viewport":   {"width": 1366, "height": 768},
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "viewport":   {"width": 1536, "height": 864},
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0",
        "viewport":   {"width": 1440, "height": 900},
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "viewport":   {"width": 1600, "height": 900},
    },
]

# ---- Sitemap discovery ----
SITEMAP_PATHS = ["/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap/sitemap.xml"]
SITEMAP_REQUEST_TIMEOUT = 6      # plain requests.get(), no Playwright/watchdog needed here
MAX_SUB_SITEMAPS = 3             # cap when a sitemap_index references many child sitemaps
MAX_URLSET_SLOTS = 10            # ceiling on how many matched sitemap URLs we'll even consider

# Confirmed 2026-07-12 from manual review of real target sites (30-50
# urlsets typical, nothing close to 3 digits). 150 is a deliberately safe
# margin above that, not the observed ceiling itself. Above this, a
# sitemap is treated as belonging to an institutional/facility/franchisor
# -scale site rather than a single small business, and per-slot testing
# is skipped entirely in favor of section-text (see MAX_SITEMAP_TOTAL_LOCS
# usage in resolve_social_links).
MAX_SITEMAP_TOTAL_LOCS = 150

# Used both for sitemap <loc> slug filtering AND homepage section-link text
# matching -- both are SUBSTRING checks (`kw in path` / `kw in text`), so
# every entry here needs to be long/specific enough not to false-positive
# inside unrelated words. "home" added 2026-07-12 for the rare case where
# a business's own Google Maps-listed URL is itself a path like /home
# rather than a bare domain root.
SECTION_KEYWORDS = ["contact", "about", "reach", "connect", "find-us", "findus", "team", "social", "follow", "home"]

# Separate, narrower set used ONLY by _should_trust_domain_sitemap()'s
# exact-first-word check on a single isolated path segment -- never for
# substring matching. "us" belongs here and NOT in SECTION_KEYWORDS above:
# as a raw substring it would false-positive inside "just", "trust",
# "custom", "discuss", "focus", etc., but as an exact, isolated first-word
# match on a Maps-listed URL path (www.example.com/us/) it's safe and
# matches a real pattern seen in older business sites.
INPUT_PATH_TRUST_KEYWORDS = SECTION_KEYWORDS + ["us"]

# ---- Scoped selector scan ----
# Checked together in one pass, not sequentially -- link position varies
# enough between sites (nav vs footer vs neither) that a "check footer,
# then give up" approach misses real hits.
SCOPED_SELECTORS = [
    "footer a[href]", "header a[href]", "nav a[href]",
    '[role="navigation"] a[href]', '[role="contentinfo"] a[href]',
    '[class*="footer" i] a[href]', '[class*="nav" i] a[href]',
    '[class*="menu" i] a[href]', '[class*="social" i] a[href]',
    '[class*="follow" i] a[href]',
    "a[href]",  # catch-all for Tailwind/CSR sites; must stay LAST
]

NAVIGATIONAL_REGIONS = {
    "footer a[href]", "header a[href]", "nav a[href]",
    '[role="navigation"] a[href]', '[role="contentinfo"] a[href]',
    '[class*="footer" i] a[href]', '[class*="nav" i] a[href]',
    '[class*="menu" i] a[href]', '[class*="social" i] a[href]',
    '[class*="follow" i] a[href]',
}

SCOPED_SCAN_JS = """
(selectors) => {
    const seen = new Set();
    const results = [];
    for (const sel of selectors) {
        try {
            document.querySelectorAll(sel).forEach((a) => {
                const href = a.href;
                if (!href || seen.has(href)) return;
                seen.add(href);
                results.push({
                    href: href,
                    text: (a.innerText || a.textContent || '').trim(),
                    region: sel
                });
            });
        } catch (e) { /* invalid selector on this particular page -- skip it */ }
    }
    return results;
}
"""

MAX_SECTION_TEXT_CANDIDATES = 5

# ---- Watchdog ----
HOMEPAGE_WATCHDOG_SECONDS = 60   # covers goto() + social-link poll + adaptive wait + jitter scroll + scoped scan
FALLBACK_WATCHDOG_SECONDS = 25   # applied individually per sitemap/section/guessed-path visit


# =====================================================================
# Browser session helpers -- unchanged from main_apify.py
# =====================================================================
def structural_resource_router(route: Any) -> Any:
    """Abort heavy/tracking requests; continue everything else."""
    req = route.request
    url_lower = req.url.lower()
    if BLOCK_EXTENSIONS.search(url_lower):
        return route.abort()
    if req.resource_type in ["image", "media", "font", "stylesheet", "websocket", "eventsource"]:
        return route.abort()
    if any(domain in url_lower for domain in BLOCK_DOMAINS):
        return route.abort()
    return route.continue_()


def start_fresh_browser_session(playwright_instance: Any) -> tuple[Any, Any, str]:
    """Launch a new headless Chromium browser with anti-bot hardening.
    Picks one (UA, viewport) profile for the whole session -- used
    consistently for both the launch window size and the context viewport
    so there's no UA/viewport mismatch. The chosen user_agent is also
    returned (not just baked into the context) so the caller can keep any
    non-Playwright requests it makes on this site's behalf -- e.g. the
    sitemap fetch in discover_sitemap_urlsets() -- presenting the same
    identity as the browser instead of an independently re-rolled one."""
    profile: ViewportProfile = random.choice(USER_AGENT_PROFILES)
    viewport: dict[str, int] = profile["viewport"]

    _browser = playwright_instance.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage", "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-infobars", "--disable-gpu",
            f"--window-size={viewport['width']},{viewport['height']}",
        ],
    )
    _context = _browser.new_context(
        user_agent=profile["user_agent"],
        viewport=viewport,
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={
            "Accept-Language":           "en-US,en;q=0.9",
            "Accept-Encoding":           "gzip, deflate, br",
            "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "DNT":                       "1",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Site":            "none",
            "Sec-Fetch-Mode":            "navigate",
            "Sec-Fetch-Dest":            "document",
            "Sec-Fetch-User":            "?1",
        },
    )
    _context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver',  { get: () => undefined });
        Object.defineProperty(navigator, 'plugins',    { get: () => [1, 2, 3, 4, 5] });
        Object.defineProperty(navigator, 'languages',  { get: () => ['en-US', 'en'] });
        window.chrome = { runtime: {} };
    """)
    return _browser, _context, profile["user_agent"]


def safe_close_session(ctx: Optional[Any], brw: Optional[Any]) -> None:
    """Close context and browser, silently ignoring any errors."""
    if ctx is not None:
        try: ctx.close()
        except Exception: pass
    if brw is not None:
        try: brw.close()
        except Exception: pass


def is_context_dead(error: Exception) -> bool:
    """Return True if the error indicates the Playwright browser context has died."""
    msg = str(error).lower()
    return any(phrase in msg for phrase in [
        "browser context was destroyed", "target closed", "browser has been closed",
        "connection closed", "context was destroyed", "page was closed",
    ])


# =====================================================================
# Process-tree cleanup for the watchdog
# =====================================================================
def get_child_pids() -> set[int]:
    """Snapshot current child process PIDs of this script (best-effort)."""
    if psutil is None:
        return set()
    try:
        return {p.pid for p in psutil.Process(os.getpid()).children(recursive=True)}
    except Exception:
        return set()


def force_kill_new_children(baseline_pids: set[int]) -> None:
    """After a watchdog timeout, forcibly kill any child processes spawned
    since baseline_pids was captured. This is how we reclaim the Chromium
    process from a wedged Playwright call -- Python threads can't be
    preempted, so we go around them at the OS process level instead."""
    if psutil is None:
        logger.warning(
            "    ↳ ⚠️ psutil not installed -- cannot force-kill a wedged browser process. "
            "Leaked Chromium process(es) may still be running in the background. "
            "Install with: pip install psutil --break-system-packages"
        )
        return
    try:
        current = {p.pid for p in psutil.Process(os.getpid()).children(recursive=True)}
        for pid in (current - baseline_pids):
            try:
                proc = psutil.Process(pid)
                name = proc.name()
                proc.kill()
                logger.info(f"    ↳ 🔪 Force-killed leaked process PID {pid} ({name})")
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"    ↳ ⚠️ Force-kill cleanup failed: {e}")


# =====================================================================
# Jitter scroll -- same pattern as main_apify.py, with round/time
# logging added so the actual cost against the watchdog budget is visible.
#
# SCROLL_WALL_CLOCK_BUDGET_SECONDS is a hard ceiling across the *entire*
# jitter sequence (both outer and inner loops share one deadline),
# independent of round counts. Round caps alone aren't enough: on a page
# with genuinely infinite/continuously-lazy-loaded content (an endless
# photo gallery, an infinite-scroll feed -- not rare in this niche),
# scrollHeight legitimately never stabilizes, so every round runs to its
# ceiling -- worst case 4 outer rounds x 10 inner rounds x ~0.5s sleep is
# already ~20s, before page.goto() itself, wait_for_selector, and the
# networkidle fallback are even counted. That can exceed
# HOMEPAGE_WATCHDOG_SECONDS on its own, which means the *whole worker*
# gets force-killed and rebuilt for a page that was never actually wedged
# -- just scrolling normally. This budget makes scrolling degrade to
# "scrolled what we could, moving on" instead.
# =====================================================================
SCROLL_WALL_CLOCK_BUDGET_SECONDS = 15.0


def scroll_page_with_jitter(page: Any, deadline: float, max_rounds: int = 10) -> None:
    start = time.monotonic()
    last_height = 0
    rounds_used = 0
    hit_deadline = False

    for i in range(max_rounds):
        if time.monotonic() >= deadline:
            hit_deadline = True
            break
        rounds_used = i + 1
        page.evaluate("window.scrollBy(0, Math.floor(Math.random() * 300) + 200)")
        time.sleep(random.uniform(0.2, 0.5))
        new_height = page.evaluate("() => document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

    elapsed = time.monotonic() - start
    if hit_deadline:
        reason = " (hit wall-clock budget -- likely infinite-scroll page)"
    elif rounds_used == max_rounds:
        reason = " (hit max_rounds -- did not break early)"
    else:
        reason = " (broke early)"
    logger.info(f"    ↳ 🔃 [SCROLL] {rounds_used}/{max_rounds} rounds, {elapsed:.2f}s{reason}")


def run_jittery_scroll(page: Any) -> None:
    outer_rounds = random.randint(2, 4)
    outer_start = time.monotonic()
    deadline = outer_start + SCROLL_WALL_CLOCK_BUDGET_SECONDS
    logger.info(
        f"    ↳ 🔃 [SCROLL] Starting jitter sequence -- {outer_rounds} outer round(s) planned, "
        f"{SCROLL_WALL_CLOCK_BUDGET_SECONDS:.0f}s wall-clock budget"
    )
    for _ in range(outer_rounds):
        if time.monotonic() >= deadline:
            logger.info("    ↳ 🔃 [SCROLL] Wall-clock budget reached before all outer rounds ran -- stopping early")
            break
        scroll_page_with_jitter(page, deadline)
        time.sleep(random.uniform(0.3, 0.9))
    logger.info(f"    ↳ 🔃 [SCROLL] Jitter sequence complete -- total {time.monotonic() - outer_start:.2f}s")


# =====================================================================
# Small shared helpers -- dedup, failure logging, sitemap-trust
# classification. NOTE: Added 2026-07-12.
# =====================================================================
def _normalize_url_for_dedup(url: str) -> str:
    """Pragmatic, not fully RFC-3986-rigorous: strips any query string and
    trailing slash and lowercases the result, so trivial formatting
    differences don't let the same page get visited twice across stages.
    Matches the same href.split('?')[0].rstrip('/') pattern already used
    elsewhere in this file when extracting links."""
    return url.split('?')[0].rstrip('/').lower()


def _describe_visit_failure(visit_result: Optional[dict[str, Any]]) -> str:
    """Turn a failed playwright_visit_and_scan() result into a readable
    log fragment. Fixes a real bug from the 2026-07-11 test run: when a
    visit failed via a genuine exception (network error, nav timeout, not
    a clean HTTP response), http_status is None and the old code printed
    the uninformative 'HTTP None' instead of the actual error."""
    if not visit_result:
        return "No Response"
    if visit_result.get("error"):
        return f"Error: {visit_result['error']}"
    status = visit_result.get("http_status")
    return f"HTTP {status}" if status is not None else "No Response"


def _should_trust_domain_sitemap(website: str) -> bool:
    """Decide whether to attempt domain-root sitemap discovery for this
    specific input URL, based on its own path component (not the sitemap
    contents -- that's the separate MAX_SITEMAP_TOTAL_LOCS check).

    Sitemaps live at the domain root by convention, not per-subpath, so a
    Maps-listed URL with a path raises a real question: is this path (a)
    nothing -- a normal root-domain business site, (b) a recognized single
    page name the owner themselves put in their Maps listing (/about,
    /home, /us), where the domain-root sitemap is still genuinely theirs,
    or (c) an unrecognized subpath (/partner, /locations/123) -- most
    likely a franchisee or partner page nested under a larger site, where
    a domain-root sitemap would answer for the WRONG business entirely,
    not just 404.

    Only (a) and (b) return True. Case (c) returns False, and the caller
    skips sitemap discovery altogether rather than risk querying a
    franchisor/partner's unrelated sitemap. Deliberately conservative:
    only a single, recognized path segment earns trust -- anything with
    more than one segment, or a segment that doesn't start with a
    recognized word, falls to the untrusted case. Checking only the FIRST
    hyphen/underscore-split word (not any word anywhere in the segment) is
    a deliberate choice, not an oversight -- it's what lets 'about-us' and
    'home-page' match while still rejecting something like
    'yapo-bi-connect-db', where the keyword is buried mid-slug rather than
    naming the page itself. Uses INPUT_PATH_TRUST_KEYWORDS (SECTION_KEYWORDS
    plus 'us') rather than SECTION_KEYWORDS directly, since exact-word
    matching on an isolated segment is safe for a short word like 'us' in
    a way that substring matching elsewhere in this file is not."""
    parsed = urlparse(website)
    segments = [seg for seg in parsed.path.strip("/").split("/") if seg]

    if not segments:
        return True  # root path -- normal case

    if len(segments) == 1:
        first_word = re.split(r"[-_]", segments[0].lower())[0]
        if first_word in INPUT_PATH_TRUST_KEYWORDS:
            return True  # recognized page name on the business's own site

    return False  # unrecognized subpath


# =====================================================================
# Facebook URL extraction -- FIXED per 2026-07-11 test run findings
# =====================================================================
def extract_facebook_url(href: str) -> Optional[str]:
    """Facebook Page URLs come in two valid shapes:
      - path-based:  facebook.com/BusinessName
      - profile.php: facebook.com/profile.php?id=123456789  -- the id is
        the ONLY identifier here; a blanket `.split('?')[0]` (the old
        approach) destroys it, which is exactly what happened to
        endeavorvolleyballacademy.sportngin.com's link in the 2026-07-10
        test run (came out as bare 'profile.php', unusable).
    A bare domain with neither (just facebook.com or facebook.com/, like
    btrainedlegacy.com's footer link) isn't a page reference at all and is
    rejected rather than accepted as a false-positive match."""
    parsed = urlparse(href)
    path = parsed.path.rstrip('/')

    if path.lower() == "/profile.php":
        qs = parse_qs(parsed.query)
        page_id = qs.get("id", [None])[0]
        if not page_id:
            return None
        return f"https://www.facebook.com/profile.php?id={page_id}"

    if not path or path == "/":
        return None  # bare domain -- not a page reference

    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _is_same_domain(href: str, base_domain: str) -> bool:
    """Proper same-site check for section-text candidates -- compares the
    candidate link's own netloc against base_domain (exact match, or a
    subdomain of it), rather than a raw substring test. A substring test
    (`base_domain in href_lower`) would incorrectly treat
    'myexample.com', 'notexample.com', or 'example.com.au' as the same
    site as 'example.com' purely because that string appears somewhere
    inside the candidate href -- which could send the fallback chain
    navigating to a completely unrelated business's site and treating
    whatever it finds there as this business's contact/about page."""
    if not base_domain:
        return False
    try:
        href_netloc = urlparse(href).netloc.lower()
    except Exception:
        return False
    if href_netloc.startswith("www."):
        href_netloc = href_netloc[4:]
    return href_netloc == base_domain or href_netloc.endswith("." + base_domain)


# =====================================================================
# Scoped scan signal extraction
# =====================================================================
def extract_signals_from_scoped_anchors(anchors: list[dict[str, Any]], base_domain: str) -> dict[str, Any]:
    """Two independent signals, both scoped to footer/header/nav/role/class
    regions (design notes Part 1):
      A. Direct social links -- primary signal
      B. Section-link candidates -- same-domain links whose visible text
         suggests a contact/about/team page, for use as a fallback target
    Includes the isinstance(href, str) guard from the dict-attribute crash
    fix, and the corrected Facebook extraction (see extract_facebook_url)."""
    instagram_url = instagram_handle = facebook_url = None
    scoped_region_hit: Optional[str] = None
    section_candidates: list[dict[str, str]] = []
    seen_section_urls: set[str] = set()

    for a in anchors:
        href = a.get("href")
        text = a.get("text") or ""
        region = a.get("region") or ""

        if not isinstance(href, str) or not href:
            continue
        href_lower = href.lower()

        if not instagram_url and "instagram.com" in href_lower:
            if not _should_skip_social_href(href, IG_SKIP_PATH_PREFIXES, IG_SKIP_SUBSTRINGS):
                match = ig_profile_regex.search(href)
                if match:
                    username = match.group(1).split('?')[0].strip("/").lower()
                    # Item 4: the exact-match list only ever covers known-bad
                    # values (e.g. "blog", "v" from the 132-handle dataset
                    # review) -- the length guard is the general defense
                    # against future unknown junk of the same shape.
                    if username not in IG_SKIP_USERNAMES and len(username) >= 2:
                        instagram_url = f"https://www.instagram.com/{username}/"
                        instagram_handle = username
                        scoped_region_hit = scoped_region_hit or region

        if not facebook_url and "facebook.com" in href_lower:
            if not _should_skip_social_href(href, FB_SKIP_PATH_PREFIXES, FB_SKIP_SUBSTRINGS):
                candidate = extract_facebook_url(href)
                if candidate:
                    facebook_url = candidate
                    scoped_region_hit = scoped_region_hit or region

        if (region in NAVIGATIONAL_REGIONS
                and _is_same_domain(href, base_domain)
                and href_lower not in seen_section_urls):
            text_lower = text.lower()
            hit_kw = next((kw for kw in SECTION_KEYWORDS if kw in text_lower), None)
            if hit_kw:
                seen_section_urls.add(href_lower)
                section_candidates.append({
                    "url": href.split('?')[0].rstrip('/'),
                    "matched_text": text.strip()[:60],
                    "matched_keyword": hit_kw,
                })

        if instagram_url and facebook_url:
            break

    return {
        "instagram_url": instagram_url,
        "instagram_handle": instagram_handle,
        "facebook_url": facebook_url,
        "scoped_region_hit": scoped_region_hit,
        "section_candidates": section_candidates[:MAX_SECTION_TEXT_CANDIDATES],
    }


def _wait_for_social_links_or_settle(page: Any, max_wait_seconds: float = 12.0) -> bool:
    """Poll for an IG/FB link appearing anywhere in the DOM with a bounded
    budget, early-exiting the moment one is found.  Costs near-zero on
    sites that load normally; only spends extra time on genuinely slow-
    rendering pages.  Called right after goto() succeeds, before scroll."""
    deadline = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline:
        try:
            found = page.evaluate(
                '() => !!document.querySelector(\'a[href*="instagram.com"], a[href*="facebook.com"]\')'
            )
        except Exception:
            return False
        if found:
            return True
        time.sleep(0.5)
    return False


def _adaptive_content_wait(page: Any, max_extension_seconds: float = 7.0) -> float:
    """Extend the wait only while anchor count is still changing between
    checks; stop the moment it stabilizes.  Hard-capped so a site with
    endless background DOM churn can't consume unbounded time."""
    start = time.monotonic()
    last_count = -1
    elapsed = 0.0
    while elapsed < max_extension_seconds:
        try:
            current_count = page.evaluate("() => document.querySelectorAll('a[href]').length")
        except Exception:
            break
        if current_count == last_count:
            break  # stabilized -- stop waiting
        last_count = current_count
        time.sleep(1.0)
        elapsed = time.monotonic() - start
    return time.monotonic() - start


def playwright_visit_and_scan(context: Any, url: str) -> dict[str, Any]:
    """Runs entirely inside the PlaywrightWorker's dedicated thread. Opens
    a page, navigates, runs the logged jitter scroll, then the scoped
    multi-selector scan. Returns a plain dict so it crosses the
    future.result() boundary safely."""
    page = None
    try:
        page = context.new_page()
        page.route("**/*", structural_resource_router)
        # Item 2: every stage (homepage, sitemap-slot loop, section-text
        # candidate loop) funnels through this one function, so a single
        # log line here labels ALL of them with zero duplication at the
        # call sites. Fixes real production logs where multiple
        # jitter-scroll blocks ran back-to-back with no way to tell which
        # stage each one belonged to.
        logger.info(f"    ↳ 🌐 [VISITING] {url}")
        response = page.goto(url, timeout=15000, wait_until="domcontentloaded")

        if not response or response.status != 200:
            status = response.status if response else "No Response"
            return {"ok": False, "http_status": status, "error": None}

        _wait_for_social_links_or_settle(page)
        extra = _adaptive_content_wait(page)
        if extra > 1.0:
            logger.info(f"    ↳ ⏳ [ADAPTIVE WAIT] Extended {extra:.1f}s -- anchor count still growing")

        run_jittery_scroll(page)

        try:
            page.wait_for_selector(
                "a[href*='instagram.com'], a[href*='facebook.com']", timeout=5000,
            )
        except PlaywrightTimeoutError:
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except PlaywrightTimeoutError:
                pass

        anchors = page.evaluate(SCOPED_SCAN_JS, SCOPED_SELECTORS)
        base_domain = urlparse(url).netloc.lower().replace("www.", "")
        signals = extract_signals_from_scoped_anchors(anchors, base_domain)
        signals["ok"] = True
        signals["http_status"] = response.status
        signals["error"] = None
        return signals

    except Exception as e:
        return {"ok": False, "http_status": None, "error": str(e)}
    finally:
        if page:
            try: page.close()
            except Exception: pass


# =====================================================================
# Sitemap discovery
# =====================================================================
def _extract_locs_from_sitemap_bytes(
    content: bytes, depth: int, base_domain: Optional[str] = None,
) -> Optional[list[str]]:
    """Parse a sitemap XML body. Handles both <urlset> (direct URL list)
    and <sitemapindex> (recurses one level, capped at MAX_SUB_SITEMAPS).
    Returns None if content isn't parseable XML at all (distinct from an
    empty list, which means "parsed fine, just no matching URLs")."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None

    tag = root.tag.lower()
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    if tag.endswith("urlset"):
        locs = [loc.text.strip() for loc in root.findall(".//sm:url/sm:loc", ns) if loc.text]
        if not locs:
            locs = [loc.text.strip() for loc in root.findall(".//url/loc") if loc.text]
        return locs

    if tag.endswith("sitemapindex") and depth == 0:
        child_locs = [loc.text.strip() for loc in root.findall(".//sm:sitemap/sm:loc", ns) if loc.text]
        if not child_locs:
            child_locs = [loc.text.strip() for loc in root.findall(".//sitemap/loc") if loc.text]

        collected: list[str] = []
        for child_url in child_locs[:MAX_SUB_SITEMAPS]:
            if base_domain and not _is_same_domain(child_url, base_domain):
                logger.warning(
                    f"    [SITEMAP] Ignoring off-domain child sitemap: {child_url}"
                )
                continue
            try:
                resp = requests.get(child_url, timeout=SITEMAP_REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    sub_locs = _extract_locs_from_sitemap_bytes(
                        resp.content, depth=1, base_domain=base_domain,
                    )
                    if sub_locs:
                        collected.extend(sub_locs)
            except requests.RequestException:
                continue
        return collected

    return []


def _filter_section_urls(locs: list[str]) -> list[dict[str, str]]:
    """Keep only <loc> URLs whose path slug matches a section keyword,
    preserving sitemap order."""
    matched: list[dict[str, str]] = []
    seen: set[str] = set()
    for loc in locs:
        if loc in seen:
            continue
        try:
            path = urlparse(loc).path.lower()
        except Exception:
            continue
        hit_keyword = next((kw for kw in SECTION_KEYWORDS if kw in path), None)
        if hit_keyword:
            seen.add(loc)
            matched.append({"url": loc, "matched_keyword": hit_keyword})
    return matched


def discover_sitemap_urlsets(website: str, user_agent: Optional[str] = None) -> dict[str, Any]:
    """Try SITEMAP_PATHS in order (plain requests, no Playwright/watchdog
    needed here). Returns which path worked (or 'Not Found'), the total
    <loc> count, and up to MAX_URLSET_SLOTS section-matching URLs in the
    order encountered.

    `user_agent` should be the calling PlaywrightWorker's own session UA
    (worker.user_agent) whenever one is available. This request runs
    outside the browser context on purpose (plain requests is faster and
    doesn't need Playwright's thread-affinity machinery for a static XML
    fetch) -- but presenting a fresh, independently-random UA here instead
    of the browser's actual one means the same "visitor" shows two
    different fingerprints to the same server within seconds, which is
    exactly the kind of inconsistency correlation-based bot detection is
    built to catch. Falls back to a random pick only if no worker/UA is
    available (e.g. calling this function standalone, outside the normal
    resolve_social_links() flow).

    Always operates on the DOMAIN ROOT, even if `website` itself carries a
    path (e.g. https://example.com/about) -- sitemaps live at the domain
    root by convention, not per-subpath, so building the request off the
    input URL's own path would either 404 needlessly or (worse) silently
    succeed against an unrelated resource. Whether it's even appropriate
    to trust a domain-root sitemap for a given input URL at all is a
    separate decision made by the caller via _should_trust_domain_sitemap()
    before this function is invoked.

    NOTE: .xml.gz sitemap variants aren't handled yet -- candidate
    addition once real run data shows how common they are among your
    target sites, same as the guessed-path list itself was built.
    """
    parsed_root = urlparse(website)
    domain_root = f"{parsed_root.scheme}://{parsed_root.netloc}"
    base_domain = parsed_root.netloc.lower()
    if base_domain.startswith("www."):
        base_domain = base_domain[4:]

    headers = {"User-Agent": user_agent or random.choice(USER_AGENT_PROFILES)["user_agent"]}
    for path in SITEMAP_PATHS:
        sitemap_url = domain_root + path
        try:
            resp = requests.get(sitemap_url, headers=headers, timeout=SITEMAP_REQUEST_TIMEOUT, allow_redirects=True)
            if resp.status_code != 200 or not resp.content:
                continue

            locs = _extract_locs_from_sitemap_bytes(
                resp.content, depth=0, base_domain=base_domain,
            )
            if locs is None:
                continue  # 200 but not parseable XML -- not a real sitemap, try next path

            on_domain_locs = [loc for loc in locs if _is_same_domain(loc, base_domain)]
            ignored_locs = len(locs) - len(on_domain_locs)
            if ignored_locs:
                logger.warning(
                    f"    [SITEMAP] Ignoring {ignored_locs} off-domain URL(s) from {sitemap_url}"
                )

            matched = _filter_section_urls(on_domain_locs)
            return {
                "sitemap_status": path.lstrip("/"),
                "matched_urlsets": matched[:MAX_URLSET_SLOTS],
                "total_locs_found": len(on_domain_locs),
            }
        except requests.RequestException:
            continue

    return {"sitemap_status": "Not Found", "matched_urlsets": [], "total_locs_found": 0}


# =====================================================================
# PlaywrightWorker -- owns one Playwright instance + browser + context,
# all created and used exclusively inside a single persistent worker
# thread (Playwright's sync API is not safe to call across threads).
#
# On a watchdog timeout, the worker is permanently unusable -- the wedged
# call inside it can't be preempted -- so it's marked dead and must be
# discarded; the caller constructs a fresh PlaywrightWorker() to continue.
# =====================================================================
class PlaywrightWorker:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._user_agent: Optional[str] = None
        self._baseline_pids = get_child_pids()
        self._dead = False

        init_future = self._executor.submit(self._init_session)
        try:
            init_future.result(timeout=30)
        except FutureTimeoutError:
            self._dead = True
            self._executor.shutdown(wait=False)
            raise RuntimeError(
                "Browser session failed to start within 30s. Check that "
                "`playwright install chromium` has been run, and that no "
                "leftover Chromium processes are hogging resources."
            )

    def _init_session(self) -> None:
        self._pw = sync_playwright().start()
        self._browser, self._context, self._user_agent = start_fresh_browser_session(self._pw)

    @property
    def is_dead(self) -> bool:
        return self._dead

    @property
    def user_agent(self) -> Optional[str]:
        """This worker's actual browser-session UA -- pass this into any
        non-Playwright request made on this site's behalf (e.g.
        discover_sitemap_urlsets) so it presents the same identity as the
        browser instead of an independently re-rolled one."""
        return self._user_agent

    def run(self, func: Any, timeout: float) -> tuple[Any, bool]:
        """Run func(context) inside the worker thread with a hard ceiling.
        Returns (result, timed_out)."""
        if self._dead:
            return None, True
        future = self._executor.submit(func, self._context)
        try:
            return future.result(timeout=timeout), False
        except FutureTimeoutError:
            self._dead = True
            return None, True

    def kill(self) -> None:
        """Best-effort teardown. If already dead, we don't wait on the
        stuck thread -- just force-kill any leaked process tree and let
        the thread itself leak harmlessly (it'll exit on its own if/when
        the wedged call ever actually returns).

        Both safe_close_session() and self._pw.stop() are submitted as a
        single callable to the worker's own executor thread -- Playwright's
        sync API (including the driver connection self._pw manages) is
        only safe to touch from the thread that created it, the same
        constraint that's the entire reason this class exists. Calling
        self._pw.stop() directly from the caller's thread would violate
        that on every clean recycle (not just watchdog kills), risking a
        leaked driver subprocess that a bare `except Exception: pass`
        would silently hide."""
        if not self._dead:
            try:
                def _teardown() -> None:
                    safe_close_session(self._context, self._browser)
                    if self._pw:
                        self._pw.stop()
                close_future = self._executor.submit(_teardown)
                close_future.result(timeout=10)
            except Exception:
                pass
        self._executor.shutdown(wait=False)
        force_kill_new_children(self._baseline_pids)


# =====================================================================
# resolve_social_links -- the single entry point main_apify.py
# calls. Runs the full fallback chain (see module docstring for order)
# and returns a plain dict ready to merge into a record.
# =====================================================================
def resolve_social_links(worker: "PlaywrightWorker", website: str) -> dict[str, Any]:
    """Returns:
        instagram_url, instagram_handle, facebook_url,
        playwright_status, fallback_status, watchdog_timed_out
    Caller must check the returned "watchdog_timed_out" flag (or
    worker.is_dead directly) after this call and replace the worker if
    true -- this function does not do that itself, since only the
    caller's loop knows when it's safe to pause and swap browsers.

    NOTE on `website`: this should be the full URL as given (including any
    path component, e.g. https://example.com/about) -- NOT pre-stripped to
    just the domain root. The subpath-trust logic in Stage 2 needs the
    real path to work at all. If the caller strips the path before calling
    this function, that logic silently degrades to always-trusting the
    sitemap, same as before 2026-07-12."""
    # Item 1: scheme-normalization guard -- must run before ANY other
    # processing in this function, including the urlparse() calls later
    # in Stage 2 (_should_trust_domain_sitemap, discover_sitemap_urlsets)
    # and the homepage visit itself. A bare domain with no scheme (e.g.
    # "example.com", which Google Maps sometimes returns) makes
    # urlparse() dump the whole string into .path with an empty .netloc
    # -- silently corrupting every path-segment/domain-matching function
    # downstream.
    if not urlparse(website).scheme:
        website = "https://" + website

    fallback_log: list[str] = []
    # Dedup guard (2026-07-12): any URL visited once in this call is never
    # revisited later, regardless of which stage would have picked it
    # again -- e.g. a sitemap slot and a section-text candidate landing on
    # the same page, or the homepage itself reappearing as a sitemap <loc>.
    visited_urls: set[str] = set()

    # ---- Stage 1: Homepage, scoped scan, 40s watchdog ----
    homepage_result, timed_out = worker.run(
        lambda ctx: playwright_visit_and_scan(ctx, website), timeout=HOMEPAGE_WATCHDOG_SECONDS
    )
    if timed_out:
        logger.warning(f"    ↳ ⏱️💀 [WATCHDOG] Homepage visit exceeded {HOMEPAGE_WATCHDOG_SECONDS}s -- worker discarded")
        return {
            "instagram_url": None, "instagram_handle": None, "facebook_url": None,
            "playwright_status": f"Timeout - Watchdog killed after {HOMEPAGE_WATCHDOG_SECONDS}s",
            "fallback_status": "Not attempted - homepage watchdog fired",
            "watchdog_timed_out": True,
        }

    visited_urls.add(_normalize_url_for_dedup(website))

    if not homepage_result or not homepage_result.get("ok"):
        status = homepage_result.get("http_status") if homepage_result else "No Response"
        err = homepage_result.get("error") if homepage_result else "Unknown"
        return {
            "instagram_url": None, "instagram_handle": None, "facebook_url": None,
            "playwright_status": f"HTTP {status}" if status else f"Error: {err}",
            "fallback_status": "Not attempted - homepage failed to load",
            "watchdog_timed_out": False,
        }

    playwright_status = f"HTTP {homepage_result['http_status']}"
    ig_url = homepage_result.get("instagram_url")
    ig_handle = homepage_result.get("instagram_handle")
    fb_url = homepage_result.get("facebook_url")
    region_hit = homepage_result.get("scoped_region_hit")
    section_candidates = homepage_result.get("section_candidates") or []

    if ig_url or fb_url:
        logger.info(f"    ↳ ✅ [DIRECT HIT] region={region_hit} ig={bool(ig_url)} fb={bool(fb_url)}")
        return {
            "instagram_url": ig_url, "instagram_handle": ig_handle, "facebook_url": fb_url,
            "playwright_status": playwright_status,
            "fallback_status": f"Direct Homepage Match (region: {region_hit})",
            "watchdog_timed_out": False,
        }

    fallback_log.append("Homepage: no direct social links in scoped regions")

    # ---- Stage 2: Sitemap discovery. Stops at first hit. ----
    # Two independent gates decide whether matched urlset slots get tested
    # at all, added 2026-07-12:
    #   (a) input-URL subpath trust -- see _should_trust_domain_sitemap()
    #   (b) sitemap size cap -- see MAX_SITEMAP_TOTAL_LOCS
    if not _should_trust_domain_sitemap(website):
        input_path = urlparse(website).path
        fallback_log.append(
            f"Sitemap: Skipped -- input URL path '{input_path}' isn't a recognized "
            f"single-page slug (e.g. /about, /home); likely a franchisee/partner "
            f"subpath where a domain-root sitemap would answer for a different site"
        )
    else:
        sitemap_info = discover_sitemap_urlsets(website, worker.user_agent)
        sitemap_loaded = sitemap_info["sitemap_status"] != "Not Found"

        if not sitemap_loaded:
            fallback_log.append("Sitemap: Not Found (tried sitemap.xml / sitemap_index.xml / wp-sitemap.xml / sitemap/sitemap.xml)")

        elif sitemap_info["total_locs_found"] > MAX_SITEMAP_TOTAL_LOCS:
            fallback_log.append(
                f"Sitemap: Found at /{sitemap_info['sitemap_status']} but has "
                f"{sitemap_info['total_locs_found']} URLs (over the {MAX_SITEMAP_TOTAL_LOCS}-URL "
                f"trust threshold) -- likely an institutional/facility/franchisor-scale "
                f"site rather than a single business; skipping urlset testing"
            )

        else:
            fallback_log.append(
                f"Sitemap: Found at /{sitemap_info['sitemap_status']} "
                f"({sitemap_info['total_locs_found']} URLs total, "
                f"{len(sitemap_info['matched_urlsets'])} matched section keywords)"
            )

            for slot_idx, entry in enumerate(sitemap_info["matched_urlsets"][:MAX_URLSET_SLOTS]):
                slot_url, slot_keyword = entry["url"], entry["matched_keyword"]
                normalized = _normalize_url_for_dedup(slot_url)
                if normalized in visited_urls:
                    fallback_log.append(f"urlset[{slot_idx}] {slot_url} [{slot_keyword}] -> Skipped (already visited)")
                    continue
                visited_urls.add(normalized)

                visit_result, slot_timed_out = worker.run(
                    lambda ctx, u=slot_url: playwright_visit_and_scan(ctx, u), timeout=FALLBACK_WATCHDOG_SECONDS
                )
                if slot_timed_out:
                    logger.warning(f"    ↳ ⏱️💀 [WATCHDOG] urlset[{slot_idx}] ({slot_url}) exceeded {FALLBACK_WATCHDOG_SECONDS}s -- worker discarded")
                    fallback_log.append(f"Aborted at urlset[{slot_idx}] ({slot_url}) - watchdog")
                    return {
                        "instagram_url": None, "instagram_handle": None, "facebook_url": None,
                        "playwright_status": playwright_status + " + Sitemap Watchdog Timeout",
                        "fallback_status": " | ".join(fallback_log),
                        "watchdog_timed_out": True,
                    }

                if visit_result and visit_result.get("ok"):
                    s_ig, s_fb = visit_result.get("instagram_url"), visit_result.get("facebook_url")
                    if s_ig or s_fb:
                        fallback_log.append(f"urlset[{slot_idx}] {slot_url} [{slot_keyword}] -> Found!")
                        logger.info(f"    ↳ ✅ [SITEMAP HIT] {slot_url}")
                        return {
                            "instagram_url": s_ig, "instagram_handle": visit_result.get("instagram_handle"),
                            "facebook_url": s_fb,
                            "playwright_status": playwright_status,
                            "fallback_status": " | ".join(fallback_log),
                            "watchdog_timed_out": False,
                        }
                    fallback_log.append(f"urlset[{slot_idx}] {slot_url} [{slot_keyword}] -> No Links")
                else:
                    fallback_log.append(f"urlset[{slot_idx}] {slot_url} [{slot_keyword}] -> {_describe_visit_failure(visit_result)}")

            if sitemap_info["matched_urlsets"]:
                fallback_log.append("Sitemap urlsets tested -- none yielded links")

    # ---- Stage 3: Homepage section-link text candidates -- tried
    # regardless of sitemap outcome, since these came free with the
    # homepage visit already. ----
    if section_candidates:
        fallback_log.append(f"Section-text candidates: {len(section_candidates)} (testing up to {MAX_SECTION_TEXT_CANDIDATES})")
        for cand in section_candidates[:MAX_SECTION_TEXT_CANDIDATES]:
            cand_url = cand["url"]
            normalized = _normalize_url_for_dedup(cand_url)
            if normalized in visited_urls:
                fallback_log.append(f"Section-text '{cand['matched_text']}' ({cand_url}) -> Skipped (already visited)")
                continue
            visited_urls.add(normalized)

            visit_result, cand_timed_out = worker.run(
                lambda ctx, u=cand_url: playwright_visit_and_scan(ctx, u), timeout=FALLBACK_WATCHDOG_SECONDS
            )
            if cand_timed_out:
                logger.warning(f"    ↳ ⏱️💀 [WATCHDOG] Section-text visit ({cand_url}) exceeded {FALLBACK_WATCHDOG_SECONDS}s -- worker discarded")
                fallback_log.append(f"Section-text '{cand['matched_text']}' -> Watchdog Timeout (aborted)")
                return {
                    "instagram_url": None, "instagram_handle": None, "facebook_url": None,
                    "playwright_status": playwright_status + " + Section-Link Watchdog Timeout",
                    "fallback_status": " | ".join(fallback_log),
                    "watchdog_timed_out": True,
                }
            if visit_result and visit_result.get("ok"):
                c_ig, c_fb = visit_result.get("instagram_url"), visit_result.get("facebook_url")
                if c_ig or c_fb:
                    fallback_log.append(f"Section-text '{cand['matched_text']}' ({cand_url}) -> Found!")
                    logger.info(f"    ↳ ✅ [SECTION-TEXT HIT] {cand_url}")
                    return {
                        "instagram_url": c_ig, "instagram_handle": visit_result.get("instagram_handle"),
                        "facebook_url": c_fb,
                        "playwright_status": playwright_status,
                        "fallback_status": " | ".join(fallback_log),
                        "watchdog_timed_out": False,
                    }
                fallback_log.append(f"Section-text '{cand['matched_text']}' ({cand_url}) -> No Links")
            else:
                fallback_log.append(f"Section-text '{cand['matched_text']}' ({cand_url}) -> {_describe_visit_failure(visit_result)}")
    else:
        fallback_log.append("Section-text candidates: none")

    # Section-text was the true last resort -- guessed-path is retired
    # entirely (see module docstring). Nothing left to try.
    fallback_log.append("No links found anywhere")
    return {
        "instagram_url": None, "instagram_handle": None, "facebook_url": None,
        "playwright_status": playwright_status + " + No Links Found Anywhere",
        "fallback_status": " | ".join(fallback_log),
        "watchdog_timed_out": False,
    }
