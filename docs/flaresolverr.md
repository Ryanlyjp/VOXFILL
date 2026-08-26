# Voxtry FlareSolverr

Voxtry 在每次任务打开目标页前，向项目内的 FlareSolverr 服务发送标准 `request.get` 请求。服务使用 Chromium stealth driver 执行页面 JavaScript 和 fingerprint 校验，并返回 HTML、Cookie 集合和 user-agent。

如果目标站点触发 Cloudflare challenge，返回的 Cookie 集合通常包含 `cf_clearance`；如果本次请求没有触发 challenge，solver 可能只返回站点普通 Cookie。两种 `status=ok` 且带 HTML 的结果都可继续。任务使用同一代理和返回的 user-agent 创建 Playwright context，注入完整 Cookie 集合，再打开目标 URL 执行原有填表步骤。HTML 只用于确认 solver 返回了有效页面，不直接替代 Playwright 页面，因为后续流程仍需要真实页面资源和脚本。

FlareSolverr 服务只通过 Docker 内部的 `flaresolverr:8191` 地址访问，不映射宿主机端口。solver 超时由 `VOXTRY_FLARESOLVERR_TIMEOUT_MS` 控制，默认值为 `180000` 毫秒。
