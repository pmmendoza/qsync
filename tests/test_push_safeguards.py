"""Unit tests for qsync push safeguards.

Tests cover:
- Locked survey blocks push
- Live responses require --force-live
- --dry-run does not call send_api_request
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qsync.survey_lock import is_locked, ensure_unlocked
from qsync.push_policy import load_push_context, PushContext
from tests.workspace_helpers import write_inventory_csv


class TestSurveyLock(unittest.TestCase):
    """Tests for survey_lock.py functions."""

    def setUp(self):
        """Create a temporary inventory CSV for testing."""
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)
        self.surveys_dir = self.root / "surveys"
        self.surveys_dir.mkdir(parents=True, exist_ok=True)
        self.inventory_csv = self.surveys_dir / "inventory.csv"

    def tearDown(self):
        """Clean up temporary files."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_inventory(self, rows: list[dict]):
        """Helper to write test inventory CSV."""
        if not rows:
            return
        fieldnames = list(rows[0].keys())
        csv_text = (
            ",".join(fieldnames)
            + "\n"
            + "".join(
                ",".join(str(row.get(k, "")) for k in fieldnames) + "\n" for row in rows
            )
        )
        write_inventory_csv(self.root, csv_text)

    @patch("qsync.survey_inventory.resolve_inventory_csv_path")
    def test_is_locked_returns_true_for_locked_survey(self, mock_resolve):
        """Locked survey should return True."""
        self._write_inventory(
            [
                {"id": "SV_LOCKED", "name": "Locked Survey", "locked": "TRUE"},
                {"id": "SV_UNLOCKED", "name": "Unlocked Survey", "locked": "FALSE"},
            ]
        )
        mock_resolve.return_value = self.inventory_csv

        # Force cache refresh
        import qsync.survey_lock as sl

        sl._LOCK_CACHE = None
        sl._NAME_CACHE = None
        sl._CACHE_MTIME = None
        sl._CACHE_PATH = None

        self.assertTrue(is_locked("SV_LOCKED"))
        self.assertFalse(is_locked("SV_UNLOCKED"))

    @patch("qsync.survey_inventory.resolve_inventory_csv_path")
    def test_ensure_unlocked_raises_for_locked_survey(self, mock_resolve):
        """ensure_unlocked should raise RuntimeError for locked survey."""
        self._write_inventory(
            [
                {"id": "SV_LOCKED", "name": "Locked Survey", "locked": "TRUE"},
            ]
        )

        mock_resolve.return_value = self.inventory_csv

        import qsync.survey_lock as sl

        sl._LOCK_CACHE = None
        sl._NAME_CACHE = None
        sl._CACHE_MTIME = None
        sl._CACHE_PATH = None

        with self.assertRaises(RuntimeError) as ctx:
            ensure_unlocked("SV_LOCKED")
        msg = str(ctx.exception)
        self.assertIn("QSYNC-LOCKED-SURVEY-001", msg)
        self.assertIn("locked", msg.lower())
        self.assertIn("--allow-locked", msg)

    @patch("qsync.survey_inventory.resolve_inventory_csv_path")
    def test_ensure_unlocked_allows_override_env(self, mock_resolve):
        """QSYNC_ALLOW_LOCKED bypasses lock enforcement (dangerous)."""
        self._write_inventory(
            [
                {"id": "SV_LOCKED", "name": "Locked Survey", "locked": "TRUE"},
            ]
        )

        mock_resolve.return_value = self.inventory_csv

        import qsync.survey_lock as sl

        sl._LOCK_CACHE = None
        sl._NAME_CACHE = None
        sl._CACHE_MTIME = None
        sl._CACHE_PATH = None

        with patch.dict(os.environ, {"QSYNC_ALLOW_LOCKED": "1"}):
            ensure_unlocked("SV_LOCKED")

    @patch("qsync.survey_inventory.resolve_inventory_csv_path")
    def test_ensure_unlocked_passes_for_unlocked_survey(self, mock_resolve):
        """ensure_unlocked should not raise for unlocked survey."""
        self._write_inventory(
            [
                {"id": "SV_UNLOCKED", "name": "Unlocked Survey", "locked": "FALSE"},
            ]
        )

        mock_resolve.return_value = self.inventory_csv

        import qsync.survey_lock as sl

        sl._LOCK_CACHE = None
        sl._NAME_CACHE = None
        sl._CACHE_MTIME = None
        sl._CACHE_PATH = None

        # Should not raise
        ensure_unlocked("SV_UNLOCKED")


class TestPushPolicy(unittest.TestCase):
    """Tests for push_policy.py functions."""

    def setUp(self):
        """Create a temporary inventory CSV for testing."""
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)
        self.surveys_dir = self.root / "surveys"
        self.surveys_dir.mkdir(parents=True, exist_ok=True)
        self.inventory_csv = self.surveys_dir / "inventory.csv"

    def tearDown(self):
        """Clean up temporary files."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_inventory(self, rows: list[dict]):
        """Helper to write test inventory CSV."""
        if not rows:
            return
        fieldnames = list(rows[0].keys())
        csv_text = (
            ",".join(fieldnames)
            + "\n"
            + "".join(
                ",".join(str(row.get(k, "")) for k in fieldnames) + "\n" for row in rows
            )
        )
        write_inventory_csv(self.root, csv_text)

    @patch("qsync.survey_inventory.resolve_inventory_csv_path")
    @patch("qsync.push_policy._fetch_quick_counts")
    def test_load_push_context_detects_live_responses(
        self, mock_fetch, mock_resolve
    ):
        """PushContext should correctly report live response counts."""
        self._write_inventory(
            [
                {
                    "id": "SV_LIVE",
                    "name": "Live Survey",
                    "preview_count": "5",
                    "response_count": "10",
                    "generated_at": "2025-12-03T10:00:00Z",
                },
            ]
        )
        mock_resolve.return_value = self.inventory_csv

        # Mock the live fetch to avoid actual API calls
        mock_fetch.return_value = (5, 10)

        ctx = load_push_context("SV_LIVE")
        self.assertEqual(ctx.survey_id, "SV_LIVE")
        self.assertEqual(ctx.response_count, 10)
        self.assertEqual(ctx.preview_count, 5)

    @patch("qsync.survey_inventory.resolve_inventory_csv_path")
    def test_load_push_context_raises_for_missing_survey(self, mock_resolve):
        """Missing inventory row should yield an unknown-count PushContext."""
        self._write_inventory(
            [
                {
                    "id": "SV_OTHER",
                    "name": "Other Survey",
                    "preview_count": "0",
                    "response_count": "0",
                },
            ]
        )

        mock_resolve.return_value = self.inventory_csv

        with patch(
            "qsync.push_policy._fetch_quick_counts", side_effect=Exception("no network")
        ):
            ctx = load_push_context("SV_NONEXISTENT")
        self.assertEqual(ctx.survey_id, "SV_NONEXISTENT")
        self.assertTrue(ctx.counts_unknown)
        self.assertEqual(ctx.counts_source, "missing-inventory")
        self.assertEqual(ctx.preview_count, 0)
        self.assertEqual(ctx.response_count, 0)


class TestPushQuestionCLI(unittest.TestCase):
    """Tests for the push-question CLI command."""

    def setUp(self):
        """Create temporary files for testing."""
        self.temp_dir = tempfile.mkdtemp()
        self.surveys_dir = Path(self.temp_dir) / "surveys"
        self.surveys_dir.mkdir()

        # Create a mock survey JSON
        self.survey_json = self.surveys_dir / "Test__SV_TEST.json"
        self.survey_json.write_text(
            json.dumps(
                {
                    "result": {
                        "Questions": {
                            "QID1": {
                                "QuestionID": "QID1",
                                "QuestionText": "Test question",
                                "QuestionType": "MC",
                            }
                        }
                    }
                }
            )
        )

        # Create inventory CSV
        self.inventory_csv = self.surveys_dir / "inventory.csv"
        self.inventory_csv.write_text(
            "id,name,locked,preview_count,response_count,generated_at\n"
            "SV_TEST,Test Survey,FALSE,0,0,2025-12-03T10:00:00Z\n"
            "SV_LOCKED,Locked Survey,TRUE,0,0,2025-12-03T10:00:00Z\n"
            "SV_LIVE,Live Survey,FALSE,5,10,2025-12-03T10:00:00Z\n"
        )

    def tearDown(self):
        """Clean up temporary files."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("qsync.cli_survey.send_api_request")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey._fetch_remote_question")
    @patch("qsync.cli_survey.load_push_context")
    @patch("qsync.cli_survey.find_cached_survey_file")
    def test_dry_run_does_not_call_api(
        self,
        mock_find_cache,
        mock_push_ctx,
        mock_fetch_remote,
        mock_config,
        mock_send_api,
    ):
        """--dry-run should not call send_api_request."""
        mock_find_cache.return_value = self.survey_json
        mock_config.return_value = ("test.qualtrics.com", {"X-API-TOKEN": "test"})
        mock_fetch_remote.return_value = {
            "QuestionID": "QID1",
            "QuestionText": "Different",
        }
        mock_push_ctx.return_value = PushContext(
            survey_id="SV_TEST",
            survey_name="Test Survey",
            preview_count=0,
            response_count=0,
            counts_source="test",
            generated_at=None,
            stale=False,
            counts_unknown=False,
        )

        from qsync.cli_survey import handle_push_question
        import argparse

        args = argparse.Namespace(
            survey_id="SV_TEST",
            question_id="QID1",
            survey_file=None,
            dry_run=True,
            force_live=False,
            yes=False,
            show_diff=False,
        )

        # The command should exit 0 and never call send_api_request.
        handle_push_question(args)
        mock_send_api.assert_not_called()

    @patch("qsync.cli_survey.send_api_request")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey._fetch_remote_question")
    @patch("qsync.cli_survey.load_push_context")
    @patch("qsync.cli_survey.find_cached_survey_file")
    def test_live_responses_block_without_force(
        self,
        mock_find_cache,
        mock_push_ctx,
        mock_fetch_remote,
        mock_config,
        mock_send_api,
    ):
        """Live responses should cause an error unless --force-live is set."""
        mock_find_cache.return_value = self.survey_json
        mock_config.return_value = ("test.qualtrics.com", {"X-API-TOKEN": "test"})
        mock_fetch_remote.return_value = {
            "QuestionID": "QID1",
            "QuestionText": "Different",
        }
        mock_push_ctx.return_value = PushContext(
            survey_id="SV_LIVE",
            survey_name="Live Survey",
            preview_count=5,
            response_count=10,
            counts_source="test",
            generated_at=None,
            stale=False,
            counts_unknown=False,
        )

        from qsync.cli_survey import handle_push_question
        import argparse

        args = argparse.Namespace(
            survey_id="SV_LIVE",
            question_id="QID1",
            survey_file=None,
            dry_run=False,
            force_live=False,
            yes=True,
            show_diff=False,
        )

        with self.assertRaises(SystemExit) as ctx:
            handle_push_question(args)
        self.assertNotEqual(ctx.exception.code, 0)
        mock_send_api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
