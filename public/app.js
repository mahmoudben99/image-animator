// Image Animator — client logic

const state = {
  config: null,
  items: [], // { id, filename, image_id, preview_url, motion_id, status, job_id, output_url, error }
};

const els = {
  versionLabel: document.querySelector("#versionLabel"),
  dropZone: document.querySelector("#dropZone"),
  fileInput: document.querySelector("#fileInput"),
  grid: document.querySelector("#grid"),
  shuffleBtn: document.querySelector("#shuffleBtn"),
  clearBtn: document.querySelector("#clearBtn"),
  renderAllBtn: document.querySelector("#renderAllBtn"),
  queueStatus: document.querySelector("#queueStatus"),
  outputDirLabel: document.querySelector("#outputDirLabel"),
  changeOutputBtn: document.querySelector("#changeOutputBtn"),
  openOutputBtn: document.querySelector("#openOutputBtn"),
  checkUpdateBtn: document.querySelector("#checkUpdateBtn"),
  defaultDurationInput: document.querySelector("#defaultDurationInput"),
  updateModal: document.querySelector("#updateModal"),
  updateTitle: document.querySelector("#updateTitle"),
  updateBody: document.querySelector("#updateBody"),
  updateCloseBtn: document.querySelector("#updateCloseBtn"),
  installUpdateBtn: document.querySelector("#installUpdateBtn"),
};

boot();

async function boot() {
  bindEvents();
  try {
    state.config = await fetchJson("/api/config");
    els.versionLabel.textContent = `Build ${state.config.build} · v${state.config.version}`;
    updateOutputDirLabel(state.config.output_dir);
    els.defaultDurationInput.min = state.config.duration_min;
    els.defaultDurationInput.max = state.config.duration_max;
    els.defaultDurationInput.value = state.config.default_duration;
  } catch (err) {
    els.versionLabel.textContent = "Build offline";
    console.error(err);
  }
}

function updateOutputDirLabel(path) {
  if (!path) return;
  state.config.output_dir = path;
  // Truncate the start of long paths so the tail (folder name) stays visible
  const max = 50;
  const display = path.length > max ? "…" + path.slice(path.length - max + 1) : path;
  els.outputDirLabel.textContent = display;
  els.outputDirLabel.title = "Output folder: " + path + "\nClick to change";
}

function bindEvents() {
  els.dropZone.addEventListener("click", () => els.fileInput.click());
  els.dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    els.dropZone.classList.add("drag-over");
  });
  els.dropZone.addEventListener("dragleave", () => els.dropZone.classList.remove("drag-over"));
  els.dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    els.dropZone.classList.remove("drag-over");
    const files = [...e.dataTransfer.files].filter((f) => f.type.startsWith("image/"));
    if (files.length) addFiles(files);
  });
  els.fileInput.addEventListener("change", () => {
    const files = [...(els.fileInput.files || [])];
    if (files.length) addFiles(files);
    els.fileInput.value = "";
  });

  document.addEventListener("paste", (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files = [];
    for (const item of items) {
      if (item.kind === "file" && item.type.startsWith("image/")) {
        const f = item.getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length) {
      e.preventDefault();
      addFiles(files);
    }
  });

  els.shuffleBtn.addEventListener("click", reRollAll);
  els.clearBtn.addEventListener("click", clearAll);
  els.renderAllBtn.addEventListener("click", renderAll);
  els.openOutputBtn.addEventListener("click", () => fetchJson("/api/reveal-output", { method: "POST" }));
  els.changeOutputBtn.addEventListener("click", pickOutputFolder);
  els.outputDirLabel.addEventListener("click", pickOutputFolder);
  els.checkUpdateBtn.addEventListener("click", checkForUpdates);
  els.defaultDurationInput.addEventListener("change", saveDefaultDuration);
  els.updateCloseBtn.addEventListener("click", () => els.updateModal.setAttribute("hidden", ""));
  els.installUpdateBtn.addEventListener("click", installUpdate);
}

function pickRandomMotion() {
  const ids = state.config?.default_motion_ids || ["pan_right"];
  return ids[Math.floor(Math.random() * ids.length)];
}

function motionLabel(id) {
  const m = (state.config?.motions || []).find((x) => x.id === id);
  return m?.label || id;
}

async function addFiles(files) {
  for (const file of files) {
    const item = {
      id: crypto.randomUUID().slice(0, 8),
      filename: file.name,
      image_id: null,
      preview_url: null,
      motion_id: pickRandomMotion(),
      duration: state.config?.default_duration ?? 7,
      status: "uploading",
      job_id: null,
      output_url: null,
      error: null,
    };
    state.items.push(item);
    render();
    try {
      const fd = new FormData();
      fd.append("file", file);
      const resp = await fetch("/api/upload", { method: "POST", body: fd });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "upload failed");
      item.image_id = data.image_id;
      item.preview_url = data.preview_url;
      item.status = "ready";
    } catch (err) {
      item.status = "error";
      item.error = err.message;
    }
    render();
  }
}

function reRollAll() {
  for (const it of state.items) {
    if (it.status === "ready" || it.status === "done") {
      it.motion_id = pickRandomMotion();
      // Reset done items so re-render uses the new motion
      if (it.status === "done") {
        it.status = "ready";
        it.output_url = null;
        it.job_id = null;
      }
    }
  }
  render();
}

function clearAll() {
  state.items = [];
  render();
}

async function renderAll() {
  const pending = state.items.filter((it) => it.status === "ready");
  for (const item of pending) {
    await startRender(item);
  }
}

async function startRender(item) {
  if (!item.image_id || !item.motion_id) return;
  item.status = "rendering";
  item.error = null;
  render();
  try {
    const resp = await fetch("/api/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        image_id: item.image_id,
        motion_id: item.motion_id,
        duration: clampDuration(item.duration),
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "render failed");
    item.job_id = data.job_id;
    await pollJob(item);
  } catch (err) {
    item.status = "error";
    item.error = err.message;
    render();
  }
}

async function pollJob(item) {
  const started = Date.now();
  while (Date.now() - started < 15 * 60 * 1000) {
    await sleep(1200);
    try {
      const data = await fetchJson(`/api/job/${item.job_id}`);
      if (data.status === "done") {
        item.status = "done";
        item.output_url = data.output_url;
        render();
        return;
      }
      if (data.status === "error") {
        item.status = "error";
        item.error = data.error || "render failed";
        render();
        return;
      }
    } catch (err) {
      console.error(err);
    }
  }
  item.status = "error";
  item.error = "Render timed out";
  render();
}

function render() {
  els.grid.innerHTML = "";
  const pending = state.items.filter((it) => it.status === "ready").length;
  els.renderAllBtn.disabled = pending === 0;
  if (state.items.length === 0) {
    els.queueStatus.textContent = "No images yet";
  } else {
    const done = state.items.filter((it) => it.status === "done").length;
    const rendering = state.items.filter((it) => it.status === "rendering").length;
    els.queueStatus.textContent = `${state.items.length} image${state.items.length > 1 ? "s" : ""} · ${done} done · ${pending} ready · ${rendering} rendering`;
  }

  for (const item of state.items) {
    const card = document.createElement("article");
    card.className = "card";
    card.innerHTML = cardTemplate(item);
    els.grid.appendChild(card);

    const select = card.querySelector("select");
    if (select) {
      select.addEventListener("change", (e) => {
        item.motion_id = e.target.value;
        // If it had a render already, going back to ready so user can re-render with the new motion
        if (item.status === "done") {
          item.status = "ready";
          item.output_url = null;
          item.job_id = null;
          render();
        }
      });
    }

    const renderBtn = card.querySelector("[data-action='render']");
    if (renderBtn) {
      renderBtn.addEventListener("click", () => startRender(item));
    }

    const removeBtn = card.querySelector("[data-action='remove']");
    if (removeBtn) {
      removeBtn.addEventListener("click", () => {
        state.items = state.items.filter((it) => it.id !== item.id);
        render();
      });
    }

    const shuffleBtn = card.querySelector("[data-action='shuffle']");
    if (shuffleBtn) {
      shuffleBtn.addEventListener("click", () => {
        item.motion_id = pickRandomMotion();
        if (item.status === "done") {
          item.status = "ready";
          item.output_url = null;
          item.job_id = null;
        }
        render();
      });
    }

    const durationInput = card.querySelector("[data-field='duration']");
    if (durationInput) {
      durationInput.addEventListener("change", (e) => {
        item.duration = clampDuration(e.target.value);
        e.target.value = item.duration;
        if (item.status === "done") {
          item.status = "ready";
          item.output_url = null;
          item.job_id = null;
          render();
        }
      });
    }
  }
}

function cardTemplate(item) {
  const media = item.output_url
    ? `<video src="${item.output_url}" autoplay loop muted playsinline></video>`
    : item.preview_url
      ? `<img src="${item.preview_url}" alt="${escapeAttr(item.filename)}" />`
      : `<div></div>`;
  const badgeText = item.status === "rendering"
    ? `<span class="spinner"></span>Rendering`
    : item.status === "done"
      ? "Ready"
      : item.status === "error"
        ? "Failed"
        : item.status === "uploading"
          ? "Uploading"
          : "Queued";

  const motionOpts = (state.config?.motions || []).map((m) =>
    `<option value="${m.id}"${m.id === item.motion_id ? " selected" : ""}>${escapeHtml(m.label)}</option>`
  ).join("");

  const renderButton = item.status === "ready" || item.status === "error"
    ? `<button class="primary" data-action="render" type="button">${item.status === "error" ? "Retry" : "Render"}</button>`
    : item.status === "done"
      ? `<button class="secondary" data-action="render" type="button">Re-render</button>`
      : `<button class="secondary" disabled type="button">…</button>`;

  return `
    <div class="card-media">
      ${media}
      <span class="badge ${item.status}">${badgeText}</span>
    </div>
    <div class="card-row">
      <span class="filename" title="${escapeAttr(item.filename)}">${escapeHtml(item.filename)}</span>
      <button class="secondary" data-action="remove" type="button" title="Remove">✕</button>
    </div>
    <div class="card-row">
      <select ${item.status === "rendering" || item.status === "uploading" ? "disabled" : ""}>
        ${motionOpts}
      </select>
      <button class="secondary" data-action="shuffle" type="button" title="Random motion">🎲</button>
    </div>
    <div class="card-row card-duration">
      <label>Duration</label>
      <input data-field="duration" type="number"
        min="${state.config?.duration_min ?? 1}" max="${state.config?.duration_max ?? 30}" step="1"
        value="${item.duration}"
        ${item.status === "rendering" || item.status === "uploading" ? "disabled" : ""} />
      <span>s</span>
    </div>
    <div class="card-row">
      ${renderButton}
    </div>
    ${item.error ? `<p class="card-error">${escapeHtml(item.error)}</p>` : ""}
  `;
}

function clampDuration(value) {
  const min = state.config?.duration_min ?? 1;
  const max = state.config?.duration_max ?? 30;
  const fallback = state.config?.default_duration ?? 7;
  const n = Number.parseInt(value, 10);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}

async function saveDefaultDuration() {
  const next = clampDuration(els.defaultDurationInput.value);
  els.defaultDurationInput.value = next;
  if (state.config) state.config.default_duration = next;
  try {
    await fetchJson("/api/set-default-duration", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seconds: next }),
    });
  } catch (err) {
    console.error("save default duration failed", err);
  }
}

async function pickOutputFolder() {
  els.changeOutputBtn.disabled = true;
  const prevLabel = els.changeOutputBtn.textContent;
  els.changeOutputBtn.textContent = "Picking…";
  try {
    const data = await fetchJson("/api/pick-output-dir", { method: "POST" });
    if (data.output_dir) updateOutputDirLabel(data.output_dir);
  } catch (err) {
    alert("Could not change folder: " + err.message);
  } finally {
    els.changeOutputBtn.disabled = false;
    els.changeOutputBtn.textContent = prevLabel;
  }
}

async function checkForUpdates() {
  els.installUpdateBtn.setAttribute("hidden", "");
  els.updateTitle.textContent = "Checking…";
  els.updateBody.innerHTML = "<p>Contacting GitHub…</p>";
  els.updateModal.removeAttribute("hidden");
  try {
    const data = await fetchJson("/api/check-update");
    if (!data.available) {
      els.updateTitle.textContent = "You're up to date";
      const note = data.latest_version
        ? `Current: v${data.current_version} · Latest: v${data.latest_version}`
        : `Current: v${data.current_version}`;
      els.updateBody.innerHTML = `<p>${escapeHtml(note)}</p>` + (data.reason ? `<p style="color:var(--muted);font-size:12px;">${escapeHtml(data.reason)}</p>` : "");
      return;
    }

    els.updateTitle.textContent = `Update available: v${data.latest_version}`;
    const notesHtml = data.release_notes
      ? `<details style="margin-top:8px;"><summary style="cursor:pointer;color:var(--muted);font-size:12px;">Release notes</summary><pre style="white-space:pre-wrap;color:var(--muted);font-size:12px;margin:6px 0 0;">${escapeHtml(data.release_notes)}</pre></details>`
      : "";

    if (data.patch_available) {
      els.updateBody.innerHTML = `
        <p>Current: v${escapeHtml(data.current_version)} → v${escapeHtml(data.latest_version)}</p>
        <p style="color:var(--muted);font-size:12px;">Small download (~100 KB). The app will restart automatically.</p>
        ${notesHtml}
      `;
      els.installUpdateBtn.textContent = `Install v${data.latest_version}`;
      els.installUpdateBtn.removeAttribute("hidden");
      els.installUpdateBtn.disabled = false;
    } else {
      const link = data.release_url
        ? `<p><a href="${data.release_url}" target="_blank" rel="noopener">Open release page</a></p>`
        : "";
      els.updateBody.innerHTML = `
        <p>Current: v${escapeHtml(data.current_version)} → v${escapeHtml(data.latest_version)}</p>
        <p style="color:var(--muted);font-size:12px;">This update requires a manual full re-download — likely a major change.</p>
        ${link}
        ${notesHtml}
      `;
    }
  } catch (err) {
    els.updateTitle.textContent = "Could not check updates";
    els.updateBody.innerHTML = `<p>${escapeHtml(err.message)}</p>`;
  }
}

async function installUpdate() {
  els.installUpdateBtn.disabled = true;
  els.updateTitle.textContent = "Installing…";
  els.updateBody.innerHTML = `<p><span class="spinner"></span>&nbsp;Downloading update…</p>`;
  try {
    const data = await fetchJson("/api/perform-update", { method: "POST" });
    els.updateTitle.textContent = `Updating to v${data.new_version}`;
    els.updateBody.innerHTML = `<p><span class="spinner"></span>&nbsp;Restarting the app…</p><p style="color:var(--muted);font-size:12px;">This window will close in a few seconds. The new version will open automatically.</p>`;
  } catch (err) {
    els.updateTitle.textContent = "Update failed";
    els.updateBody.innerHTML = `<p>${escapeHtml(err.message)}</p>`;
    els.installUpdateBtn.disabled = false;
  }
}

async function fetchJson(url, options = {}) {
  const resp = await fetch(url, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `Request failed: ${resp.status}`);
  return data;
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function escapeHtml(s) {
  return String(s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/`/g, "&#096;");
}
