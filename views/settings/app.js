/* 视频解析自动归档 - 插件管理页 */
const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);

const state = {
  settings: null,
  meta: null,
  history: [],
  dirty: false,
  previewTimer: null,
  saving: false,
};

const STATUS_TEXT = { ok: "已保存", error: "失败", skipped: "跳过" };

/* ------------------------------ 基础工具 ------------------------------ */

function fmtSize(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${units[index]}`;
}

function showToast(message, type = "ok") {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast show${type === "error" ? " error" : ""}`;
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => {
    toast.className = "toast";
  }, type === "error" ? 4200 : 2400);
}

async function api(path, { method = "GET", params, body } = {}) {
  if (!bridge || typeof bridge.apiGet !== "function") {
    throw new Error("无法连接 AstrBot 页面桥接，请从插件详情页打开本页面");
  }
  const raw =
    method === "GET" ? await bridge.apiGet(path, params) : await bridge.apiPost(path, body);
  let data = raw;
  if (typeof data === "string") {
    try {
      data = JSON.parse(data);
    } catch (error) {
      throw new Error("服务端返回了无法解析的内容");
    }
  }
  if (!data) throw new Error("服务端没有返回数据");
  if (data.status === "error") throw new Error(data.message || "请求失败");
  if (data.ok === false) throw new Error(data.message || "操作失败");
  return data;
}

function markDirty() {
  state.dirty = true;
  $("dirty-hint").textContent = "有未保存的修改";
  $("dirty-hint").classList.remove("hidden");
}

function clearDirty() {
  state.dirty = false;
  $("dirty-hint").classList.add("hidden");
}

function confirmDialog(message, { danger = false } = {}) {
  return new Promise((resolve) => {
    const modal = $("modal");
    const ok = $("modal-ok");
    const cancel = $("modal-cancel");
    $("modal-text").textContent = message;
    ok.className = `btn ${danger ? "confirm-danger" : "primary"}`;
    modal.classList.remove("hidden");

    const done = (value) => {
      modal.classList.add("hidden");
      ok.onclick = null;
      cancel.onclick = null;
      modal.onclick = null;
      document.removeEventListener("keydown", onKey);
      resolve(value);
    };
    const onKey = (event) => {
      if (event.key === "Escape") done(false);
      if (event.key === "Enter") done(true);
    };
    ok.onclick = () => done(true);
    cancel.onclick = () => done(false);
    modal.onclick = (event) => {
      if (event.target === modal) done(false);
    };
    document.addEventListener("keydown", onKey);
    cancel.focus();
  });
}

function applyTheme(isDark) {
  document.documentElement.dataset.theme = isDark ? "dark" : "light";
}

/* ------------------------------ 渲染 ------------------------------ */

function renderStatus() {
  const settings = state.settings;
  const meta = state.meta;
  const parser = meta.parser || {};
  const stats = meta.stats || {};

  const chipEnabled = $("chip-enabled");
  chipEnabled.textContent = settings.enabled ? "自动归档：已启用" : "自动归档：已停用";
  chipEnabled.className = `chip ${settings.enabled ? "ok" : "off"}`;

  const chipParser = $("chip-parser");
  if (parser.installed && parser.hooked) {
    chipParser.textContent = "解析插件：已连接";
    chipParser.className = "chip ok";
  } else if (parser.installed) {
    chipParser.textContent = "解析插件：已安装，等待挂钩";
    chipParser.className = "chip";
  } else {
    chipParser.textContent = "解析插件：未检测到";
    chipParser.className = "chip bad";
  }

  $("version-badge").textContent = `v${meta.version || "1.0.0"}`;
  $("enabled").checked = !!settings.enabled;
  $("parser-state").textContent = parser.installed
    ? `${parser.version || "未知版本"}${parser.hooked ? " · 已挂钩" : ""}`
    : "未检测到";
  $("stat-saved").textContent = String(stats.saved ?? 0);
  $("stat-bytes").textContent = fmtSize(stats.bytes ?? 0);
  $("stat-fail").textContent = `${stats.failed ?? 0} / ${stats.skipped ?? 0}`;

  let hint = "";
  if (!parser.installed) {
    hint =
      "未检测到 astrbot_plugin_api_video_parser（ParseVideo 视频图集解析）。本插件依赖它解析链接；请先安装并启用解析插件，然后重载本插件。";
  } else if (!parser.hooked) {
    hint =
      "已检测到解析插件，但尚未挂钩到它的解析函数。可以重载一次本插件；如果仍然不行，可能是解析插件版本改了方法名，此时只能从消息文本里提取标题/作者。";
  } else {
    hint = `解析服务地址：${parser.api_base || "未知"}。归档发生在「解析完成、发送给平台之前」，不会重复解析或重复下载视频。`;
  }
  $("parser-hint").textContent = hint;
}

function renderWhitelist() {
  const body = $("wl-body");
  body.replaceChildren();
  const list = state.settings.whitelist || [];
  $("wl-count").textContent = `${list.length} 人`;

  if (!list.length) {
    const tr = document.createElement("tr");
    tr.className = "empty";
    const td = document.createElement("td");
    td.colSpan = 3;
    td.textContent = "白名单为空，暂时不会保存任何内容";
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }

  list.forEach((item, index) => {
    const tr = document.createElement("tr");

    const idCell = document.createElement("td");
    idCell.className = "mono-cell";
    idCell.textContent = item.id;

    const noteCell = document.createElement("td");
    noteCell.className = "note-cell";
    noteCell.textContent = item.note || "—";

    const actCell = document.createElement("td");
    actCell.className = "col-act";
    const remove = document.createElement("button");
    remove.className = "btn link danger";
    remove.textContent = "移除";
    remove.addEventListener("click", () => {
      state.settings.whitelist.splice(index, 1);
      renderWhitelist();
      markDirty();
    });
    actCell.appendChild(remove);

    tr.append(idCell, noteCell, actCell);
    body.appendChild(tr);
  });
}

function renderPlaceholders() {
  const wrap = $("placeholder-chips");
  wrap.replaceChildren();
  (state.meta.placeholders || []).forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chip-btn";
    button.textContent = `[${item.key}]`;
    button.title = `${item.desc || ""}${item.example ? `（示例：${item.example}）` : ""}`;
    button.addEventListener("click", () => insertAtCursor($("template"), `[${item.key}]`));
    wrap.appendChild(button);
  });
}

function renderPresets() {
  const wrap = $("preset-chips");
  wrap.replaceChildren();
  const presets = state.meta.presets || [];
  presets.forEach((preset) => {
    if (preset === state.settings.template) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chip-btn preset";
    button.textContent = preset;
    button.addEventListener("click", () => {
      $("template").value = preset;
      markDirty();
      updatePreview();
    });
    wrap.appendChild(button);
  });
}

function renderHistory(items) {
  state.history = Array.isArray(items) ? items : state.history;
  const body = $("history-body");
  body.replaceChildren();
  const list = state.history || [];
  $("history-hint").textContent = list.length
    ? `共 ${state.meta?.history_total ?? list.length} 条记录，最多保留 ${state.settings.history_limit} 条`
    : "只记录白名单用户且真正触发保存的条目。";

  if (!list.length) {
    const tr = document.createElement("tr");
    tr.className = "empty";
    const td = document.createElement("td");
    td.colSpan = 6;
    td.textContent = "暂无归档记录";
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }

  list.forEach((item) => {
    const tr = document.createElement("tr");

    const time = document.createElement("td");
    time.className = "mono-cell";
    time.textContent = (item.time || "").replace(/^\d{4}-/, "");

    const sender = document.createElement("td");
    sender.textContent = item.sender_name || item.sender_id || "—";
    if (item.sender_id) sender.title = `ID: ${item.sender_id}`;

    const platform = document.createElement("td");
    platform.textContent = item.platform || "—";

    const title = document.createElement("td");
    title.className = "path-cell";
    title.textContent = item.title || item.author || "—";
    title.title = [item.title, item.author].filter(Boolean).join("\n");

    const file = document.createElement("td");
    file.className = "path-cell";
    const files = item.files || [];
    if (files.length) {
      const first = files[0].split("/").pop();
      file.textContent = files.length > 1 ? `${first}  +${files.length - 1}` : first;
      file.title = files.join("\n");
    } else {
      file.textContent = item.message || "—";
      file.title = item.message || "";
    }

    const status = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = `status-pill ${item.status || "skipped"}`;
    pill.textContent = STATUS_TEXT[item.status] || item.status || "—";
    if (item.message && item.status !== "ok") pill.title = item.message;
    status.appendChild(pill);

    tr.append(time, sender, platform, title, file, status);
    body.appendChild(tr);
  });
}

function renderAll() {
  const settings = state.settings;
  $("content").classList.remove("hidden");

  renderStatus();
  renderWhitelist();
  renderPlaceholders();
  renderPresets();
  renderHistory(state.meta.history || []);

  $("save-dir").value = settings.save_dir || "";
  $("template").value = settings.template || "";
  $("save-video").checked = !!settings.save_video;
  $("save-album").checked = !!settings.save_album;
  $("overwrite").checked = !!settings.overwrite;
  $("skip-duplicate").checked = !!settings.skip_duplicate;
  $("max-len").value = settings.max_filename_len ?? 100;
  $("history-limit").value = settings.history_limit ?? 200;

  updatePreview();
}

/* ------------------------------ 预览 ------------------------------ */

function insertAtCursor(input, text) {
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? input.value.length;
  input.value = `${input.value.slice(0, start)}${text}${input.value.slice(end)}`;
  input.focus();
  input.selectionStart = input.selectionEnd = start + text.length;
  markDirty();
  schedulePreview();
}

function schedulePreview() {
  clearTimeout(state.previewTimer);
  state.previewTimer = setTimeout(updatePreview, 220);
}

async function updatePreview() {
  const template = $("template").value;
  const warn = $("preview-warn");
  const pathNode = $("preview-path");
  if (!template.trim()) {
    pathNode.textContent = "—";
    warn.classList.add("hidden");
    return;
  }
  try {
    const result = await api("preview", {
      method: "POST",
      body: { template, max_len: Number($("max-len").value) || 100 },
    });
    const root = state.meta?.save_root || result.save_root || "";
    pathNode.textContent = root ? `${root}/${result.path}` : result.path;
    if (result.unknown && result.unknown.length) {
      warn.textContent = `无法识别的占位符：${result.unknown
        .map((item) => `[${item}]`)
        .join("、")}，保存时会被替换为空`;
      warn.classList.remove("hidden");
    } else {
      warn.classList.add("hidden");
    }
  } catch (error) {
    pathNode.textContent = "—";
    warn.textContent = error.message;
    warn.classList.remove("hidden");
  }
}

/* ------------------------------ 事件 ------------------------------ */

function bindEvents() {
  const dirtyInputs = [
    "enabled",
    "save-video",
    "save-album",
    "overwrite",
    "skip-duplicate",
    "max-len",
    "history-limit",
    "save-dir",
  ];
  dirtyInputs.forEach((id) => {
    $(id).addEventListener("input", markDirty);
    $(id).addEventListener("change", markDirty);
  });

  $("template").addEventListener("input", () => {
    markDirty();
    schedulePreview();
  });

  $("max-len").addEventListener("input", schedulePreview);

  $("wl-add").addEventListener("click", addWhitelist);
  ["wl-id", "wl-note"].forEach((id) => {
    $(id).addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        addWhitelist();
      }
    });
  });

  $("wl-import").addEventListener("click", importFromHistory);

  $("test-dir").addEventListener("click", testDirectory);

  $("save").addEventListener("click", saveSettings);

  $("reset").addEventListener("click", async () => {
    const confirmed = await confirmDialog(
      "确定恢复默认设置吗？白名单和命名模板都会被重置。",
    );
    if (!confirmed) return;
    state.settings = JSON.parse(JSON.stringify(state.meta.defaults));
    renderAll();
    markDirty();
    showToast("已恢复默认值，记得点击保存");
  });

  $("history-refresh").addEventListener("click", refreshHistory);

  $("history-clear").addEventListener("click", async () => {
    const confirmed = await confirmDialog(
      "确定清空所有归档记录吗？已保存的文件不会被删除。",
      { danger: true },
    );
    if (!confirmed) return;
    try {
      await api("history/clear", { method: "POST", body: {} });
      state.meta.history = [];
      renderHistory([]);
      showToast("归档记录已清空");
    } catch (error) {
      showToast(error.message, "error");
    }
  });

  $("toggle-advanced").addEventListener("click", (event) => {
    const body = $("advanced-body");
    const hidden = body.classList.toggle("hidden");
    event.currentTarget.textContent = hidden ? "展开" : "收起";
    event.currentTarget.setAttribute("aria-expanded", String(!hidden));
  });

  $("retry-btn").addEventListener("click", () => {
    $("load-error").classList.add("hidden");
    loadAll();
  });

  window.addEventListener("beforeunload", (event) => {
    if (!state.dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

function addWhitelist() {
  const idInput = $("wl-id");
  const noteInput = $("wl-note");
  const id = idInput.value.trim();
  const note = noteInput.value.trim();
  if (!id) {
    showToast("请先填写用户 ID", "error");
    return;
  }
  if (/\s/.test(id)) {
    showToast("用户 ID 不能包含空格", "error");
    return;
  }
  if ((state.settings.whitelist || []).some((item) => item.id.toLowerCase() === id.toLowerCase())) {
    showToast("该用户已在白名单里", "error");
    return;
  }
  state.settings.whitelist = state.settings.whitelist || [];
  state.settings.whitelist.push({ id, note });
  idInput.value = "";
  noteInput.value = "";
  idInput.focus();
  renderWhitelist();
  markDirty();
}

function importFromHistory() {
  const history = state.meta?.history || [];
  const existing = new Set(
    (state.settings.whitelist || []).map((item) => String(item.id).toLowerCase()),
  );
  let added = 0;
  history.forEach((item) => {
    const id = String(item.sender_id || "").trim();
    if (!id || existing.has(id.toLowerCase())) return;
    state.settings.whitelist.push({ id, note: item.sender_name || "最近发送者" });
    existing.add(id.toLowerCase());
    added += 1;
  });
  if (!added) {
    showToast("没有可导入的发送者（可能都已在白名单中）");
    return;
  }
  renderWhitelist();
  markDirty();
  showToast(`已导入 ${added} 位发送者，记得保存`);
}

async function testDirectory() {
  const button = $("test-dir");
  const box = $("dir-result");
  const path = $("save-dir").value.trim();
  button.disabled = true;
  box.classList.add("hidden");
  try {
    const result = await api("test_dir", { method: "POST", body: { path } });
    box.className = "inline-note ok";
    box.textContent = `✓ 目录可写：${result.path}${result.created ? "（已新建）" : ""} · 剩余 ${result.free_text} / 共 ${result.total_text}`;
    box.classList.remove("hidden");
  } catch (error) {
    box.className = "inline-note bad";
    box.textContent = `✗ ${error.message}`;
    box.classList.remove("hidden");
  } finally {
    button.disabled = false;
  }
}

function collectSettings() {
  return {
    enabled: $("enabled").checked,
    save_video: $("save-video").checked,
    save_album: $("save-album").checked,
    overwrite: $("overwrite").checked,
    skip_duplicate: $("skip-duplicate").checked,
    save_dir: $("save-dir").value.trim(),
    template: $("template").value.trim(),
    max_filename_len: Number($("max-len").value) || 100,
    history_limit: Number($("history-limit").value) || 200,
    whitelist: state.settings.whitelist || [],
  };
}

async function saveSettings() {
  if (state.saving) return;
  const payload = collectSettings();
  if (!payload.template) {
    showToast("命名模板不能为空", "error");
    return;
  }
  if (!payload.save_dir) {
    showToast("保存目录不能为空", "error");
    return;
  }
  state.saving = true;
  const button = $("save");
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    const result = await api("settings", { method: "POST", body: { settings: payload } });
    state.settings = result.settings;
    clearDirty();
    if (result.warning) {
      showToast(result.warning, "error");
    } else {
      showToast("设置已保存");
    }
    await loadAll({ quiet: true });
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    state.saving = false;
    button.disabled = false;
    button.textContent = "保存设置";
  }
}

async function refreshHistory() {
  try {
    const result = await api("history", { params: { limit: 50 } });
    state.meta.history = result.items.slice(0, 20);
    state.meta.history_total = result.total;
    state.meta.stats = result.stats;
    renderHistory(result.items);
    renderStatus();
    showToast("记录已刷新");
  } catch (error) {
    showToast(error.message, "error");
  }
}

/* ------------------------------ 启动 ------------------------------ */

async function loadAll({ quiet = false } = {}) {
  try {
    const data = await api("settings");
    state.meta = data;
    state.settings = data.settings;
    state.meta.history_total = data.history_total ?? (data.history || []).length;
    if (!quiet) clearDirty();
    renderAll();
    $("load-error").classList.add("hidden");
  } catch (error) {
    $("content").classList.add("hidden");
    $("load-error").classList.remove("hidden");
    $("load-error-text").textContent = error.message || String(error);
  }
}

async function bootstrap() {
  try {
    const context = await Promise.race([
      bridge.ready(),
      new Promise((resolve) => setTimeout(() => resolve(null), 4000)),
    ]);
    if (context) applyTheme(!!context.isDark);
    if (typeof bridge.onContext === "function") {
      bridge.onContext((next) => applyTheme(!!next?.isDark));
    }
  } catch (error) {
    /* 主题不可用时保持默认 */
  }
  bindEvents();
  await loadAll();
}

bootstrap();
