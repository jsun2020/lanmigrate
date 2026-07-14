// LanMigrate GUI frontend. Talks to the Python sidecar through the Rust
// shell: requests go out via invoke("ipc_send"), replies and events come
// back as "ipc" Tauri events carrying one JSON line each.
"use strict";

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;
const dialog = window.__TAURI__.dialog;

// ------------------------------------------------------------ ipc plumbing

let nextId = 1;
const pending = new Map();
const eventHandlers = new Map();

function call(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    invoke("ipc_send", { line: JSON.stringify({ id, method, params }) })
      .catch((e) => { pending.delete(id); reject(e); });
  });
}

function on(event, handler) {
  if (!eventHandlers.has(event)) eventHandlers.set(event, []);
  eventHandlers.get(event).push(handler);
}

listen("ipc", (e) => {
  let msg;
  try { msg = JSON.parse(e.payload); } catch { return; }
  if (msg.id != null && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.ok) resolve(msg.result);
    else reject(new Error(msg.error || "unknown error"));
  } else if (msg.event) {
    (eventHandlers.get(msg.event) || []).forEach((h) => h(msg));
  }
});

listen("ipc-closed", () => {
  toast("后台进程已退出,请重启应用");
});

// ------------------------------------------------------------ helpers

const $ = (id) => document.getElementById(id);

function show(screenId) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.add("hidden"));
  $(screenId).classList.remove("hidden");
}

let toastTimer = null;
function toast(msg, ms = 4000) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), ms);
}

function human(n) {
  if (n == null || n < 0) return "?";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(1)) + " " + units[i];
}

function humanEta(sec) {
  if (sec == null) return "-";
  if (sec < 60) return Math.round(sec) + " 秒";
  if (sec < 3600) return Math.round(sec / 60) + " 分钟";
  return (sec / 3600).toFixed(1) + " 小时";
}

// ------------------------------------------------------------ startup

async function startup() {
  try {
    const { version } = await call("ping");
    $("version").textContent = "v" + version;
  } catch (e) {
    $("home-status").textContent = "后台启动失败: " + e.message;
    return;
  }
  refreshResumeBanner();
  try {
    const r = await call("prepare"); // first run downloads rclone (~25MB)
    if (r.downloaded) toast("rclone 已下载完成");
    $("home-status").textContent = "";
  } catch (e) {
    $("home-status").textContent = "rclone 准备失败: " + e.message;
  }
}

async function refreshResumeBanner() {
  try {
    const { task } = await call("latest_incomplete");
    if (task) {
      $("resume-banner").classList.remove("hidden");
      $("resume-info").textContent =
        `${task.source} -> ${task.host}:${task.port}(已完成 ${task.rounds_completed} 轮)`;
      $("btn-resume").onclick = () => doResume();
    } else {
      $("resume-banner").classList.add("hidden");
    }
  } catch { /* banner is best-effort */ }
  refreshSyncList();
}

async function refreshSyncList() {
  try {
    const { tasks } = await call("list_tasks");
    // one row per unique source->destination, newest first, done tasks only
    const seen = new Set();
    const rows = tasks
      .filter((t) => t.status === "done")
      .sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1))
      .filter((t) => {
        const key = `${t.source}|${t.host}|${t.dest}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .slice(0, 5);
    const list = $("sync-list");
    list.innerHTML = "";
    if (!rows.length) { $("sync-section").classList.add("hidden"); return; }
    rows.forEach((t) => {
      const item = document.createElement("div");
      item.className = "sync-item";
      const src = document.createElement("span");
      src.className = "src";
      src.textContent = t.source;
      const dst = document.createElement("span");
      dst.className = "dst";
      dst.textContent = `-> ${t.host}:${t.port}`;
      const btn = document.createElement("button");
      btn.textContent = "同步更新";
      btn.onclick = () => doSync(t.task_id);
      item.append(src, dst, btn);
      list.appendChild(item);
    });
    $("sync-section").classList.remove("hidden");
  } catch { /* best-effort */ }
}

async function doSync(taskId) {
  try {
    const { task } = await call("sync", { task_id: taskId });
    openProgress(`${task.source} -> ${task.host}:${task.port}`, "正在同步…(只传输有变化的文件)");
  } catch (e) {
    toast("同步启动失败: " + e.message);
  }
}

on("status", (m) => { $("home-status").textContent = m.msg; toast(m.msg); });

// ------------------------------------------------------------ receive flow

let receiveDir = "~/Migration";

$("card-receive").onclick = () => {
  $("receive-dir").textContent = receiveDir;
  $("receive-wait").classList.remove("hidden");
  $("receive-active").classList.add("hidden");
  show("screen-receive");
};

$("btn-receive-dir").onclick = async () => {
  const picked = await dialog.open({ directory: true, title: "选择接收文件夹" });
  if (picked) { receiveDir = picked; $("receive-dir").textContent = picked; }
};

$("btn-receive-start").onclick = async () => {
  const btn = $("btn-receive-start");
  btn.disabled = true;
  try {
    const r = await call("start_receive", { directory: receiveDir });
    $("pair-code").textContent = r.code;
    $("receive-ip").textContent = `${r.ip}:${r.port}`;
    $("receive-dir2").textContent = r.directory;
    $("receive-mdns").textContent = r.mdns
      ? "已开启自动发现,发送端无需输入 IP"
      : "自动发现不可用(mDNS 被拦截),发送端请手动输入上面的 IP";
    $("receive-wait").classList.add("hidden");
    $("receive-active").classList.remove("hidden");
  } catch (e) {
    toast("启动接收失败: " + e.message);
  } finally {
    btn.disabled = false;
  }
};

$("btn-receive-stop").onclick = async () => {
  try { await call("stop_receive"); } catch { /* already stopped */ }
  show("screen-home");
};

on("receive_stopped", (m) => {
  toast(`接收服务意外停止(exit ${m.exit_code}),端口可能被占用`);
  $("receive-wait").classList.remove("hidden");
  $("receive-active").classList.add("hidden");
});

// ------------------------------------------------------------ send flow

let scanResult = null;
let sourceDir = null;
let selectedDevice = null;

$("card-send").onclick = () => {
  resetSendScreen();
  show("screen-send");
};

function resetSendScreen() {
  $("send-step-pick").classList.remove("hidden");
  $("send-step-exclude").classList.add("hidden");
  $("send-step-device").classList.add("hidden");
  $("send-source").textContent = sourceDir || "尚未选择";
  $("scan-status").textContent = "";
}

on("scan_progress", (m) => {
  $("scan-status").textContent =
    `已扫描 ${m.files} 个文件` + (m.bytes > 0 ? ` / ${human(m.bytes)}` : "") +
    `  当前: ${m.rel}`;
});

$("btn-send-pick").onclick = async () => {
  const picked = await dialog.open({ directory: true, title: "选择要迁移的文件夹" });
  if (!picked) return;
  sourceDir = picked;
  $("send-source").textContent = picked;
  $("scan-status").textContent = "正在快速扫描…(仅识别可跳过的依赖目录,秒级)";
  $("btn-send-pick").disabled = true;
  try {
    scanResult = await call("scan", { path: picked });
    $("scan-status").textContent =
      `共 ${scanResult.file_count} 个文件,发现 ${scanResult.exclusions.length} 个可跳过的依赖目录`;
    renderExclusions();
    $("send-step-exclude").classList.remove("hidden");
    $("send-step-pick").classList.add("hidden");
  } catch (e) {
    $("scan-status").textContent = "扫描失败: " + e.message;
  } finally {
    $("btn-send-pick").disabled = false;
  }
};

function renderExclusions() {
  const list = $("exclude-list");
  list.innerHTML = "";
  const excl = scanResult.exclusions;
  $("exclude-summary").textContent = excl.length
    ? `勾选 = 跳过不传(默认全部跳过)。共 ${excl.length} 项` +
      (scanResult.saved_bytes > 0 ? `,可节省 ${human(scanResult.saved_bytes)}` : "")
    : "未发现可跳过的依赖目录,将完整传输。";
  excl.forEach((e, i) => {
    const item = document.createElement("label");
    item.className = "exclude-item";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = true;
    cb.dataset.rel = e.rel;
    const rule = document.createElement("span");
    rule.className = "rule";
    rule.textContent = e.rule;
    const rel = document.createElement("span");
    rel.className = "rel";
    rel.textContent = e.rel;
    const size = document.createElement("span");
    size.className = "size";
    size.textContent = e.size >= 0 ? human(e.size) : "";
    item.append(cb, rule, rel, size);
    list.appendChild(item);
  });
}

$("btn-rescan").onclick = () => resetSendScreen();

$("btn-exclude-next").onclick = () => {
  $("send-step-exclude").classList.add("hidden");
  $("send-step-device").classList.remove("hidden");
  discoverDevices();
};

async function discoverDevices() {
  const list = $("device-list");
  list.innerHTML = "";
  selectedDevice = null;
  $("device-status").textContent = "正在搜索局域网设备…(约 5 秒)";
  $("btn-rediscover").disabled = true;
  try {
    const { receivers } = await call("discover", { timeout: 5.0 });
    $("device-status").textContent = receivers.length
      ? "点击选择接收设备"
      : "未发现设备(接收端未启动或 mDNS 被防火墙拦截),可手动输入 IP";
    receivers.forEach((r) => {
      const item = document.createElement("div");
      item.className = "device-item";
      item.innerHTML = `<span class="name"></span><span class="addr"></span>`;
      item.querySelector(".name").textContent = r.name;
      item.querySelector(".addr").textContent = `${r.host}:${r.port}`;
      item.onclick = () => selectDevice(r, item);
      list.appendChild(item);
    });
    if (receivers.length === 1) selectDevice(receivers[0], list.firstChild);
  } catch (e) {
    $("device-status").textContent = "搜索失败: " + e.message;
  } finally {
    $("btn-rediscover").disabled = false;
  }
}

async function selectDevice(r, el) {
  selectedDevice = r;
  document.querySelectorAll(".device-item").forEach((d) => d.classList.remove("selected"));
  el.classList.add("selected");
  $("manual-host").value = "";
  try { // previously paired? then the code is remembered
    const { device } = await call("recall_device", { fingerprint: r.fingerprint });
    if (device && device.code) {
      $("pair-input").value = device.code;
      toast("已配对过的设备,自动填入上次配对码");
    }
  } catch { /* optional convenience */ }
}

$("btn-rediscover").onclick = () => discoverDevices();

$("btn-start-send").onclick = async () => {
  const manualHost = $("manual-host").value.trim();
  const host = manualHost || (selectedDevice && selectedDevice.host);
  const port = manualHost
    ? parseInt($("manual-port").value, 10) || 2022
    : (selectedDevice ? selectedDevice.port : 2022);
  const fingerprint = manualHost ? "" : (selectedDevice ? selectedDevice.fingerprint : "");
  const code = $("pair-input").value.trim();
  if (!host) { toast("请选择设备或输入接收端 IP"); return; }
  if (!/^\d{6}$/.test(code)) { toast("请输入 6 位数字配对码"); return; }
  const enabled = [...document.querySelectorAll("#exclude-list input:checked")]
    .map((cb) => cb.dataset.rel);
  $("btn-start-send").disabled = true;
  try {
    await call("start_send",
      { source: sourceDir, host, port, code, fingerprint, enabled });
    openProgress(`${sourceDir} -> ${host}:${port}`);
  } catch (e) {
    toast("启动失败: " + e.message);
  } finally {
    $("btn-start-send").disabled = false;
  }
};

// ------------------------------------------------------------ progress

function openProgress(taskLabel, title) {
  $("progress-task").textContent = taskLabel;
  $("progress-bar").style.width = "0%";
  $("progress-bar").classList.add("indeterminate");
  $("stat-bytes").textContent = "-";
  $("stat-speed").textContent = "-";
  $("stat-eta").textContent = "-";
  $("stat-files").textContent = "-";
  $("stat-round").textContent = "1";
  $("progress-current").textContent = "";
  $("progress-title").textContent = title || "正在迁移…";
  show("screen-progress");
}

on("round", (m) => { $("stat-round").textContent = m.round; });

on("round_failed", (m) => {
  $("progress-title").textContent =
    `第 ${m.round} 轮结束,仍有文件未完成,${m.wait} 秒后自动重试…`;
});

on("transfer_progress", (m) => {
  const bar = $("progress-bar");
  if (m.total > 0) {
    bar.classList.remove("indeterminate");
    bar.style.width = Math.min(100, (m.bytes / m.total) * 100).toFixed(1) + "%";
  }
  $("stat-bytes").textContent = human(m.bytes) + (m.total > 0 ? " / " + human(m.total) : "");
  $("stat-speed").textContent = human(m.speed) + "/s";
  $("stat-eta").textContent = humanEta(m.eta);
  $("stat-files").textContent = `${m.transfers}/${m.total_transfers || "?"}`;
  $("progress-current").textContent = m.current ? "当前: " + m.current : "";
});

on("send_done", (m) => {
  refreshResumeBanner();
  if (m.ok) {
    $("done-icon").classList.remove("fail");
    $("done-icon").innerHTML = "&#10003;";
    $("done-title").textContent = "迁移完成!";
    const mins = (m.elapsed / 60).toFixed(1);
    let detail = `共传输 ${human(m.bytes)},耗时 ${mins} 分钟,${m.rounds} 轮`;
    if (m.saved_bytes > 0) detail += `;智能排除为你节省了 ${human(m.saved_bytes)}`;
    $("done-detail").textContent = detail;
    show("screen-done");
  } else if (m.cancelled) {
    toast("已暂停,任务已保存,可随时继续");
    show("screen-home");
  } else {
    $("done-icon").classList.add("fail");
    $("done-icon").innerHTML = "&#10007;";
    $("done-title").textContent = "迁移未完成";
    $("done-detail").textContent =
      (m.error || "未知错误") + "。任务已保存,回到首页可继续传输。";
    show("screen-done");
  }
});

$("btn-cancel-send").onclick = async () => {
  $("btn-cancel-send").disabled = true;
  try { await call("cancel_send"); } catch { /* idle */ }
  $("btn-cancel-send").disabled = false;
};

async function doResume() {
  try {
    const { task } = await call("resume");
    openProgress(`${task.source} -> ${task.host}:${task.port}`);
    $("stat-round").textContent = task.rounds_completed + 1;
  } catch (e) {
    toast("恢复失败: " + e.message);
  }
}

// ------------------------------------------------------------ nav

document.querySelectorAll(".btn-home").forEach((b) => {
  b.onclick = () => { refreshResumeBanner(); show("screen-home"); };
});

startup();
