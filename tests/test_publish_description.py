import unittest


class PublishDescriptionTests(unittest.TestCase):
    def test_truncates_to_limit(self) -> None:
        from qsync.publish_description import make_publish_description

        desc = make_publish_description(
            operation="push",
            changed_qids=[f"QID{i}" for i in range(1, 50)],
            count=49,
            label="x" * 200,
            max_chars=140,
        )
        self.assertLessEqual(len(desc), 140)

    def test_includes_qid_suffix_when_space(self) -> None:
        from qsync.publish_description import make_publish_description

        desc = make_publish_description(
            operation="push",
            changed_qids=["QID1", "QID2", "QID3"],
            count=3,
            label=None,
            max_chars=140,
        )
        self.assertIn("[QID1,QID2,QID3]", desc)

    def test_qid_suffix_uses_plus_n_when_needed(self) -> None:
        from qsync.publish_description import make_publish_description

        qids = [f"QID{i}" for i in range(1, 60)]
        desc = make_publish_description(
            operation="push",
            changed_qids=qids,
            count=len(qids),
            label=None,
            max_chars=60,
        )
        self.assertLessEqual(len(desc), 60)
        self.assertIn("+", desc)


if __name__ == "__main__":
    unittest.main()
