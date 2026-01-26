import unittest
from unittest.mock import patch


class TestItemsPushUsePendingFlag(unittest.TestCase):
    def test_items_push_use_pending_flag_is_plumbed(self):
        import qsync.cli as cli

        captured: dict[str, object] = {}

        def _fake_push_items_pending_record(**kwargs):
            captured.update(kwargs)

        with patch.object(cli, "_push_items_pending_record", side_effect=_fake_push_items_pending_record), patch.object(
            cli.os, "chdir", lambda *_args, **_kwargs: None
        ):
            cli._main_impl(
                [
                    "items",
                    "push",
                    "--survey-id",
                    "SV_TEST",
                    "--use-pending",
                    "--dry-run",
                ]
            )

        self.assertTrue(captured.get("prefer_pending"))

    def test_items_push_use_pending_default_is_none(self):
        import qsync.cli as cli

        captured: dict[str, object] = {}

        def _fake_push_items_pending_record(**kwargs):
            captured.update(kwargs)

        with patch.object(cli, "_push_items_pending_record", side_effect=_fake_push_items_pending_record), patch.object(
            cli.os, "chdir", lambda *_args, **_kwargs: None
        ):
            cli._main_impl(
                [
                    "items",
                    "push",
                    "--survey-id",
                    "SV_TEST",
                    "--dry-run",
                ]
            )

        self.assertIsNone(captured.get("prefer_pending"))


if __name__ == "__main__":
    unittest.main()

