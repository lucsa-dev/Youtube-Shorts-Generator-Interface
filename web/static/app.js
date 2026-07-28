(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const state = {
    activeJobId: null,
    pollTimer: null,
    lastLogCount: 0,
    selectedIds: new Set(),
    highlights: [],
  };

  /* ---------- Router ---------- */
  function parseRoute(pathname) {
    const path = (pathname || "/").replace(/\/+$/, "") || "/";
    if (path === "/") return { tab: "generate", jobId: null };
    if (path === "/config") return { tab: "config", jobId: null };
    if (path === "/jobs") return { tab: "jobs", jobId: null };
    const jobMatch = path.match(/^\/jobs\/([^/]+)$/);
    if (jobMatch) return { tab: "generate", jobId: jobMatch[1] };
    return { tab: "generate", jobId: null };
  }

  function pathFor(tab, jobId = null) {
    if (jobId) return `/jobs/${jobId}`;
    if (tab === "jobs") return "/jobs";
    if (tab === "config") return "/config";
    return "/";
  }

  function showTab(name) {
    $$(".tab").forEach((t) => {
      const on = t.dataset.tab === name;
      t.classList.toggle("is-active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    $$(".panel").forEach((p) => {
      const on = p.id === `panel-${name}`;
      p.classList.toggle("is-active", on);
      p.hidden = !on;
    });
  }

  function navigate(path, { replace = false } = {}) {
    const next = path || "/";
    if (location.pathname !== next) {
      history[replace ? "replaceState" : "pushState"]({ path: next }, "", next);
    }
    applyRoute(parseRoute(next));
  }

  function applyRoute(route) {
    if (route.jobId) {
      showTab("generate");
      if (state.activeJobId !== route.jobId) {
        watchJob(route.jobId, { syncUrl: false });
      } else {
        $("#run-area").hidden = false;
      }
      return;
    }
    showTab(route.tab);
    if (route.tab === "generate") {
      setFlowStep(1);
      $("#generate-form")?.classList.remove("is-locked");
    }
    if (route.tab === "jobs") loadJobs();
    if (route.tab === "config") loadConfig();
  }

  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", (e) => {
      e.preventDefault();
      navigate(pathFor(tab.dataset.tab));
    });
  });

  window.addEventListener("popstate", () => {
    applyRoute(parseRoute(location.pathname));
  });

  function setFlowStep(step) {
    $$(".step-dot").forEach((dot) => {
      const n = Number(dot.dataset.step);
      dot.classList.toggle("is-active", n === step);
      dot.classList.toggle("is-done", n < step);
    });
    const labels = {
      1: "1 · Configurar fonte",
      2: "2 · Escolher tópicos",
      3: "3 · Cortar shorts",
    };
    const el = $("#step-label");
    if (el) el.textContent = labels[step] || labels[1];
    const form = $("#generate-form");
    if (form) form.classList.toggle("is-locked", step > 1);
  }

  /* ---------- Mode / upload ---------- */
  const modeEl = $("#mode");
  const uploadWrap = $("#upload-wrap");
  const fileInput = $("#file");
  const fileHint = $("#file-hint");
  const MODE_LABELS = {
    api: "API (MuAPI)",
    local: "Local (yt-dlp + Whisper)",
  };

  function syncModeOptions(status = {}) {
    const modes = Array.isArray(status.modes) && status.modes.length
      ? status.modes
      : status.muapi
        ? ["api", "local"]
        : ["local"];
    const preferred = status.default_mode || modes[0];
    const previous = modeEl.value;
    const modeField = $("#mode-field") || modeEl.closest(".field");
    const fieldRow = modeField?.closest(".field-row");
    const uploadNote = $("#upload-mode-note");
    modeEl.innerHTML = "";
    for (const value of modes) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = MODE_LABELS[value] || value;
      modeEl.appendChild(opt);
    }
    if (modes.includes(previous)) {
      modeEl.value = previous;
    } else if (modes.includes(preferred)) {
      modeEl.value = preferred;
    } else {
      modeEl.value = modes[0];
    }
    const showMode = modes.length >= 2;
    if (modeField) modeField.hidden = !showMode;
    if (fieldRow) fieldRow.classList.toggle("has-mode", showMode);
    if (uploadNote) uploadNote.hidden = !showMode;
    syncUploadState();
  }

  function syncUploadState() {
    const local = modeEl.value === "local";
    uploadWrap.classList.toggle("is-disabled", !local);
    fileInput.disabled = !local;
  }
  modeEl.addEventListener("change", syncUploadState);
  syncUploadState();

  /* ---------- Recent sources ---------- */
  async function loadSources() {
    const block = $("#recent-block");
    const list = $("#recent-sources");
    try {
      const res = await fetch("/api/sources");
      const data = await res.json();
      const sources = data.sources || [];
      if (!sources.length) {
        block.hidden = true;
        list.innerHTML = "";
        return;
      }
      block.hidden = false;
      list.innerHTML = "";
      for (const s of sources) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "recent-chip";
        btn.title = s.title || s.url || "";
        const thumb = s.thumbnail
          ? `<img class="recent-chip-thumb" src="${escapeAttr(s.thumbnail)}" alt="" loading="lazy" referrerpolicy="no-referrer" />`
          : `<span class="recent-chip-thumb is-placeholder">mp4</span>`;
        const bits = [];
        if (s.size_label) bits.push(s.size_label);
        if (s.has_transcript_cache) bits.push("srt");
        const titleLabel = truncate(s.title || s.url || s.id || "fonte", 42);
        btn.innerHTML = `
          ${thumb}
          <span class="recent-chip-text">
            <span class="recent-chip-id">${escapeHtml(titleLabel)}</span>
            <span class="recent-chip-meta">${escapeHtml(bits.join(" · ") || (s.mode || "local"))}</span>
          </span>
        `;
        btn.addEventListener("click", () => selectSource(s, btn));
        list.appendChild(btn);
      }
    } catch {
      block.hidden = true;
    }
  }

  function selectSource(source, cardEl) {
    $$(".recent-chip").forEach((c) => c.classList.remove("is-selected"));
    cardEl.classList.add("is-selected");
    $("#url").value = source.url;
    if (source.kind === "cache" || source.kind === "upload") {
      if ([...modeEl.options].some((o) => o.value === "local")) {
        modeEl.value = "local";
      }
    } else if (source.mode && [...modeEl.options].some((o) => o.value === source.mode)) {
      modeEl.value = source.mode;
    }
    syncUploadState();
    fileInput.value = "";
    fileHint.textContent = "Arraste um mp4 ou clique para escolher";
    $("#form-hint").textContent = source.has_transcript_cache
      ? "fonte em cache — download/transcrição serão reaproveitados"
      : "fonte selecionada — pronta para gerar";
    $("#url").focus();
  }

  $("#refresh-sources").addEventListener("click", loadSources);

  fileInput.addEventListener("change", () => {
    fileHint.textContent = fileInput.files?.[0]?.name || "Arraste um mp4 ou clique para escolher";
  });

  ["dragenter", "dragover"].forEach((ev) => {
    uploadWrap.addEventListener(ev, (e) => {
      e.preventDefault();
      if (!fileInput.disabled) uploadWrap.classList.add("is-drag");
    });
  });
  ["dragleave", "drop"].forEach((ev) => {
    uploadWrap.addEventListener(ev, (e) => {
      e.preventDefault();
      uploadWrap.classList.remove("is-drag");
    });
  });
  uploadWrap.addEventListener("drop", (e) => {
    if (fileInput.disabled) return;
    const f = e.dataTransfer?.files?.[0];
    if (!f) return;
    const dt = new DataTransfer();
    dt.items.add(f);
    fileInput.files = dt.files;
    fileHint.textContent = f.name;
  });

  /* ---------- Health / config status ---------- */
  async function refreshHealth() {
    const pill = $("#api-status");
    try {
      const res = await fetch("/api/health");
      const data = await res.json();
      const s = data.config || {};
      syncModeOptions(s);
      const parts = [];
      if (s.muapi) parts.push("MuAPI");
      if (s.openai) parts.push("OpenAI");
      if (s.gemini) parts.push("Gemini");
      if (parts.length) {
        pill.className = "status-pill is-ok";
        $(".status-text", pill).textContent = parts.join(" · ");
      } else {
        pill.className = "status-pill is-warn";
        $(".status-text", pill).textContent = "sem chaves — veja Config";
      }
    } catch {
      syncModeOptions({ modes: ["local"], default_mode: "local" });
      pill.className = "status-pill is-warn";
      $(".status-text", pill).textContent = "API offline";
    }
  }

  /* ---------- Config ---------- */
  async function loadConfig() {
    const form = $("#config-form");
    form.innerHTML = "";
    const res = await fetch("/api/config");
    const data = await res.json();
    const langOpts = data.language_options || [];
    for (const item of data.items) {
      const wrap = document.createElement("label");
      wrap.className = "field config-item";
      if (item.input_type === "language") {
        const options = langOpts
          .map(
            (o) =>
              `<option value="${escapeAttr(o.value)}" ${
                o.value === item.value ? "selected" : ""
              }>${escapeHtml(o.label)}</option>`
          )
          .join("");
        wrap.innerHTML = `
          <span class="label">${item.key} <em>— padrão para Whisper, títulos e hooks</em></span>
          <select name="${item.key}">${options}</select>
        `;
      } else {
        const note = item.is_secret
          ? `<span class="secret-note">${item.is_set ? `definida: ${item.masked}` : "não definida"} — deixe vazio para manter</span>`
          : "";
        wrap.innerHTML = `
          <span class="label">${item.key}</span>
          <input
            type="${item.is_secret ? "password" : "text"}"
            name="${item.key}"
            placeholder="${item.is_secret ? "" : item.value || ""}"
            value="${item.is_secret ? "" : escapeAttr(item.value || "")}"
            autocomplete="off"
          />
          ${note}
        `;
      }
      form.appendChild(wrap);
    }
  }

  $("#config-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const hint = $("#config-hint");
    const fd = new FormData(e.target);
    const values = {};
    for (const [k, v] of fd.entries()) values[k] = String(v);
    hint.textContent = "salvando…";
    try {
      const res = await fetch("/api/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values }),
      });
      if (!res.ok) throw new Error(await res.text());
      hint.textContent = "configuração salva — idioma padrão atualizado";
      await loadConfig();
      await refreshHealth();
    } catch (err) {
      hint.textContent = `erro: ${err.message}`;
    }
  });

  /* ---------- Generate ---------- */
  $("#generate-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("#submit-btn");
    const hint = $("#form-hint");
    btn.disabled = true;
    hint.textContent = "enviando job…";

    const fd = new FormData(e.target);
    if (!fileInput.files?.length) fd.delete("file");
    // Idioma vem de CONTENT_LANGUAGE na Config (padrão das gerações)

    try {
      const res = await fetch("/api/jobs", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      hint.textContent = `job ${data.id} na fila`;
      setFlowStep(1);
      watchJob(data.id, { syncUrl: true });
    } catch (err) {
      hint.textContent = `erro: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  });

  function watchJob(jobId, { syncUrl = true } = {}) {
    state.activeJobId = jobId;
    state.lastLogCount = 0;
    state.selectedIds = new Set();
    state.highlights = [];
    $("#run-area").hidden = false;
    $("#pick-area").hidden = true;
    $("#topic-list").innerHTML = "";
    $("#active-job-title").textContent = jobId;
    $("#job-log").textContent = "";
    $("#results").innerHTML = "";
    setBadge("queued");
    setFlowStep(1);
    if (syncUrl) {
      const path = pathFor("generate", jobId);
      if (location.pathname !== path) {
        history.pushState({ path }, "", path);
      }
    }
    if (state.pollTimer) clearInterval(state.pollTimer);
    pollJob();
    state.pollTimer = setInterval(pollJob, 2000);
  }

  async function pollJob() {
    if (!state.activeJobId) return;
    try {
      const res = await fetch(`/api/jobs/${state.activeJobId}`);
      if (!res.ok) return;
      const job = await res.json();
      setBadge(job.status);
      const logs = job.logs || [];
      if (logs.length > state.lastLogCount) {
        const logEl = $("#job-log");
        const newLines = logs.slice(state.lastLogCount);
        for (const line of newLines) {
          logEl.textContent += (logEl.textContent ? "\n" : "") + line.message;
        }
        state.lastLogCount = logs.length;
        logEl.scrollTop = logEl.scrollHeight;
      }

      if (job.status === "analyzing" || job.status === "queued") {
        setFlowStep(1);
      } else if (job.status === "awaiting_selection") {
        setFlowStep(2);
        renderTopicPicker(job);
        if (state.pollTimer) {
          clearInterval(state.pollTimer);
          state.pollTimer = null;
        }
      } else if (job.status === "rendering") {
        setFlowStep(3);
        $("#pick-area").hidden = true;
      } else if (job.status === "completed" && job.result) {
        setFlowStep(3);
        $("#pick-area").hidden = true;
        renderResults(job);
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        loadJobs();
        loadSources();
      } else if (job.status === "failed") {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        const logEl = $("#job-log");
        logEl.textContent += `\n\nFALHOU: ${job.error || "erro desconhecido"}`;
      }
    } catch {
      /* ignore transient poll errors */
    }
  }

  function setBadge(status) {
    const el = $("#active-job-badge");
    el.textContent = status;
    el.className = `badge is-${status}`;
  }

  function renderTopicPicker(job) {
    const highlights = job.result?.highlights || [];
    state.highlights = highlights;
    const list = $("#topic-list");
    const pick = $("#pick-area");
    pick.hidden = false;

    if (!highlights.length) {
      list.innerHTML = `<p class="empty">Nenhum tópico encontrado.</p>`;
      $("#pick-continue").disabled = true;
      $("#pick-hint").textContent = "Nada para selecionar";
      return;
    }

    // Default: select all
    if (state.selectedIds.size === 0) {
      highlights.forEach((h, i) => state.selectedIds.add(Number(h.id ?? i)));
    }

    list.innerHTML = "";
    highlights.forEach((h, i) => {
      const id = Number(h.id ?? i);
      const selected = state.selectedIds.has(id);
      const card = document.createElement("button");
      card.type = "button";
      card.className = `topic-card${selected ? " is-selected" : ""}`;
      card.dataset.id = String(id);
      card.style.animationDelay = `${i * 0.04}s`;
      const thumb = h.thumbnail_url
        ? `<img class="topic-thumb" src="${escapeAttr(h.thumbnail_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" />`
        : `<span class="topic-thumb is-placeholder">frame</span>`;
      card.innerHTML = `
        <input class="topic-check" type="checkbox" ${selected ? "checked" : ""} tabindex="-1" aria-hidden="true" />
        ${thumb}
        <div class="topic-body">
          <div class="score"><strong>${h.score ?? "—"}</strong> / 100</div>
          <h3>${escapeHtml(h.title || `Tópico #${i + 1}`)}</h3>
          <p class="meta-row"><strong>Tempo:</strong> ${fmtTime(h.start_time)} → ${fmtTime(h.end_time)}</p>
          <p class="meta-row"><strong>Hook:</strong> ${escapeHtml(h.hook_sentence || "—")}</p>
          <p class="topic-snippet">${escapeHtml(h.snippet || h.virality_reason || "")}</p>
        </div>
      `;
      card.addEventListener("click", () => toggleTopic(id, card));
      list.appendChild(card);
    });

    syncPickContinue();
    $("#pick-hint").textContent = `${state.selectedIds.size} de ${highlights.length} selecionados · ordenados por tempo`;
  }

  function toggleTopic(id, card) {
    if (state.selectedIds.has(id)) {
      state.selectedIds.delete(id);
      card.classList.remove("is-selected");
      const cb = $(".topic-check", card);
      if (cb) cb.checked = false;
    } else {
      state.selectedIds.add(id);
      card.classList.add("is-selected");
      const cb = $(".topic-check", card);
      if (cb) cb.checked = true;
    }
    syncPickContinue();
  }

  function syncPickContinue() {
    const n = state.selectedIds.size;
    const total = state.highlights.length;
    $("#pick-continue").disabled = n === 0;
    $("#pick-hint").textContent = `${n} de ${total} selecionados · ordenados por tempo`;
  }

  $("#pick-all").addEventListener("click", () => {
    state.highlights.forEach((h, i) => state.selectedIds.add(Number(h.id ?? i)));
    $$(".topic-card").forEach((card) => {
      card.classList.add("is-selected");
      const cb = $(".topic-check", card);
      if (cb) cb.checked = true;
    });
    syncPickContinue();
  });

  $("#pick-none").addEventListener("click", () => {
    state.selectedIds.clear();
    $$(".topic-card").forEach((card) => {
      card.classList.remove("is-selected");
      const cb = $(".topic-check", card);
      if (cb) cb.checked = false;
    });
    syncPickContinue();
  });

  $("#pick-continue").addEventListener("click", async () => {
    if (!state.activeJobId || state.selectedIds.size === 0) return;
    const btn = $("#pick-continue");
    btn.disabled = true;
    try {
      const res = await fetch(`/api/jobs/${state.activeJobId}/select`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: [...state.selectedIds] }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      setFlowStep(3);
      $("#pick-area").hidden = true;
      setBadge("rendering");
      if (state.pollTimer) clearInterval(state.pollTimer);
      pollJob();
      state.pollTimer = setInterval(pollJob, 2000);
    } catch (err) {
      $("#pick-hint").textContent = `erro: ${err.message}`;
      btn.disabled = false;
    }
  });

  function renderResults(job) {
    const box = $("#results");
    box.innerHTML = "";
    const shorts = job.result?.shorts || [];
    if (!shorts.length) {
      box.innerHTML = `<p class="empty">Nenhum short gerado.</p>`;
      return;
    }

    const head = document.createElement("div");
    head.className = "section-head";
    head.innerHTML = `
      <h2 style="font-size:1.2rem">
        ${shorts.length} shorts · ${job.result.highlights?.length || 0} tópicos analisados
      </h2>
      <a class="btn ghost" href="/api/jobs/${job.id}/result.json" download>Baixar JSON</a>
    `;
    box.appendChild(head);

    shorts.forEach((s, i) => {
      const card = document.createElement("article");
      card.className = "short-card";
      card.style.animationDelay = `${i * 0.06}s`;
      const clip = s.clip_url || "";
      const videoSrc = clip;
      card.innerHTML = `
        <div>
          ${
            clip
              ? `<video controls playsinline src="${escapeAttr(videoSrc)}"></video>`
              : `<p class="empty">Clip indisponível${s.error ? `: ${escapeHtml(s.error)}` : ""}</p>`
          }
        </div>
        <div class="short-meta">
          <div class="score"><strong>${s.score ?? "—"}</strong> / 100</div>
          <h3>${escapeHtml(s.title || `Short #${i + 1}`)}</h3>
          <p class="meta-row"><strong>Tempo:</strong> ${fmtTime(s.start_time)} → ${fmtTime(s.end_time)}</p>
          <p class="meta-row"><strong>Hook:</strong> ${escapeHtml(s.hook_sentence || "—")}</p>
          <p class="meta-row"><strong>Por quê:</strong> ${escapeHtml(s.virality_reason || "—")}</p>
          ${
            clip
              ? `<p class="meta-row"><a href="${escapeAttr(clip)}" download="short_${i + 1}.mp4">Baixar clip</a></p>`
              : ""
          }
        </div>
      `;
      box.appendChild(card);
    });
  }

  /* ---------- Jobs list ---------- */
  $("#refresh-jobs").addEventListener("click", loadJobs);

  async function loadJobs() {
    const list = $("#jobs-list");
    try {
      const res = await fetch("/api/jobs");
      const jobs = await res.json();
      if (!jobs.length) {
        list.innerHTML = `<p class="empty">Nenhum job ainda.</p>`;
        return;
      }
      list.innerHTML = "";
      for (const j of jobs) {
        const row = document.createElement("div");
        row.className = "job-row";
        row.innerHTML = `
          <div>
            <div class="id">${escapeHtml(j.id)} · ${escapeHtml(j.params?.mode || "")}</div>
            <div class="url">${escapeHtml(truncate(j.params?.url || "", 80))}</div>
          </div>
          <span class="badge is-${j.status}">${j.status}</span>
          <span class="hint">${j.highlights_count || j.shorts_count || 0} tópicos · ${j.shorts_count || 0} clips</span>
        `;
        row.addEventListener("click", () => {
          navigate(`/jobs/${j.id}`);
        });
        list.appendChild(row);
      }
    } catch (err) {
      list.innerHTML = `<p class="empty">Erro ao carregar: ${escapeHtml(err.message)}</p>`;
    }
  }

  /* ---------- helpers ---------- */
  function fmtTime(t) {
    if (t == null || Number.isNaN(Number(t))) return "—";
    const n = Number(t);
    const m = Math.floor(n / 60);
    const s = (n % 60).toFixed(1);
    return `${m}:${s.padStart(4, "0")}`;
  }
  function truncate(s, n) {
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function escapeAttr(s) {
    return escapeHtml(s).replace(/'/g, "&#39;");
  }

  refreshHealth();
  loadSources();
  setFlowStep(1);
  applyRoute(parseRoute(location.pathname));
})();
