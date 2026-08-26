"""Playwright runner for the complete local form flow."""
from __future__ import annotations

import asyncio
import calendar
import re
import secrets
import string
import sys
import uuid
from typing import Any

from playwright.async_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from flaresolverr_client import FlareSolverError, solve_page
from proxy_pool import ProxyPool, mask_proxy, parse_proxy_pool, to_playwright_proxy
from storage import Store


FIRST_NAMES = ["Ava", "Chloe", "Ella", "Haily", "Isla", "Lily", "Maya", "Sophie"]
LAST_NAMES = ["Bennett", "Carter", "Gilson", "Harris", "Morgan", "Parker", "Taylor", "Wilson"]
MEMORABLE_WORDS = ["bluebird", "ellabo", "moonlight", "pebble", "sunrise", "violet"]


class FormRunner:
    def __init__(self, store: Store):
        self.store = store
        self._batch_task: asyncio.Task[None] | None = None
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

    def status(self) -> dict[str, Any]:
        return self.store.snapshot()["runtime"]

    def start(self) -> dict[str, Any]:
        if self._batch_task and not self._batch_task.done():
            raise RuntimeError("已有任务正在运行")
        snapshot = self.store.snapshot()
        postcodes = snapshot["postcodes"]
        if not postcodes:
            raise RuntimeError("没有可用邮编，请先录入邮编")
        settings = snapshot["settings"]
        try:
            task_count = int(settings.get("task_count", 1))
            concurrency = int(settings.get("concurrency", 1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("任务次数和并发数必须是整数") from exc
        if task_count < 1:
            raise RuntimeError("任务次数不能小于 1")
        if concurrency < 1:
            raise RuntimeError("并发数不能小于 1")
        if task_count > self.store.available_email_count():
            raise RuntimeError("任务次数不能大于可用邮箱数量")
        emails = self.store.claim_next_emails(task_count)
        if len(emails) != task_count:
            raise RuntimeError("可用邮箱数量不足，任务未启动")

        jobs: list[dict[str, str]] = []
        for email in emails:
            record_id = uuid.uuid4().hex
            job = {
                "record_id": record_id,
                "email": email,
                "postcode": secrets.choice(postcodes),
            }
            jobs.append(job)
            self.store.add_task_record(
                {
                    "id": record_id,
                    "email": email,
                    "postcode": job["postcode"],
                    "status": "running",
                    "message": "等待执行",
                    "started_at": None,
                    "finished_at": None,
                    "screenshots": [],
                }
            )

        self._stop = asyncio.Event()
        self.store.set_runtime(
            status="running",
            message=f"批次已启动：{task_count} 个任务，并发 {concurrency}",
            email="",
            postcode="",
            started_at=self._timestamp(),
            finished_at=None,
            logs=[],
            screenshots=[],
            task_total=task_count,
            task_completed=0,
            task_success=0,
            task_failed=0,
            task_concurrency=concurrency,
        )
        self._log(f"批次启动：任务数={task_count}，并发数={concurrency}")
        self._batch_task = asyncio.create_task(self._run_batch(jobs, settings, concurrency))
        return self.status()

    def stop(self) -> dict[str, Any]:
        if not self._batch_task or self._batch_task.done():
            return self.status()
        self._stop.set()
        self._log("收到停止请求", "warn")
        self.store.set_runtime(status="stopping", message="正在停止浏览器任务")
        for task in self._worker_tasks:
            task.cancel()
        return self.status()

    async def _run_batch(
        self,
        jobs: list[dict[str, str]],
        settings: dict[str, Any],
        concurrency: int,
    ) -> None:
        queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        for job in jobs:
            queue.put_nowait(job)
        pool = ProxyPool(
            parse_proxy_pool(str(settings.get("proxy_pool", ""))),
            bool(settings.get("proxy_rotation", False)),
        )

        async def worker() -> None:
            while True:
                try:
                    job = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    if self._stop.is_set():
                        self.store.complete_task_record(
                            job["record_id"],
                            "stopped",
                            "任务已停止",
                            self._timestamp(),
                        )
                    else:
                        await self._execute_one(job, settings, pool)
                except asyncio.CancelledError:
                    self.store.complete_task_record(
                        job["record_id"],
                        "stopped",
                        "任务已停止",
                        self._timestamp(),
                    )
                    raise
                except Exception as exc:
                    message = f"任务异常：{type(exc).__name__}: {exc}"
                    self._log(message, "error")
                    self.store.complete_task_record(
                        job["record_id"],
                        "error",
                        message,
                        self._timestamp(),
                    )
                finally:
                    queue.task_done()

        worker_count = min(max(1, concurrency), len(jobs))
        try:
            self._worker_tasks = [asyncio.create_task(worker()) for _ in range(worker_count)]
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        finally:
            self._worker_tasks = []
            pool.release_blocked()

        runtime = self.status()
        if self._stop.is_set():
            message = f"批次已停止：完成 {runtime.get('task_completed', 0)}/{len(jobs)}"
            self.store.set_runtime(status="stopped", message=message, finished_at=self._timestamp())
        elif runtime.get("task_failed", 0):
            message = (
                f"批次完成：成功 {runtime.get('task_success', 0)}，"
                f"失败 {runtime.get('task_failed', 0)}"
            )
            self.store.set_runtime(status="error", message=message, finished_at=self._timestamp())
        else:
            message = f"批次完成：成功 {runtime.get('task_success', 0)}"
            self.store.set_runtime(status="success", message=message, finished_at=self._timestamp())

    async def _execute_one(
        self,
        job: dict[str, str],
        settings: dict[str, Any],
        pool: ProxyPool,
    ) -> None:
        record_id = job["record_id"]
        email = job["email"]
        postcode = job["postcode"]
        profile = self._generate_profile()
        proxy = pool.acquire()
        browser: Browser | None = None
        page: Page | None = None
        try:
            if proxy is None and pool.has_configured_proxies():
                raise RuntimeError("当前批次没有可用代理")
            timeout = max(1000, int(settings.get("timeout_ms", 15000)))
            self.store.update_task_record(record_id, started_at=self._timestamp(), message="任务执行中")
            self._log(f"任务启动：邮箱={email}，随机邮编={postcode}")
            self._log(
                "随机资料："
                f"firstName={profile['first_name']}，lastName={profile['last_name']}，"
                f"DOB={profile['dob_day']}/{profile['dob_month']}/{profile['dob_year']}，"
                f"password={profile['password']}，PIN={profile['pin']}，"
                f"memorableWord={profile['memorable_word']}"
            )
            self._log(f"代理：{mask_proxy(proxy) if proxy else '直连'}")
            self._ensure_running()
            playwright_manager = async_playwright()
            playwright = await playwright_manager.start()
            try:
                self._log("准备阶段：启动内置 FlareSolverr Chromium 并处理页面校验")
                solver_solution, proxy = await self._solve_with_proxy_retry(
                    str(settings["target_url"]),
                    proxy,
                    pool,
                )
                self._ensure_running()
                self._log(
                    "准备阶段：FlareSolverr 已返回 HTML（"
                    f"{len(solver_solution.html)} 字符）和 {len(solver_solution.cookies)} 条 Cookie"
                )
                self._log("步骤 1/16：启动 Chromium 并注入 solver cookies")
                browser = await playwright.chromium.launch(
                    headless=bool(settings.get("headless", True)),
                    proxy=to_playwright_proxy(proxy),
                )
                context = await browser.new_context(
                    user_agent=solver_solution.user_agent or None,
                )
                await context.add_cookies(solver_solution.cookies)
                page = await context.new_page()
                self._configure_page(page, timeout)
                self._log(f"页面超时设置已生效：{timeout}ms")

                self._log(f"步骤 2/16：打开目标页面 {settings['target_url']}")
                await page.goto(str(settings["target_url"]), wait_until="domcontentloaded")
                self._log(f"页面已打开：{page.url}")

                await self._optional_click(
                    page,
                    str(settings["cookie_reject_selector"]),
                    "步骤 3/16：处理 Cookie 弹窗",
                    wait_timeout_ms=10_000,
                    fallback_selectors=(
                        'button:has-text("Accept essential cookies only")',
                        '[role="button"]:has-text("Accept essential cookies only")',
                    ),
                )
                self._ensure_running()

                if bool(settings.get("follow_plan_flow", True)):
                    await self._optional_click(page, str(settings["plan_link_selector"]), "步骤 4/16：从首页进入套餐列表")
                else:
                    self._log("步骤 4/16：已直接打开套餐列表", "info")
                await self._click_required(page, str(settings["choose_plan_selector"]), "步骤 5/16：选择套餐")
                await self._click_required(page, str(settings["continue_selector"]), "步骤 6/16：套餐页点击 Continue")

                self._ensure_running()
                await self._fill_required(page, str(settings["email_selector"]), email, "步骤 7/16：填写邮箱")
                await self._fill_required(page, str(settings["first_name_selector"]), profile["first_name"], "步骤 8/16：填写名字")
                await self._fill_required(page, str(settings["last_name_selector"]), profile["last_name"], "步骤 9/16：填写姓氏")
                await self._fill_required(page, str(settings["dob_day_selector"]), profile["dob_day"], "步骤 10/16：填写出生日期-日")
                await self._fill_required(page, str(settings["dob_month_selector"]), profile["dob_month"], "步骤 10/16：填写出生日期-月")
                await self._fill_required(page, str(settings["dob_year_selector"]), profile["dob_year"], "步骤 10/16：填写出生日期-年")
                await self._click_required(page, str(settings["continue_selector"]), "步骤 11/16：个人资料点击 Continue")

                await self._fill_required(page, str(settings["postcode_selector"]), postcode, "步骤 12/16：填写邮编")
                await self._click_required(page, str(settings["postcode_trigger_selector"]), "步骤 12/16：查询邮编地址")
                await self._select_random_address(page, str(settings["address_selector"]), "步骤 12/16：随机选择地址")
                await self._click_required(page, str(settings["continue_selector"]), "步骤 13/16：地址页点击 Continue")

                await self._fill_required(page, str(settings["password_selector"]), profile["password"], "步骤 14/16：填写随机密码")
                await self._fill_required(page, str(settings["pin_selector"]), profile["pin"], "步骤 14/16：填写随机 PIN")
                await self._fill_required(page, str(settings["memorable_word_selector"]), profile["memorable_word"], "步骤 14/16：填写记忆词")
                await self._click_required(page, str(settings["continue_selector"]), "步骤 15/16：安全资料点击 Continue")
                await self._ensure_checked(page, str(settings["consent_selector"]), "步骤 15/16：勾选同意条款")

                page, cancel_locator = await self._wait_for_cancel_page(
                    page,
                    str(settings["payment_cancel_selector"]),
                    timeout,
                )
                self._log(f"步骤 16/16：已到支付页 {page.url}")
                await self._wait_for_settlement(settings)
                if bool(settings.get("click_cancel", True)):
                    before_frame_urls = {frame.url for frame in page.frames}
                    before_page_count = len(page.context.pages)
                    cancel_deadline = asyncio.get_running_loop().time() + max(1, timeout / 1000)
                    try:
                        await cancel_locator.click(timeout=min(timeout, 5000))
                    except PlaywrightTimeoutError:
                        self._log("步骤 16/16：Cancel 常规点击被拦截，尝试强制点击", "warn")
                        await cancel_locator.click(force=True)
                    self._log("步骤 16/16：Cancel 点击事件已发送，等待心碎结果页", "debug")
                    grace_ms = min(timeout, 3000)
                    confirmed = await self._wait_for_cancel_result(
                        page,
                        before_frame_urls,
                        before_page_count,
                        grace_ms,
                    )
                    if not confirmed:
                        self._log("步骤 16/16：未检测到结果页，补发 Cancel 点击", "warn")
                        try:
                            await cancel_locator.click(force=True)
                        except Exception:
                            await cancel_locator.evaluate("element => element.click()")
                        remaining_ms = max(
                            1,
                            int((cancel_deadline - asyncio.get_running_loop().time()) * 1000),
                        )
                        confirmed = await self._wait_for_cancel_result(
                            page,
                            before_frame_urls,
                            before_page_count,
                            remaining_ms,
                        )
                    if not confirmed:
                        raise RuntimeError("Cancel 已点击，但未确认后续结果页")
                    self._log("步骤 16/16：点击 Cancel：完成")
                    self._log("流程完成：Cancel 已点击", "ok")
                    result_message = "流程完成，已点击 Cancel"
                else:
                    self._log(
                        "步骤 16/16：已取消点击 Cancel，等待结束后关闭浏览器",
                        "info",
                    )
                    self._log("步骤 16/16：等待结束，保留当前支付页", "ok")
                    result_message = "流程完成，未点击 Cancel，已完成结算等待"
                self.store.complete_task_record(
                    record_id,
                    "success",
                    result_message,
                    self._timestamp(),
                )
            finally:
                if bool(settings.get("debug_mode", False)):
                    stage = "failure" if sys.exc_info()[0] is not None else "success"
                    await self._debug_screenshot(page, browser, stage, record_id)
                if browser:
                    try:
                        await browser.close()
                    except Exception as exc:
                        self._log(f"浏览器关闭失败：{type(exc).__name__}: {exc}", "warn")
                    browser = None
                try:
                    await playwright.stop()
                except Exception as exc:
                    self._log(f"Playwright 关闭失败：{type(exc).__name__}: {exc}", "warn")
        except asyncio.CancelledError:
            self._log("任务已停止", "warn")
            self.store.complete_task_record(record_id, "stopped", "任务已停止", self._timestamp())
        except Exception as exc:
            message = f"流程失败：{type(exc).__name__}: {exc}"
            self._log(message, "error")
            self.store.complete_task_record(record_id, "error", message, self._timestamp())
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception as exc:
                    self._log(f"浏览器关闭失败：{type(exc).__name__}: {exc}", "warn")

    async def _solve_with_proxy_retry(
        self,
        url: str,
        proxy: str | None,
        pool: ProxyPool,
    ) -> tuple[Any, str | None]:
        try:
            return await solve_page(url, proxy), proxy
        except Exception as exc:
            if not proxy or not self._is_proxy_connectivity_error(exc):
                raise
            pool.block(proxy)
            retry_proxy = pool.acquire()
            if retry_proxy is None and pool.has_configured_proxies():
                raise
            self._log(
                f"代理连接失败，已临时拉黑：{mask_proxy(proxy)}；"
                f"切换代理重试：{mask_proxy(retry_proxy) if retry_proxy else '直连'}",
                "warn",
            )
            try:
                return await solve_page(url, retry_proxy), retry_proxy
            except Exception as retry_exc:
                if retry_proxy and self._is_proxy_connectivity_error(retry_exc):
                    pool.block(retry_proxy)
                    self._log(f"重试代理连接仍失败，已临时拉黑：{mask_proxy(retry_proxy)}", "warn")
                raise

    @staticmethod
    def _is_proxy_connectivity_error(exc: Exception) -> bool:
        if not isinstance(exc, FlareSolverError):
            return False
        text = str(exc).casefold()
        return any(
            marker in text
            for marker in (
                "err_tunnel_connection_failed",
                "connection refused",
                "connection reset",
                "network is unreachable",
                "timed out",
                "name or service not known",
            )
        )

    async def _fill_required(self, page: Page, selector: str, value: str, step: str) -> None:
        self._ensure_running()
        if not selector:
            raise RuntimeError(f"{step} 未配置选择器")
        locator = await self._first_visible(page, selector)
        await locator.wait_for(state="visible")
        await locator.fill(value)
        self._log(f"{step}：完成，值={value}")

    @staticmethod
    def _configure_page(page: Page, timeout: int) -> None:
        page.set_default_timeout(timeout)
        page.set_default_navigation_timeout(timeout)

    async def _click_required(self, page: Page, selector: str, step: str) -> None:
        self._ensure_running()
        if not selector:
            raise RuntimeError(f"{step} 未配置选择器")
        locator = await self._first_visible(page, selector)
        await locator.wait_for(state="visible")
        await locator.click()
        self._log(f"{step}：完成")

    async def _first_visible(self, page: Page, selector: str):
        return page.locator(f"{selector}:visible").first

    async def _optional_click(
        self,
        page: Page,
        selector: str,
        step: str,
        *,
        wait_timeout_ms: int = 2500,
        fallback_selectors: tuple[str, ...] = (),
    ) -> bool:
        candidates = tuple(item for item in (selector, *fallback_selectors) if item)
        if not candidates:
            self._log(f"{step}：已跳过，未配置选择器", "debug")
            return False
        deadline = asyncio.get_running_loop().time() + max(1, wait_timeout_ms / 1000)
        while asyncio.get_running_loop().time() < deadline:
            for candidate in candidates:
                locator = await self._first_visible(page, candidate)
                if await locator.count():
                    try:
                        await locator.click()
                    except PlaywrightTimeoutError:
                        continue
                    self._log(f"{step}：完成，选择器={candidate}")
                    return True
            await asyncio.sleep(0.25)
        self._log(f"{step}：未找到，继续执行", "warn")
        return False

    async def _ensure_checked(self, page: Page, selector: str, step: str) -> None:
        self._ensure_running()
        if not selector:
            raise RuntimeError(f"{step} 未配置选择器")
        locator = await self._first_visible(page, selector)
        await locator.wait_for(state="visible")
        try:
            checked = await locator.is_checked()
        except Exception:
            checked = False
        if not checked:
            await locator.click()
        self._log(f"{step}：已勾选")

    async def _select_random_address(self, page: Page, selector: str, step: str) -> None:
        self._ensure_running()
        if not selector:
            raise RuntimeError(f"{step} 未配置选择器")
        locator = await self._first_visible(page, selector)
        await locator.wait_for(state="visible")
        tag_name = await locator.evaluate("element => element.tagName")
        if tag_name.upper() == "SELECT":
            values = await locator.locator("option").evaluate_all(
                "options => options.map(option => ({value: option.value, text: option.textContent.trim(), disabled: option.disabled}))"
            )
            values = [item for item in values if item["value"] and not item["disabled"]]
            if values:
                selected = secrets.choice(values)
                await locator.select_option(value=selected["value"])
                self._log(f"{step}：已选择 {selected['text']}")
                return
        await locator.click()
        options = page.locator('[role="option"]:visible')
        count = await options.count()
        if not count:
            raise RuntimeError(f"{step} 未找到地址候选项")
        selected = options.nth(secrets.randbelow(count))
        label = (await selected.inner_text()).strip()
        await selected.click()
        self._log(f"{step}：已选择 {label or '候选项'}")

    async def _wait_for_cancel_page(self, page: Page, selector: str, timeout_ms: int):
        self._ensure_running()
        if not selector:
            raise RuntimeError("等待支付页 Cancel 超时：未配置 Cancel 选择器")
        self._log("等待支付页加载和 Cancel 按钮")
        deadline = asyncio.get_running_loop().time() + max(1, timeout_ms / 1000)
        while asyncio.get_running_loop().time() < deadline:
            self._ensure_running()
            target = await self._find_cancel_target(page, selector)
            if target:
                owner, locator, matched_selector = target
                self._log(f"Cancel 已识别：{matched_selector}，页面={owner.url}", "debug")
                return owner, locator
            await asyncio.sleep(0.25)
        raise RuntimeError(f"等待支付页 Cancel 超时：{selector}")

    async def _find_cancel_target(self, page: Page, selector: str):
        fallback_selectors = (
            'button:has-text("Cancel")',
            'input[type="button"][value*="Cancel" i]',
            'input[type="submit"][value*="Cancel" i]',
            '[role="button"]:has-text("Cancel")',
        )
        for candidate in page.context.pages:
            documents = [candidate, *candidate.frames]
            for document in documents:
                for candidate_selector in (selector, *fallback_selectors):
                    locator = document.locator(f"{candidate_selector}:visible").first
                    if await locator.count():
                        return candidate, locator, candidate_selector
        return None

    async def _wait_for_cancel_result(
        self,
        page: Page,
        before_frame_urls: set[str],
        before_page_count: int,
        timeout_ms: int,
    ) -> bool:
        self._log("等待 Cancel 点击后的结果页")
        deadline = asyncio.get_running_loop().time() + max(1, timeout_ms / 1000)
        while asyncio.get_running_loop().time() < deadline:
            self._ensure_running()
            if len(page.context.pages) > before_page_count:
                candidates = page.context.pages[before_page_count:]
                if await self._has_cancel_result_page(candidates):
                    self._log("Cancel 后结果页已确认：新页面出现心碎结果", "debug")
                    return True
            current_frame_urls = {frame.url for frame in page.frames}
            if current_frame_urls - before_frame_urls:
                if await self._has_cancel_result_page(page.context.pages):
                    self._log("Cancel 后结果页已确认：页面出现心碎结果", "debug")
                    return True
            if await self._has_cancel_result_page(page.context.pages):
                self._log("Cancel 后结果页已确认：页面出现心碎结果", "debug")
                return True
            await asyncio.sleep(0.25)
        return False

    async def _has_cancel_result_page(self, pages: list[Page]) -> bool:
        error_markers = (
            "it's not you, it's us",
            "we had a problem processing your order",
        )
        completion_markers = ("you haven't been charged", "go back to plans")
        for candidate in pages:
            if candidate.is_closed():
                continue
            for document in (candidate, *candidate.frames):
                try:
                    text = await document.locator("body").inner_text(timeout=1000)
                except Exception:
                    continue
                normalized = " ".join(
                    text.replace("\u2018", "'").replace("\u2019", "'").split()
                ).casefold()
                if any(marker in normalized for marker in error_markers) and any(
                    marker in normalized for marker in completion_markers
                ):
                    return True
        return False

    async def _wait_for_timeout(self, timeout_ms: int) -> None:
        deadline = asyncio.get_running_loop().time() + max(1, timeout_ms / 1000)
        while asyncio.get_running_loop().time() < deadline:
            self._ensure_running()
            remaining = deadline - asyncio.get_running_loop().time()
            await asyncio.sleep(min(0.25, max(0.01, remaining)))

    async def _wait_for_settlement(self, settings: dict[str, Any]) -> None:
        try:
            minutes = float(settings.get("settlement_wait_minutes", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("结算等待时间必须是数字") from exc
        if minutes < 0:
            raise RuntimeError("结算等待时间不能小于 0")
        wait_ms = int(round(minutes * 60_000))
        if wait_ms <= 0:
            self._log("结算等待：0 分钟，继续执行最终步骤", "debug")
            return
        self._log(f"结算等待开始：保持浏览器开启 {minutes:g} 分钟", "info")
        await self._wait_for_timeout(wait_ms)
        self._log("结算等待结束：开始执行最终步骤", "info")

    async def _debug_screenshot(
        self,
        page: Page | None,
        browser: Browser | None,
        stage: str,
        record_id: str,
    ) -> None:
        try:
            debug_dir = self.store.data_dir / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            safe_stage = re.sub(r"[^A-Za-z0-9_.-]+", "_", stage)
            filename = f"debug/{self._timestamp().replace(':', '-')}_{safe_stage}_{record_id[:8]}.png"
            path = self.store.data_dir / filename
            candidates: list[Page] = []
            if page is not None:
                candidates.append(page)
            if browser is not None:
                for context in browser.contexts:
                    candidates.extend(context.pages)
            seen: set[int] = set()
            for candidate in candidates:
                marker = id(candidate)
                if marker in seen or candidate.is_closed():
                    continue
                seen.add(marker)
                try:
                    await candidate.screenshot(path=str(path), full_page=True, timeout=5000)
                    self.store.add_screenshot_for_task(filename, record_id)
                    self._log(f"Debug 截图已保存：{filename}", "debug")
                    return
                except Exception:
                    continue
            raise RuntimeError("没有可用的浏览器页面")
        except Exception as exc:
            self._log(f"Debug 截图失败：{type(exc).__name__}: {exc}", "warn")

    def _log(self, message: str, level: str = "info") -> None:
        self.store.append_log(message, level)

    def _ensure_running(self) -> None:
        if self._stop.is_set():
            raise asyncio.CancelledError

    @staticmethod
    def _generate_profile() -> dict[str, str]:
        rng = secrets.SystemRandom()
        first_name = rng.choice(FIRST_NAMES)
        last_name = rng.choice(LAST_NAMES)
        year = rng.randint(2000, 2004)
        month = rng.randint(1, 12)
        day = rng.randint(1, calendar.monthrange(year, month)[1])
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        password_chars = [
            rng.choice(string.ascii_uppercase),
            rng.choice(string.ascii_lowercase),
            rng.choice(string.digits),
            rng.choice("!@#$%^&*"),
        ]
        password_chars.extend(rng.choice(alphabet) for _ in range(12))
        rng.shuffle(password_chars)
        return {
            "first_name": first_name,
            "last_name": last_name,
            "dob_day": f"{day:02d}",
            "dob_month": f"{month:02d}",
            "dob_year": str(year),
            "password": "".join(password_chars),
            "pin": f"{rng.randint(0, 9999):04d}",
            "memorable_word": rng.choice(MEMORABLE_WORDS),
        }

    @staticmethod
    def _timestamp() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()
