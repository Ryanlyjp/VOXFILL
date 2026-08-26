import os
import unittest
from io import BytesIO
from urllib.error import HTTPError
from unittest.mock import patch

from flaresolverr_client import FlareSolverError, _post_json, solve_page


class FlareSolverrClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_html_and_cf_clearance_cookie(self) -> None:
        response = {
            "status": "ok",
            "solution": {
                "url": "https://example.test/",
                "response": "<html>solved</html>",
                "userAgent": "solver-agent",
                "cookies": [
                    {"name": "cf_clearance", "value": "clear", "domain": ".example.test"},
                    {"name": "session", "value": "session-value", "domain": ".example.test"},
                ],
            },
        }
        with patch.dict(
            os.environ,
            {
                "VOXTRY_FLARESOLVERR_URL": "http://solver:8191",
                "VOXTRY_FLARESOLVERR_TIMEOUT_MS": "120000",
            },
        ), patch("flaresolverr_client._post_json", return_value=response) as post:
            solution = await solve_page("https://example.test/", "http://user:pass@proxy.test:8080")

        self.assertEqual(solution.html, "<html>solved</html>")
        self.assertEqual(solution.user_agent, "solver-agent")
        self.assertEqual([item["name"] for item in solution.cookies], ["cf_clearance", "session"])
        payload = post.call_args.args[1]
        self.assertEqual(payload["url"], "https://example.test/")
        self.assertFalse(payload["returnOnlyCookies"])
        self.assertEqual(payload["proxy"]["username"], "user")
        self.assertEqual(post.call_args.args[2], 135)

    async def test_accepts_a_page_without_challenge_cookie(self) -> None:
        response = {
            "status": "ok",
            "solution": {"response": "<html>challenge</html>", "cookies": []},
        }
        with patch.dict(os.environ, {"VOXTRY_FLARESOLVERR_URL": "http://solver:8191"}), patch(
            "flaresolverr_client._post_json", return_value=response
        ):
            solution = await solve_page("https://example.test/")

        self.assertEqual(solution.html, "<html>challenge</html>")
        self.assertEqual(solution.cookies, [])

    def test_includes_solver_error_body_in_http_error(self) -> None:
        error = HTTPError(
            "http://solver:8191/v1",
            500,
            "server error",
            {},
            BytesIO(b'{"message":"ERR_TUNNEL_CONNECTION_FAILED"}'),
        )

        with patch("flaresolverr_client.urlopen", side_effect=error):
            with self.assertRaisesRegex(FlareSolverError, "ERR_TUNNEL_CONNECTION_FAILED"):
                _post_json("http://solver:8191/v1", {}, 1)
