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
    openProgress(task.task_id,
      `${task.source} -> ${task.host}:${task.port}(同步:只传有变化的文件)`);
  } catch (e) {
    toast("同步启动失败: " + e.message);
  }
}

on("status", (m) => { $("home-status").textContent = m.msg; toast(m.msg); });

// ------------------------------------------------------------ receive flow

let receiveDir = "~/Migration";
let receiveSession = null; // start_receive result while the server runs (F15)
let receiveFiles = 0;
let receiveLastActivity = 0;
let receiveIdleTimer = null;

$("card-receive").onclick = () => {
  if (receiveSession) { // server still running: show the live screen as-is
    show("screen-receive");
    return;
  }
  showReceiveWait();
  show("screen-receive");
};

function showReceiveWait() {
  $("receive-dir").textContent = receiveDir;
  // remembered so a standard user who must use e.g. 8080 sets it only once
  $("receive-port").value = localStorage.getItem("receivePort") || "2022";
  $("receive-wait").classList.remove("hidden");
  $("receive-active").classList.add("hidden");
}

function clearReceiveSession() {
  receiveSession = null;
  receiveFiles = 0;
  clearInterval(receiveIdleTimer);
  receiveIdleTimer = null;
  $("receive-activity").textContent = "";
}

$("btn-receive-dir").onclick = async () => {
  const picked = await dialog.open({ directory: true, title: "选择接收文件夹" });
  if (picked) { receiveDir = picked; $("receive-dir").textContent = picked; }
};

$("btn-receive-start").onclick = async () => {
  const btn = $("btn-receive-start");
  const port = parseInt($("receive-port").value, 10);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    toast("端口无效,请输入 1~65535 之间的数字");
    return;
  }
  btn.disabled = true;
  try {
    const r = await call("start_receive", { directory: receiveDir, port });
    localStorage.setItem("receivePort", String(port));
    receiveSession = r;
    receiveFiles = 0;
    receiveLastActivity = Date.now();
    receiveIdleTimer = setInterval(updateReceiveIdleHint, 5000);
    $("pair-code").textContent = r.code;
    $("receive-ip").textContent = `${r.ip}:${r.port}`;
    $("receive-dir2").textContent = r.directory;
    $("receive-mdns").textContent = r.mdns
      ? "已开启自动发现,发送端无需输入 IP"
      : "自动发现不可用(mDNS 被拦截),发送端请手动输入上面的 IP";
    const fw = $("receive-firewall");
    if (r.firewall_ok) {
      fw.classList.add("hidden");
    } else {
      fw.textContent = (r.firewall_msg || "防火墙可能拦截连接")
        + " — 若发送端连不上:让管理员运行一次本程序,或改用本机发送(发送不需要权限)";
      fw.classList.remove("hidden");
    }
    $("receive-wait").classList.add("hidden");
    $("receive-active").classList.remove("hidden");
  } catch (e) {
    toast("启动接收失败: " + e.message);
  } finally {
    btn.disabled = false;
  }
};

// PRD F15: end this batch and immediately offer the next one - the backend
// fully tears down the SFTP server + mDNS announcer, so a fresh start works
// without restarting the app (this used to die with NonUniqueNameException).
$("btn-receive-next").onclick = async () => {
  const got = receiveFiles;
  try { await call("stop_receive"); } catch { /* already stopped */ }
  clearReceiveSession();
  showReceiveWait();
  toast(got > 0 ? `本批接收完成(${got} 个文件),可继续接收下一个文件夹`
                : "已结束本次接收,可继续接收下一个文件夹");
};

$("btn-receive-stop").onclick = async () => {
  try { await call("stop_receive"); } catch { /* already stopped */ }
  clearReceiveSession();
  show("screen-home");
};

// PRD F16: the serve log finally gives the receiver visible progress
on("receive_activity", (m) => {
  receiveLastActivity = Date.now();
  receiveFiles = m.files;
  $("receive-activity").textContent = m.kind === "login"
    ? "发送端已连接,开始接收…"
    : `已接收 ${m.files} 个文件 · 最新: ${m.value}`;
});

function updateReceiveIdleHint() {
  if (!receiveSession || receiveFiles === 0) return;
  const idle = Math.round((Date.now() - receiveLastActivity) / 1000);
  if (idle >= 30) {
    $("receive-activity").textContent =
      `已接收 ${receiveFiles} 个文件 · 约 ${idle} 秒没有新文件了,` +
      `若发送端已显示完成,可点"完成接收"`;
  }
}

on("receive_stopped", (m) => {
  clearReceiveSession();
  toast(`接收服务意外停止(exit ${m.exit_code}),端口可能被占用`);
  $("receive-wait").classList.remove("hidden");
  $("receive-active").classList.add("hidden");
});

// ------------------------------------------------------------ send flow

let scanResult = null;
let sourceDir = null;
let selectedDevice = null;
let conflictChecked = false;

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
  $("dest-subdir").checked = localStorage.getItem("destSubdir") !== "0";
  resetConflictCheck();
}

// PRD F12: the conflict probe is bound to a specific receiver+source pair;
// changing either invalidates it and the two-step confirm starts over.
function resetConflictCheck() {
  conflictChecked = false;
  $("conflict-box").classList.add("hidden");
  $("btn-start-send").textContent = "开始迁移";
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
  resetConflictCheck();
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

$("manual-host").addEventListener("input", resetConflictCheck);
$("manual-port").addEventListener("input", resetConflictCheck);
// the conflict probe looks INSIDE the destination dir - toggling the
// subfolder option changes the destination, so the probe must rerun
$("dest-subdir").addEventListener("change", () => {
  localStorage.setItem("destSubdir", $("dest-subdir").checked ? "1" : "0");
  resetConflictCheck();
});

function sendDest() {
  if (!$("dest-subdir").checked) return "/";
  const name = sourceDir.replace(/[\\/]+$/, "").split(/[\\/]/).pop();
  return name ? "/" + name : "/";
}

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
    // PRD F12: probe once for same-name content on the receiver. If found,
    // reveal the 3-way choice and let the user confirm with a second click.
    const dest = sendDest();
    if (!conflictChecked) {
      $("device-status").textContent = "正在检查接收端是否已有同名内容…";
      const chk = await call("check_dest", { host, port, code, source: sourceDir, dest });
      conflictChecked = true;
      if (chk.conflict) {
        const more = chk.existing_total > chk.existing.length
          ? ` 等共 ${chk.existing_total} 项` : "";
        $("conflict-names").textContent = chk.existing.join("、") + more;
        $("conflict-box").classList.remove("hidden");
        $("btn-start-send").textContent = "确认并开始";
        $("device-status").textContent = "请选择同名文件的处理方式,然后再点一次开始";
        return;
      }
      $("device-status").textContent = "";
    }
    const picked = document.querySelector('input[name="conflict"]:checked');
    const conflict = picked ? picked.value : "overwrite";
    const r = await call("start_send",
      { source: sourceDir, host, port, code, fingerprint, enabled, conflict, dest });
    openProgress(r.task_id, `${sourceDir} -> ${host}:${port}${dest === "/" ? "" : dest}`);
  } catch (e) {
    toast("启动失败: " + e.message);
  } finally {
    $("btn-start-send").disabled = false;
  }
};

// ------------------------------------------------------------ progress
// PRD F14: several transfers can run at once - one card per task.

const activeTasks = new Map(); // task_id -> card state

function ensureTaskCard(taskId, label) {
  let t = activeTasks.get(taskId);
  if (t) return t;
  const card = document.createElement("div");
  card.className = "task-card";
  card.innerHTML = `
    <div class="task-head">
      <span class="task-label"></span>
      <span class="task-state"></span>
      <button class="task-cancel">暂停</button>
    </div>
    <div class="progress-outer"><div class="progress-inner indeterminate"></div></div>
    <div class="task-stats dim">正在启动…</div>
    <div class="task-current dim ellipsis"></div>`;
  card.querySelector(".task-label").textContent = label || taskId;
  card.querySelector(".task-cancel").onclick = async () => {
    try { await call("cancel_send", { task_id: taskId }); } catch { /* idle */ }
  };
  $("progress-tasks").appendChild(card);
  t = {
    card,
    bar: card.querySelector(".progress-inner"),
    stateEl: card.querySelector(".task-state"),
    statsEl: card.querySelector(".task-stats"),
    currentEl: card.querySelector(".task-current"),
    cancelBtn: card.querySelector(".task-cancel"),
    running: true, round: 1,
  };
  activeTasks.set(taskId, t);
  updateProgressTitle();
  return t;
}

function runningTasks() {
  return [...activeTasks.values()].filter((t) => t.running);
}

function updateProgressTitle() {
  const n = runningTasks().length;
  $("progress-title").textContent =
    n > 1 ? `正在迁移…(${n} 个传输进行中)` : "正在迁移…";
}

function clearTaskCards() {
  activeTasks.clear();
  $("progress-tasks").innerHTML = "";
}

function openProgress(taskId, label) {
  ensureTaskCard(taskId, label);
  show("screen-progress");
}

function refreshProgressBanner() {
  const running = runningTasks().length;
  const banner = $("progress-banner");
  if (activeTasks.size === 0) { banner.classList.add("hidden"); return; }
  $("progress-banner-info").textContent = running > 0
    ? `${running} 个传输进行中`
    : "传输已结束,点击查看结果";
  banner.classList.remove("hidden");
}

$("btn-view-progress").onclick = () => {
  show("screen-progress");
  maybeFinishAll();
};

$("btn-send-more").onclick = () => { // queue another folder while running
  resetSendScreen();
  show("screen-send");
};

on("round", (m) => {
  const t = activeTasks.get(m.task_id);
  if (t) t.round = m.round;
});

on("round_failed", (m) => {
  const t = activeTasks.get(m.task_id);
  if (t) t.statsEl.textContent =
    `第 ${m.round} 轮结束,仍有文件未完成,${m.wait} 秒后自动重试…`;
});

on("transfer_progress", (m) => {
  const t = activeTasks.get(m.task_id);
  if (!t || !t.running) return;
  if (m.total > 0) {
    t.bar.classList.remove("indeterminate");
    t.bar.style.width = Math.min(100, (m.bytes / m.total) * 100).toFixed(1) + "%";
  }
  t.statsEl.textContent =
    human(m.bytes) + (m.total > 0 ? " / " + human(m.total) : "") +
    ` · ${human(m.speed)}/s · 剩余 ${humanEta(m.eta)}` +
    ` · 文件 ${m.transfers}/${m.total_transfers || "?"} · 第 ${t.round} 轮`;
  t.currentEl.textContent = m.current ? "当前: " + m.current : "";
});

on("send_done", (m) => {
  refreshResumeBanner();
  const t = m.task_id
    ? ensureTaskCard(m.task_id, m.source || m.task_id)
    : null;
  if (t) {
    t.running = false;
    t.done = m;
    t.cancelBtn.classList.add("hidden");
    t.bar.classList.remove("indeterminate");
    t.currentEl.textContent = "";
    if (m.ok) {
      t.bar.style.width = "100%";
      t.stateEl.textContent = "✓ 完成";
      t.stateEl.className = "task-state ok";
      const mins = (m.elapsed / 60).toFixed(1);
      t.statsEl.textContent =
        `共传输 ${human(m.bytes)} · ${mins} 分钟 · ${m.rounds} 轮` +
        (m.saved_bytes > 0 ? ` · 智能排除节省 ${human(m.saved_bytes)}` : "");
    } else if (m.cancelled) {
      t.stateEl.textContent = "已暂停";
      t.stateEl.className = "task-state paused";
      t.statsEl.textContent = `已传输 ${human(m.bytes)},任务已保存,可随时继续`;
    } else {
      t.stateEl.textContent = "✗ 未完成";
      t.stateEl.className = "task-state fail";
      t.statsEl.textContent =
        (m.error || "未知错误") + "。任务已保存,回到首页可继续传输。";
    }
    updateProgressTitle();
  }
  refreshProgressBanner();
  // only auto-navigate when the user is watching the progress screen -
  // finishing must never yank them out of the send wizard (PRD F14)
  if (!$("screen-progress").classList.contains("hidden")) maybeFinishAll();
  else if (t) toast(m.ok ? "一个传输已完成" : "一个传输已结束");
});

function maybeFinishAll() {
  if (activeTasks.size === 0 || runningTasks().length > 0) return;
  const all = [...activeTasks.values()];
  const okTasks = all.filter((t) => t.done && t.done.ok);
  const cancelled = all.filter((t) => t.done && t.done.cancelled);
  const totalBytes = all.reduce((s, t) => s + ((t.done && t.done.bytes) || 0), 0);
  const saved = okTasks.reduce((s, t) => s + (t.done.saved_bytes || 0), 0);
  clearTaskCards();
  refreshProgressBanner();
  if (okTasks.length === all.length) {
    $("done-icon").classList.remove("fail");
    $("done-icon").innerHTML = "&#10003;";
    $("done-title").textContent = all.length > 1
      ? `全部 ${all.length} 个文件夹迁移完成!` : "迁移完成!";
    let detail = `共传输 ${human(totalBytes)}`;
    if (saved > 0) detail += `;智能排除为你节省了 ${human(saved)}`;
    $("done-detail").textContent = detail;
    show("screen-done");
  } else if (okTasks.length === 0 && cancelled.length === all.length) {
    toast("已暂停,任务已保存,可随时继续");
    show("screen-home");
  } else {
    $("done-icon").classList.add("fail");
    $("done-icon").innerHTML = "&#10007;";
    $("done-title").textContent = "部分传输未完成";
    $("done-detail").textContent =
      `完成 ${okTasks.length} 个,未完成 ${all.length - okTasks.length} 个。` +
      "任务已保存,回到首页可继续传输。";
    show("screen-done");
  }
}

$("btn-cancel-send").onclick = async () => {
  $("btn-cancel-send").disabled = true;
  try { await call("cancel_send"); } catch { /* idle */ }
  $("btn-cancel-send").disabled = false;
};

async function doResume() {
  try {
    const { task } = await call("resume");
    const t = ensureTaskCard(task.task_id, `${task.source} -> ${task.host}:${task.port}`);
    t.round = task.rounds_completed + 1;
    show("screen-progress");
  } catch (e) {
    toast("恢复失败: " + e.message);
  }
}

// ------------------------------------------------------------ nav

document.querySelectorAll(".btn-home").forEach((b) => {
  b.onclick = () => { refreshResumeBanner(); refreshProgressBanner(); show("screen-home"); };
});

startup();
