import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

from flaresolverr_client import FlareSolverError, FlareSolverSolution
from proxy_pool import ProxyPool
from storage import Store
from storage import DEFAULT_SETTINGS


if importlib.util.find_spec("playwright"):
    from runner import FormRunner
    from playwright.async_api import async_playwright
else:
    FormRunner = None


class FakePage:
    def __init__(self) -> None:
        self.action_timeout = None
        self.navigation_timeout = None

    def set_default_timeout(self, timeout: int) -> None:
        self.action_timeout = timeout

    def set_default_navigation_timeout(self, timeout: int) -> None:
        self.navigation_timeout = timeout


@unittest.skipUnless(FormRunner is not None, "Playwright is not installed")
class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self) -> tuple[tempfile.TemporaryDirectory[str], Store]:
        directory = tempfile.TemporaryDirectory()
        return directory, Store(Path(directory.name))

    async def test_rejects_task_count_above_available_emails_before_claiming(self) -> None:
        directory, store = self.make_store()
        self.addCleanup(directory.cleanup)
        store.replace_emails("one@example.com")
        store.replace_postcodes("M14 7HX")
        store.update_settings({"task_count": 2})

        with self.assertRaisesRegex(RuntimeError, "可用邮箱数量"):
            FormRunner(store).start()

        state = store.snapshot()
        self.assertIsNone(state["emails"][0]["used_at"])
        self.assertEqual(state["task_records"], [])

    async def test_batch_runs_with_configured_concurrency_and_tracks_progress(self) -> None:
        directory, store = self.make_store()
        self.addCleanup(directory.cleanup)
        runner = FormRunner(store)
        jobs = []
        for index in range(4):
            record_id = f"record-{index}"
            jobs.append({"record_id": record_id, "email": f"{index}@example.com", "postcode": "M14 7HX"})
            store.add_task_record(
                {
                    "id": record_id,
                    "email": f"{index}@example.com",
                    "status": "running",
                }
            )
        store.set_runtime(task_total=4, task_completed=0, task_success=0, task_failed=0)
        active = 0
        maximum = 0

        async def fake_execute(job, settings, pool) -> None:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            store.complete_task_record(job["record_id"], "success", "完成", runner._timestamp())

        runner._execute_one = fake_execute
        await runner._run_batch(jobs, {}, 2)

        state = store.snapshot()
        self.assertEqual(maximum, 2)
        self.assertEqual(state["runtime"]["task_completed"], 4)
        self.assertEqual(state["runtime"]["task_success"], 4)
        self.assertEqual(state["runtime"]["status"], "success")

    async def test_retries_once_with_a_new_proxy_after_tunnel_failure(self) -> None:
        directory, store = self.make_store()
        self.addCleanup(directory.cleanup)
        runner = FormRunner(store)
        pool = ProxyPool(["http://proxy-a:8000", "http://proxy-b:8000"], True)
        solution = FlareSolverSolution("<html></html>", [], "", "https://example.test")

        with patch(
            "runner.solve_page",
            new=AsyncMock(
                side_effect=[
                    FlareSolverError("FlareSolverr HTTP 500: ERR_TUNNEL_CONNECTION_FAILED"),
                    solution,
                ]
            ),
        ) as solve:
            result, proxy = await runner._solve_with_proxy_retry(
                "https://example.test",
                "http://proxy-a:8000",
                pool,
            )

        self.assertIs(result, solution)
        self.assertEqual(proxy, "http://proxy-b:8000")
        self.assertTrue(pool.is_blocked("http://proxy-a:8000"))
        self.assertEqual(solve.await_count, 2)
        pool.release_blocked()
        self.assertFalse(pool.is_blocked("http://proxy-a:8000"))

    async def test_cancel_result_can_be_confirmed_inside_a_frame(self) -> None:
        directory, store = self.make_store()
        self.addCleanup(directory.cleanup)
        runner = FormRunner(store)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            await page.set_content(
                '<iframe srcdoc="<p>It\'s not you, it\'s us. We had a problem processing your order. You haven\'t been charged. Go back to plans</p>"></iframe>'
            )
            self.assertTrue(await runner._has_cancel_result_page([page]))
            await browser.close()

    async def test_wait_without_cancel_honors_configured_timeout(self) -> None:
        directory, store = self.make_store()
        self.addCleanup(directory.cleanup)
        runner = FormRunner(store)
        started = asyncio.get_running_loop().time()
        await runner._wait_for_timeout(40)
        self.assertGreaterEqual(asyncio.get_running_loop().time() - started, 0.03)

    async def test_settlement_wait_uses_minutes_and_keeps_browser_wait_helper(self) -> None:
        directory, store = self.make_store()
        self.addCleanup(directory.cleanup)
        runner = FormRunner(store)
        runner._wait_for_timeout = AsyncMock()

        await runner._wait_for_settlement({"settlement_wait_minutes": 1.5})

        runner._wait_for_timeout.assert_awaited_once_with(90000)

    def test_page_uses_panel_timeout_for_actions_and_navigation(self) -> None:
        page = FakePage()
        FormRunner._configure_page(page, 45000)
        self.assertEqual(page.action_timeout, 45000)
        self.assertEqual(page.navigation_timeout, 45000)

    def test_generated_birth_year_is_limited_to_2000_through_2004(self) -> None:
        years = {int(FormRunner._generate_profile()["dob_year"]) for _ in range(100)}
        self.assertTrue(years)
        self.assertTrue(all(2000 <= year <= 2004 for year in years))

    async def test_timeout_screenshot_is_saved_before_playwright_stops(self) -> None:
        directory, store = self.make_store()
        self.addCleanup(directory.cleanup)
        store.add_task_record(
            {
                "id": "record-1",
                "email": "one@example.com",
                "status": "running",
                "screenshots": [],
            }
        )
        settings = {**DEFAULT_SETTINGS, "target_url": "data:text/html,<html><body>test</body></html>", "timeout_ms": 1000, "debug_mode": True, "cookie_reject_selector": "", "choose_plan_selector": "#missing"}
        solution = FlareSolverSolution("<html></html>", [], "", settings["target_url"])
        runner = FormRunner(store)

        with patch("runner.solve_page", new=AsyncMock(return_value=solution)):
            await runner._execute_one(
                {"record_id": "record-1", "email": "one@example.com", "postcode": "M14 7HX"},
                settings,
                ProxyPool([], False),
            )

        state = store.snapshot()
        record = state["task_records"][0]
        self.assertEqual(record["status"], "error")
        self.assertEqual(len(record["screenshots"]), 1)
        self.assertTrue((Path(directory.name) / record["screenshots"][0]).is_file())

    async def test_debug_screenshot_is_saved_on_success_before_playwright_stops(self) -> None:
        directory, store = self.make_store()
        self.addCleanup(directory.cleanup)
        store.add_task_record(
            {
                "id": "record-1",
                "email": "one@example.com",
                "status": "running",
                "screenshots": [],
            }
        )
        html = """
            <div id="cookie-overlay" style="display:block;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:99">
                <button id="onetrust-reject-all-handler" style="display:none" onclick="document.querySelector('#cookie-overlay').style.display='none'">Accept essential cookies only</button>
            </div>
            <button style="display:none">Continue</button><button id="choose">Choose</button><button id="continue">Continue</button>
            <input id="email"><input id="firstName"><input id="lastName">
            <input id="dateOfBirth-day"><input id="dateOfBirth-month"><input id="dateOfBirth-year">
            <input id="postcode-input-postcode"><button id="postcode-input-postcode-trigger">Lookup</button>
            <select id="address-select-postcode"><option value="address">Address</option></select>
            <input id="password"><input id="pin"><input id="memorableWord">
            <input id="consentAgreement" type="checkbox">
            <input id="btnCancel" type="button" value="Cancel" style="display:none" onclick="this.style.display='none'; document.querySelector('#cancel-result').style.display='block'">
            <div id="cancel-result" style="display:none">It's not you, it's us! We had a problem processing your order. You haven't been charged. Please try again. Go back to plans Order ID: TEST</div>
            <script>
                setTimeout(() => document.querySelector('#onetrust-reject-all-handler').style.display = 'block', 3000);
                document.querySelector('#consentAgreement').addEventListener('click', () => {
                    setTimeout(() => document.querySelector('#btnCancel').style.display = 'block', 250);
                });
            </script>
        """
        settings = {
            **DEFAULT_SETTINGS,
            "target_url": "data:text/html," + quote(html),
            "timeout_ms": 1000,
            "debug_mode": True,
            "cookie_reject_selector": "#onetrust-reject-all-handler",
            "choose_plan_selector": "#choose",
            "continue_selector": "#continue",
        }
        solution = FlareSolverSolution("<html></html>", [], "", settings["target_url"])
        runner = FormRunner(store)

        with patch("runner.solve_page", new=AsyncMock(return_value=solution)):
            await runner._execute_one(
                {"record_id": "record-1", "email": "one@example.com", "postcode": "M14 7HX"},
                settings,
                ProxyPool([], False),
            )

        state = store.snapshot()
        record = state["task_records"][0]
        self.assertEqual(record["status"], "success")
        self.assertEqual(len(record["screenshots"]), 1)
        self.assertIn("success", record["screenshots"][0])
        self.assertTrue((Path(directory.name) / record["screenshots"][0]).is_file())


class TaskRecordTests(unittest.TestCase):
    def test_completion_is_counted_once_and_records_can_be_deleted(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = Store(directory.name)
        store.add_task_record({"id": "record-1", "email": "one@example.com", "status": "running"})

        self.assertTrue(store.complete_task_record("record-1", "success", "完成", "finished"))
        self.assertFalse(store.complete_task_record("record-1", "error", "重复", "finished-again"))
        self.assertEqual(store.snapshot()["runtime"]["task_completed"], 1)
        self.assertTrue(store.delete_task_record("record-1"))
        self.assertFalse(store.delete_task_record("record-1"))

    def test_clear_task_records_removes_all_records(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = Store(directory.name)
        store.add_task_record({"id": "record-1", "email": "one@example.com", "status": "running"})
        store.add_task_record({"id": "record-2", "email": "two@example.com", "status": "running"})
        store.clear_task_records()
        self.assertEqual(store.snapshot()["task_records"], [])


if __name__ == "__main__":
    unittest.main()
