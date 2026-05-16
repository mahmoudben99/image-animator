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
  openOutputBtn: document.querySelector("#openOutputBtn"),
  checkUpdateBtn: document.querySelector("#checkUpdateBtn"),
  updateModal: document.querySelector("#updateModal"),
  updateTitle: document.querySelector("#updateTitle"),
  updateBody: document.querySelector("#updateBody"),
  updateCloseBtn: document.querySelector("#updateCloseBtn"),
};

boot();

async function boot() {
  bindEvents();
  try {
    state.config = await fetchJson("/api/config");
    els.versionLabel.textContent = `Build ${state.config.build} · v${state.config.version}`;
  } catch (err) {
    els.versionLabel.textContent = "Build offline";
    console.error(err);
  }
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
  els.checkUpdateBtn.addEventListener("click", checkForUpdates);
  els.updateCloseBtn.addEventListener("click", () => els.updateModal.setAttribute("hidden", ""));
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
      body: JSON.stringify({ image_id: item.image_id, motion_id: item.motion_id, duration: 5 }),
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
    <div class="card-row">
      ${renderButton}
    </div>
    ${item.error ? `<p class="card-error">${escapeHtml(item.error)}</p>` : ""}
  `;
}

async function checkForUpdates() {
  els.updateTitle.textContent = "Checking…";
  els.updateBody.textContent = "Contacting GitHub…";
  els.updateModal.removeAttribute("hidden");
  try {
    const data = await fetchJson("/api/check-update");
    if (!data.available) {
      els.updateTitle.textContent = "You're up to date";
      const note = data.latest_version
        ? `Current: v${data.current_version} · Latest: v${data.latest_version}`
        : `Current: v${data.current_version}`;
      els.updateBody.textContent = note + (data.reason ? `\n${data.reason}` : "");
    } else {
      els.updateTitle.textContent = `Update available: v${data.latest_version}`;
      const link = data.asset_url
        ? `<p><a href="${data.asset_url}" target="_blank" rel="noopener">Download v${data.latest_version}</a></p>`
        : "";
      els.updateBody.innerHTML = `
        <p>Current: v${data.current_version}</p>
        ${link}
        <p style="margin-top:8px;color:var(--muted);font-size:12px;">${escapeHtml(data.release_notes || "")}</p>
      `;
    }
  } catch (err) {
    els.updateTitle.textContent = "Could not check updates";
    els.updateBody.textContent = err.message;
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
