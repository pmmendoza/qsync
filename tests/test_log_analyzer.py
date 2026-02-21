from __future__ import annotations

from qsync.log_analyzer import generate_error_report


def test_generate_error_report_includes_expected_sections() -> None:
    entries = [
        {
            "timestamp": "2026-02-20T10:00:00+00:00",
            "action": "qsync.items.push",
            "status": 200,
        },
        {
            "timestamp": "2026-02-20T10:01:00+00:00",
            "action": "qsync.items.push",
            "status": 500,
            "error": {
                "type": "HTTPError",
                "message": "Internal",
                "qualtrics_error_code": "QVAL_3",
            },
        },
        {
            "timestamp": "2026-02-20T10:02:00+00:00",
            "action": "qsync.items.push",
            "status": 500,
            "error": {
                "type": "HTTPError",
                "message": "Internal",
                "qualtrics_error_code": "QVAL_3",
            },
        },
    ]

    report = generate_error_report(entries, granularity="daily", errors_only=False)

    assert report["totals"]["operations"] == 3
    assert report["totals"]["errors"] == 2
    assert "patterns" in report
    assert "by_action" in report["patterns"]
    assert report["patterns"]["by_code"]["QVAL_3"] == 2
    assert isinstance(report["systemic_issues"], list)
    assert isinstance(report["suggestions"], list)
    assert report["suggestions"]
