const $ = (id) => document.getElementById(id);
let pollTimer = null;
let autoScrollLogs = localStorage.getItem("voxtry-auto-scroll-logs") !== "false";

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) { showLogin(); throw new Error("未登录"); }
  if (!response.ok) throw new Error(data.detail || response.statusText);
  return data;
}

function showLogin() { $("loginMask").classList.remove("hidden"); }
function hideLogin() { $("loginMask").classList.add("hidden"); }
function lines(id) { return $(id).value.split("\n").map((item) => item.trim()).filter(Boolean); }
function setLines(id, values) { $(id).value = values.join("\n"); }
function log(message) { $("log").textContent = `[${new Date().toLocaleTimeString()}] ${message}\n` + $("log").textContent; }

function renderQueue(items) {
  $("emailCount").textContent = `${items.filter((x) => !x.used_at).length} 可用 / ${items.length} 总数`;
  $("emailTable").innerHTML = items.map((item, index) => `<div class="queue-item ${item.used_at ? "used" : ""}"><span>${index + 1}. ${escapeHtml(item.value)}</span><span>${item.used_at ? "已用" : "待用"}</span></div>`).join("");
}

function escapeHtml(value) { return String(value).replace(/[&<>"']/g, (char) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[char])); }

function renderLogs(items) {
  $("log").textContent = items.length
    ? items.map((item) => `[${new Date(item.at).toLocaleTimeString()}] ${item.message}`).join("\n")
    : "等待任务...";
  if (autoScrollLogs) $("log").scrollTop = $("log").scrollHeight;
}

function renderScreenshots(items) {
  $("screenshots").innerHTML = items.length
    ? "Debug 截图：" + items.map((item) => {
      const path = item.filename.split("/").map(encodeURIComponent).join("/");
      return `<a href="/api/debug/${path}" target="_blank" rel="noopener">${escapeHtml(item.filename)}</a>`;
    }).join(" · ")
    : "";
}

function renderTaskRecords(items) {
  $("taskRecords").innerHTML = items.length
    ? items.map((item) => {
      const status = item.status || "error";
      const label = status === "success" ? "成功" : status === "running" ? "运行中" : status === "stopped" ? "已停止" : "失败";
      const at = item.started_at ? new Date(item.started_at).toLocaleString() : "等待执行";
      return `<div class="task-record"><div><strong>${escapeHtml(item.email || "未知邮箱")}</strong><span>${escapeHtml(at)}</span></div><div class="record-result ${status}">${label}</div><button class="muted record-delete" data-record-id="${escapeHtml(item.id)}" title="删除记录">删除</button></div>`;
    }).join("")
    : '<div class="empty-state">暂无任务记录</div>';
}

function profileCardText(card) {
  return [
    `邮箱：${card.email || ""}`,
    `生日（PDF密码）：${card.dob || ""}`,
    `名字：${[card.first_name, card.last_name].filter(Boolean).join(" ")}`,
    `地址：${card.address || ""}`,
    `密码：${card.password || ""}`,
    `PIN码：${card.pin || ""}`,
    `密保答案：${card.memorable_word || ""}`,
    `号码：${card.phone || ""}`,
  ].join("\n");
}

function profileField(label, value, key) {
  return `<label class="profile-field"><span>${label}</span><div class="profile-value"><input class="profile-copyable" data-profile-key="${key}" readonly value="${escapeHtml(value || "")}" placeholder="留空" title="点击复制"></div></label>`;
}

function renderProfileCards(items) {
  $("profileCardCount").textContent = `${items.length} 张`;
  $("profileCards").innerHTML = items.length
    ? items.map((card) => `<article class="profile-card" data-profile-id="${escapeHtml(card.id)}">
        <div class="profile-card-header">
          <div class="profile-card-name"><strong>${escapeHtml(card.name)}</strong><button class="icon-button edit-profile" data-profile-id="${escapeHtml(card.id)}" aria-label="编辑名称" title="编辑名称">✎</button></div>
          <div class="profile-card-actions"><button class="primary profile-copy-all" data-profile-id="${escapeHtml(card.id)}">全部复制</button><button class="danger profile-delete" data-profile-id="${escapeHtml(card.id)}">删除</button></div>
        </div>
        <div class="profile-fields">
          ${profileField("邮箱：", card.email, "email")}
          ${profileField("生日（PDF密码）：", card.dob, "dob")}
          <div class="profile-name-row">${profileField("名字：", card.first_name, "first_name")}${profileField("姓氏：", card.last_name, "last_name")}</div>
          ${profileField("地址：", card.address, "address")}
          ${profileField("密码：", card.password, "password")}
          ${profileField("PIN码：", card.pin, "pin")}
          ${profileField("密保答案：", card.memorable_word, "memorable_word")}
          ${profileField("号码：", card.phone, "phone")}
        </div>
      </article>`).join("")
    : '<div class="empty-state">暂无资料卡，点击“生成资料”创建一张。</div>';
}

async function copyText(value) {
  try {
    await navigator.clipboard.writeText(value);
  } catch (_) {
    const input = document.createElement("textarea");
    input.value = value;
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    input.remove();
  }
}

function findProfileCard(cardId) {
  return profileCards.find((card) => card.id === cardId);
}

let profileCards = [];

function switchPage(pageId) {
  document.querySelectorAll(".page-view").forEach((page) => page.classList.toggle("hidden", page.id !== pageId));
  document.querySelectorAll(".page-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.page === pageId));
}

function updateRuntime(runtime) {
  const status = runtime.status || "idle";
  const label = { idle:"未运行", running:"运行中", stopping:"停止中", success:"已完成", error:"出错", stopped:"已停止" }[status] || status;
  $("runtimeStatus").textContent = label;
  $("runtimeStatus").className = `status ${status}`;
  const progress = runtime.task_total ? `进度：${runtime.task_completed || 0}/${runtime.task_total} · 成功 ${runtime.task_success || 0} · 失败 ${runtime.task_failed || 0}` : "";
  $("runtimeInfo").textContent = [progress, runtime.message || ""].filter(Boolean).join(" · ");
  $("startRun").disabled = status === "running" || status === "stopping";
  $("stopRun").disabled = !["running", "stopping"].includes(status);
  renderLogs(runtime.logs || []);
  renderScreenshots(runtime.screenshots || []);
}

function loadSettings(settings) {
  $("targetUrl").value = settings.target_url || "";
  $("timeoutMs").value = settings.timeout_ms ?? 15000;
  $("settlementWaitMinutes").value = settings.settlement_wait_minutes ?? 0;
  $("taskCount").value = settings.task_count ?? 1;
  $("concurrency").value = settings.concurrency ?? 1;
  $("followPlanFlow").checked = !!settings.follow_plan_flow;
  $("headless").checked = !!settings.headless;
  $("debugMode").checked = !!settings.debug_mode;
  $("clickCancel").checked = settings.click_cancel !== false;
  $("proxyRotation").checked = !!settings.proxy_rotation;
  $("proxyPool").value = settings.proxy_pool || "";
  const fields = { emailSelector:"email_selector", firstNameSelector:"first_name_selector", lastNameSelector:"last_name_selector", dobDaySelector:"dob_day_selector", dobMonthSelector:"dob_month_selector", dobYearSelector:"dob_year_selector", postcodeSelector:"postcode_selector", postcodeTriggerSelector:"postcode_trigger_selector", addressSelector:"address_selector", passwordSelector:"password_selector", pinSelector:"pin_selector", memorableWordSelector:"memorable_word_selector", consentSelector:"consent_selector", paymentCancelSelector:"payment_cancel_selector", planLinkSelector:"plan_link_selector", choosePlanSelector:"choose_plan_selector", continueSelector:"continue_selector", cookieRejectSelector:"cookie_reject_selector" };
  Object.entries(fields).forEach(([id, key]) => $(id).value = settings[key] || "");
}

async function refresh() {
  const state = await api("/api/state");
  setLines("emails", state.emails.map((x) => x.value));
  setLines("postcodes", state.postcodes);
  renderQueue(state.emails);
  $("postcodeCount").textContent = `${state.postcodes.length} 条`;
  loadSettings(state.settings);
  updateRuntime(state.runtime);
  renderTaskRecords(state.task_records || []);
  profileCards = state.profile_cards || [];
  renderProfileCards(profileCards);
}

async function saveInputs() {
  const [emails, postcodes] = await Promise.all([
    api("/api/emails", { method:"PUT", body:JSON.stringify({ text:$("emails").value }) }),
    api("/api/postcodes", { method:"PUT", body:JSON.stringify({ text:$("postcodes").value }) }),
  ]);
  renderQueue(emails.items);
  $("postcodeCount").textContent = `${postcodes.items.length} 条`;
  $("emailSaveState").textContent = "已保存";
  $("postcodeSaveState").textContent = "已保存";
  log("输入池已保存");
}

function settingsPayload() {
  const values = { target_url:$("targetUrl").value.trim(), timeout_ms:Number($("timeoutMs").value), settlement_wait_minutes:Number($("settlementWaitMinutes").value), task_count:Number($("taskCount").value), concurrency:Number($("concurrency").value), follow_plan_flow:$("followPlanFlow").checked, headless:$("headless").checked, debug_mode:$("debugMode").checked, click_cancel:$("clickCancel").checked, proxy_rotation:$("proxyRotation").checked, proxy_pool:$("proxyPool").value };
  const fields = { emailSelector:"email_selector", firstNameSelector:"first_name_selector", lastNameSelector:"last_name_selector", dobDaySelector:"dob_day_selector", dobMonthSelector:"dob_month_selector", dobYearSelector:"dob_year_selector", postcodeSelector:"postcode_selector", postcodeTriggerSelector:"postcode_trigger_selector", addressSelector:"address_selector", passwordSelector:"password_selector", pinSelector:"pin_selector", memorableWordSelector:"memorable_word_selector", consentSelector:"consent_selector", paymentCancelSelector:"payment_cancel_selector", planLinkSelector:"plan_link_selector", choosePlanSelector:"choose_plan_selector", continueSelector:"continue_selector", cookieRejectSelector:"cookie_reject_selector" };
  Object.entries(fields).forEach(([id, key]) => values[key] = $(id).value.trim());
  return values;
}

async function saveSettings() {
  const result = await api("/api/settings", { method:"PUT", body:JSON.stringify({ values:settingsPayload() }) });
  loadSettings(result.settings);
  $("settingsState").textContent = "已保存";
  return result.settings;
}

$("loginForm").addEventListener("submit", async (event) => { event.preventDefault(); try { await api("/api/login", { method:"POST", body:JSON.stringify({ password:$("loginPassword").value }) }); $("loginError").textContent = ""; hideLogin(); await boot(); } catch (error) { $("loginError").textContent = error.message; } });
$("logout").addEventListener("click", async () => { await api("/api/logout", { method:"POST" }); location.reload(); });
$("saveAll").addEventListener("click", () => saveInputs().catch((error) => alert(error.message)));
$("saveSettings").addEventListener("click", async () => { try { await saveSettings(); log("任务设置已保存"); } catch (error) { $("settingsState").textContent = error.message; } });
$("resetEmailMarks").addEventListener("click", async () => { if (!confirm("清除全部邮箱的已用标记？")) return; await api("/api/emails/reset", { method:"POST" }); await refresh(); log("已清除全部邮箱标记"); });
$("startRun").addEventListener("click", async () => { try { await saveInputs(); const settings = await saveSettings(); const result = await api("/api/run/start", { method:"POST", body:JSON.stringify({ values:settings }) }); updateRuntime(result.runtime); log("已开始任务批次"); } catch (error) { alert(error.message); } });
$("stopRun").addEventListener("click", async () => { const result = await api("/api/run/stop", { method:"POST" }); updateRuntime(result.runtime); });
$("autoScrollLogs").checked = autoScrollLogs;
$("autoScrollLogs").addEventListener("change", () => {
  autoScrollLogs = $("autoScrollLogs").checked;
  localStorage.setItem("voxtry-auto-scroll-logs", String(autoScrollLogs));
  if (autoScrollLogs) $("log").scrollTop = $("log").scrollHeight;
});
$("clearTaskRecords").addEventListener("click", async () => {
  if (!confirm("删除全部任务记录？")) return;
  await api("/api/task-records", { method:"DELETE" });
  await refresh();
});
$("taskRecords").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-record-id]");
  if (!button) return;
  await api(`/api/task-records/${encodeURIComponent(button.dataset.recordId)}`, { method:"DELETE" });
  await refresh();
});

document.querySelectorAll(".page-tab").forEach((tab) => tab.addEventListener("click", () => switchPage(tab.dataset.page)));
$("generateProfile").addEventListener("click", async () => {
  try {
    const result = await api("/api/profile-cards", { method:"POST" });
    profileCards = [result.item, ...profileCards];
    renderProfileCards(profileCards);
  } catch (error) {
    alert(error.message);
  }
});
$("profileCards").addEventListener("click", async (event) => {
  const input = event.target.closest(".profile-copyable");
  if (input) {
    const cardId = input.closest("[data-profile-id]")?.dataset.profileId;
    const card = findProfileCard(cardId);
    if (card) await copyText(card[input.dataset.profileKey] || "");
    return;
  }
  const button = event.target.closest("button");
  if (!button) return;
  const cardId = button.dataset.profileId || button.closest("[data-profile-id]")?.dataset.profileId;
  const card = findProfileCard(cardId);
  if (!card) return;
  if (button.classList.contains("profile-copy-all")) {
    await copyText(profileCardText(card));
    return;
  }
  if (button.classList.contains("edit-profile")) {
    const name = prompt("请输入资料卡名称", card.name);
    if (name === null) return;
    try {
      const result = await api(`/api/profile-cards/${encodeURIComponent(card.id)}`, { method:"PUT", body:JSON.stringify({ name }) });
      profileCards = profileCards.map((item) => item.id === card.id ? result.item : item);
      renderProfileCards(profileCards);
    } catch (error) {
      alert(error.message);
    }
    return;
  }
  if (button.classList.contains("profile-delete")) {
    if (!confirm(`删除资料卡“${card.name}”？`)) return;
    try {
      await api(`/api/profile-cards/${encodeURIComponent(card.id)}`, { method:"DELETE" });
      profileCards = profileCards.filter((item) => item.id !== card.id);
      renderProfileCards(profileCards);
    } catch (error) {
      alert(error.message);
    }
  }
});

async function boot() { await refresh(); clearInterval(pollTimer); pollTimer = setInterval(async () => { try { const state = await api("/api/state"); renderQueue(state.emails); updateRuntime(state.runtime); renderTaskRecords(state.task_records || []); profileCards = state.profile_cards || []; renderProfileCards(profileCards); } catch (_) {} }, 1500); }
(async () => { try { const me = await api("/api/me"); if (me.authed) { hideLogin(); await boot(); } else showLogin(); } catch (_) { showLogin(); } })();
