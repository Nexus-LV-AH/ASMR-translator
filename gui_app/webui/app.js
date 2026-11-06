/* 音声字幕工房 · 图形界面
 * 原生 JS，无构建、无外部依赖。
 * token 从地址栏 ?token= 读取；所有 API 用 X-GUI-Token 头。
 */
"use strict";

const $ = (id) => document.getElementById(id);
const TOKEN = new URLSearchParams(location.search).get("token") || "";

const MODE_TEXT = {
  all: { label: "完整运行", drop: "拖入作品文件夹最方便；作品名会自动识别。也可以拖入 mp3 / wav / flac 等文件。" },
  extract: { label: "只提取字幕", drop: "拖入作品文件夹或音频；只提取日文，不翻译、不归档。" },
  translate: { label: "制作翻译", drop: "拖入日文字幕 .srt / .lrc / .txt（带时间轴）；不需要音频。" },
};
const STATUS_TEXT = {
  preparing: "准备中",
  running: "运行中",
  cancelling: "取消中",
  cancelled: "已取消",
  done: "完成",
  error: "失败",
  interrupted: "已中断",
};
const STAGE_LABEL = {
  preparing: "准备",
  extract: "提取日文字幕",
  refine: "台本精修",
  translate: "翻译",
  archive: "归档",
  done: "完成",
};
const STAGE_ORDER = {
  all: ["extract", "refine", "translate", "archive"],
  extract: ["extract", "refine"],
  translate: ["translate", "archive"],
};
const KEY_SOURCE_TEXT = { env: "已配置（.env / 环境变量）", config: "已配置（配置文件 api_key）", none: "未配置" };

let stateData = null;
let lastSeq = 0;
let pollTimer = null;
let polling = true;
let pollBusy = false;
let connOk = true;
let initialPoll = true;
let uploadBusy = false;
let selectedMode = "all";
let modelSelection = "";
let workNameTouched = false;
let logJobId = "";
let onboarded = false;
let pendingModel = null;
let pendingModelKey = "";
let editingApiKey = "";
let toastTimer = null;

/* ---------------------------------------------------------------- 基础 */
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function fmtBytes(n) {
  n = Number(n || 0);
  if (n < 1024) return `${n} B`;
  const u = ["KB", "MB", "GB", "TB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return `${n.toFixed(n >= 100 ? 0 : n >= 10 ? 1 : 2)} ${u[i]}`;
}
function fmtDur(sec) {
  sec = Math.max(0, Math.round(Number(sec || 0)));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}
function fmtGB(n) { return n ? `${Number(n).toFixed(2)} GB` : ""; }
function toast(msg, kind = "") {
  const t = $("toast");
  t.textContent = String(msg || "");
  t.className = `toast ${kind}`.trim();
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 4200);
}
function setView(name) {
  const settings = name === "settings";
  $("mainView").classList.toggle("hidden", settings);
  $("settingsView").classList.toggle("hidden", !settings);
  $("openSettings").classList.toggle("hidden", settings);
  try {
    const u = new URL(location.href);
    u.hash = settings ? "settings" : "";
    history.replaceState(null, "", u);
  } catch (_) { /* ignore */ }
  if (settings) window.scrollTo({ top: 0, behavior: "smooth" });
}
async function api(path, { json, method, allowError = false, headers = {} } = {}) {
  const opt = {
    method: method || (json !== undefined ? "POST" : "GET"),
    headers: Object.assign({ "X-GUI-Token": TOKEN }, headers),
  };
  if (json !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(json);
  }
  const res = await fetch(path, opt);
  let data = null;
  try { data = await res.json(); } catch (_) { /* ignore */ }
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  if (data && data.ok === false && !allowError) throw new Error(data.error || "请求失败");
  return data;
}
function savePrefs() {
  try {
    localStorage.setItem("asmr.mode", selectedMode);
    localStorage.setItem("asmr.model", modelSelection);
    localStorage.setItem("asmr.opts", JSON.stringify({
      force: $("optForce").checked,
      bilingual: $("optBilingual").checked,
      noConsolidate: $("optNoConsolidate").checked,
      refineNoModel: $("optRefineNoModel").checked,
      preventSleep: $("optPreventSleep").checked,
      limit: $("optLimit").value,
    }));
  } catch (_) { /* ignore */ }
}
function loadPrefs() {
  try {
    const mode = localStorage.getItem("asmr.mode");
    if (MODE_TEXT[mode]) selectedMode = mode;
    modelSelection = localStorage.getItem("asmr.model") || "";
    const opts = JSON.parse(localStorage.getItem("asmr.opts") || "{}");
    $("optForce").checked = !!opts.force;
    $("optBilingual").checked = !!opts.bilingual;
    $("optNoConsolidate").checked = !!opts.noConsolidate;
    $("optRefineNoModel").checked = !!opts.refineNoModel;
    $("optPreventSleep").checked = opts.preventSleep !== false;
    $("optLimit").value = opts.limit || "";
  } catch (_) { /* ignore */ }
  document.querySelectorAll('input[name="mode"]').forEach((r) => { r.checked = r.value === selectedMode; });
}

/* ---------------------------------------------------------------- 轮询 */
function schedulePoll(delay) {
  if (!polling) return;
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => pollState(false), delay);
}
async function pollState(initial = false) {
  if (pollBusy) return;
  pollBusy = true;
  try {
    const data = await api(`/api/state?since=${lastSeq}`);
    connOk = true;
    handleState(data, initial);
  } catch (e) {
    connOk = false;
    renderTopStatus(null);
    $("statusPill").textContent = "连接失败";
    $("statusPill").className = "pill error";
    if (initial) toast(`无法连接本地服务：${e.message}`, "error");
  } finally {
    pollBusy = false;
    const running = stateData && stateData.job && ["preparing", "running", "cancelling"].includes(stateData.job.status);
    schedulePoll(running ? 700 : 1800);
  }
}
function requestRefresh() {
  clearTimeout(pollTimer);
  if (!pollBusy && polling) pollState(false);
}
function handleState(data, initial) {
  stateData = data;
  if (data.logs) appendLogs(data.logs);
  render();
  renderTopStatus(data.job);
  if (!onboarded && !(data.settings && data.settings.models || []).some((m) => m.available)) {
    onboarded = true;
    try { sessionStorage.setItem("asmr.onboarded", "1"); } catch (_) { /* ignore */ }
    setView("settings");
    toast("还没有可用的翻译模型：先填 DeepSeek API Key 或选择本地模型", "error");
  }
  initialPoll = false;
}

/* ---------------------------------------------------------------- 顶层渲染 */
function render() {
  if (!stateData) return;
  const settings = stateData.settings || {};
  const models = settings.models || [];
  renderGlobalWarnings(stateData.config_warnings || []);
  renderModelSelect(models);
  renderStaged(stateData.staged || {});
  renderExisting(stateData.existing || {});
  renderMode();
  renderProgress(stateData.job);
  renderResults(stateData.job);
  renderTaskInfo(stateData.job);
  renderSettings(stateData);
  updateStart(models, stateData.job);
}
function renderTopStatus(job) {
  const pill = $("statusPill");
  if (!connOk) { pill.textContent = "连接失败"; pill.className = "pill error"; return; }
  if (job && ["preparing", "running", "cancelling"].includes(job.status)) {
    pill.textContent = "任务运行中";
    pill.className = "pill running";
  } else if (job && job.status === "error") {
    pill.textContent = "上次任务失败";
    pill.className = "pill error";
  } else if (job && job.status === "interrupted") {
    pill.textContent = "上次任务中断";
    pill.className = "pill interrupted";
  } else {
    pill.textContent = "就绪";
    pill.className = "pill idle";
  }
}
function renderGlobalWarnings(warnings) {
  const box = $("globalWarnings");
  box.replaceChildren();
  if (!warnings || !warnings.length) { box.classList.add("hidden"); return; }
  const shown = warnings.slice(0, 4);
  shown.forEach((w) => box.append(el("div", null, w)));
  if (warnings.length > shown.length) box.append(el("div", "muted small", `另有 ${warnings.length - shown.length} 条提示，见设置页。`));
  box.classList.remove("hidden");
}
function renderMode() {
  const mode = selectedMode;
  const info = MODE_TEXT[mode] || MODE_TEXT.all;
  $("dropModeHint").textContent = `当前任务：${info.label}`;
  $("dropSub").textContent = info.drop;
  $("promptbookBox").classList.toggle("hidden", mode === "translate");
  const showTranslateOpts = mode === "all" || mode === "translate";
  const showRefineOpts = mode === "extract" || mode === "all";
  toggleLabel("optBilingual", showTranslateOpts);
  toggleLabel("optNoConsolidate", showTranslateOpts);
  toggleLabel("optLimit", showTranslateOpts);
  toggleLabel("optRefineNoModel", showRefineOpts);
}
function toggleLabel(id, show) {
  const input = $(id);
  if (!input) return;
  const label = input.closest("label") || input.parentElement;
  if (label) label.classList.toggle("hidden", !show);
}
function renderStaged(staged) {
  const files = staged.files || {};
  const audio = files.audio || [];
  const subs = files.subtitle || [];
  const pbs = files.promptbook || [];
  if (!workNameTouched && staged.work_hint && !$("workName").value) {
    $("workName").value = staged.work_hint;
  }
  $("workHint").textContent = staged.work_hint ? `自动识别：${staged.work_hint}` : "";
  const box = $("fileList");
  box.replaceChildren();
  const all = audio.concat(subs);
  if (!all.length) {
    box.append(el("div", "empty-hint", selectedMode === "translate"
      ? "还没有字幕文件。拖入 .srt / .lrc / .txt，或点上方「选择文件」。"
      : "还没有文件。拖入作品文件夹（推荐）或音频文件。"));
  } else {
    all.forEach((f) => box.append(fileRow(f)));
  }
  const total = all.reduce((n, f) => n + (f.size || 0), 0);
  $("fileSummary").textContent = all.length
    ? `${all.length} 个文件 · ${fmtBytes(total)}${audio.some((f) => !f.is_audio) ? "（含附件）" : ""}`
    : "还没有文件";
  const pbBox = $("promptbookList");
  pbBox.replaceChildren();
  pbs.forEach((f) => pbBox.append(fileRow(f, "台本")));
  if (!pbs.length) pbBox.append(el("div", "empty-hint", "没有台本；没有台本也能正常翻译。"));
  // 兼容性提示（类型不匹配时由 startHint 显示）
}
function fileRow(f, forcedKind) {
  const row = el("div", "file-row");
  let kind = forcedKind || "文件";
  if (!forcedKind) {
    if (f.is_subtitle) kind = "字幕";
    else if (f.is_image) kind = "图片";
    else if (f.is_audio) kind = "音频";
    else kind = "附件";
  }
  row.append(el("span", `file-kind ${["图片", "附件"].includes(kind) ? "attachment" : ""}`, kind));
  row.append(el("span", "file-name", f.relpath || f.name || ""));
  row.append(el("span", "file-size", fmtBytes(f.size || 0)));
  return row;
}
function renderExisting(existing) {
  const box = $("existingNotice");
  box.replaceChildren();
  const name = $("workName").value.trim();
  const hint = stateData?.staged?.work_hint || "";
  if (!name || (hint && name !== hint)) { box.classList.add("hidden"); return; }
  const parts = [];
  if (existing.input_exists) parts.push(`input/${name} 里已有 ${existing.audio || 0} 个音频：本次会一并处理，同名文件覆盖。`);
  if (existing.output_exists) parts.push(`output/${name} 作品包已存在：本次会更新。`);
  if (existing.collect_exists) parts.push(`LRC成品库/${name} 已存在：LRC 会更新。`);
  if (!parts.length) { box.classList.add("hidden"); return; }
  parts.forEach((p) => box.append(el("div", null, p)));
  box.className = "notice warn";
}

/* ---------------------------------------------------------------- 模型/任务 */
function renderModelSelect(models) {
  const sel = $("modelSelect");
  const prev = modelSelection || sel.value;
  sel.replaceChildren();
  if (!models.length) {
    const opt = el("option", null, "（还没有配置翻译模型）");
    opt.value = "";
    sel.append(opt);
    modelSelection = "";
    renderModelNote(null, models);
    return;
  }
  models.forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m.key;
    const tag = m.kind === "api" ? "API" : "本地";
    let label = `[${tag}] ${m.display_name || m.key}`;
    if (m.kind === "local") {
      const shown = String(m.display_name || "").toLowerCase();
      if (m.quant && !shown.includes(String(m.quant).toLowerCase())) label += ` · ${m.quant}`;
      if (m.size_gb) label += ` · ${fmtGB(m.size_gb)}`;
    } else if (m.model) {
      label += ` · ${m.model}`;
    }
    if (!m.available) label += "（不可用）";
    opt.textContent = label;
    opt.disabled = !m.available;
    sel.append(opt);
  });
  const usable = models.filter((m) => m.available);
  let want = prev || stateData?.settings?.default_key || "";
  if (!models.some((m) => m.key === want && m.available)) {
    want = (usable.find((m) => m.is_default) || usable[0] || models[0]).key;
  }
  sel.value = want;
  modelSelection = want;
  renderModelNote(models.find((m) => m.key === want), models);
}
function renderModelNote(model, models) {
  const box = $("modelNote");
  box.replaceChildren();
  if (!model) {
    box.className = "notice error";
    box.append(el("div", null, "还没有可用的翻译模型。完整运行/制作翻译需要先在设置里配置云端 API Key 或选择本地 .gguf。"));
    box.classList.remove("hidden");
    return;
  }
  const lines = [];
  if (!model.available) lines.push(model.reason || "这个模型当前不可用。");
  (model.warnings || []).slice(0, 2).forEach((w) => lines.push(w));
  if (model.kind === "local") {
    const detail = [model.params, model.quant, model.size_gb ? fmtGB(model.size_gb) : ""].filter(Boolean).join(" · ");
    if (detail && !lines.length) lines.push(`${detail}${model.note ? ` · ${model.note}` : ""}`);
  } else if (!lines.length) {
    lines.push(`云端 API · ${model.base_url || ""} · ${model.model || ""}`);
  }
  if (!lines.length) { box.classList.add("hidden"); return; }
  lines.forEach((l) => box.append(el("div", null, l)));
  box.className = `notice ${model.available ? ((model.warnings || []).length ? "warn" : "info") : "error"}`;
  box.classList.remove("hidden");
}
function modelNeedsSelection() {
  return selectedMode === "all" || selectedMode === "translate";
}
function currentModel() {
  return (stateData?.settings?.models || []).find((m) => m.key === $("modelSelect").value) || null;
}
function updateStart(models, job) {
  const btn = $("startBtn");
  const hint = $("startHint");
  const running = !!(job && ["preparing", "running", "cancelling"].includes(job.status));
  const model = currentModel();
  const mode = selectedMode;
  const staged = stateData?.staged || {};
  const existing = stateData?.existing || {};
  const relevant = mode === "translate"
    ? (staged.files?.subtitle || []).length
    : (staged.files?.audio || []).filter((f) => f.is_audio).length;
  const other = mode === "translate"
    ? ((staged.files?.audio || []).length + (staged.files?.promptbook || []).length)
    : (staged.files?.subtitle || []).length;
  const existingRelevant = mode === "translate" ? (existing.subtitle || 0) : (existing.audio || 0);
  let disabled = false;
  let text = "开始任务";
  const tips = [];
  if (running) {
    disabled = true;
    text = job.status === "cancelling" ? "正在取消…" : "任务运行中…";
    tips.push("等当前任务结束，或点「取消任务」。");
  }
  if (uploadBusy) {
    disabled = true;
    text = "正在上传…";
    tips.push("文件上传完成后才能开始。");
  }
  if (modelNeedsSelection() && (!model || !model.available)) {
    disabled = true;
    tips.push("先到「设置」配置可用的翻译模型。");
  }
  if (mode === "extract" && !$("optRefineNoModel").checked && model && !model.available) {
    tips.push("台本精修需要模型；可勾选「台本精修只用规则」，或换一个可用模型。");
  }
  if (other > 0) {
    tips.push(`暂存里有 ${other} 个当前模式用不到的文件；请清空，或切换任务类型。`);
  }
  if (!relevant && !existingRelevant) {
    tips.push(mode === "translate" ? "先拖入字幕文件。" : "先拖入音频/作品文件夹。");
  } else if (!relevant && existingRelevant) {
    tips.push("将使用工作区已有的文件。");
  }
  btn.disabled = disabled;
  btn.textContent = text;
  hint.textContent = tips.join(" ");
}
function currentModelForJob() {
  if (selectedMode === "extract" && $("optRefineNoModel").checked) return "";
  return $("modelSelect").value || "";
}

/* ---------------------------------------------------------------- 进度 */
function renderProgress(job) {
  const stepsBox = $("stageSteps");
  stepsBox.replaceChildren();
  const stages = STAGE_ORDER[(job && job.mode) || selectedMode] || STAGE_ORDER.all;
  const active = job ? job.stage : "";
  const orderIdx = stages.indexOf(active);
  const allDone = job && job.status === "done";
  stages.forEach((s, i) => {
    const step = el("span", "step", STAGE_LABEL[s] || s);
    if (allDone || (orderIdx >= 0 && i < orderIdx)) step.classList.add("done");
    if (active === s && job && job.status !== "done") step.classList.add("active");
    stepsBox.append(step);
  });
  const pct = job ? Number(job.percent || 0) : 0;
  const bar = $("jobBar");
  bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  bar.classList.toggle("indeterminate", !!(job && job.indeterminate && ["preparing", "running", "cancelling"].includes(job.status)));
  $("jobPercent").textContent = `${pct.toFixed(pct >= 10 ? 0 : 1)}%`;
  $("jobStageText").textContent = job
    ? (job.status === "error" ? (job.error || "任务失败") : (job.stage_label || STAGE_LABEL[job.stage] || "运行中"))
    : "等待任务";
  $("jobFile").textContent = job && job.current_file ? `当前：${job.current_file}` : "";
  const pill = $("jobStatusPill");
  pill.textContent = job ? (STATUS_TEXT[job.status] || job.status) : "空闲";
  pill.className = `pill ${job && ["preparing", "running", "cancelling"].includes(job.status) ? "running" : job ? (job.status === "done" ? "done" : job.status === "error" ? "error" : "cancelled") : "idle"}`;
  const start = job ? Date.parse(job.started_at || job.created_at) : 0;
  const end = job && job.ended_at ? Date.parse(job.ended_at) : Date.now();
  $("jobElapsed").textContent = start ? `已用 ${fmtDur((end - start) / 1000)}` : "";
  $("jobEta").textContent = job && job.eta_seconds != null ? `剩余约 ${fmtDur(job.eta_seconds)}` : "";
  const running = job && ["preparing", "running", "cancelling"].includes(job.status);
  const cancel = $("cancelBtn");
  cancel.classList.toggle("hidden", !running);
  cancel.disabled = !!(job && job.status === "cancelling");
}

/* ---------------------------------------------------------------- 结果 */
function renderResults(job) {
  const empty = $("resultsEmpty");
  const body = $("resultsBody");
  const results = (job && job.results) ? job.results : {};
  const running = !!(job && ["preparing", "running", "cancelling"].includes(job.status));
  const hasAnything = !!((results.lrc || []).length || (results.ja || []).length || results.bundle);
  if (!job || running || (job.status === "done" && !hasAnything && !results.incomplete)) {
    empty.classList.remove("hidden");
    body.classList.add("hidden");
    return;
  }
  empty.classList.add("hidden");
  body.classList.remove("hidden");
  const banner = $("resultBanner");
  banner.replaceChildren();
  let cls = "notice info";
  if (job.status === "error") { cls = "notice error"; banner.append(el("div", null, job.error || "任务失败，查看日志。")); }
  else if (job.status === "cancelled") { cls = "notice warn"; banner.append(el("div", null, "任务已取消；已生成的字幕/缓存保留，可在工作区继续。")); }
  else if (job.status === "interrupted") { cls = "notice warn"; banner.append(el("div", null, job.error || "任务被中断。")); }
  else if (results.incomplete) {
    cls = "notice warn";
    banner.append(el("div", null, `还有 ${results.missing?.length || 0} 轨没有中文字幕，本轮未归档：${(results.missing || []).slice(0, 8).join("、")}`));
    banner.append(el("div", "small", "文件留在工作区，补齐后再运行一次。"));
  } else if (results.has_bundle) {
    cls = "notice ok";
    banner.append(el("div", null, "已归档到作品包，LRC 也在 LRC 成品库里。"));
  } else if (job.options && job.options.limit) {
    cls = "notice info";
    banner.append(el("div", null, "试翻模式没有归档；产物在工作区，确认效果后正常跑一次。"));
  } else if (job.mode === "extract") {
    cls = "notice info";
    banner.append(el("div", null, `已提取 ${results.counts?.ja || 0} 轨日文字幕（工作区）。`));
  } else {
    cls = "notice info";
    banner.append(el("div", null, "任务完成，产物在工作区（未归档）。"));
  }
  banner.className = cls;
  banner.classList.remove("hidden");

  const paths = $("resultPaths");
  paths.replaceChildren();
  const addPath = (label, value) => {
    if (!value) return;
    const row = el("div");
    row.append(el("span", "muted", `${label}：`));
    row.append(el("code", null, value));
    paths.append(row);
  };
  addPath("作品包", results.bundle);
  addPath("LRC 成品库", results.collect);
  if (results.workspace) {
    addPath("工作区·日文", results.workspace.ja);
    addPath("工作区·中文", results.workspace.zh);
    addPath("工作区·LRC", results.workspace.lrc);
  }
  const lrcList = $("lrcList");
  lrcList.replaceChildren();
  (results.lrc || []).forEach((f) => lrcList.append(resultRow(f, "LRC")));
  (results.ja || []).forEach((f) => lrcList.append(resultRow(f, "日文字幕")));
  if (!(results.lrc || []).length && !(results.ja || []).length) {
    lrcList.append(el("div", "empty-hint", "没有可预览的产物；查看日志或打开工作区。"));
  }
  const data = stateData;
  $("openBundleBtn").disabled = !results.bundle;
  $("openCollectBtn").disabled = !results.collect;
  const zip = $("downloadZip");
  if ((results.lrc || []).length) {
    zip.href = `/api/download?work=${encodeURIComponent(job.work)}&token=${encodeURIComponent(TOKEN)}`;
    zip.classList.remove("hidden");
  } else {
    zip.classList.add("hidden");
    zip.removeAttribute("href");
  }
  $("openWorkspaceBtn").disabled = !(data && data.paths && data.paths.work);
}
function resultRow(f, label) {
  const row = el("div", "file-row");
  row.append(el("span", "file-kind", label));
  row.append(el("span", "file-name", f.name || ""));
  row.append(el("span", "file-size", fmtBytes(f.size || 0)));
  row.title = "点击预览";
  row.dataset.path = f.path || "";
  return row;
}

/* ---------------------------------------------------------------- 任务信息 */
function renderTaskInfo(job) {
  const box = $("taskInfo");
  box.replaceChildren();
  if (!job) { box.textContent = "还没有任务。"; return; }
  const lines = [];
  lines.push(`任务：${job.mode} / 作品 ${job.work} / 模型 ${job.model || "（不用模型）"}`);
  lines.push(`日志：${job.log_file || "（无）"}`);
  if (job.options) lines.push(`选项：${JSON.stringify(job.options)}`);
  (job.commands || []).forEach((c, i) => {
    lines.push("");
    lines.push(`[${i + 1}] ${c.label || c.name}`);
    lines.push(c.command || "");
  });
  box.textContent = lines.join("\n");
}

/* ---------------------------------------------------------------- 日志 */
function appendLogs(logs) {
  const entries = logs.entries || [];
  const jobId = stateData?.job?.id || "";
  const log = $("log");
  if (jobId !== logJobId) {
    logJobId = jobId;
    log.replaceChildren();
  }
  if (logs.truncated) {
    log.append(el("div", "empty-hint", "（更早的日志已省略；可打开日志文件查看完整记录）"));
  }
  entries.forEach((e) => {
    const line = el("div", `log-line ${(e.level || "INFO").toLowerCase()}`);
    line.append(el("span", "lt", e.t || ""));
    line.append(el("span", "lv", e.level || "INFO"));
    line.append(el("span", "tx", e.text || ""));
    log.append(line);
  });
  while (log.childElementCount > 3000) log.firstElementChild.remove();
  lastSeq = logs.last_seq || lastSeq;
  if ($("autoScroll").checked) log.scrollTop = log.scrollHeight;
}

/* ---------------------------------------------------------------- 设置页 */
function renderSettings(data) {
  const settings = data.settings || {};
  const models = settings.models || [];
  const warnings = settings.config_warnings || [];
  const box = $("settingsWarnings");
  box.replaceChildren();
  if (warnings.length) {
    warnings.forEach((w) => box.append(el("div", null, w)));
    box.classList.remove("hidden");
  } else {
    box.classList.add("hidden");
  }
  renderLocalModels(models.filter((m) => m.kind === "local"));
  renderApiList(models.filter((m) => m.kind === "api"));
  const about = $("aboutInfo");
  const paths = settings.paths || {};
  const llama = settings.llama_server || "";
  about.innerHTML = [
    `版本：${esc(settings.version || "dev")}`,
    `项目目录：<code>${esc(paths.root || "")}</code>`,
    `配置文件：<code>${esc(settings.config_path || "")}</code>`,
    `本地覆盖：<code>${esc(settings.local_config_path || "")}</code>`,
    `Key 文件：<code>${esc(settings.env_path || "")}</code>`,
    `llama-server：${llama ? esc(llama) : "未找到（本地模型需要；云端 API 不需要）"}`,
    "音频只在本机处理；云端模式只把字幕文本发给你配置的 API。",
  ].map((s) => `<div>${s}</div>`).join("");
}
function renderLocalModels(models) {
  const box = $("localModels");
  box.replaceChildren();
  if (!models.length) {
    box.append(el("div", "empty-hint", "还没有本地模型。点右上角「添加模型」选择 .gguf。"));
    return;
  }
  models.forEach((m) => {
    const row = el("div", `model-row ${m.available ? "" : "unavailable"}`);
    const main = el("div", "model-main");
    const name = el("div", "model-name");
    name.append(el("span", null, m.display_name || m.key));
    if (m.is_default) name.append(el("span", "tag default", "默认"));
    name.append(el("span", "tag", m.available ? "可用" : "不可用"));
    if (m.local_override) name.append(el("span", "tag off", "本地覆盖"));
    main.append(name);
    if (m.path) main.append(el("div", "model-path", m.path));
    const detail = [
      m.params, m.quant, m.size_gb ? fmtGB(m.size_gb) : "",
      m.architecture, m.context_length ? `${m.context_length} ctx` : "",
    ].filter(Boolean).join(" · ");
    if (detail) main.append(el("div", "model-detail", detail));
    if (m.auto_display_name && m.auto_display_name !== m.display_name) main.append(el("div", "model-detail", `自动识别：${m.auto_display_name}`));
    if (m.reason) main.append(el("div", "model-detail", m.reason));
    (m.warnings || []).slice(0, 2).forEach((w) => main.append(el("div", "model-detail", `⚠ ${w}`)));
    const actions = el("div", "model-actions");
    const edit = el("button", "ghost small", "编辑");
    edit.dataset.action = "edit-local"; edit.dataset.key = m.key; edit.dataset.path = m.path || "";
    actions.append(edit);
    if (m.available && !m.is_default) {
      const def = el("button", "ghost small", "设为默认");
      def.dataset.action = "default"; def.dataset.key = m.key;
      actions.append(def);
    }
    if (m.path) {
      const rein = el("button", "ghost small", "重新识别");
      rein.dataset.action = "reinspect"; rein.dataset.path = m.path;
      actions.append(rein);
    }
    if (m.local_override) {
      const rm = el("button", "ghost danger small", "移除");
      rm.dataset.action = "remove-local"; rm.dataset.key = m.key;
      actions.append(rm);
    }
    row.append(main, actions);
    box.append(row);
  });
}
function renderApiList(models) {
  const box = $("apiList");
  box.replaceChildren();
  if (!models.length) {
    box.append(el("div", "empty-hint", "还没有云端 API 条目。"));
    return;
  }
  models.forEach((m) => {
    const row = el("div", `model-row ${m.available ? "" : "unavailable"}`);
    const main = el("div", "model-main");
    const name = el("div", "model-name");
    name.append(el("span", null, m.display_name || m.key));
    if (m.is_default) name.append(el("span", "tag default", "默认"));
    name.append(el("span", "tag", m.provider_label || "API"));
    name.append(el("span", `tag ${m.key_configured ? "" : "off"}`, KEY_SOURCE_TEXT[m.key_source] || "未配置"));
    main.append(name);
    main.append(el("div", "model-detail", `${m.base_url || ""} · ${m.model || ""}`));
    if (m.reason) main.append(el("div", "model-detail", m.reason));
    (m.warnings || []).slice(0, 2).forEach((w) => main.append(el("div", "model-detail", `⚠ ${w}`)));
    const actions = el("div", "model-actions");
    const edit = el("button", "ghost small", "编辑");
    edit.dataset.action = "edit-api"; edit.dataset.key = m.key;
    actions.append(edit);
    if (m.available && !m.is_default) {
      const def = el("button", "ghost small", "设为默认");
      def.dataset.action = "default"; def.dataset.key = m.key;
      actions.append(def);
    }
    if (m.key_configured) {
      const clr = el("button", "ghost danger small", "清除 Key");
      clr.dataset.action = "clear-api"; clr.dataset.key = m.key;
      actions.append(clr);
    }
    row.append(main, actions);
    box.append(row);
  });
}

/* ---------------------------------------------------------------- 设置动作 */
function fillApiForm(model) {
  editingApiKey = model ? model.key : "";
  $("apiPanel").classList.remove("hidden");
  $("apiProvider").value = model && model.provider ? (model.provider === "deepseek" ? "deepseek" : "custom") : "custom";
  $("apiBase").value = model?.base_url || (model && model.provider === "deepseek" ? "https://api.deepseek.com/v1" : "");
  $("apiModel").value = model?.model || "";
  $("apiDisplay").value = model?.by_tag || model?.display_name || "";
  $("apiKey").value = "";
  $("apiTestResult").classList.add("hidden");
  $("apiTestResult").replaceChildren();
  updateApiKeyStatus(model);
  $("apiPanel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}
function newApiForm() {
  editingApiKey = "";
  $("apiPanel").classList.remove("hidden");
  $("apiProvider").value = "custom";
  $("apiBase").value = "";
  $("apiModel").value = "";
  $("apiDisplay").value = "";
  $("apiKey").value = "";
  $("apiTestResult").classList.add("hidden");
  $("apiTestResult").replaceChildren();
  $("apiKeyStatus").textContent = "新的自定义 API；保存后生效。";
  $("apiPanel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}
function updateApiKeyStatus(model) {
  const box = $("apiKeyStatus");
  if (!model) {
    box.textContent = editingApiKey ? "" : "新的自定义 API；保存后生效。";
    return;
  }
  box.textContent = `Key：${KEY_SOURCE_TEXT[model.key_source] || "未配置"}；环境变量 ${model.key_env || ""}`;
  if (model.key_source === "config") box.textContent += "（config 里的 api_key 优先于 .env，建议清空）";
}
async function testApi() {
  const btn = $("apiTestBtn");
  const box = $("apiTestResult");
  box.classList.add("hidden");
  box.replaceChildren();
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "测试中…";
  try {
    const data = await api("/api/api/test", {
      json: {
        key: editingApiKey,
        provider: $("apiProvider").value,
        api_key: $("apiKey").value.trim(),
        base_url: $("apiBase").value.trim(),
        model: $("apiModel").value.trim(),
      },
      allowError: true,
    });
    box.replaceChildren();
    if (data.ok) {
      box.className = "notice ok";
      box.append(el("div", null, `连接成功：${data.model} @ ${data.base_url}`));
      if ((data.models || []).length) box.append(el("div", "small", `端点模型：${data.models.slice(0, 12).join("、")}`));
      if (data.sample) box.append(el("div", "small", `测试译文：${data.sample}`));
      (data.warnings || []).forEach((w) => box.append(el("div", "small", `⚠ ${w}`)));
      const dl = $("apiModelList");
      dl.replaceChildren();
      (data.models || []).forEach((id) => {
        const o = document.createElement("option"); o.value = id; dl.append(o);
      });
    } else {
      box.className = "notice error";
      box.append(el("div", null, data.error || "测试失败"));
      (data.warnings || []).forEach((w) => box.append(el("div", "small", `⚠ ${w}`)));
    }
    box.classList.remove("hidden");
  } catch (e) {
    box.className = "notice error";
    box.replaceChildren(el("div", null, e.message));
    box.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}
async function saveApi() {
  const btn = $("apiSaveBtn");
  const base = $("apiBase").value.trim();
  const model = $("apiModel").value.trim();
  if (!base || !model) { toast("请填写 Base URL 和模型名", "error"); return; }
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "保存中…";
  try {
    const data = await api("/api/api/save", {
      json: {
        key: editingApiKey,
        provider: $("apiProvider").value,
        api_key: $("apiKey").value.trim(),
        base_url: base,
        model,
        display_name: $("apiDisplay").value.trim(),
      },
    });
    editingApiKey = data.key || editingApiKey;
    $("apiKey").value = "";
    if (data.model && data.model.key_configured) toast("API 设置已保存", "ok");
    else toast("已保存 API 设置，但还没有 Key；填上 Key 后再测试/保存一次");
    updateApiKeyStatus(data.model || null);
    requestRefresh();
    setView("main");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}
async function clearApiKey(key) {
  if (!key) return;
  if (!confirm("确定清除这个 API 的 Key？清除后该条目会变为不可用。")) return;
  try {
    await api("/api/api/clear", { json: { key } });
    toast("已清除 API Key", "ok");
    requestRefresh();
  } catch (e) { toast(e.message, "error"); }
}
function updateProviderDefaults() {
  const provider = $("apiProvider").value;
  if (provider === "deepseek") {
    if (!$("apiBase").value.trim()) $("apiBase").value = "https://api.deepseek.com/v1";
    if (!$("apiModel").value.trim()) $("apiModel").value = "deepseek-flash";
  }
}

/* ---------------------------------------------------------------- 模型识别/保存 */
async function pickModel(kind) {
  const btn = kind === "file" ? $("pickModelFile") : $("pickModelFolder");
  btn.disabled = true;
  try {
    toast("等待系统文件窗口…");
    const data = await api("/api/models/pick", { json: { kind }, allowError: true });
    if (data.cancelled) return;
    if (!data.path) { if (data.error) toast(data.error, "error"); return; }
    $("modelPath").value = data.path;
    await inspectModelPath(data.path);
  } catch (e) { toast(e.message, "error"); }
  finally { btn.disabled = false; }
}
async function inspectModelPath(path, key = "") {
  const box = $("inspectResult");
  box.className = "notice info";
  box.textContent = "识别中…";
  box.classList.remove("hidden");
  try {
    const data = await api("/api/models/inspect", { json: { path, force: true }, allowError: true });
    if (data.is_dir) {
      $("modelSavePanel").classList.add("hidden");
      pendingModel = null;
      renderCandidates(data.candidates || []);
      box.className = "notice info";
      box.textContent = `文件夹里有 ${data.candidates.length} 个模型，选择一个继续。`;
      return;
    }
    const model = data.model || {};
    if (!data.ok || !model.ok) {
      $("modelSavePanel").classList.add("hidden");
      pendingModel = null;
      box.className = "notice error";
      box.textContent = data.error || model.error || "无法识别这个文件";
      return;
    }
    pendingModel = model;
    pendingModelKey = key || "";
    $("modelDisplayName").value = model.auto_display_name || model.name || "";
    renderModelMeta(model);
    $("modelSavePanel").classList.remove("hidden");
    box.className = "notice ok";
    box.textContent = `识别成功：${model.auto_display_name}${model.quant ? ` · ${model.quant}` : ""}`;
    $("modelCandidates").replaceChildren();
  } catch (e) {
    box.className = "notice error";
    box.textContent = e.message;
    $("modelSavePanel").classList.add("hidden");
  }
}
function renderCandidates(candidates) {
  const box = $("modelCandidates");
  box.replaceChildren();
  (candidates || []).forEach((c) => {
    const row = el("div", "file-row");
    row.append(el("span", "file-kind", "GGUF"));
    row.append(el("span", "file-name", c.file_name || c.path || ""));
    row.append(el("span", "file-size", fmtBytes(c.size_bytes || 0)));
    const btn = el("button", "ghost small", "选择");
    btn.dataset.path = c.path || "";
    btn.dataset.action = "candidate";
    row.append(btn);
    box.append(row);
  });
}
function renderModelMeta(model) {
  const box = $("modelMeta");
  box.replaceChildren();
  const add = (label, value) => {
    if (value === undefined || value === null || value === "") return;
    const wrap = el("div");
    wrap.append(el("b", null, `${label}：`));
    wrap.append(document.createTextNode(String(value)));
    box.append(wrap);
  };
  add("文件", model.file_name);
  add("文件内名称", model.name);
  add("参数量", model.params);
  add("量化", model.quant);
  add("架构", model.architecture);
  add("上下文", model.context_length);
  add("大小", model.size_gb ? fmtGB(model.size_gb) : "");
  add("聊天模板", model.has_chat_template ? "有" : "没有");
  if ((model.split_count || 1) > 1) add("分片", `${model.split_no}/${model.split_count}`);
  (model.warnings || []).forEach((w) => add("提示", w));
}
async function saveModel() {
  if (!pendingModel || !pendingModel.path) { toast("请先识别一个模型", "error"); return; }
  const btn = $("saveModelBtn");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "保存中…";
  try {
    await api("/api/models/add", {
      json: {
        path: pendingModel.load_path || pendingModel.path,
        key: pendingModelKey,
        display_name: $("modelDisplayName").value.trim(),
        make_default: $("modelMakeDefault").checked,
      },
    });
    toast("本地模型已保存", "ok");
    pendingModel = null;
    pendingModelKey = "";
    $("modelSavePanel").classList.add("hidden");
    $("inspectResult").classList.add("hidden");
    $("modelCandidates").replaceChildren();
    $("addModelPanel").classList.add("hidden");
    requestRefresh();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}
async function localRowAction(action, key, path) {
  try {
    if (action === "remove-local") {
      if (!confirm("确定移除这个本地模型？只会删除 config.local.yaml 里的覆盖，不删除 .gguf 文件。")) return;
      await api("/api/models/remove", { json: { key } });
      toast("已移除", "ok");
    } else if (action === "default") {
      await api("/api/models/default", { json: { key } });
      toast("已设为默认", "ok");
    } else if (action === "reinspect") {
      await api("/api/models/inspect", { json: { path, force: true }, allowError: true });
      toast("已重新识别", "ok");
    } else if (action === "edit-local") {
      editingApiKey = "";
      $("addModelPanel").classList.remove("hidden");
      $("modelPath").value = path || "";
      $("modelDisplayName").value = "";
      await inspectModelPath(path || "", key);
      return;
    }
    requestRefresh();
  } catch (e) { toast(e.message, "error"); }
}
async function apiRowAction(action, key) {
  if (action === "edit-api") {
    const model = (stateData?.settings?.models || []).find((m) => m.key === key);
    fillApiForm(model || { key });
  } else if (action === "default") {
    try {
      await api("/api/models/default", { json: { key } });
      toast("已设为默认", "ok");
      requestRefresh();
    } catch (e) { toast(e.message, "error"); }
  } else if (action === "clear-api") {
    await clearApiKey(key);
  }
}

/* ---------------------------------------------------------------- 打开/预览/日志 */
async function openPath(path) {
  if (!path) { toast("这个文件夹不存在", "error"); return; }
  try {
    const data = await api("/api/reveal", { json: { path }, allowError: true });
    if (!data.ok) throw new Error(data.error || "系统文件管理器打开失败");
  } catch (e) { toast(e.message, "error"); }
}
async function previewPath(path) {
  if (!path) return;
  try {
    const data = await api(`/api/preview?path=${encodeURIComponent(path)}`);
    $("modalTitle").textContent = data.name || "预览";
    $("modalBody").textContent = data.text + (data.truncated ? "\n\n…（内容较长，已截断）" : "");
    $("modal").classList.remove("hidden");
  } catch (e) { toast(e.message, "error"); }
}
function closeModal() { $("modal").classList.add("hidden"); }
function openFolderKey(key) {
  const paths = stateData?.settings?.paths || {};
  const map = {
    input: paths.input,
    output: paths.output,
    collect: paths.collect,
    config: paths.root,
    logs: paths.logs,
  };
  openPath(map[key]);
}
function openJobLog() {
  const job = stateData?.job;
  if (job && job.log_file) openPath(job.log_file);
  else toast("还没有日志文件", "error");
}
async function copyLog() {
  const text = $("log").innerText || "";
  try {
    await navigator.clipboard.writeText(text);
    toast("日志已复制", "ok");
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.append(ta);
    ta.select();
    try { document.execCommand("copy"); toast("日志已复制", "ok"); }
    catch (_) { toast("复制失败，请手动选择", "error"); }
    ta.remove();
  }
}

/* ---------------------------------------------------------------- 上传 */
function setUploadUi(busy, text = "", percent = 0) {
  uploadBusy = busy;
  $("uploadState").classList.toggle("hidden", !busy && !text);
  $("uploadBar").style.width = `${Math.max(0, Math.min(100, percent))}%`;
  $("uploadText").textContent = text;
  if (stateData) updateStart(stateData.settings?.models || [], stateData.job);
}
function xhrUpload(file, kind, relpath, workHint, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const q = new URLSearchParams({ kind, relpath, work_hint: workHint || "" });
    xhr.open("POST", `/api/upload?${q.toString()}`);
    xhr.setRequestHeader("X-GUI-Token", TOKEN);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded, e.total); };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (_) { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300 && data && data.ok) resolve(data);
      else reject(new Error((data && data.error) || `上传失败（HTTP ${xhr.status}）`));
    };
    xhr.onerror = () => reject(new Error("上传网络错误"));
    xhr.send(file);
  });
}
async function uploadFiles(items, { workHint = "" } = {}) {
  if (!items.length) { toast("没有可上传的文件", "error"); return; }
  if (stateData?.job && ["preparing", "running", "cancelling"].includes(stateData.job.status)) {
    toast("任务运行中不能继续上传", "error");
    return;
  }
  const total = items.reduce((n, it) => n + (it.file.size || 0), 0);
  let doneBytes = 0;
  setUploadUi(true, "准备上传…", 0);
  let uploaded = 0;
  try {
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      const label = it.relpath || it.file.name;
      const res = await xhrUpload(it.file, it.kind, it.relpath, workHint, (loaded) => {
        const pct = total ? ((doneBytes + loaded) / total) * 100 : 0;
        setUploadUi(true, `上传 ${i + 1}/${items.length}：${label}`, pct);
      });
      doneBytes += it.file.size || 0;
      uploaded++;
      if (res.staged && res.staged.work_hint && !workNameTouched && !$("workName").value) {
        $("workName").value = res.staged.work_hint;
      }
    }
    const hint = $("workName").value.trim();
    if (!hint && !workNameTouched) $("workName").value = suggestWorkName();
    toast(`已上传 ${uploaded} 个文件`, "ok");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    setUploadUi(false, "", 0);
    requestRefresh();
  }
}
function suggestWorkName() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `作品_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}`;
}
function relativeFromPath(path, top) {
  let p = String(path || "").replace(/^\/+/, "");
  if (top && p.startsWith(top + "/")) p = p.slice(top.length + 1);
  return p || "";
}
function topFolderOf(paths) {
  const tops = new Set();
  paths.forEach((p) => {
    const clean = String(p || "").replace(/^\/+/, "");
    if (clean.includes("/")) tops.add(clean.split("/")[0]);
  });
  if (tops.size > 1) return { conflict: true, top: "" };
  return { conflict: false, top: tops.size === 1 ? [...tops][0] : "" };
}
function workHintFrom(top, names) {
  if (top) return top;
  const rjs = new Set();
  names.forEach((n) => {
    const m = String(n || "").match(/RJ\d{5,}/i);
    if (m) rjs.add(m[0].toUpperCase());
  });
  return rjs.size === 1 ? [...rjs][0] : "";
}
async function walkEntry(entry, out) {
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    const path = (entry.fullPath || file.name).replace(/^\/+/, "");
    out.push({ file, path });
    return;
  }
  if (entry.isDirectory) {
    const reader = entry.createReader();
    while (true) {
      const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
      if (!batch.length) break;
      for (const e of batch) await walkEntry(e, out);
    }
  }
}
async function filesFromDataTransfer(dt) {
  const items = Array.from(dt.items || []);
  const entries = items.map((it) => (it.webkitGetAsEntry ? it.webkitGetAsEntry() : null)).filter(Boolean);
  if (entries.length) {
    const out = [];
    for (const e of entries) await walkEntry(e, out);
    return out;
  }
  return Array.from(dt.files || []).map((f) => ({ file: f, path: f.webkitRelativePath || f.name }));
}
async function handleIncoming(items, target) {
  if (!items.length) { toast("没有读到文件", "error"); return; }
  const kind = target === "promptbook" ? "promptbook"
    : (selectedMode === "translate" ? "subtitle" : "audio");
  const names = items.map((it) => it.path || it.file.name);
  const { conflict, top } = topFolderOf(names);
  if (conflict) { toast("一次只能上传一个作品文件夹，请分批拖入", "error"); return; }
  const hint = workHintFrom(top, names);
  const picked = items.filter((it) => {
    const name = it.file.name || "";
    if (kind === "promptbook") return /\.txt$/i.test(name);
    return true;
  });
  if (!picked.length) {
    toast(kind === "promptbook" ? "台本只接受 .txt 文件" : "没有可上传的文件", "error");
    return;
  }
  if (kind === "audio" && picked.some((it) => /\.(gguf|safetensors|bin|pt|onnx)$/i.test(it.file.name))) {
    toast("模型文件请到「设置 → 本地模型」里选择，不要通过音频上传", "error");
    return;
  }
  const uploads = picked.map((it) => ({
    file: it.file,
    kind,
    relpath: relativeFromPath(it.path, top),
  }));
  await uploadFiles(uploads, { workHint: hint });
}
function bindDropZone(zone, target) {
  ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => {
    e.preventDefault();
    e.stopPropagation();
    zone.classList.add("dragover");
  }));
  ["dragleave", "dragend"].forEach((ev) => zone.addEventListener(ev, () => zone.classList.remove("dragover")));
  zone.addEventListener("drop", async (e) => {
    e.preventDefault();
    e.stopPropagation();
    zone.classList.remove("dragover");
    try {
      const items = await filesFromDataTransfer(e.dataTransfer);
      await handleIncoming(items, target);
    } catch (err) { toast(err.message, "error"); }
  });
  zone.addEventListener("click", (e) => {
    if (e.target.closest("button")) return;
    if (e.target.closest("input")) return;
    if (target === "promptbook") $("promptbookInput").click(); else $("fileInput").click();
  });
  zone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (target === "promptbook") $("promptbookInput").click(); else $("fileInput").click();
    }
  });
}
function filesFromInput(input) {
  return Array.from(input.files || []).map((f) => ({
    file: f,
    path: f.webkitRelativePath || f.name,
  }));
}

/* ---------------------------------------------------------------- 任务动作 */
async function startJob() {
  const work = $("workName").value.trim();
  if (!work) { toast("请先填写作品名，或拖入文件夹自动识别", "error"); $("workName").focus(); return; }
  const mode = selectedMode;
  const model = currentModelForJob();
  const staged = stateData?.staged || {};
  const relevant = mode === "translate" ? (staged.files?.subtitle || []).length : (staged.files?.audio || []).filter((f) => f.is_audio).length;
  const limitRaw = $("optLimit").value.trim();
  const payload = {
    mode,
    work,
    model,
    use_existing: !relevant,
    options: {
      force: $("optForce").checked,
      bilingual: (mode === "all" || mode === "translate") && $("optBilingual").checked,
      no_consolidate: (mode === "all" || mode === "translate") && $("optNoConsolidate").checked,
      limit: limitRaw ? Number(limitRaw) : null,
      prevent_sleep: $("optPreventSleep").checked,
      refine_no_model: (mode === "extract" || mode === "all") && $("optRefineNoModel").checked,
    },
  };
  $("startBtn").disabled = true;
  try {
    await api("/api/start", { json: payload });
    toast("任务已开始", "ok");
    lastSeq = 0;
    logJobId = "";
    requestRefresh();
    $("log").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (e) {
    toast(e.message, "error");
  } finally {
    if (stateData) updateStart(stateData.settings?.models || [], stateData.job);
  }
}
async function cancelJob() {
  if (!confirm("确定取消当前任务？已生成的字幕/缓存会保留。")) return;
  try {
    await api("/api/cancel", { json: {} });
    toast("正在取消…");
    requestRefresh();
  } catch (e) { toast(e.message, "error"); }
}
async function clearStaged() {
  const has = stateData?.staged && ((stateData.staged.files?.audio || []).length
    || (stateData.staged.files?.subtitle || []).length
    || (stateData.staged.files?.promptbook || []).length);
  if (!has) { toast("暂存已经是空的"); return; }
  if (!confirm("清空所有已上传的暂存文件？（已经进入 input/工作区的不会删除）")) return;
  try {
    await api("/api/clear", { json: {} });
    $("fileInput").value = "";
    $("folderInput").value = "";
    $("promptbookInput").value = "";
    toast("暂存已清空", "ok");
    requestRefresh();
  } catch (e) { toast(e.message, "error"); }
}
async function shutdown() {
  if (!confirm("退出图形界面服务？正在运行的任务会被取消。")) return;
  try {
    await api("/api/shutdown", { json: {} });
    polling = false;
    clearTimeout(pollTimer);
    $("statusPill").textContent = "服务已退出";
    $("statusPill").className = "pill idle";
    $("shutdownBtn").disabled = true;
    toast("服务已退出，可以关闭本页", "ok");
  } catch (e) { toast(e.message, "error"); }
}

/* ---------------------------------------------------------------- 设置页绑定 */
function bindSettings() {
  $("openSettings").addEventListener("click", () => { setView("settings"); renderSettings(stateData || { settings: {} }); });
  $("openSettingsSmall").addEventListener("click", () => { setView("settings"); renderSettings(stateData || { settings: {} }); });
  $("backToMain").addEventListener("click", () => setView("main"));
  $("addModelToggle").addEventListener("click", () => {
    $("addModelPanel").classList.toggle("hidden");
    if (!$("addModelPanel").classList.contains("hidden")) $("modelPath").focus();
  });
  $("addApiBtn").addEventListener("click", newApiForm);
  $("pickModelFile").addEventListener("click", () => pickModel("file"));
  $("pickModelFolder").addEventListener("click", () => pickModel("folder"));
  $("inspectModelBtn").addEventListener("click", () => {
    const p = $("modelPath").value.trim();
    if (!p) { toast("请先选择或输入模型路径", "error"); return; }
    inspectModelPath(p);
  });
  $("modelPath").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); $("inspectModelBtn").click(); }
  });
  $("saveModelBtn").addEventListener("click", saveModel);
  $("apiProvider").addEventListener("change", updateProviderDefaults);
  $("apiTestBtn").addEventListener("click", testApi);
  $("apiSaveBtn").addEventListener("click", saveApi);
  $("apiKeyClear").addEventListener("click", () => {
    if (!editingApiKey) { toast("先保存这个 API 条目，再清除 Key", "error"); return; }
    clearApiKey(editingApiKey);
  });
  $("localModels").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    localRowAction(btn.dataset.action, btn.dataset.key, btn.dataset.path);
  });
  $("apiList").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    apiRowAction(btn.dataset.action, btn.dataset.key);
  });
  $("modelCandidates").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-action='candidate']");
    if (!btn) return;
    $("modelPath").value = btn.dataset.path || "";
    inspectModelPath(btn.dataset.path || "", pendingModelKey);
  });
  document.querySelectorAll("#settingsView button[data-open]").forEach((btn) => {
    btn.addEventListener("click", () => openFolderKey(btn.dataset.open));
  });
}

/* ---------------------------------------------------------------- 绑定 */
function bindMain() {
  document.querySelectorAll('input[name="mode"]').forEach((r) => {
    r.addEventListener("change", () => {
      if (!r.checked) return;
      selectedMode = r.value;
      savePrefs();
      renderMode();
      render();
    });
  });
  $("modelSelect").addEventListener("change", () => {
    modelSelection = $("modelSelect").value;
    savePrefs();
    renderModelNote(currentModel());
    updateStart(stateData?.settings?.models || [], stateData?.job || null);
  });
  ["optForce", "optBilingual", "optNoConsolidate", "optRefineNoModel", "optPreventSleep", "optLimit"].forEach((id) => {
    $(id).addEventListener("change", () => { savePrefs(); updateStart(stateData?.settings?.models || [], stateData?.job || null); });
  });
  $("workName").addEventListener("input", () => {
    workNameTouched = true;
    renderExisting(stateData?.existing || {});
    updateStart(stateData?.settings?.models || [], stateData?.job || null);
  });
  $("pickFiles").addEventListener("click", (e) => { e.stopPropagation(); $("fileInput").click(); });
  $("pickFolder").addEventListener("click", (e) => { e.stopPropagation(); $("folderInput").click(); });
  $("pickPromptbook").addEventListener("click", (e) => { e.stopPropagation(); $("promptbookInput").click(); });
  $("fileInput").addEventListener("change", async () => {
    const items = filesFromInput($("fileInput"));
    $("fileInput").value = "";
    await handleIncoming(items, "main");
  });
  $("folderInput").addEventListener("change", async () => {
    const items = filesFromInput($("folderInput"));
    $("folderInput").value = "";
    await handleIncoming(items, "main");
  });
  $("promptbookInput").addEventListener("change", async () => {
    const items = filesFromInput($("promptbookInput"));
    $("promptbookInput").value = "";
    await handleIncoming(items, "promptbook");
  });
  $("clearFiles").addEventListener("click", clearStaged);
  $("startBtn").addEventListener("click", startJob);
  $("cancelBtn").addEventListener("click", cancelJob);
  $("shutdownBtn").addEventListener("click", shutdown);
  $("copyLogBtn").addEventListener("click", copyLog);
  $("openLogBtn").addEventListener("click", openJobLog);
  $("openBundleBtn").addEventListener("click", () => openPath(stateData?.job?.results?.bundle));
  $("openCollectBtn").addEventListener("click", () => openPath(stateData?.job?.results?.collect));
  $("openWorkspaceBtn").addEventListener("click", () => openPath(stateData?.paths?.work));
  $("lrcList").addEventListener("click", (e) => {
    const row = e.target.closest(".file-row");
    if (row && row.dataset.path) previewPath(row.dataset.path);
  });
  $("log").addEventListener("click", (e) => {
    const row = e.target.closest(".log-line");
    if (!row) return;
  });
  $("modalClose").addEventListener("click", closeModal);
  $("modal").addEventListener("click", (e) => { if (e.target === $("modal")) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
  $("autoScroll").addEventListener("change", () => {
    if ($("autoScroll").checked) $("log").scrollTop = $("log").scrollHeight;
  });
  bindDropZone($("dropZone"), "main");
  bindDropZone($("promptbookDrop"), "promptbook");
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", (e) => e.preventDefault());
}

/* ---------------------------------------------------------------- 启动 */
function init() {
  if (!TOKEN) {
    $("tokenError").classList.remove("hidden");
    $("app").classList.add("hidden");
    return;
  }
  loadPrefs();
  bindMain();
  bindSettings();
  $("modelSelect").disabled = false;
  try { onboarded = sessionStorage.getItem("asmr.onboarded") === "1"; } catch (_) { onboarded = false; }
  renderMode();
  if (location.hash === "#settings") setView("settings");
  pollState(true);
}

document.addEventListener("DOMContentLoaded", init);
