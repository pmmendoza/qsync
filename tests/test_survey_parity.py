from __future__ import annotations

from qsync.survey_parity import compare_qsf_parity


def _qsf_simple(*, qids: list[str], tags: dict[str, str]) -> dict:
    elements = []
    for qid in qids:
        elements.append(
            {
                "Element": "SQ",
                "PrimaryAttribute": qid,
                "Payload": {
                    "QuestionID": qid,
                    "DataExportTag": tags.get(qid, ""),
                },
            }
        )

    elements.append(
        {
            "Element": "BL",
            "PrimaryAttribute": "Survey Blocks",
            "Payload": {
                "1": {
                    "ID": "BL_1",
                    "Type": "Default",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": qid} for qid in qids
                    ],
                }
            },
        }
    )
    elements.append(
        {
            "Element": "FL",
            "PrimaryAttribute": "Survey Flow",
            "Payload": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
        }
    )

    return {"SurveyElements": elements}


def test_compare_qsf_parity_matches() -> None:
    qsf = _qsf_simple(qids=["QID1", "QID2"], tags={"QID1": "t1", "QID2": "t2"})
    report = compare_qsf_parity(qsf, qsf)
    assert report.ok


def test_compare_qsf_parity_detects_mismatch() -> None:
    qsf_a = _qsf_simple(qids=["QID1", "QID2"], tags={"QID1": "t1", "QID2": "t2"})
    qsf_b = _qsf_simple(qids=["QID1"], tags={"QID1": "t1"})
    report = compare_qsf_parity(qsf_a, qsf_b)
    assert not report.ok
    assert "QID2" in report.qids_only_in_a
