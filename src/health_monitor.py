"""
health_monitor.py -- the pluggable pipeline-health circuit breaker for the
sports-academy lead-generation pipeline (implementation_plan_4.md, Part B).

=============================================================================
WHY THIS IS A SEPARATE FILE FROM pipeline_session.py
=============================================================================
Originally drafted as one section inside pipeline_session.py, split out on
request once that file grew large enough to be genuinely hard to maintain
and test as a single unit. The split also tracks a real difference in how
finished each half is: session-resume (schema, reconciliation, resumable
Apify calls) is mostly settled, with only minor fixes left; health/circuit-
breaking had real work ahead of it at the time of the split -- B4's
resource detectors (RAM/CPU) were explicitly deferred pending more
profiler data. That data has since come in (a real, full 86-minute run,
5164 samples, reviewed 2026-07-21) and RAMDetector/CPUDetector below are
now real implementations, not placeholders. B6's other candidates
(process count, pagefile, per-process memory, disk) still haven't been
designed. Keeping this in its own file means that ongoing work doesn't
require touching, re-testing, or risking regressions in the session-resume
machinery, and vice versa.

=============================================================================
WHAT THIS FILE OWNS -- AND ITS RELATIONSHIP TO pipeline_session.py
=============================================================================
Owns: the pluggable HealthDetector framework (B3), the network-
disconnection detectors (B5), the resource-exhaustion detectors (B4 Phase
2 -- RAMDetector, CPUDetector), and PipelineHealthMonitor, a convenience
wrapper bundling the default detector set.

=============================================================================
WHERE THRESHOLDS/TOGGLES LIVE: config.py OWNS THE VALUES, THIS FILE DOESN'T
IMPORT IT
=============================================================================
RAMDetector/CPUDetector's threshold_percent / sustained_seconds / enabled
are plain constructor arguments here, not values this file reads from
config.py itself. This isn't about preserving isolation as an abstract
principle -- config.py is exactly the right place for these numbers to
live (the same "small, per-machine tunable you'd want to change without
touching pipeline code" category SESSION_BATCH_LIMIT already occupies
there). The reason the import doesn't happen here specifically: config.py
self-validates its ENTIRE Maps/IG/Ads surface unconditionally at import
time (location-config conflicts, polygon ring-closure, coordinate ranges,
etc.) as one all-or-nothing package. Health monitoring only needs 5 scalar
values that have nothing to do with any of that -- importing config.py
just to get them would mean a broken (or mid-edit) Maps polygon can block
`python health_monitor.py` from even running, which is a real cost given
this file is specifically meant to be testable/debuggable in total
isolation (see test_health_monitor.py's own standalone-harness use).

The resolution: PipelineHealthMonitor's constructor takes the toggle/
threshold values as plain arguments (ram_enabled, ram_threshold_percent,
cpu_enabled, cpu_threshold_percent, cpu_sustained_seconds), each with a
standalone-use default (see _DEFAULT_* constants below, derived from the
2026-07-21 profiler run) so the class still works with zero arguments.
main_apify.py -- not this file -- is expected to read config.py's
values and pass them in at construction time, the exact same "compose at
the call site, one level up" pattern already used for PipelineSession +
PipelineHealthMonitor being built independently. Not yet wired into
config.py or main_apify.py as of this change -- that's the next
step, out of scope for this specific edit, which is scoped to
health_monitor.py's own detector classes only.

This file has ZERO import of, or dependency on, pipeline_session.py --
that's deliberate, not an oversight. The two were coupled once (a
PipelineSession used to construct its own health_detectors list directly
in __init__), and that coupling is exactly what made independent testing
and independent evolution harder: any change to a detector's constructor
signature, or a new detector being added, meant touching (and re-running
every test against) the session-resume file too, even though the two
concerns have no actual logical dependency on each other. Composition
happens one level up, in main_apify.py, which constructs one
PipelineSession and one PipelineHealthMonitor independently and calls into
each on its own terms -- see "HOW main_apify.py IS EXPECTED TO
CONSUME THIS FILE" below.

One thing worth being explicit about, since it's easy to assume otherwise:
there is no natural point where a resumable Apify call
(pipeline_session.py's _start_and_track(), wrapping .start() + persist +
.wait_for_finish()) checks health mid-call. That blocking wait is a single
call into apify-client with no health-check hook inside it today. Health
checks happen BETWEEN records in main_apify.py's own loops (Step
2/Step 4), which is also where a network-dependent call's success/failure
gets reported back via record_network_result() -- this file only ever
learns about a failure because the orchestration script tells it, not by
observing pipeline_session.py's calls directly.

=============================================================================
HOW main_apify.py IS EXPECTED TO CONSUME THIS FILE
=============================================================================
    import health_monitor

    health = health_monitor.PipelineHealthMonitor()

    # ... inside Step 2 / Step 4's per-record loop, after each
    # network-dependent operation:
    try:
        result = session.resolve_ads_call(client, place_id, fb_link, run_input)
        health.record_network_result(success=True)
    except Exception:
        health.record_network_result(success=False)
        raise  # or handle as today -- this file doesn't change failure handling

    # ... periodically (e.g. once per record, or once per N records):
    reason = health.check_health()
    if reason:
        logging.error(f"Circuit breaker tripped: {reason}")
        # ... pause / abort / whatever policy main_apify.py decides --
        # this file only detects and reports, it never itself stops anything.

RAMDetector and CPUDetector (B4 Phase 2) are now included in
PipelineHealthMonitor's default detector list, both `enabled=False` by
default -- see each class's own docstring, and the "WHERE THRESHOLDS/
TOGGLES LIVE" section above for how their values are expected to reach
this file. Adding any FUTURE detector (one of B6's candidates -- process
count, pagefile, per-process memory, disk) means writing one class that
implements the HealthDetector Protocol below and calling
`health.register_detector(MyNewDetector())` -- check_health()'s own logic
never needs to change.

=============================================================================
WHAT'S STILL UNVERIFIED (carried over from before the split, unchanged)
=============================================================================
B5's NLM COM syntax -- the CLSID and enum values below are confirmed
against multiple independent, mutually-agreeing sources (see
NetworkListManagerDetector's own docstring for exactly which), but this
class has not been executed against a live Windows machine as part of
authoring this file (written in a non-Windows environment). It degrades
safely either way -- any COM failure, at construction or during a run,
disables this detector via its own `enabled` flag, and
ConsecutiveFailureDetector keeps working independently regardless, with no
dependency on pywin32 at all.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional, Protocol, cast

try:
    import psutil  # type: ignore[import-not-found]
except ImportError:
    psutil = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# =====================================================================
# B3: pluggable detector framework -- verbatim shape from
# implementation_plan_4.md, so any future detector (B6's process-count,
# pagefile, per-process memory, disk candidates; eventually B4 Phase 2's
# RAM/CPU detectors once real thresholds exist) just implements this
# Protocol and gets registered -- check_health() itself never changes.
# =====================================================================
class HealthDetector(Protocol):
    name: str
    enabled: bool

    def check(self) -> Optional[str]:
        """None if healthy. A short, human-readable reason string if this
        detector believes the pipeline should stop."""
        ...


def check_health(detectors: list[Any]) -> Optional[str]:
    reasons: list[str] = []
    for d in detectors:
        if not d.enabled:
            continue
        reason = d.check()
        if reason:
            reasons.append(f"{d.name}: {reason}")
    return " | ".join(reasons) if reasons else None


# =====================================================================
# B5: network-disconnection detection. NLM confirmed as the primary
# detector; consecutive-failure inference kept as a second, independent
# detector -- not a fallback in the "lesser" sense, a genuinely separate
# signal that also happens to not depend on pywin32 at all.
# =====================================================================
# CLSID_NetworkListManager -- the COCLASS to Dispatch(), NOT
# IID_InetworkListManager (the interface identifier, a different, similarly
# -patterned but unrelated GUID: DCB00000-... vs this one, DCB00C01-...).
# Confirmed via two independent sources agreeing on this exact string: (1)
# Microsoft's own win32metadata GitHub issue thread naming this as
# CLSID_NetworkListManager's real value, and (2) a real, first-hand report
# of a Python user calling win32com.client.Dispatch() with this exact
# string and confirming it dispatches successfully. Stable since Windows
# Vista per Microsoft's own current docs -- no Windows-11-specific
# variance found or expected.
_NLM_CLSID = "{DCB00C01-570F-4A9B-8D69-199FDBA5723B}"

# NLM_CONNECTIVITY enum values -- confirmed against Microsoft's own current
# Win32 API docs (learn.microsoft.com/.../ne-netlistmgr-nlm_connectivity)
# AND multiple independent header mirrors (mingw-w64, an older Windows SDK
# mirror) all agreeing on the same hex values. Only the two flags this
# detector actually needs are reproduced here, not the full enum.
_NLM_CONNECTIVITY_DISCONNECTED = 0x0000
_NLM_CONNECTIVITY_IPV4_INTERNET = 0x0040
_NLM_CONNECTIVITY_IPV6_INTERNET = 0x0400
_NLM_INTERNET_FLAGS = _NLM_CONNECTIVITY_IPV4_INTERNET | _NLM_CONNECTIVITY_IPV6_INTERNET


class NetworkListManagerDetector:
    """B5: primary network detector via Windows' Network List Manager COM
    API -- Microsoft's own recommended replacement for the older
    InternetGetConnectedState WinINet call, which is known to false-
    negative on "connected but broken" (a live adapter, dead DNS/gateway).

    UNVERIFIED CAVEAT, stated plainly rather than glossed over: the CLSID
    and enum values here are confirmed against multiple independent,
    agreeing sources (see the module-level constants' own comments for
    exactly which), but this class has not been executed against a live
    Windows machine as part of authoring this file (written in a non-
    Windows environment). It degrades safely either way -- any failure at
    construction (pywin32 missing, COM error) or at call time disables
    this detector via its own `enabled` flag rather than raising into the
    pipeline, and ConsecutiveFailureDetector remains fully independent as
    a second signal regardless of whether this one works.

    One specific detail genuinely needs a live-machine check, not a design
    decision: whether `.GetConnectivity()` on a plain (non-makepy'd)
    Dispatch object returns a raw int or a wrapped/named type. The bitwise
    check below (`int(raw) & _NLM_INTERNET_FLAGS`) works identically
    either way, so this isn't a blocker -- just worth a
    `print(type(raw), raw)` the first real run, per implementation_plan_4.md's
    B5 section.

    TWO FAILURE MODES, HANDLED DIFFERENTLY ON PURPOSE:
      1. Dispatch() itself succeeds, but the COM registration is subtly
         broken in a way that only surfaces on the first real method call
         -- late-bound Dispatch() is fairly permissive about this, so
         "Dispatch() didn't raise" is NOT proof the object actually works.
         Caught by calling GetConnectivity() once here, at construction,
         rather than waiting for the first real check() during the run.
      2. GetConnectivity() itself starts failing partway through a run
         (not caught at construction, since it worked once there). A
         single call-time failure is treated as "unknown, don't trip the
         breaker on a COM hiccup alone" -- but if it fails
         _check_failure_disable_threshold times in a row, this detector
         disables itself for the rest of the run and logs clearly, rather
         than silently becoming a permanent no-op with enabled staying
         True the whole time. ConsecutiveFailureDetector keeps working
         independently either way.
    """

    name = "network_list_manager"

    def __init__(self, check_failure_disable_threshold: int = 3) -> None:
        self.enabled = True
        self._client: Any = None
        self._consecutive_check_failures = 0
        self._check_failure_disable_threshold = check_failure_disable_threshold
        try:
            import win32com.client  # type: ignore[import-not-found]

            client = win32com.client.Dispatch(_NLM_CLSID)
            # Verify it actually works, not just that Dispatch() didn't
            # raise -- see this class's docstring, failure mode 1.
            int(client.GetConnectivity())
            self._client = client
        except Exception as e:
            logger.warning(
                f"[HEALTH] NetworkListManagerDetector unavailable ({e}) -- disabling. "
                f"ConsecutiveFailureDetector remains active as an independent network "
                f"safety net, no dependency on pywin32 at all."
            )
            self.enabled = False

    def check(self) -> Optional[str]:
        if not self.enabled or self._client is None:
            return None
        try:
            raw = self._client.GetConnectivity()
            connectivity = int(raw)
        except Exception as e:
            self._consecutive_check_failures += 1
            if self._consecutive_check_failures >= self._check_failure_disable_threshold:
                logger.warning(
                    f"[HEALTH] NLM GetConnectivity() has failed "
                    f"{self._consecutive_check_failures} consecutive times ({e}) -- "
                    f"disabling NetworkListManagerDetector for the rest of this run "
                    f"rather than silently no-op'ing on every future check. "
                    f"ConsecutiveFailureDetector remains active as the sole network "
                    f"signal from here."
                )
                self.enabled = False
            else:
                logger.warning(
                    f"[HEALTH] NLM GetConnectivity() call failed ({e}) -- treating as "
                    f"unknown for this check, not tripping the breaker on a single "
                    f"COM hiccup alone."
                )
            return None

        self._consecutive_check_failures = 0
        if connectivity == _NLM_CONNECTIVITY_DISCONNECTED:
            return "no network adapter connectivity (NLM_CONNECTIVITY_DISCONNECTED)"
        if not (connectivity & _NLM_INTERNET_FLAGS):
            return (
                "network adapter is up but no internet reachability "
                "(NLM connected-but-no-internet state -- e.g. DNS/gateway trouble)"
            )
        return None


class ConsecutiveFailureDetector:
    """B5: the original consecutive-failure inference detector, kept as a
    second, independent signal alongside NetworkListManagerDetector -- not
    a replacement, and not merely a fallback in the lesser sense. Doubles
    as the safety net if pywin32/NLM ever fails for an unrelated reason (a
    COM error, a permissions quirk): this detector has no dependency on
    that at all, it only depends on the pipeline itself reporting whether
    its own network-dependent calls are succeeding or failing, via
    record_result()."""

    name = "consecutive_network_failures"

    def __init__(self, threshold: int = 5) -> None:
        self.enabled = True
        self._threshold = threshold
        self._consecutive_failures = 0

    def record_result(self, success: bool) -> None:
        if success:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1

    def check(self) -> Optional[str]:
        if self._consecutive_failures >= self._threshold:
            return (
                f"{self._consecutive_failures} consecutive network-dependent call "
                f"failures (threshold {self._threshold}) -- inferring a real "
                f"connectivity problem, not a string of coincidental one-off failures"
            )
        return None


# =====================================================================
# B4 Phase 2: resource-exhaustion detection. Real, already-reviewed
# profiler data backs the module-level defaults below -- a full
# 86-minute run on Shiv's own 8GB-RAM Windows machine, 5164 samples,
# reviewed 2026-07-21:
#
#   RAM%  -- min 61.2, max 93.5, p95 83.8, p99 88.2, avg 70.3. This
#            machine idles hot (8GB total, browser + OS baseline), so the
#            threshold has to sit meaningfully above the REAL observed
#            peak (93.5%) rather than an abstract "90 sounds high enough"
#            guess -- 90.0 sits just above the observed p99 (88.2%) while
#            still catching the kind of climb that preceded the actual
#            93.5% peak.
#   CPU%  -- contiguous >=95%-CPU runs: 129/148 (87%) lasted under 10s
#            (ordinary noise -- background scheduling, a brief spike),
#            while the genuinely sustained episodes ran 60-150+ seconds
#            and corresponded to real pipeline work (Playwright browser
#            launches, IG/Ads batches). An 8s window sits right at the
#            boundary separating that noise from real sustained load,
#            backed by this distribution rather than the "7-10s, pending
#            real data" placeholder implementation_plan_4.md's B4 section
#            originally left open.
#
# These are Shiv's-machine-specific defaults for STANDALONE/smoke-test use
# only (i.e. what PipelineHealthMonitor() falls back to if constructed
# with zero arguments) -- once config.py carries the real
# RAM_THRESHOLD_PERCENT / CPU_THRESHOLD_PERCENT / CPU_SUSTAINED_SECONDS
# values, main_apify.py's real construction call is expected to
# pass those in instead of relying on these fallbacks (see the module
# docstring's "WHERE THRESHOLDS/TOGGLES LIVE" section).
# =====================================================================
_DEFAULT_RAM_THRESHOLD_PERCENT = 90.0
_DEFAULT_CPU_THRESHOLD_PERCENT = 95.0
_DEFAULT_CPU_SUSTAINED_SECONDS = 8.0


class RAMDetector:
    """B4 Phase 2: plain single-sample threshold-crossing on system-wide
    RAM% (psutil.virtual_memory().percent). Matches B4's own finding that
    RAM is "a clean, level-based signal" -- a real profiler run showed it
    tracking actual pipeline state directly (a sharp, sustained drop when
    the Step 1 browser closes, a stable plateau otherwise), so a plain
    single-sample threshold is a sound detector shape here, unlike CPU
    (see CPUDetector below for why that one needs a sustained window
    instead).

    Degrades safely if psutil isn't installed, same philosophy as
    NetworkListManagerDetector degrading safely without pywin32: forces
    `enabled=False` at construction rather than raising. Only warns if the
    caller actually asked for this to be enabled -- warning about a
    missing dependency for a detector that was going to stay off anyway
    (the class default) would just be noise.
    """

    name = "ram_threshold"

    def __init__(
        self,
        threshold_percent: float = _DEFAULT_RAM_THRESHOLD_PERCENT,
        enabled: bool = False,
    ) -> None:
        self.threshold_percent = threshold_percent
        self.enabled = enabled
        if enabled and psutil is None:
            logger.warning(
                "[HEALTH] RAMDetector was requested enabled, but psutil isn't "
                "installed -- disabling. Install with: pip install psutil "
                "--break-system-packages"
            )
            self.enabled = False

    def check(self) -> Optional[str]:
        if not self.enabled or psutil is None:
            return None
        current = psutil.virtual_memory().percent
        if current >= self.threshold_percent:
            return f"RAM at {current:.1f}% >= threshold {self.threshold_percent:.1f}%"
        return None


class CPUDetector:
    """B4 Phase 2: sustained-duration windowed check on system-wide CPU%
    -- deliberately NOT a single-sample threshold. The same profiler run
    that validated RAMDetector's simpler shape showed system-wide CPU%
    swinging 60-80 percentage points sample to sample with no correlation
    to pipeline stage, so any single sample is close to meaningless; what
    actually separates real pressure from ordinary noise is a sustained
    run of high samples across a rolling window (see the module-level
    comment above this class for the exact distribution that motivated
    the defaults).

    Uses a frequency-based tolerance rather than a strict contiguous
    streak: tolerates a small fraction of below-threshold dips within a
    sustained window, rather than resetting on any single dip.  The
    `min_fraction_above` parameter (default 0.7) controls what fraction
    of samples within the window must be at or above threshold for the
    window to keep growing; if the fraction drops below this floor, the
    window resets.  This avoids false negatives from the occasional
    momentary CPU% dip in an otherwise sustained high-CPU episode.

    Deliberately stays system-wide rather than per-process. This detector
    answers "is the machine, for whatever reason, under enough pressure
    that the pipeline might get killed or wedged" -- a survival question,
    not an attribution question. A per-process view would systematically
    miss the case where something ELSE on the machine is pinning the CPU,
    which is real pressure the pipeline suffers from regardless of whose
    process caused it. (A future, separate per-process detector -- "is
    THIS script specifically leaking/spinning" -- is a distinct B6
    candidate, not a variant of this one.)

    Same psutil degrade-safely behavior as RAMDetector -- see that
    class's docstring.
    """

    name = "cpu_sustained"

    def __init__(
        self,
        threshold_percent: float = _DEFAULT_CPU_THRESHOLD_PERCENT,
        sustained_seconds: float = _DEFAULT_CPU_SUSTAINED_SECONDS,
        min_fraction_above: float = 0.7,
        enabled: bool = False,
    ) -> None:
        self.threshold_percent = threshold_percent
        self.sustained_seconds = sustained_seconds
        self.min_fraction_above = min_fraction_above
        self.enabled = enabled
        # Frequency-based streak tracking: _streak_start marks when the
        # current sustained window began, and _streak_samples collects
        # ALL samples (including below-threshold dips) within that window.
        # Unlike the earlier strict-streak approach that reset on any
        # single below-threshold sample, this version tracks what FRACTION
        # of samples cleared the threshold (via min_fraction_above) and
        # only resets when too few samples qualify -- tolerating occasional
        # momentary dips within an otherwise sustained high-CPU episode.
        self._streak_start: Optional[float] = None
        self._streak_samples: list[float] = []
        if enabled and psutil is None:
            logger.warning(
                "[HEALTH] CPUDetector was requested enabled, but psutil isn't "
                "installed -- disabling. Install with: pip install psutil "
                "--break-system-packages"
            )
            self.enabled = False
        elif enabled and psutil is not None:
            # Prime psutil's internal baseline -- the first
            # cpu_percent(interval=None) call always returns a meaningless
            # 0.0 with nothing yet to compare against.
            psutil.cpu_percent(interval=None)

    def check(self) -> Optional[str]:
        if not self.enabled or psutil is None:
            return None
        value = psutil.cpu_percent(interval=None)
        now = time.monotonic()

        if self._streak_start is None:
            self._streak_start = now
            self._streak_samples = []
        self._streak_samples.append(value)

        duration = now - self._streak_start
        frac_above = sum(1 for v in self._streak_samples if v >= self.threshold_percent) / len(self._streak_samples)

        if frac_above < self.min_fraction_above:
            self._streak_start = None
            self._streak_samples = []
            return None

        if duration >= self.sustained_seconds:
            avg = sum(self._streak_samples) / len(self._streak_samples)
            return (
                f"CPU sustained >= {self.threshold_percent:.1f}% for {duration:.1f}s "
                f"({frac_above*100:.0f}% of samples qualifying, avg {avg:.1f}%)"
            )
        return None


# =====================================================================
# PipelineHealthMonitor -- bundles the default detector set into one
# convenience object, so main_apify.py doesn't have to manually
# assemble a detector list itself. Mirrors the ergonomic "construct one
# thing and it works" pattern PipelineSession used to provide directly,
# before the split -- now scoped correctly to this module instead of
# leaking a health-specific convenience API onto a session object whose
# job is session state, not circuit-breaking.
# =====================================================================
class PipelineHealthMonitor:
    def __init__(
        self,
        ram_enabled: bool = False,
        ram_threshold_percent: float = _DEFAULT_RAM_THRESHOLD_PERCENT,
        cpu_enabled: bool = False,
        cpu_threshold_percent: float = _DEFAULT_CPU_THRESHOLD_PERCENT,
        cpu_sustained_seconds: float = _DEFAULT_CPU_SUSTAINED_SECONDS,
        cpu_min_fraction_above: float = 0.7,
    ) -> None:
        """RAM/CPU args default to disabled + Shiv's-machine-specific
        thresholds, so `PipelineHealthMonitor()` with zero arguments still
        works standalone (matching NLM/consecutive-failure's existing
        zero-arg construction). Real production use is expected to pass
        these in from config.py via main_apify.py -- see the
        module docstring's "WHERE THRESHOLDS/TOGGLES LIVE" section for why
        that wiring happens there and not inside this file."""
        self.detectors: list[Any] = [
            NetworkListManagerDetector(),
            ConsecutiveFailureDetector(),
            RAMDetector(threshold_percent=ram_threshold_percent, enabled=ram_enabled),
            CPUDetector(
                threshold_percent=cpu_threshold_percent,
                sustained_seconds=cpu_sustained_seconds,
                min_fraction_above=cpu_min_fraction_above,
                enabled=cpu_enabled,
            ),
        ]

    def check_health(self) -> Optional[str]:
        return check_health(self.detectors)

    def record_network_result(self, success: bool) -> None:
        """Only ConsecutiveFailureDetector needs results fed to it.
        RAMDetector/CPUDetector poll psutil directly on check() -- there's
        no equivalent "feed a result" step for them, so this scoping to
        ConsecutiveFailureDetector only is intentional, not a gap left
        over from before RAM/CPU existed."""
        for d in self.detectors:
            if isinstance(d, ConsecutiveFailureDetector):
                d.record_result(success)

    def register_detector(self, detector: Any) -> None:
        """Extension point for B6's future candidates and B4 Phase 2's
        eventual RAM/CPU detectors once real thresholds exist -- add one
        new class implementing HealthDetector, register it here, done.
        check_health()'s own trigger logic never has to change."""
        self.detectors.append(detector)


# =====================================================================
# Standalone smoke test -- `python health_monitor.py` constructs a
# PipelineHealthMonitor with default (zero-argument) settings and reports
# which detectors are actually active on this machine.
#   - NetworkListManagerDetector reporting itself disabled is expected on
#     any non-Windows box, or a Windows box without pywin32 installed --
#     not a failure.
#   - RAMDetector/CPUDetector reporting enabled=False here is ALSO
#     expected -- they default off (B4 Phase 2's own "shouldn't be a
#     silent default" mandate), not because anything is broken. If psutil
#     is missing entirely, constructing them with enabled=True would
#     force enabled back to False with a warning; that's exercised here
#     too, just to confirm the degrade-safely path actually works, not to
#     suggest RAM/CPU should be on by default.
# Has zero dependency on config.py, playwright_engine.py, apify_client, or
# pipeline_session.py -- this file can be fully exercised in total
# isolation. (See the module docstring's "WHERE THRESHOLDS/TOGGLES LIVE"
# section for why config.py specifically stays out of this file even
# though it's where the real RAM/CPU threshold values are meant to live.)
# =====================================================================
if __name__ == "__main__":
    print("Running health_monitor.py smoke test...\n")

    print(f"psutil available: {psutil is not None}\n")

    monitor = PipelineHealthMonitor()
    print("-- Default construction (zero arguments, matches real-world startup) --")
    for d in monitor.detectors:
        print(f"   {d.name:15s} enabled={d.enabled}")
    reason = monitor.check_health()
    print(f"\ncheck_health() -> {reason!r}")
    print(
        "\n(NetworkListManagerDetector showing enabled=False here is expected on "
        "non-Windows machines, or Windows without pywin32 installed -- "
        "ConsecutiveFailureDetector is the one that should always show enabled=True. "
        "RAMDetector/CPUDetector showing enabled=False is also expected: they default "
        "off until a caller explicitly opts in.)"
    )

    print("\n-- Explicitly enabling RAM/CPU, to confirm they actually work --")
    enabled_monitor = PipelineHealthMonitor(ram_enabled=True, cpu_enabled=True)
    ram_d = next(d for d in enabled_monitor.detectors if d.name == "ram_threshold")
    cpu_d = next(d for d in enabled_monitor.detectors if d.name == "cpu_sustained")
    print(f"   ram_threshold   enabled={ram_d.enabled} (threshold={ram_d.threshold_percent}%)")
    print(
        f"   cpu_sustained   enabled={cpu_d.enabled} "
        f"(threshold={cpu_d.threshold_percent}%, window={cpu_d.sustained_seconds}s, "
        f"min_fraction_above={cpu_d.min_fraction_above})"
    )
    if psutil is not None:
        print(f"   Live RAM% right now: {psutil.virtual_memory().percent:.1f}%")
        print(f"   Live CPU% right now (single sample): {psutil.cpu_percent(interval=None):.1f}%")
    else:
        print(
            "   psutil not installed -- both should show enabled=False despite "
            "ram_enabled=True/cpu_enabled=True being requested (degrade-safely path)."
        )
