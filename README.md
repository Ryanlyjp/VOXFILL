# Voxtry

本地网站的 Playwright 填表面板，面板风格参考 `gmail-username-checker`，支持：

- 邮箱一行一个，按顺序取用；已使用邮箱持久化并自动跳过；可清除全部标记。
- 邮编一行一个，每次任务随机取一个。
- 输入邮编后，随机选择页面返回的地址候选项。
- 随机生成名字、姓氏、出生日期、密码、PIN 和记忆词；姓名池包含大量常见和少见名字、姓氏，降低重复概率。
- 顶部支持在“填表”和“随机生成”页面之间切换，默认进入随机生成页；首次打开或从填表页切回时会自动按当前邮编池创建一张资料卡，支持单字段复制、逐行全部复制、改名、固定和删除。
- 资料卡固定后不会被“全部删除”清理，固定状态不改变卡片原有排列顺序；删除固定卡片时会在面板中央要求确认，未固定卡片可直接删除。
- 资料卡保存在 `data/state.json`，关闭并重新打开网页后仍会保留。
- 完整执行到支付页，可按设置选择点击 `Cancel` 进入订单错误结果页，或停留在支付页等待超时后关闭浏览器；不执行支付。
- Debug 模式下，流程异常会自动保存卡点截图，并在面板中提供查看链接。
- 打开目标页面前由项目内置的 FlareSolverr Chromium 处理 Cloudflare 校验，并将返回的 HTML、`cf_clearance` Cookie 和 user-agent 交给后续 Playwright 流程。
- 目标 URL、关键 CSS 选择器、无头模式和代理池可在面板设置；代理自动识别常用格式。
- 支持设置任务次数和并发数：并发数为 `1` 时连续串行执行，大于 `1` 时并行执行。
- 代理池默认每个任务随机选择代理；检测到代理连接失败时会在当前批次内暂时拉黑并换代理重试一次，批次结束后解除拉黑。
- Cookie 弹窗会等待延迟渲染的“Accept essential cookies only”按钮并点击，避免遮罩层拦截后续 Continue。
- 每批任务启动前校验任务次数不得超过可用邮箱数量；任务使用的邮箱会先标记为已用。
- 面板保留每次任务的邮箱、开始时间、成功/失败结果和失败截图，并支持删除单条或全部记录。
- 运行日志支持关闭自动滚动；关闭后轮询不会把日志视图强制跳到底部。
- 单次任务默认不提交支付，流程执行到支付页的 `Cancel`；Debug 模式下无论成功或失败都会在浏览器关闭前保存最后界面截图。

## Docker 启动

```bash
cd voxtry
cp .env.example .env
docker-compose up -d --build
```

打开 `http://127.0.0.1:48765`。在首次启动前，编辑 `.env`：至少将
`SECRET_KEY` 替换为稳定的随机值，并设置仅供本机使用的 `PANEL_PASSWORD`。

Compose 只将宿主机的 `127.0.0.1:48765` 映射到容器面板端口；如果需要换端口，修改 `.env` 中的 `VOXTRY_PORT`。默认目标 URL 为 `https://voxi.co.uk/sim-only-plans`，可通过 `TARGET_URL` 修改。

Compose 会从项目内的 `flaresolverr/` 目录构建 Chromium stealth solver。FlareSolverr 只在 Docker 网络内供 `voxtry` 使用，不发布宿主机端口。求解请求沿用当前任务代理，默认等待 `180000` 毫秒，可通过 `.env` 中的 `VOXTRY_FLARESOLVERR_TIMEOUT_MS` 调整。

默认流程已从套餐列表页开始，直接选择套餐并继续。只有将目标 URL 改回首页时，才需要在面板中启用“从首页进入套餐列表”。

如果系统安装的是 Docker Compose v2，也可将以下命令中的 `docker-compose`
替换为 `docker compose`。查看运行状态和停止服务：

```bash
docker-compose ps
docker-compose down
```

如果目标网站运行在宿主机，例如宿主机的 `59173` 端口，可将 `TARGET_URL` 改为
`http://host.docker.internal:59173`。Linux Docker 环境通过 compose 中的
`host-gateway` 映射访问宿主机。

## 代理池格式

代理一行一条，以下格式会自动转换为 HTTP 代理，无需手工补充协议：

```text
host:port
host:port:username:password
username:password@host:port
```

已带协议的标准地址会原样使用，例如 `https://host:port` 或
`socks5://username:password@host:port`。纯 `host:port` 无法自动判断协议，因此按
HTTP 处理；SOCKS 代理请保留 `socks5://` 前缀。

面板勾选“每次任务随机选择代理”后，同一批次会随机取用可用代理；连接失败的代理不会被同一批次后续任务再次选取。

## 录制选择器

初始选择器来自目标站点页面录制：

- 邮箱：`#email`
- 邮编：`#postcode-input-postcode`
- 邮编查询触发器：`#postcode-input-postcode-trigger`
- 地址候选：`#address-select-postcode`
- 选择套餐：`[data-test="choose-plan-button"]`
- Continue：`button[aria-label="Continue"]`
- 支付页 Cancel：`input#btnCancel`（支付控件位于 iframe）
- “结算等待时间（分钟）”：到达 checkout 后保持浏览器开启并等待指定时间，默认 `0` 分钟。
- “任务结束时点击 Cancel”：默认勾选。结算等待结束后，勾选时点击支付页 Cancel 并确认错误结果页；取消勾选时直接关闭浏览器。

随机出生年份限制为 `2000` 至 `2004`。

录制包含真实站点的密码、PIN、记忆词和支付跳转步骤，本项目会执行这些填写；是否点击支付页的 `Cancel` 由面板设置决定，不会提交支付。

## 本地运行

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
python start_webui.py --host 127.0.0.1 --port 48765
```

需要先安装 Chromium：

```bash
python -m playwright install chromium
```
