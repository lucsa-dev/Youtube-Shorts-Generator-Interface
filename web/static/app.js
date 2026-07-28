(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const state = {
    activeJobId: null,
    pollTimer: null,
    lastLogCount: 0,
    selectedIds: new Set(),
    highlights: [],
    viewStep: 1,
    maxStep: 1,
    jobStatus: null,
    jobParams: null,
    lastJob: null,
    renderedIds: new Set(),
    followJobStep: true,
    captionThemes: [],
    captionStyle: {
      theme: "bold-white",
      enabled: true,
      font_name: "Arial Black",
      font_size: 72,
      primary_colour: "&H0000FFFF",
      secondary_colour: "&H00FFFFFF",
      outline_colour: "&H00000000",
      bold: true,
      outline: 4,
      shadow: 0,
      margin_v: 160,
      max_words_per_line: 4,
    },
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
        showFlowView(state.viewStep);
      }
      return;
    }
    showTab(route.tab);
    if (route.tab === "generate") {
      resetGeneratePanel();
    }
    if (route.tab === "jobs") loadJobs();
    if (route.tab === "config") loadConfig();
  }

  function resetGeneratePanel() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    state.activeJobId = null;
    state.lastJob = null;
    state.jobStatus = null;
    state.jobParams = null;
    state.selectedIds = new Set();
    state.highlights = [];
    state.renderedIds = new Set();
    state.maxStep = 1;
    state.viewStep = 1;
    state.followJobStep = true;
    $("#run-area").hidden = true;
    $("#pick-area").hidden = true;
    $("#results").innerHTML = "";
    $("#job-log").textContent = "";
    setFlowStep(1);
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

  function statusToStep(status) {
    if (status === "awaiting_selection") return 2;
    if (status === "rendering" || status === "completed") return 4;
    return 1;
  }

  function setFlowStep(step, { maxStep } = {}) {
    if (maxStep != null) state.maxStep = Math.max(state.maxStep, maxStep);
    state.viewStep = step;
    state.maxStep = Math.max(state.maxStep, step);

    $$(".step-dot").forEach((dot) => {
      const n = Number(dot.dataset.step);
      const reachable = n <= state.maxStep;
      dot.classList.toggle("is-active", n === step);
      dot.classList.toggle("is-done", n < step || (n <= state.maxStep && n !== step));
      dot.disabled = !reachable;
      if (n === step) dot.setAttribute("aria-current", "step");
      else dot.removeAttribute("aria-current");
    });
    const labels = {
      1: "1 · Configurar fonte",
      2: "2 · Escolher tópicos",
      3: "3 · Legendas karaoke",
      4: "4 · Cortar shorts",
    };
    const el = $("#step-label");
    if (el) el.textContent = labels[step] || labels[1];
    showFlowView(step);
  }

  function showFlowView(step) {
    const form = $("#generate-form");
    const hasJob = Boolean(state.activeJobId);
    const hasTopics = state.highlights.length > 0;
    const selectableStatus = ["awaiting_selection", "completed", "rendering", "failed"].includes(
      state.jobStatus
    );
    const editable =
      hasJob &&
      step === 1 &&
      (["awaiting_selection", "completed"].includes(state.jobStatus) ||
        (state.jobStatus === "failed" && hasTopics));

    const step1Fields = $("#step1-fields");
    if (step1Fields) step1Fields.hidden = step !== 1;

    // Lock only on step 1 while the job is running (fields are hidden on steps 2–4).
    form?.classList.toggle("is-locked", hasJob && step === 1 && !editable);
    form?.classList.toggle("is-editing-job", editable);

    const newActions = $("#new-job-actions");
    const editActions = $("#edit-job-actions");
    if (newActions) newActions.hidden = editable;
    if (editActions) editActions.hidden = !editable;

    const pick = $("#pick-area");
    const captions = $("#caption-area");
    const results = $("#results");
    const run = $("#run-area");

    if (!hasJob) {
      if (run) run.hidden = true;
      return;
    }
    if (run) run.hidden = false;

    const canPick = hasTopics && selectableStatus;

    if (pick) {
      pick.hidden = !(step === 2 && canPick);
    }
    if (captions) {
      captions.hidden = !(step === 3 && canPick);
      if (step === 3 && canPick) syncCaptionForm();
    }
    if (results) {
      results.hidden = step !== 4;
      if (step === 4 && state.lastJob?.result) {
        renderResults(state.lastJob);
      }
    }

    syncPickContinueLabel();
  }

  $$(".step-dot").forEach((dot) => {
    dot.addEventListener("click", () => {
      const n = Number(dot.dataset.step);
      if (dot.disabled || n > state.maxStep) return;
      state.followJobStep = false;
      setFlowStep(n);
      if (n === 2 && state.lastJob) renderTopicPicker(state.lastJob);
      if (n === 3) syncCaptionForm();
      if (n === 4 && state.lastJob?.result) renderResults(state.lastJob);
    });
  });

  function fillFormFromJob(job) {
    const params = job?.params || {};
    state.jobParams = params;
    if (params.url) $("#url").value = params.url;
    if (params.mode && [...modeEl.options].some((o) => o.value === params.mode)) {
      modeEl.value = params.mode;
      syncUploadState();
    }
    if (params.aspect_ratio) $("#aspect_ratio").value = params.aspect_ratio;
    if (params.download_format) $("#download_format").value = params.download_format;
    if (params.caption_style) {
      state.captionStyle = { ...state.captionStyle, ...params.caption_style };
    }
  }

  function syncPickContinueLabel() {
    const label = $("#pick-continue-label");
    if (!label) return;
    label.textContent = "Continuar para legendas";
    const capLabel = $("#caption-continue-label");
    if (capLabel) {
      const hasRendered = state.renderedIds.size > 0 || state.jobStatus === "completed";
      capLabel.textContent = hasRendered ? "Atualizar shorts" : "Gerar shorts";
    }
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

  /* ---------- Health / mode options ---------- */
  async function refreshHealth() {
    try {
      const res = await fetch("/api/health");
      const data = await res.json();
      syncModeOptions(data.config || {});
    } catch {
      syncModeOptions({ modes: ["local"], default_mode: "local" });
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
        const labelExtra =
          item.key === "LOCAL_FACE_SMOOTHING"
            ? ` <em>— suavização do crop de rosto (0–1; padrão 0.15; só modo local)</em>`
            : item.key === "LOCAL_OUTPUT_DIR"
              ? ` <em>— pasta onde os shorts (mp4) são salvos</em>`
              : "";
        const resolved =
          item.key === "LOCAL_OUTPUT_DIR" && item.resolved_path
            ? `<span class="secret-note">caminho absoluto: <code>${escapeHtml(
                item.resolved_path
              )}</code></span>`
            : "";
        wrap.innerHTML = `
          <span class="label">${item.key}${labelExtra}</span>
          <input
            type="text"
            name="${item.key}"
            placeholder="${escapeAttr(item.value || "")}"
            value="${escapeAttr(item.value || "")}"
            autocomplete="off"
          />
          ${resolved}
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
      hint.textContent = "configuração salva";
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
    state.renderedIds = new Set();
    state.lastJob = null;
    state.jobStatus = "queued";
    state.maxStep = 1;
    $("#run-area").hidden = false;
    $("#pick-area").hidden = true;
    $("#topic-list").innerHTML = "";
    $("#active-job-title").textContent = jobId;
    $("#job-log").textContent = "";
    $("#results").innerHTML = "";
    setBadge("queued");
    state.followJobStep = true;
    setFlowStep(1, { maxStep: 1 });
    if (syncUrl) {
      const path = pathFor("generate", jobId);
      if (location.pathname !== path) {
        history.pushState({ path }, "", path);
      }
    }
    if (state.pollTimer) clearInterval(state.pollTimer);
    pollJob();
    state.pollTimer = setInterval(pollJob, 1500);
  }

  async function pollJob() {
    if (!state.activeJobId) return;
    try {
      const res = await fetch(`/api/jobs/${state.activeJobId}`);
      if (!res.ok) return;
      const job = await res.json();
      state.lastJob = job;
      state.jobStatus = job.status;
      setBadge(job.status);
      fillFormFromJob(job);

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

      const reached = statusToStep(job.status);
      state.maxStep = Math.max(state.maxStep, reached);

      if (job.status === "analyzing" || job.status === "queued") {
        if (state.followJobStep) setFlowStep(1);
        else showFlowView(state.viewStep);
      } else if (job.status === "awaiting_selection") {
        prepareSelection(job);
        // Allow navigating to caption step if they already picked topics before
        if (state.selectedIds.size > 0) {
          state.maxStep = Math.max(state.maxStep, 3);
        }
        if (state.followJobStep) setFlowStep(2);
        else showFlowView(state.viewStep);
        if (state.viewStep === 2) renderTopicPicker(job);
        if (state.viewStep === 3) syncCaptionForm();
        if (state.pollTimer) {
          clearInterval(state.pollTimer);
          state.pollTimer = null;
        }
      } else if (job.status === "rendering") {
        const shorts = job.result?.shorts || [];
        state.renderedIds = new Set(
          shorts
            .filter((s) => s.clip_url && !s.error)
            .map((s, i) => Number(s.id ?? i))
            .filter((n) => !Number.isNaN(n))
        );
        if (job.params?.selected_ids?.length) {
          state.selectedIds = new Set(job.params.selected_ids.map(Number));
        } else if (job.result?.selected_ids?.length) {
          state.selectedIds = new Set(job.result.selected_ids.map(Number));
        }
        state.highlights = job.result?.highlights || state.highlights;
        state.maxStep = Math.max(state.maxStep, 4);
        if (state.followJobStep) setFlowStep(4);
        else showFlowView(state.viewStep);
        if (state.viewStep === 4) renderResults(job);
      } else if (job.status === "completed" && job.result) {
        const shorts = job.result.shorts || [];
        state.renderedIds = new Set(
          shorts.map((s, i) => Number(s.id ?? i)).filter((n) => !Number.isNaN(n))
        );
        prepareSelection(job);
        if (state.pollTimer) {
          clearInterval(state.pollTimer);
          state.pollTimer = null;
        }
        if (state.followJobStep) {
          setFlowStep(4);
          renderResults(job);
        } else {
          state.maxStep = Math.max(state.maxStep, 4);
          showFlowView(state.viewStep);
          if (state.viewStep === 2) renderTopicPicker(job);
          if (state.viewStep === 3) syncCaptionForm();
          if (state.viewStep === 4) renderResults(job);
        }
        const capBtn = $("#caption-continue");
        if (capBtn) capBtn.disabled = false;
        loadJobs();
        loadSources();
      } else if (job.status === "failed") {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        const capBtn = $("#caption-continue");
        if (capBtn) capBtn.disabled = false;
        const logEl = $("#job-log");
        logEl.textContent += `\n\nFALHOU: ${job.error || "erro desconhecido"}`;
        const highlights = job.result?.highlights || [];
        if (highlights.length) {
          // Análise sobreviveu — permite retentar a seleção/corte
          prepareSelection(job);
          state.maxStep = Math.max(state.maxStep, 3);
          if (state.followJobStep) setFlowStep(2);
          else showFlowView(state.viewStep);
          if (state.viewStep === 2) renderTopicPicker(job);
          if (state.viewStep === 3) syncCaptionForm();
        } else {
          showFlowView(state.viewStep);
        }
      }
    } catch {
      /* ignore transient poll errors */
    }
  }

  function prepareSelection(job) {
    const highlights = job.result?.highlights || [];
    state.highlights = highlights;
    if (state.selectedIds.size === 0) {
      const selectedFromJob = job.params?.selected_ids || job.result?.selected_ids;
      if (Array.isArray(selectedFromJob) && selectedFromJob.length) {
        state.selectedIds = new Set(selectedFromJob.map(Number));
      } else if (state.renderedIds.size) {
        state.selectedIds = new Set(state.renderedIds);
      } else {
        highlights.forEach((h, i) => state.selectedIds.add(Number(h.id ?? i)));
      }
    }
    if (state.viewStep === 2) renderTopicPicker(job);
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
    if (state.viewStep === 2) pick.hidden = false;

    if (!highlights.length) {
      list.innerHTML = `<p class="empty">Nenhum tópico encontrado.</p>`;
      $("#pick-continue").disabled = true;
      $("#pick-hint").textContent = "Nada para selecionar";
      return;
    }

    if (state.selectedIds.size === 0) {
      const fromJob = job.params?.selected_ids || job.result?.selected_ids;
      if (Array.isArray(fromJob) && fromJob.length) {
        fromJob.forEach((id) => state.selectedIds.add(Number(id)));
      } else if (state.renderedIds.size) {
        state.renderedIds.forEach((id) => state.selectedIds.add(id));
      } else {
        highlights.forEach((h, i) => state.selectedIds.add(Number(h.id ?? i)));
      }
    }

    list.innerHTML = "";
    highlights.forEach((h, i) => {
      const id = Number(h.id ?? i);
      const selected = state.selectedIds.has(id);
      const already = state.renderedIds.has(id);
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
          <h3>${escapeHtml(h.title || `Tópico #${i + 1}`)}${
            already ? ` <span class="topic-ready">pronto</span>` : ""
          }</h3>
          <p class="meta-row"><strong>Tempo:</strong> ${fmtTime(h.start_time)} → ${fmtTime(h.end_time)}</p>
          <p class="meta-row"><strong>Hook:</strong> ${escapeHtml(h.hook_sentence || "—")}</p>
          <p class="topic-snippet">${escapeHtml(h.snippet || h.virality_reason || "")}</p>
        </div>
      `;
      card.addEventListener("click", () => toggleTopic(id, card));
      list.appendChild(card);
    });

    syncPickContinue();
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
    const ready = [...state.selectedIds].filter((id) => state.renderedIds.has(id)).length;
    const neu = n - ready;
    let hint = `${n} de ${total} selecionados`;
    if (state.renderedIds.size) {
      hint += ` · ${ready} prontos · ${neu} novos`;
    } else {
      hint += " · ordenados por tempo";
    }
    $("#pick-hint").textContent = hint;
    syncPickContinueLabel();
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

  $("#pick-continue").addEventListener("click", () => {
    if (!state.activeJobId || state.selectedIds.size === 0) return;
    state.followJobStep = false;
    setFlowStep(3, { maxStep: 3 });
    syncCaptionForm();
  });

  /* ---------- Caption karaoke UI ---------- */
  function assToHex(ass) {
    // &HAABBGGRR → #RRGGBB
    const m = String(ass || "").match(/&H([0-9A-Fa-f]{8})/);
    if (!m) return "#ffffff";
    const hex = m[1];
    const bb = hex.slice(2, 4);
    const gg = hex.slice(4, 6);
    const rr = hex.slice(6, 8);
    return `#${rr}${gg}${bb}`.toLowerCase();
  }

  function hexToAss(hex, alpha = "00") {
    const h = String(hex || "#ffffff").replace("#", "");
    if (h.length !== 6) return `&H${alpha}FFFFFF`;
    const rr = h.slice(0, 2);
    const gg = h.slice(2, 4);
    const bb = h.slice(4, 6);
    return `&H${alpha}${bb}${gg}${rr}`.toUpperCase();
  }

  function readCaptionForm() {
    const enabled = $("#caption-enabled")?.checked ?? true;
    const themeBtn = $(".theme-chip.is-selected");
    return {
      theme: themeBtn?.dataset.theme || state.captionStyle.theme || "bold-white",
      enabled,
      font_name: $("#caption-font")?.value || "Arial Black",
      font_size: Number($("#caption-size")?.value || 72),
      outline: Number($("#caption-outline")?.value || 4),
      max_words_per_line: Number($("#caption-words")?.value || 4),
      primary_colour: hexToAss($("#caption-primary")?.value || "#ffff00"),
      secondary_colour: hexToAss($("#caption-secondary")?.value || "#ffffff"),
      outline_colour: hexToAss($("#caption-outline-color")?.value || "#000000"),
      bold: $("#caption-bold")?.checked ?? true,
      shadow: state.captionStyle.shadow ?? 0,
      margin_v: state.captionStyle.margin_v ?? 160,
      back_colour: state.captionStyle.back_colour || "&H80000000",
    };
  }

  function applyThemeToForm(theme) {
    if (!theme) return;
    state.captionStyle = { ...state.captionStyle, ...theme, theme: theme.id || theme.theme };
    const font = $("#caption-font");
    if (font && theme.font_name) {
      if (![...font.options].some((o) => o.value === theme.font_name)) {
        const opt = document.createElement("option");
        opt.value = theme.font_name;
        opt.textContent = theme.font_name;
        font.appendChild(opt);
      }
      font.value = theme.font_name;
    }
    if ($("#caption-size") && theme.font_size != null) $("#caption-size").value = theme.font_size;
    if ($("#caption-outline") && theme.outline != null) $("#caption-outline").value = theme.outline;
    if ($("#caption-words") && theme.max_words_per_line != null) {
      $("#caption-words").value = theme.max_words_per_line;
    }
    if ($("#caption-primary") && theme.primary_colour) {
      $("#caption-primary").value = assToHex(theme.primary_colour);
    }
    if ($("#caption-secondary") && theme.secondary_colour) {
      $("#caption-secondary").value = assToHex(theme.secondary_colour);
    }
    if ($("#caption-outline-color") && theme.outline_colour) {
      $("#caption-outline-color").value = assToHex(theme.outline_colour);
    }
    if ($("#caption-bold") && theme.bold != null) $("#caption-bold").checked = !!theme.bold;
    updateCaptionPreview();
  }

  function previewHighlight() {
    const ordered = [...state.selectedIds]
      .map((id) => state.highlights.find((h, i) => Number(h.id ?? i) === Number(id)))
      .filter(Boolean)
      .sort((a, b) => float(a.start_time) - float(b.start_time));
    return ordered[0] || state.highlights[0] || null;
  }

  function float(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  }

  function previewWordsFromHighlight(h) {
    const raw = (h?.snippet || h?.hook_sentence || "Isso muda tudo").trim();
    const tokens = raw.split(/\s+/).filter(Boolean).slice(0, 5);
    return tokens.length ? tokens : ["Isso", "muda", "tudo"];
  }

  function updateCaptionPreview() {
    const preview = $("#caption-preview");
    const frame = $("#caption-preview-frame");
    const bg = $("#caption-preview-bg");
    const fallback = $("#caption-preview-fallback");
    const badge = $("#caption-preview-badge");
    const meta = $("#caption-preview-meta");
    if (!preview || !frame) return;

    const aspect =
      $("#aspect_ratio")?.value ||
      state.jobParams?.aspect_ratio ||
      "9:16";
    frame.dataset.ratio = aspect;
    if (badge) badge.textContent = aspect;

    const highlight = previewHighlight();
    const thumb = highlight?.thumbnail_url || "";
    if (bg) {
      if (thumb) {
        bg.hidden = false;
        if (bg.getAttribute("src") !== thumb) bg.src = thumb;
        if (fallback) fallback.hidden = true;
      } else {
        bg.hidden = true;
        bg.removeAttribute("src");
        if (fallback) fallback.hidden = false;
      }
    }
    if (meta) {
      const title = highlight?.title || "tópico selecionado";
      meta.textContent = thumb
        ? `Preview · ${aspect} · frame de “${title}”`
        : `Preview · ${aspect} · sem miniatura do corte ainda`;
    }

    const words = previewWordsFromHighlight(highlight);
    preview.innerHTML = words
      .map((w, i) => {
        const cls = i === 0 ? " is-done" : i === 1 ? " is-active" : "";
        return `<span class="cap-word${cls}">${escapeHtml(w)}</span>`;
      })
      .join("");

    const primary = $("#caption-primary")?.value || "#ffff00";
    const secondary = $("#caption-secondary")?.value || "#ffffff";
    const outline = $("#caption-outline-color")?.value || "#000000";
    const size = Number($("#caption-size")?.value || 72);
    const bold = $("#caption-bold")?.checked;
    const font = $("#caption-font")?.value || "Arial Black";
    const border = Number($("#caption-outline")?.value || 4);
    // Scale text relative to frame width so 9:16 vs 16:9 stay readable
    const frameW = frame.clientWidth || 280;
    const scale = frameW / 1080;
    preview.style.fontFamily = `"${font}", Impact, sans-serif`;
    preview.style.fontSize = `${Math.max(14, Math.round(size * scale * 0.95))}px`;
    preview.style.fontWeight = bold ? "900" : "600";
    preview.style.webkitTextStroke = `${Math.max(1, border * scale * 0.9)}px ${outline}`;
    preview.style.paintOrder = "stroke fill";
    $$(".cap-word", preview).forEach((w, i) => {
      w.style.color = i <= 1 ? primary : secondary;
    });
  }

  function renderThemeGrid() {
    const grid = $("#theme-grid");
    if (!grid) return;
    const themes = state.captionThemes.length
      ? state.captionThemes
      : [
          { id: "bold-white", label: "Branco bold" },
          { id: "yellow-pop", label: "Amarelo pop" },
          { id: "neon-mint", label: "Verde neon" },
          { id: "minimal", label: "Minimal" },
        ];
    const active = state.captionStyle.theme || "bold-white";
    grid.innerHTML = "";
    themes.forEach((t) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `theme-chip${t.id === active ? " is-selected" : ""}`;
      btn.dataset.theme = t.id;
      btn.setAttribute("role", "option");
      btn.setAttribute("aria-selected", t.id === active ? "true" : "false");
      btn.innerHTML = `<span class="theme-chip-label">${escapeHtml(t.label || t.id)}</span>`;
      btn.addEventListener("click", () => {
        $$(".theme-chip", grid).forEach((c) => {
          c.classList.remove("is-selected");
          c.setAttribute("aria-selected", "false");
        });
        btn.classList.add("is-selected");
        btn.setAttribute("aria-selected", "true");
        applyThemeToForm({ ...t, theme: t.id });
      });
      grid.appendChild(btn);
    });
  }

  function syncCaptionForm() {
    renderThemeGrid();
    const style = state.captionStyle;
    if ($("#caption-enabled")) $("#caption-enabled").checked = style.enabled !== false;
    applyThemeToForm({ ...style, id: style.theme });
    const controls = $("#caption-controls");
    if (controls) controls.hidden = style.enabled === false && !$("#caption-enabled")?.checked;
    const enabled = $("#caption-enabled")?.checked ?? true;
    if (controls) controls.hidden = !enabled;
    updateCaptionPreview();
  }

  async function loadCaptionThemes() {
    try {
      const res = await fetch("/api/caption-themes");
      const data = await res.json();
      if (res.ok) {
        state.captionThemes = data.themes || [];
        if (data.default && !state.jobParams?.caption_style) {
          state.captionStyle = { ...state.captionStyle, ...data.default };
        }
      }
    } catch (_) {
      /* ignore — hardcoded fallbacks */
    }
  }

  $("#caption-enabled")?.addEventListener("change", () => {
    const on = $("#caption-enabled").checked;
    const controls = $("#caption-controls");
    if (controls) controls.hidden = !on;
  });

  ["caption-font", "caption-size", "caption-outline", "caption-words", "caption-primary", "caption-secondary", "caption-outline-color", "caption-bold"].forEach((id) => {
    $(`#${id}`)?.addEventListener("input", updateCaptionPreview);
    $(`#${id}`)?.addEventListener("change", updateCaptionPreview);
  });

  $("#aspect_ratio")?.addEventListener("change", () => {
    if (state.viewStep === 3) updateCaptionPreview();
  });

  $("#caption-preview-bg")?.addEventListener("load", updateCaptionPreview);
  $("#caption-preview-bg")?.addEventListener("error", () => {
    const bg = $("#caption-preview-bg");
    const fallback = $("#caption-preview-fallback");
    if (bg) {
      bg.hidden = true;
      bg.removeAttribute("src");
    }
    if (fallback) fallback.hidden = false;
  });

  $("#caption-back")?.addEventListener("click", () => {
    state.followJobStep = false;
    setFlowStep(2);
    if (state.lastJob) renderTopicPicker(state.lastJob);
  });

  $("#caption-continue")?.addEventListener("click", async () => {
    if (!state.activeJobId || state.selectedIds.size === 0) return;
    const btn = $("#caption-continue");
    btn.disabled = true;
    const style = readCaptionForm();
    state.captionStyle = style;
    try {
      const res = await fetch(`/api/jobs/${state.activeJobId}/select`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: [...state.selectedIds], caption_style: style }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      state.jobStatus = "rendering";
      state.followJobStep = true;
      setFlowStep(4);
      setBadge("rendering");
      if (state.lastJob?.result) {
        const ready = [...state.selectedIds].filter((id) => state.renderedIds.has(id));
        const pending = [...state.selectedIds].filter((id) => !state.renderedIds.has(id));
        const currentId = pending[0] ?? null;
        renderResults({
          ...state.lastJob,
          status: "rendering",
          params: {
            ...state.lastJob.params,
            selected_ids: [...state.selectedIds],
            caption_style: style,
          },
          result: {
            ...state.lastJob.result,
            selected_ids: [...state.selectedIds],
            shorts: (state.lastJob.result.shorts || []).filter((s, i) =>
              state.renderedIds.has(Number(s.id ?? i))
            ),
            render_progress: {
              total: state.selectedIds.size,
              done: ready.length,
              current_id: currentId,
              pending_ids: pending.slice(1),
              done_ids: ready,
            },
          },
        });
      }
      if (state.pollTimer) clearInterval(state.pollTimer);
      pollJob();
      state.pollTimer = setInterval(pollJob, 1500);
    } catch (err) {
      $("#caption-hint").textContent = `erro: ${err.message}`;
      btn.disabled = false;
      pollJob();
    }
  });

  $("#goto-topics-btn")?.addEventListener("click", () => {
    if (state.maxStep >= 2) {
      state.followJobStep = false;
      setFlowStep(2);
      if (state.lastJob) renderTopicPicker(state.lastJob);
    }
  });

  $("#apply-format-btn")?.addEventListener("click", async () => {
    if (!state.activeJobId) return;
    const btn = $("#apply-format-btn");
    const hint = $("#edit-form-hint");
    const aspect = $("#aspect_ratio").value;
    btn.disabled = true;
    hint.textContent = "aplicando proporção…";
    try {
      const res = await fetch(`/api/jobs/${state.activeJobId}/params`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ aspect_ratio: aspect, regenerate: true }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      if (!data.changed) {
        hint.textContent = "proporção já estava aplicada";
        btn.disabled = false;
        return;
      }
      if (data.regenerating) {
        hint.textContent = "regenerando todos os shorts…";
        state.jobStatus = "rendering";
        state.renderedIds = new Set();
        state.followJobStep = true;
        setBadge("rendering");
        setFlowStep(4);
        if (state.lastJob?.result) {
          const ids = [
            ...(state.selectedIds.size
              ? state.selectedIds
              : state.lastJob.params?.selected_ids ||
                state.lastJob.result?.selected_ids ||
                []),
          ].map(Number);
          renderResults({
            ...state.lastJob,
            status: "rendering",
            result: {
              ...state.lastJob.result,
              shorts: [],
              selected_ids: ids,
              render_progress: {
                total: ids.length,
                done: 0,
                current_id: ids[0] ?? null,
                pending_ids: ids.slice(1),
                done_ids: [],
              },
            },
          });
        }
        if (state.pollTimer) clearInterval(state.pollTimer);
        pollJob();
        state.pollTimer = setInterval(pollJob, 1500);
      } else {
        hint.textContent = "proporção salva — escolha os tópicos na etapa 2";
        state.renderedIds = new Set();
        state.followJobStep = true;
        setFlowStep(2);
        if (state.lastJob) {
          state.lastJob.params = { ...state.lastJob.params, aspect_ratio: aspect };
          renderTopicPicker(state.lastJob);
        }
      }
    } catch (err) {
      hint.textContent = `erro: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  });

  function shortCardState(id, short, progress, jobStatus) {
    if (short?.clip_url && !short.error) return "ready";
    if (short?.error) return "error";
    if (jobStatus !== "rendering") return short ? "error" : "missing";
    const current = progress?.current_id;
    if (current != null && Number(current) === id) return "rendering";
    return "pending";
  }

  function buildShortCardHtml(id, highlight, short, cardState, index) {
    const title = highlight.title || short?.title || `Short #${id + 1}`;
    const score = short?.score ?? highlight.score ?? "—";
    const start = short?.start_time ?? highlight.start_time;
    const end = short?.end_time ?? highlight.end_time;
    const hook = short?.hook_sentence ?? highlight.hook_sentence ?? "—";
    const reason = short?.virality_reason ?? highlight.virality_reason ?? "—";
    const clip = short?.clip_url || "";
    const thumb = highlight.thumbnail_url
      ? `<img class="short-skeleton-thumb" src="${escapeAttr(highlight.thumbnail_url)}" alt="" />`
      : "";

    let media;
    if (cardState === "ready" && clip) {
      media = `<video controls playsinline src="${escapeAttr(clip)}"></video>`;
    } else if (cardState === "error") {
      media = `<div class="short-skeleton is-error"><span>${escapeHtml(
        short?.error || "Clip indisponível"
      )}</span></div>`;
    } else if (cardState === "rendering") {
      media = `<div class="short-skeleton is-rendering" aria-busy="true">
        ${thumb}
        <div class="short-skeleton-shine"></div>
        <span class="short-skeleton-label">Renderizando…</span>
      </div>`;
    } else if (cardState === "pending") {
      media = `<div class="short-skeleton is-pending" aria-busy="true">
        ${thumb}
        <div class="short-skeleton-shine"></div>
        <span class="short-skeleton-label">Na fila</span>
      </div>`;
    } else {
      media = `<p class="empty">Clip indisponível</p>`;
    }

    const download =
      cardState === "ready" && clip
        ? `<p class="meta-row"><a href="${escapeAttr(clip)}" download="short_${id}.mp4">Baixar clip</a></p>`
        : cardState === "rendering"
          ? `<p class="meta-row short-status-hint">Cortando agora…</p>`
          : cardState === "pending"
            ? `<p class="meta-row short-status-hint">Aguardando na fila</p>`
            : "";

    return `
      <div class="short-media">${media}</div>
      <div class="short-meta">
        <div class="score"><strong>${score}</strong> / 100</div>
        <h3>${escapeHtml(title)}</h3>
        <p class="meta-row"><strong>Tempo:</strong> ${fmtTime(start)} → ${fmtTime(end)}</p>
        <p class="meta-row"><strong>Hook:</strong> ${escapeHtml(hook)}</p>
        <p class="meta-row"><strong>Por quê:</strong> ${escapeHtml(reason)}</p>
        ${download}
      </div>
    `;
  }

  function renderResults(job) {
    const box = $("#results");
    if (!box) return;

    const highlights = job.result?.highlights || [];
    const shorts = job.result?.shorts || [];
    const progress = job.result?.render_progress || null;
    const jobStatus = job.status || state.jobStatus;

    const highlightsById = new Map(
      highlights.map((h, i) => [Number(h.id ?? i), h])
    );
    const shortsById = new Map(
      shorts.map((s, i) => [Number(s.id ?? i), s])
    );

    let ids = (job.params?.selected_ids || job.result?.selected_ids || [])
      .map(Number)
      .filter((n) => !Number.isNaN(n));
    if (!ids.length) {
      ids = [...shortsById.keys()];
    }
    if (!ids.length && jobStatus === "rendering" && state.selectedIds.size) {
      ids = [...state.selectedIds];
    }

    const done = progress?.done ?? shorts.filter((s) => s.clip_url && !s.error).length;
    const total = progress?.total ?? (ids.length || shorts.length);

    let head = box.querySelector(":scope > .section-head");
    if (!head) {
      head = document.createElement("div");
      head.className = "section-head";
      box.prepend(head);
    }
    head.style.order = "-1";

    const titleText =
      jobStatus === "rendering"
        ? `${done} de ${total} shorts prontos · renderizando…`
        : `${shorts.length} shorts · ${highlights.length} tópicos analisados`;

    head.innerHTML = `
      <h2 style="font-size:1.2rem">${titleText}</h2>
      ${
        jobStatus === "completed"
          ? `<a class="btn ghost" href="/api/jobs/${job.id}/result.json" download>Baixar JSON</a>`
          : `<span class="hint">${done}/${total}</span>`
      }
    `;

    if (!ids.length) {
      [...box.querySelectorAll(".short-card")].forEach((el) => el.remove());
      if (!box.querySelector(".empty")) {
        const empty = document.createElement("p");
        empty.className = "empty";
        empty.textContent =
          jobStatus === "rendering" ? "Preparando renderização…" : "Nenhum short gerado.";
        box.appendChild(empty);
      }
      return;
    }
    box.querySelectorAll(":scope > .empty").forEach((el) => el.remove());

    const existing = new Map(
      [...box.querySelectorAll(".short-card[data-id]")].map((el) => [el.dataset.id, el])
    );
    const seen = new Set();

    ids.forEach((id, i) => {
      const key = String(id);
      seen.add(key);
      const short = shortsById.get(id);
      const highlight = highlightsById.get(id) || short || {};
      const cardState = shortCardState(id, short, progress, jobStatus);
      let card = existing.get(key);

      if (card && card.dataset.state === cardState && cardState === "ready") {
        // Keep the same <video> so playback isn't interrupted
        card.style.order = String(i);
        return;
      }

      if (!card) {
        card = document.createElement("article");
        card.className = "short-card";
        card.dataset.id = key;
        box.appendChild(card);
      }

      card.dataset.state = cardState;
      card.className = `short-card is-${cardState}`;
      card.style.order = String(i);
      card.style.animationDelay = `${i * 0.06}s`;
      card.innerHTML = buildShortCardHtml(id, highlight, short, cardState, i);
    });

    existing.forEach((el, key) => {
      if (!seen.has(key)) el.remove();
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
  loadCaptionThemes();
  setFlowStep(1);
  applyRoute(parseRoute(location.pathname));
})();
