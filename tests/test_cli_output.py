import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests.workspace_helpers import ensure_qsync_workspace, write_inventory_csv


def _touch_env(root: Path) -> None:
    """Create a minimal .env to suppress first-run workspace hint."""
    (root / ".env").write_text("", encoding="utf-8")


class QsyncCliOutputTests(unittest.TestCase):
    def test_doctor_json_is_parseable(self) -> None:
        from qsync.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                main(["doctor", "--json"])
            except SystemExit:
                # doctor may exit with code 2 if workspace is invalid;
                # we're just checking that the JSON output is parseable.
                pass
        payload = json.loads(buf.getvalue())
        self.assertIn("root", payload)
        self.assertIn("surveys_dir", payload)
        self.assertIn("inventory_csv", payload)

    def test_survey_label_uses_inventory_csv(self) -> None:
        from qsync.cli import main

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_inventory_csv(root, "id,name,focal\nSV_TEST,Test Survey,TRUE\n")
            _touch_env(root)

            buf = io.StringIO()
            with redirect_stdout(buf):
                main(["--root", str(root), "survey", "label", "--survey-id", "SV_TEST"])
            self.assertEqual(buf.getvalue().strip(), "SV_TEST - Test Survey")

    def test_survey_focal_prints_space_delimited_ids(self) -> None:
        from qsync.cli import main

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_inventory_csv(
                root, "id,name,focal\nSV_A,A,TRUE\nSV_B,B,FALSE\nSV_C,C,TRUE\n"
            )
            _touch_env(root)

            buf = io.StringIO()
            with redirect_stdout(buf):
                main(["--root", str(root), "survey", "focal"])
            self.assertEqual(buf.getvalue().strip(), "SV_A SV_C")

    def test_survey_inspect_question_prints_question_payload(self) -> None:
        from qsync.cli import main

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ensure_qsync_workspace(root)
            (root / "surveys" / "Test__SV_TEST.json").write_text(
                json.dumps(
                    {
                        "result": {
                            "Questions": {
                                "QID1": {"QuestionID": "QID1", "QuestionText": "Hello"}
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            _touch_env(root)

            buf = io.StringIO()
            with redirect_stdout(buf):
                main(
                    [
                        "--root",
                        str(root),
                        "survey",
                        "inspect-question",
                        "--survey-id",
                        "SV_TEST",
                        "--question-id",
                        "QID1",
                    ]
                )
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["QuestionID"], "QID1")
            self.assertEqual(payload["QuestionText"], "Hello")

    def test_self_update_dry_run_prints_command(self) -> None:
        from qsync.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            main(
                [
                    "self-update",
                    "--dry-run",
                    "--yes",
                    "--pip",
                    "--extras",
                    "pdf,langcheck",
                    "--repo",
                    "https://github.com/pmmendoza/qsync.git",
                    "--ref",
                    "main",
                ]
            )
        output = buf.getvalue()
        self.assertIn("Dry run", output)
        self.assertIn("pip install --upgrade", output)
        self.assertIn(
            "qsync[langcheck,pdf] @ git+https://github.com/pmmendoza/qsync.git@main",
            output,
        )

    @patch("qsync.cli_survey.list_surveys", return_value=[])
    @patch(
        "qsync.cli_survey.get_client_config",
        return_value=("example.qualtrics.com", {}),
    )
    def test_survey_list_does_not_prompt_onboard_without_workspace(
        self, _cfg, _list
    ) -> None:
        from qsync.cli import main

        with tempfile.TemporaryDirectory() as td:
            old_cwd = os.getcwd()
            os.chdir(td)
            try:
                buf = io.StringIO()
                with patch.dict(
                    os.environ,
                    {"QSYNC_ROOT": "", "QSYNC_DATA_DIR": "", "QSYNC_ENV_PATH": ""},
                    clear=False,
                ):
                    with redirect_stdout(buf):
                        main(["survey", "list"])
            finally:
                os.chdir(old_cwd)

        out = buf.getvalue()
        self.assertIn("Fetching surveys from example.qualtrics.com", out)
        self.assertNotIn("No workspace found", out)

    @patch("qsync.cli_survey.publish_survey_definition")
    def test_survey_publish_dry_run_skips_api(self, mock_publish) -> None:
        from qsync.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            main(
                [
                    "survey",
                    "publish",
                    "--survey-id",
                    "SV_TEST",
                    "--description",
                    "Test publish",
                    "--dry-run",
                ]
            )
        mock_publish.assert_not_called()
        self.assertIn("DRY-RUN", buf.getvalue())

    @patch("qsync.cli_survey.publish_survey_definition")
    def test_survey_publish_accepts_retry_attempts_arg(self, mock_publish) -> None:
        from qsync.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            main(
                [
                    "survey",
                    "publish",
                    "--survey-id",
                    "SV_TEST",
                    "--description",
                    "Test publish",
                    "--retry-attempts",
                    "2",
                    "--dry-run",
                ]
            )
        mock_publish.assert_not_called()
        self.assertIn("DRY-RUN", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
