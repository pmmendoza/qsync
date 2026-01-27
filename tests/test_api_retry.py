import unittest
from unittest.mock import patch

import requests


def _response(
    status: int, *, headers: dict[str, str] | None = None
) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp.reason = "TEST"
    resp.headers.update(headers or {})
    resp._content = b"{}"
    return resp


class SendApiRequestRetryTests(unittest.TestCase):
    def test_retries_timeout_then_succeeds(self) -> None:
        from qsync.api_push import send_api_request

        ok = _response(200, headers={"Content-Type": "application/json"})

        with (
            patch("qsync.api_push._sleep", lambda _: None),
            patch(
                "qsync.api_push.requests.request",
                side_effect=[requests.Timeout("boom"), ok],
            ) as mock_request,
        ):
            resp = send_api_request(
                action="test.timeout",
                method="GET",
                base_url="example.qualtrics.com",
                headers={"X-API-TOKEN": "test"},
                path="surveys",
                log_event=False,
                timeout=1,
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_request.call_count, 2)

    def test_retries_503_then_succeeds(self) -> None:
        from qsync.api_push import send_api_request

        unavailable = _response(503, headers={"Content-Type": "application/json"})
        ok = _response(200, headers={"Content-Type": "application/json"})

        with (
            patch("qsync.api_push._sleep", lambda _: None),
            patch(
                "qsync.api_push.requests.request",
                side_effect=[unavailable, ok],
            ) as mock_request,
        ):
            resp = send_api_request(
                action="test.503",
                method="GET",
                base_url="example.qualtrics.com",
                headers={"X-API-TOKEN": "test"},
                path="surveys",
                log_event=False,
                timeout=1,
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_request.call_count, 2)

    def test_honors_retry_after_for_429(self) -> None:
        from qsync.api_push import send_api_request

        rate_limited = _response(
            429,
            headers={"Content-Type": "application/json", "Retry-After": "5"},
        )
        ok = _response(200, headers={"Content-Type": "application/json"})
        sleeps: list[float] = []

        def _record_sleep(seconds: float) -> None:
            sleeps.append(float(seconds))

        with (
            patch("qsync.api_push._sleep", _record_sleep),
            patch(
                "qsync.api_push.requests.request",
                side_effect=[rate_limited, ok],
            ),
        ):
            resp = send_api_request(
                action="test.429",
                method="GET",
                base_url="example.qualtrics.com",
                headers={"X-API-TOKEN": "test"},
                path="surveys",
                log_event=False,
                timeout=1,
            )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(sleeps, "expected at least one sleep call")
        self.assertAlmostEqual(sleeps[0], 5.0, places=2)


if __name__ == "__main__":
    unittest.main()
