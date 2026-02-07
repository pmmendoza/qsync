"""Tests for Survey Master pull progress rendering behavior."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch


class _FakeProgress:
    def __init__(self) -> None:
        self.updates: list[tuple[int, str | None]] = []
        self.advances = 0

    def update(self, task_id: int, description: str | None = None, **_: object) -> None:
        self.updates.append((task_id, description))

    def advance(self, _task_id: int, amount: int = 1) -> None:
        self.advances += amount


class SurveyMasterPullProgressTests(unittest.TestCase):
    def test_pull_master_uses_progress_for_multi_survey_non_verbose(self) -> None:
        from qsync.survey_master import pull_master

        fake_progress = _FakeProgress()
        progress_calls: list[tuple[str, int | None]] = []

        @contextmanager
        def fake_progress_context(description: str, *, total: int | None = None):
            progress_calls.append((description, total))
            yield (fake_progress, 1)

        with patch("qsync.survey_master.get_client_config", return_value=("base", {})):
            with patch(
                "qsync.survey_master._fetch_survey_name", side_effect=["S1", "S2"]
            ):
                with patch(
                    "qsync.survey_master._fetch_endpoint", return_value=({}, "")
                ):
                    with patch("qsync.survey_master.create_snapshot", return_value={}):
                        with patch("qsync.survey_master.save_snapshot"):
                            with patch(
                                "qsync.survey_master.generate_master_csv_from_snapshots",
                                return_value=[["SurveyID"], ["SV_1"], ["SV_2"]],
                            ):
                                with patch(
                                    "qsync.survey_master.write_master_csv",
                                    return_value=Path("/tmp/qualtrics_master.csv"),
                                ):
                                    with patch(
                                        "qsync.survey_master.should_use_rich",
                                        return_value=True,
                                    ):
                                        with patch(
                                            "qsync.survey_master.progress_context",
                                            side_effect=fake_progress_context,
                                        ):
                                            snapshots, csv_path = pull_master(
                                                survey_ids=["SV_1", "SV_2"],
                                                verbose=False,
                                            )

        self.assertEqual(snapshots, 2)
        self.assertEqual(csv_path, Path("/tmp/qualtrics_master.csv"))
        self.assertEqual(progress_calls, [("Pulling Survey Master snapshots", 2)])
        self.assertEqual(fake_progress.advances, 2)
        self.assertEqual(len(fake_progress.updates), 2)
        self.assertIn("(1/2)", fake_progress.updates[0][1] or "")
        self.assertIn("(2/2)", fake_progress.updates[1][1] or "")

    def test_pull_master_skips_progress_when_verbose(self) -> None:
        from qsync.survey_master import pull_master

        progress_mock = MagicMock()
        with patch("qsync.survey_master.get_client_config", return_value=("base", {})):
            with patch(
                "qsync.survey_master._fetch_survey_name", side_effect=["S1", "S2"]
            ):
                with patch(
                    "qsync.survey_master._fetch_endpoint", return_value=({}, "")
                ):
                    with patch("qsync.survey_master.create_snapshot", return_value={}):
                        with patch("qsync.survey_master.save_snapshot"):
                            with patch(
                                "qsync.survey_master.generate_master_csv_from_snapshots",
                                return_value=[["SurveyID"], ["SV_1"], ["SV_2"]],
                            ):
                                with patch(
                                    "qsync.survey_master.write_master_csv",
                                    return_value=Path("/tmp/qualtrics_master.csv"),
                                ):
                                    with patch(
                                        "qsync.survey_master.should_use_rich",
                                        return_value=True,
                                    ):
                                        with patch(
                                            "qsync.survey_master.progress_context",
                                            progress_mock,
                                        ):
                                            pull_master(
                                                survey_ids=["SV_1", "SV_2"],
                                                verbose=True,
                                            )

        progress_mock.assert_not_called()

