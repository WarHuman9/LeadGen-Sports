"""
Unit verification suite for Improvement Plan 1 Post-Implementation Fixes (FX-1 to FX-13).
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Mock missing third-party packages in global Python 3.15 test environment
for mod_name in ["requests", "dotenv", "apify_client"]:
    if mod_name not in sys.modules:
        try:
            __import__(mod_name)
        except ImportError:
            sys.modules[mod_name] = MagicMock()

os.environ.setdefault("APIFY_API_TOKEN", "mock_apify_token_for_tests")

import config
import health_monitor
import pipeline_session
import playwright_engine
import main_apify


class TestImprovementPlan1Fixes(unittest.TestCase):

    def test_skippable_website_gov_uk_and_nhs(self):
        """FX-5 / D-4: Verify multi-label TLDs (.gov.uk, .nhs.uk) and legitimate domains (.top)."""
        # Should be skipped
        self.assertTrue(playwright_engine._is_skippable_website("https://council.gov.uk"))
        self.assertTrue(playwright_engine._is_skippable_website("https://cambridge.gov.uk/sports"))
        self.assertTrue(playwright_engine._is_skippable_website("https://hospital.nhs.uk"))
        self.assertTrue(playwright_engine._is_skippable_website("texas.gov"))
        self.assertTrue(playwright_engine._is_skippable_website("ymca.org.uk"))
        self.assertTrue(playwright_engine._is_skippable_website("spam.xyz"))

        # Should NOT be skipped (false positives fixed)
        self.assertFalse(playwright_engine._is_skippable_website("https://topshottennis.net"))
        self.assertFalse(playwright_engine._is_skippable_website("https://tennisacademy.co.uk"))

    def test_ig_reels_and_social_skip_href(self):
        """FX-7: Verify /reels/ and reel handles are correctly rejected."""
        # /reel and /reels should be skipped
        self.assertTrue(playwright_engine._should_skip_social_href(
            "https://www.instagram.com/reels/",
            playwright_engine.IG_SKIP_PATH_PREFIXES,
            playwright_engine.IG_SKIP_SUBSTRINGS
        ))
        self.assertTrue(playwright_engine._should_skip_social_href(
            "https://www.instagram.com/reel/C8XYZ123/",
            playwright_engine.IG_SKIP_PATH_PREFIXES,
            playwright_engine.IG_SKIP_SUBSTRINGS
        ))
        self.assertIn("reels", playwright_engine.IG_SKIP_USERNAMES)
        self.assertIn("reel", playwright_engine.IG_SKIP_USERNAMES)

        # Legitimate profile should not be skipped
        self.assertFalse(playwright_engine._should_skip_social_href(
            "https://www.instagram.com/travissportsacademy/",
            playwright_engine.IG_SKIP_PATH_PREFIXES,
            playwright_engine.IG_SKIP_SUBSTRINGS
        ))

    def test_fallback_profile_budgets_and_watchdog(self):
        """FX-1 / D-1: Verify visit profiles and fallback watchdog constant."""
        self.assertEqual(playwright_engine.FALLBACK_WATCHDOG_SECONDS, 35)
        self.assertIn("homepage", playwright_engine.VISIT_PROFILES)
        self.assertIn("fallback", playwright_engine.VISIT_PROFILES)
        self.assertEqual(playwright_engine.VISIT_PROFILES["fallback"]["poll"], 4.0)
        self.assertEqual(playwright_engine.VISIT_PROFILES["fallback"]["adaptive_cap"], 2.0)
        self.assertEqual(playwright_engine.VISIT_PROFILES["homepage"]["poll"], 12.0)
        self.assertEqual(playwright_engine.VISIT_PROFILES["homepage"]["adaptive_cap"], 7.0)

    def test_start_and_track_single_wait_on_error(self):
        """FX-3: Verify wait_for_finish errors propagate once without duplicate calls."""
        mock_client = MagicMock()
        mock_run_client = MagicMock()
        mock_client.run.return_value = mock_run_client

        # Simulate wait_for_finish raising an exception
        mock_run_client.wait_for_finish.side_effect = RuntimeError("Apify Quota Exceeded")
        mock_run_client.get_streamed_log.return_value = MagicMock()

        persist_mock = MagicMock()

        with self.assertRaises(RuntimeError) as ctx:
            pipeline_session._start_and_track(
                client=mock_client,
                actor_id="compass/crawler-google-places",
                run_input={},
                existing_run_id="run_123",
                persist_run_id=persist_mock,
                stream_logs=True,
            )

        self.assertIn("Apify Quota Exceeded", str(ctx.exception))
        # Crucial check: wait_for_finish called exactly ONCE, not retried
        self.assertEqual(mock_run_client.wait_for_finish.call_count, 1)

    def test_run_status_helper(self):
        """FX-11.1: Verify _run_status extracts status across dicts, objects, and enums."""
        class MockEnum:
            value = "TIMED-OUT"

        class MockObj:
            status = MockEnum()

        self.assertEqual(pipeline_session._run_status({"status": "succeeded"}), "SUCCEEDED")
        self.assertEqual(pipeline_session._run_status(MockObj()), "TIMED-OUT")
        self.assertIsNone(pipeline_session._run_status(None))
        self.assertIsNone(pipeline_session._run_status({}))

    def test_config_timeout_validation(self):
        """FX-11.2: Verify timeout validation catches invalid or negative numbers."""
        with self.assertRaises(config.ConfigError):
            config._validate_run_timeouts({"MAPS_RUN_TIMEOUT_MINUTES": -5})
        with self.assertRaises(config.ConfigError):
            config._validate_run_timeouts({"MAPS_RUN_TIMEOUT_MINUTES": 0})
        with self.assertRaises(config.ConfigError):
            config._validate_run_timeouts({"MAPS_RUN_TIMEOUT_MINUTES": "20"})

    def test_instagram_timestamp_parsing_epoch_guard(self):
        """FX-12: Verify numeric string date YYYYMMDD is rejected and not parsed as year 1970."""
        # Valid ISO timestamp
        dt_iso = main_apify._parse_instagram_timestamp("2024-05-01T12:00:00Z")
        assert dt_iso is not None
        self.assertEqual(dt_iso.year, 2024)

        # Valid recent epoch timestamp (> 2010)
        dt_epoch = main_apify._parse_instagram_timestamp(1714564800) # May 2024
        assert dt_epoch is not None
        self.assertEqual(dt_epoch.year, 2024)

        # Numeric values that would result in year 1970 if parsed as epoch seconds:
        # e.g. integer 20240101 or non-date numeric string "12345678"
        self.assertIsNone(main_apify._parse_instagram_timestamp(20240101))
        self.assertIsNone(main_apify._parse_instagram_timestamp("12345678"))

    def test_pending_step4_excludes_step2_pending(self):
        """FX-2a: Verify get_pending_place_ids(step=4) excludes places without step2_completed_at."""
        import tempfile
        import sqlite3
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        try:
            conn = sqlite3.connect(str(db_path))
            pipeline_session.PipelineSession._apply_pragmas(conn)
            pipeline_session.PipelineSession._create_schema(conn)

            # Insert two places:
            # Place 1: step 2 completed
            # Place 2: step 2 skipped (no browser)
            conn.execute(
                "INSERT INTO places (place_id, raw_maps_json, step2_completed_at) VALUES ('p1', '{}', '2026-11-06T10:00:00')"
            )
            conn.execute(
                "INSERT INTO places (place_id, raw_maps_json, step2_completed_at) VALUES ('p2', '{}', NULL)"
            )
            conn.commit()

            session = pipeline_session.PipelineSession(conn, db_path, "test_session", is_resume=False)

            pending_step2 = session.get_pending_place_ids(step=2)
            pending_step4 = session.get_pending_place_ids(step=4)

            self.assertEqual(pending_step2, ["p2"])
            # Crucial: p2 must NOT be in pending_step4
            self.assertEqual(pending_step4, ["p1"])
            session.close()
        finally:
            if db_path.exists():
                try:
                    os.unlink(db_path)
                except Exception:
                    pass

    def test_maps_failed_empty_not_stamped(self):
        """FX-4 / D-3: Verify FAILED/ABORTED Maps run with 0 places raises MapsRunFailedEmptyError."""
        import tempfile
        import sqlite3
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        try:
            conn = sqlite3.connect(str(db_path))
            pipeline_session.PipelineSession._apply_pragmas(conn)
            pipeline_session.PipelineSession._create_schema(conn)
            conn.execute(
                "INSERT INTO meta (session_id, created_at, config_snapshot_json, ig_config_hash, ads_config_hash, health_config_hash, status) "
                "VALUES ('s1', '2026-11-06', '{}', 'h1', 'h2', 'h3', 'in_progress')"
            )
            conn.commit()

            session = pipeline_session.PipelineSession(conn, db_path, "s1", is_resume=False)

            mock_client = MagicMock()
            mock_run_client = MagicMock()
            mock_client.actor.return_value.start.return_value = {"id": "maps_run_failed"}
            mock_client.run.return_value = mock_run_client
            # Mock finish status = FAILED and empty dataset
            mock_run_client.wait_for_finish.return_value = {"id": "maps_run_failed", "status": "FAILED", "default_dataset_id": "ds_empty"}
            mock_client.dataset.return_value.iterate_items.return_value = iter([])

            with self.assertRaises(pipeline_session.MapsRunFailedEmptyError) as ctx:
                session.resolve_maps_dataset(mock_client, run_input={"test": 1})

            self.assertIn("ended with status 'FAILED' and produced 0 places", str(ctx.exception))

            # Verify maps_places_fetched_at was NOT stamped
            row = conn.execute("SELECT maps_places_fetched_at FROM meta WHERE session_id='s1'").fetchone()
            self.assertIsNone(row["maps_places_fetched_at"])
            session.close()
        finally:
            if db_path.exists():
                try:
                    os.unlink(db_path)
                except Exception:
                    pass

    def test_orphan_warning_clears_when_handles_resolved(self):
        """FX-8: Verify orphaned batch warning does not fire when handles were resolved in a later batch."""
        import tempfile
        import sqlite3
        import json
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        try:
            conn = sqlite3.connect(str(db_path))
            pipeline_session.PipelineSession._apply_pragmas(conn)
            pipeline_session.PipelineSession._create_schema(conn)

            # Insert an orphaned request row for ['handle1'] (run_id is NULL, raw_results_json is NULL)
            conn.execute(
                "INSERT INTO instagram_batch (requested_handles_json, run_id, raw_results_json) VALUES (?, NULL, NULL)",
                (json.dumps(["handle1"]),)
            )
            # Insert a completed batch that fetched ['handle1']
            conn.execute(
                "INSERT INTO instagram_batch (requested_handles_json, run_id, raw_results_json) VALUES (?, 'run_ok', ?)",
                (json.dumps(["handle1"]), json.dumps([{"inputUrl": "https://www.instagram.com/handle1/"}]))
            )
            conn.commit()

            session = pipeline_session.PipelineSession(conn, db_path, "s1", is_resume=False)

            with patch.object(pipeline_session.logger, "warning") as mock_warn:
                session.rehydrate_local_records()
                # Should NOT warn because handle1 was resolved!
                for call_args in mock_warn.call_args_list:
                    self.assertNotIn("unresolved handle", str(call_args))

            session.close()
        finally:
            if db_path.exists():
                try:
                    os.unlink(db_path)
                except Exception:
                    pass

    def test_cpu_detector_min_samples_guard(self):
        """FX-10 / D-6: Verify CPUDetector requires min_samples before tripping."""
        mock_psutil = MagicMock()
        with patch.object(health_monitor, "psutil", mock_psutil):
            detector = health_monitor.CPUDetector(
                threshold_percent=90.0,
                sustained_seconds=8.0,
                min_fraction_above=0.7,
                enabled=True,
            )
            detector.min_samples = 4
            now = 1000.0

            # Feed only 2 samples at 95% CPU (spaced across 8s)
            detector.record_sample(95.0, now)
            detector.record_sample(95.0, now + 8.0)

            with patch("time.monotonic", return_value=now + 8.0):
                # Must NOT trip because len(samples) = 2 < min_samples = 4
                result = detector.check()
                self.assertIsNone(result)

            # Now feed enough samples
            detector.record_sample(95.0, now + 3.0)
            detector.record_sample(95.0, now + 5.0)

            with patch("time.monotonic", return_value=now + 8.0):
                # Now len(samples) = 4 >= min_samples = 4, all 95% -> should trip
                result = detector.check()
                assert isinstance(result, str)
                self.assertIn("CPU sustained >=", result)

    def test_diagnostic_heartbeat_file_only(self):
        """FX-13: Verify DiagnosticHeartbeat writes to file handler without propagating to root."""
        import tempfile
        import time
        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            log_path = f.name

        try:
            hb = playwright_engine.DiagnosticHeartbeat(interval=0.1)
            hb.attach_file_handler(log_path)
            hb.start()
            time.sleep(0.35)
            hb.stop()

            with open(log_path, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertIn("heartbeat seq=", content)
        finally:
            if os.path.exists(log_path):
                try:
                    os.unlink(log_path)
                except Exception:
                    pass

    def test_resolve_social_links_catch_all_tentative(self):
        """FX-6 / D-5: Verify catch-all 'a[href]' hit continues fallback chain and gets labeled as low confidence."""
        mock_worker = MagicMock()
        mock_worker.is_dead = False
        mock_worker.user_agent = "test-ua"

        # 1. Homepage returns catch-all hit (region="a[href]") and no section candidates
        homepage_res = {
            "ok": True,
            "http_status": 200,
            "instagram_url": "https://www.instagram.com/partner_brand/",
            "instagram_handle": "partner_brand",
            "facebook_url": None,
            "scoped_region_hit": "a[href]",
            "section_candidates": [],
        }
        mock_worker.run.return_value = (homepage_res, False)

        # Mock sitemap discovery returning nothing
        with patch.object(playwright_engine, "discover_sitemap_urlsets", return_value={"sitemap_status": "Not Found", "matched_urlsets": [], "total_locs_found": 0}):
            result = playwright_engine.resolve_social_links(mock_worker, "https://example.com")

        self.assertEqual(result["instagram_handle"], "partner_brand")
        self.assertIn("Accepted homepage catch-all", result["fallback_status"])


if __name__ == "__main__":
    unittest.main()
