import unittest


class TestEosPushDryRun(unittest.TestCase):
    def test_push_eos_messages_dry_run_does_not_require_yes(self):
        from qsync.dimensions.eos_core import push_eos_messages
        from qsync.pending_stage import PendingStagedChanges, EosOperation, EosPendingPayload

        record = PendingStagedChanges(
            survey_id="SV_TEST",
            dimension="eos",
            payload=EosPendingPayload(
                operations=[
                    EosOperation(
                        library_id="UR_TEST",
                        message_id="MS_TEST",
                        message_dir="contents/qualtrics_library_messages/UR_TEST/MS_TEST",
                    )
                ]
            ),
            schema_version=2,
        )

        pushed = push_eos_messages(
            survey_id="SV_TEST",
            record=record,
            allow_shared=True,
            yes=False,
            dry_run=True,
        )

        self.assertEqual(pushed, [("UR_TEST", "MS_TEST")])


if __name__ == "__main__":
    unittest.main()

