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
    trim: {
      highlightId: null,
      start: 0,
      end: 0,
      originalStart: 0,
      originalEnd: 0,
      originalTitle: "",
      originalSpeaker: "",
      titleSpeaker: "",
      duration: 0,
      winStart: 0,
      winEnd: 0,
      dragging: null,
      previewLoop: false,
      dirty: false,
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
    closeTrimEditor({ silent: true });
    $("#run-area").hidden = true;
    $("#pick-area").hidden = true;
    const formatArea = $("#format-area");
    if (formatArea) formatArea.hidden = true;
    const castArea = $("#cast-area");
    if (castArea) castArea.hidden = true;
    $("#results").innerHTML = "";
    const logEl = $("#job-log");
    logEl.textContent = "";
    logEl.hidden = true;
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
    if (status === "awaiting_cast" || status === "ranking") return 2;
    if (status === "awaiting_selection") return 2;
    if (status === "rendering" || status === "completed") return 5;
    return 1;
  }

  function selectionResumeStep(job) {
    let saved = Number(job?.params?.ui_step);
    if (!Number.isFinite(saved)) return 2;
    // flow_version < 2: step 3 was topics and 4 was captions — swap on resume
    if (Number(job?.params?.flow_version) !== 2) {
      if (saved === 3) saved = 4;
      else if (saved === 4) saved = 3;
    }
    if (saved >= 2 && saved <= 4) return saved;
    return 2;
  }

  async function persistUiStep(step) {
    if (!state.activeJobId) return;
    const n = Number(step);
    if (![1, 2, 3, 4, 5].includes(n)) return;
    if (state.lastJob?.params) state.lastJob.params.ui_step = n;
    if (state.jobParams) state.jobParams.ui_step = n;
    const aspect = currentAspectRatio();
    const fmt =
      $("#download_format")?.value ||
      state.jobParams?.download_format ||
      state.lastJob?.params?.download_format ||
      "720";
    try {
      await fetch(`/api/jobs/${state.activeJobId}/params`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          aspect_ratio: aspect,
          download_format: fmt,
          ui_step: n,
          regenerate: false,
        }),
      });
    } catch (_) {
      /* best-effort */
    }
  }

  let selectedPersistTimer = null;
  function persistSelectedIds({ immediate = false } = {}) {
    if (!state.activeJobId) return;
    const ids = [...state.selectedIds]
      .map(Number)
      .filter((n) => !Number.isNaN(n));
    if (state.lastJob?.params) state.lastJob.params.selected_ids = ids;
    if (state.jobParams) state.jobParams.selected_ids = ids;

    const flush = async () => {
      selectedPersistTimer = null;
      if (!state.activeJobId) return;
      const aspect = currentAspectRatio();
      const fmt =
        $("#download_format")?.value ||
        state.jobParams?.download_format ||
        state.lastJob?.params?.download_format ||
        "720";
      try {
        await fetch(`/api/jobs/${state.activeJobId}/params`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            aspect_ratio: aspect,
            download_format: fmt,
            selected_ids: ids,
            regenerate: false,
          }),
        });
      } catch (_) {
        /* best-effort */
      }
    };

    if (selectedPersistTimer) clearTimeout(selectedPersistTimer);
    if (immediate) flush();
    else selectedPersistTimer = setTimeout(flush, 250);
  }

  function selectedIdsFromJob(job, highlights) {
    const params = job?.params || {};
    if (Array.isArray(params.selected_ids)) {
      return new Set(params.selected_ids.map(Number).filter((n) => !Number.isNaN(n)));
    }
    const fromResult = job?.result?.selected_ids;
    if (Array.isArray(fromResult)) {
      return new Set(fromResult.map(Number).filter((n) => !Number.isNaN(n)));
    }
    if (state.renderedIds.size) return new Set(state.renderedIds);
    return new Set((highlights || []).map((h, i) => Number(h.id ?? i)));
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
    const castGate =
      step === 2 &&
      (state.jobStatus === "awaiting_cast" || state.jobStatus === "ranking");
    const labels = {
      1: "1 · Configurar fonte",
      2: castGate ? "2 · Identificar locutores" : "2 · Formato",
      3: "3 · Legendas karaoke",
      4: "4 · Escolher tópicos",
      5: "5 · Cortar shorts",
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
    const castStatus = ["awaiting_cast", "ranking"].includes(state.jobStatus);
    const editable =
      hasJob &&
      step === 1 &&
      (["awaiting_selection", "completed", "awaiting_cast"].includes(state.jobStatus) ||
        (state.jobStatus === "failed" && (hasTopics || castStatus)));

    const step1Fields = $("#step1-fields");
    if (step1Fields) step1Fields.hidden = step !== 1;

    // Lock only on step 1 while the job is running (fields are hidden on later steps).
    form?.classList.toggle("is-locked", hasJob && step === 1 && !editable);
    form?.classList.toggle("is-editing-job", editable);

    const newActions = $("#new-job-actions");
    const editActions = $("#edit-job-actions");
    if (newActions) newActions.hidden = editable;
    if (editActions) editActions.hidden = !editable;

    const cast = $("#cast-area");
    const pick = $("#pick-area");
    const format = $("#format-area");
    const captions = $("#caption-area");
    const results = $("#results");
    const run = $("#run-area");
    const logEl = $("#job-log");

    if (!hasJob) {
      if (run) run.hidden = true;
      if (logEl) logEl.hidden = true;
      return;
    }
    if (logEl) logEl.hidden = false;

    const canPick = hasTopics && selectableStatus;
    const showCast = step === 2 && state.jobStatus === "awaiting_cast";
    const showFormat = step === 2 && canPick;
    const showCaptions = step === 3 && canPick;
    const showPick = step === 4 && canPick;
    const showResults = step === 5;

    if (cast) {
      cast.hidden = !showCast;
      if (showCast && state.lastJob) renderCastForm(state.lastJob);
    }
    if (pick) {
      pick.hidden = !showPick;
      if (!showPick) closeTrimEditor({ silent: true });
    }
    if (format) {
      format.hidden = !showFormat;
    }
    if (captions) {
      captions.hidden = !showCaptions;
      if (showCaptions) syncCaptionForm();
      else {
        const capVideo = $("#caption-preview-video");
        if (capVideo && !capVideo.paused) {
          try {
            capVideo.pause();
          } catch (_) {
            /* ignore */
          }
          syncCaptionPreviewUi(false);
        }
      }
    }
    if (results) {
      results.hidden = !showResults;
      if (showResults && state.lastJob?.result) {
        renderResults(state.lastJob);
      }
    }
    if (run) run.hidden = !(showCast || showPick || showFormat || showCaptions || showResults);

    syncPickContinueLabel();
  }

  $$(".step-dot").forEach((dot) => {
    dot.addEventListener("click", () => {
      const n = Number(dot.dataset.step);
      if (dot.disabled || n > state.maxStep) return;
      state.followJobStep = false;
      setFlowStep(n);
      if (
        ["awaiting_selection", "completed", "failed"].includes(state.jobStatus) &&
        n >= 2 &&
        n <= 4
      ) {
        persistUiStep(n);
      }
      if (n === 2 && state.lastJob && state.jobStatus === "awaiting_cast") {
        renderCastForm(state.lastJob);
      }
      if (n === 3) syncCaptionForm();
      if (n === 4 && state.lastJob) renderTopicPicker(state.lastJob);
      if (n === 5 && state.lastJob?.result) renderResults(state.lastJob);
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
    if (label) {
      const hasRendered = state.renderedIds.size > 0 || state.jobStatus === "completed";
      label.textContent = hasRendered ? "Atualizar shorts" : "Gerar shorts";
    }
    const capLabel = $("#caption-continue-label");
    if (capLabel) capLabel.textContent = "Continuar para tópicos";
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
    // Formato fica no passo 2 — envia defaults atuais dos selects
    fd.set("aspect_ratio", $("#aspect_ratio")?.value || "9:16");
    fd.set("download_format", $("#download_format")?.value || "720");
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
    closeTrimEditor({ silent: true });
    $("#run-area").hidden = true;
    $("#pick-area").hidden = true;
    const formatArea = $("#format-area");
    if (formatArea) formatArea.hidden = true;
    const castArea = $("#cast-area");
    if (castArea) castArea.hidden = true;
    $("#topic-list").innerHTML = "";
    const logEl = $("#job-log");
    logEl.textContent = "";
    logEl.hidden = false;
    $("#results").innerHTML = "";
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
      } else if (job.status === "awaiting_cast") {
        state.maxStep = Math.max(state.maxStep, 2);
        const btn = $("#cast-continue");
        const skipBtn = $("#cast-skip");
        if (btn) btn.disabled = false;
        if (skipBtn) skipBtn.disabled = false;
        if (state.followJobStep) setFlowStep(2);
        else showFlowView(state.viewStep);
        if (state.viewStep === 2) renderCastForm(job);
        if (state.pollTimer) {
          clearInterval(state.pollTimer);
          state.pollTimer = null;
        }
      } else if (job.status === "ranking") {
        state.maxStep = Math.max(state.maxStep, 2);
        if (state.followJobStep) setFlowStep(2);
        else showFlowView(state.viewStep);
        const cast = $("#cast-area");
        if (cast) cast.hidden = true;
      } else if (job.status === "awaiting_selection") {
        prepareSelection(job);
        const resume = selectionResumeStep(job);
        state.maxStep = Math.max(state.maxStep, Math.max(3, resume));
        // Allow navigating to topics/caption steps if they already picked topics
        if (state.selectedIds.size > 0) {
          state.maxStep = Math.max(state.maxStep, 4);
        }
        if (state.followJobStep) setFlowStep(resume);
        else showFlowView(state.viewStep);
        if (state.viewStep === 3) syncCaptionForm();
        if (state.viewStep === 4) renderTopicPicker(job);
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
        state.maxStep = Math.max(state.maxStep, 5);
        if (state.followJobStep) setFlowStep(5);
        else showFlowView(state.viewStep);
        if (state.viewStep === 5) renderResults(job);
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
          setFlowStep(5);
          renderResults(job);
        } else {
          state.maxStep = Math.max(state.maxStep, 5);
          showFlowView(state.viewStep);
          if (state.viewStep === 3) syncCaptionForm();
          if (state.viewStep === 4) renderTopicPicker(job);
          if (state.viewStep === 5) renderResults(job);
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
        const speakers = job.result?.speakers || [];
        if (highlights.length) {
          // Análise sobreviveu — permite retentar a seleção/corte
          prepareSelection(job);
          const resume = selectionResumeStep(job);
          state.maxStep = Math.max(state.maxStep, Math.max(4, resume));
          if (state.followJobStep) setFlowStep(resume);
          else showFlowView(state.viewStep);
          if (state.viewStep === 3) syncCaptionForm();
          if (state.viewStep === 4) renderTopicPicker(job);
        } else if (speakers.length && job.result?.transcript) {
          // Ranking falhou — volta para naming de locutores
          job.status = "awaiting_cast";
          state.jobStatus = "awaiting_cast";
          state.maxStep = Math.max(state.maxStep, 2);
          if (state.followJobStep) setFlowStep(2);
          else showFlowView(state.viewStep);
          if (state.viewStep === 2) renderCastForm(job);
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
      state.selectedIds = selectedIdsFromJob(job, highlights);
    }
    if (state.viewStep === 4) renderTopicPicker(job);
  }

  function renderCastForm(job) {
    const speakers = job.result?.speakers || [];
    const list = $("#cast-list");
    const area = $("#cast-area");
    if (!list || !area) return;
    if (state.viewStep === 2 && state.jobStatus === "awaiting_cast") {
      area.hidden = false;
    }

    if (!speakers.length) {
      list.innerHTML = `<p class="empty">Nenhum locutor detectado — você pode pular esta etapa.</p>`;
      return;
    }

    // Preserve in-progress name edits across poll re-renders
    const typed = {};
    $$(".cast-name").forEach((input) => {
      typed[input.dataset.id] = input.value;
    });

    list.innerHTML = "";
    speakers.forEach((sp, i) => {
      const sid = String(sp.id || `S${i + 1}`);
      const suggested = typed[sid] ?? (sp.suggested_name || sp.name || "");
      const role = sp.role || "unknown";
      const quote = sp.sample_quote || "";
      const evidence = sp.evidence || "";
      const portrait = sp.portrait_url || "";
      const card = document.createElement("div");
      card.className = "cast-card";
      card.dataset.id = sid;
      const placeholder = `<span class="cast-face is-placeholder" aria-hidden="true">${escapeHtml(sid)}</span>`;
      const face = portrait
        ? `<img class="cast-face" src="${escapeAttr(portrait)}" alt="${escapeAttr(suggested || sid)}" loading="lazy" width="112" height="112" />`
        : placeholder;
      card.innerHTML = `
        <div class="cast-avatar">
          ${face}
          <span class="cast-id">${escapeHtml(sid)}</span>
          <button type="button" class="cast-next-photo" data-id="${escapeAttr(sid)}" title="Buscar outro frame com rosto">
            Trocar foto
          </button>
        </div>
        <div class="cast-body">
          <label class="field">
            <span class="label">Nome ${role !== "unknown" ? `(${escapeHtml(role)})` : ""}</span>
            <input type="text" class="cast-name" data-id="${escapeAttr(sid)}"
              value="${escapeAttr(suggested)}"
              placeholder="Ex.: Rodrigo Pimentel" />
          </label>
          ${quote ? `<p class="cast-quote">“${escapeHtml(quote)}”</p>` : ""}
          ${evidence ? `<p class="cast-evidence">${escapeHtml(evidence)}</p>` : ""}
        </div>
      `;
      const img = card.querySelector("img.cast-face");
      if (img) {
        img.addEventListener("error", () => {
          img.replaceWith(
            Object.assign(document.createElement("span"), {
              className: "cast-face is-placeholder",
              textContent: sid,
            })
          );
        });
      }
      card.querySelector(".cast-next-photo")?.addEventListener("click", () => {
        cycleCastPortrait(sid, card);
      });
      list.appendChild(card);
    });
  }

  function setCastFace(card, sid, portraitUrl, alt) {
    const avatar = card.querySelector(".cast-avatar");
    if (!avatar) return;
    let face = avatar.querySelector(".cast-face");
    if (portraitUrl) {
      if (face && face.tagName === "IMG") {
        face.src = portraitUrl;
        face.alt = alt || sid;
      } else {
        const img = document.createElement("img");
        img.className = "cast-face";
        img.src = portraitUrl;
        img.alt = alt || sid;
        img.width = 112;
        img.height = 112;
        img.loading = "lazy";
        img.addEventListener("error", () => {
          img.replaceWith(
            Object.assign(document.createElement("span"), {
              className: "cast-face is-placeholder",
              textContent: sid,
            })
          );
        });
        if (face) face.replaceWith(img);
        else avatar.insertBefore(img, avatar.firstChild);
      }
    }
  }

  async function cycleCastPortrait(sid, card) {
    if (!state.activeJobId || state.jobStatus !== "awaiting_cast") return;
    const btn = card.querySelector(".cast-next-photo");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Buscando…";
    }
    try {
      const res = await fetch(
        `/api/jobs/${state.activeJobId}/cast/${encodeURIComponent(sid)}/next-portrait`,
        { method: "POST" }
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      const url = data.portrait_url;
      if (url) {
        setCastFace(card, sid, url, sid);
        const speakers = state.lastJob?.result?.speakers || [];
        const sp = speakers.find((s) => String(s.id) === String(sid));
        if (sp) {
          sp.portrait_url = url;
          if (data.portrait_time != null) sp.portrait_time = data.portrait_time;
        }
      }
    } catch (err) {
      const hint = $("#cast-hint");
      if (hint) hint.textContent = `Trocar foto: ${err.message}`;
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Trocar foto";
      }
    }
  }

  async function submitCast({ skip = false } = {}) {
    if (!state.activeJobId) return;
    const btn = $("#cast-continue");
    const skipBtn = $("#cast-skip");
    if (btn) btn.disabled = true;
    if (skipBtn) skipBtn.disabled = true;

    const speakers = [...$$(".cast-name")].map((input) => ({
      id: input.dataset.id,
      name: input.value.trim(),
    }));

    try {
      const res = await fetch(`/api/jobs/${state.activeJobId}/cast`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ speakers, skip: Boolean(skip) }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      state.jobStatus = "ranking";
      const cast = $("#cast-area");
      if (cast) cast.hidden = true;
      const hint = $("#cast-hint");
      if (hint) {
        hint.textContent = skip
          ? "Pulando locutores — ranqueando tópicos…"
          : "Nomes salvos — rotulando falas e ranqueando tópicos…";
      }
      if (state.pollTimer) clearInterval(state.pollTimer);
      pollJob();
      state.pollTimer = setInterval(pollJob, 1500);
    } catch (err) {
      const hint = $("#cast-hint");
      if (hint) hint.textContent = `erro: ${err.message}`;
      if (btn) btn.disabled = false;
      if (skipBtn) skipBtn.disabled = false;
    }
  }

  $("#cast-continue")?.addEventListener("click", () => submitCast({ skip: false }));
  $("#cast-skip")?.addEventListener("click", () => submitCast({ skip: true }));

  function currentAspectRatio() {
    return (
      $("#aspect_ratio")?.value ||
      state.jobParams?.aspect_ratio ||
      state.lastJob?.params?.aspect_ratio ||
      "9:16"
    );
  }

  const captionWordsCache = new Map();

  function effectiveCaptionStyle() {
    if (state.viewStep === 3 && $("#caption-controls")) {
      return readCaptionForm();
    }
    return {
      ...state.captionStyle,
      ...(state.jobParams?.caption_style || {}),
      enabled: true,
    };
  }

  async function fetchCaptionWords(start, end) {
    if (!state.activeJobId) return [];
    const key = `${state.activeJobId}:${Number(start).toFixed(2)}:${Number(end).toFixed(2)}`;
    if (captionWordsCache.has(key)) return captionWordsCache.get(key);
    try {
      const res = await fetch(
        `/api/jobs/${state.activeJobId}/caption-words?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return [];
      const words = Array.isArray(data.words) ? data.words : [];
      captionWordsCache.set(key, words);
      return words;
    } catch (_) {
      return [];
    }
  }

  function styleLiveCaption(el, frameEl) {
    if (!el) return;
    const style = effectiveCaptionStyle();
    const primary = assToHex(style.primary_colour || "&H0000FFFF");
    const secondary = assToHex(style.secondary_colour || "&H00FFFFFF");
    const outline = assToHex(style.outline_colour || "&H00000000");
    const size = Number(style.font_size || 72);
    const bold = style.bold !== false;
    const font = style.font_name || "Arial Black";
    const border = Number(style.outline || 4);
    const frameW = frameEl?.clientWidth || el.parentElement?.clientWidth || 280;
    const scale = frameW / 1080;
    const strokePx = border > 0 ? border * scale * 2 : 0;
    el.style.fontFamily = `"${font}", Impact, sans-serif`;
    el.style.fontSize = `${Math.max(12, Math.round(size * scale))}px`;
    el.style.fontWeight = bold ? "900" : "600";
    el.style.webkitTextStroke = strokePx > 0 ? `${strokePx}px ${outline}` : "0";
    el.style.paintOrder = "stroke fill";
    el.dataset.primary = primary;
    el.dataset.secondary = secondary;
  }

  function paintLiveCaption(el, words, tAbs, frameEl) {
    if (!el) return;
    const style = effectiveCaptionStyle();
    if (!words?.length) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    let activeIdx = words.findIndex((w) => tAbs >= float(w.start) && tAbs < float(w.end));
    if (activeIdx < 0) {
      activeIdx = -1;
      for (let i = 0; i < words.length; i++) {
        if (float(words[i].start) <= tAbs) activeIdx = i;
        else break;
      }
    }
    if (activeIdx < 0) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    const maxW = Math.max(1, Number(style.max_words_per_line || 4));
    const lineStart = Math.floor(activeIdx / maxW) * maxW;
    const line = words.slice(lineStart, lineStart + maxW);
    el.hidden = false;
    styleLiveCaption(el, frameEl);
    const primary = el.dataset.primary || "#ffff00";
    const secondary = el.dataset.secondary || "#ffffff";
    el.innerHTML = line
      .map((w, i) => {
        const gi = lineStart + i;
        const cls =
          gi < activeIdx ? " is-done" : gi === activeIdx ? " is-active" : "";
        return `<span class="cap-word${cls}">${escapeHtml(String(w.word || ""))}</span>`;
      })
      .join("");
    $$(".cap-word", el).forEach((node, i) => {
      const gi = lineStart + i;
      node.style.color = gi <= activeIdx ? primary : secondary;
    });
  }

  async function ensureMediaCaptionWords(media) {
    if (!media) return [];
    if (Array.isArray(media._captionWords)) return media._captionWords;
    const start = float(media.dataset.start);
    const end = Math.max(start + 0.5, float(media.dataset.end));
    const words = await fetchCaptionWords(start, end);
    media._captionWords = words;
    return words;
  }

  async function syncMediaLiveCaption(media) {
    const video = $(".topic-video", media);
    const caption = $(".live-caption", media);
    if (!video || !caption) return;
    const words = await ensureMediaCaptionWords(media);
    paintLiveCaption(caption, words, video.currentTime || 0, $(".topic-media-frame", media));
  }

  async function syncTrimLiveCaption() {
    const video = $("#trim-video");
    const caption = $("#trim-live-caption");
    if (!video || !caption || state.trim.highlightId == null) return;
    const start = state.trim.start;
    const end = state.trim.end;
    const key = `trim:${start.toFixed(2)}:${end.toFixed(2)}`;
    if (!state.trim._captionKey || state.trim._captionKey !== key) {
      state.trim._captionKey = key;
      state.trim._captionWords = await fetchCaptionWords(start, end);
    }
    paintLiveCaption(
      caption,
      state.trim._captionWords || [],
      video.currentTime || 0,
      $("#trim-player-frame")
    );
  }

  function applyPreviewAspect(aspect) {
    const ratio = aspect || currentAspectRatio();
    $$(".topic-media").forEach((el) => {
      el.dataset.ratio = ratio;
    });
    const trimFrame = $("#trim-player-frame");
    if (trimFrame) trimFrame.dataset.ratio = ratio;
  }

  function pauseAllTopicVideos({ except = null } = {}) {
    $$(".topic-media").forEach((media) => {
      if (except && media === except) return;
      const video = $(".topic-video", media);
      if (video && !video.paused) {
        try {
          video.pause();
        } catch (_) {
          /* ignore */
        }
      }
      media.classList.remove("is-playing");
    });
    const capVideo = $("#caption-preview-video");
    if (capVideo && !capVideo.paused) {
      try {
        capVideo.pause();
      } catch (_) {
        /* ignore */
      }
      syncCaptionPreviewUi(false);
    }
  }

  function syncTopicMediaUi(media, playing) {
    if (!media) return;
    media.classList.toggle("is-playing", Boolean(playing));
    if (playing) media.classList.add("has-frame");
    const icon = $(".topic-play-icon", media);
    if (icon) {
      icon.setAttribute("aria-label", playing ? "Pausar" : "Reproduzir");
    }
  }

  async function toggleTopicPreview(media) {
    if (!media) return;
    const video = $(".topic-video", media);
    if (!video) return;

    const start = float(media.dataset.start);
    const end = Math.max(start + 0.5, float(media.dataset.end));
    const src = media.dataset.src || "";

    if (!src) {
      syncTopicMediaUi(media, false);
      return;
    }

    if (!video.paused) {
      video.pause();
      syncTopicMediaUi(media, false);
      return;
    }

    pauseAllTopicVideos({ except: media });

    if (!video.getAttribute("src") || video.getAttribute("src") !== src) {
      video.src = src;
      video.load();
    }

    const seekAndPlay = async () => {
      try {
        if (Math.abs(video.currentTime - start) > 0.35 || video.currentTime >= end - 0.15) {
          video.currentTime = start;
        }
      } catch (_) {
        /* seek may fail until metadata loads */
      }
      try {
        await video.play();
        syncTopicMediaUi(media, true);
      } catch (_) {
        syncTopicMediaUi(media, false);
      }
    };

    if (video.readyState >= 1) {
      await seekAndPlay();
    } else {
      const onReady = async () => {
        video.removeEventListener("loadedmetadata", onReady);
        await seekAndPlay();
      };
      video.addEventListener("loadedmetadata", onReady);
      try {
        video.load();
      } catch (_) {
        /* ignore */
      }
    }
  }

  function bindTopicMedia(media) {
    const video = $(".topic-video", media);
    if (!video) return;

    video.addEventListener("timeupdate", () => {
      const end = float(media.dataset.end);
      if (end > 0 && video.currentTime >= end - 0.05) {
        video.pause();
        try {
          video.currentTime = float(media.dataset.start);
        } catch (_) {
          /* ignore */
        }
        syncTopicMediaUi(media, false);
      }
      syncMediaLiveCaption(media);
    });
    video.addEventListener("ended", () => {
      syncTopicMediaUi(media, false);
    });
    video.addEventListener("pause", () => syncTopicMediaUi(media, false));
    video.addEventListener("play", () => {
      syncTopicMediaUi(media, true);
      ensureMediaCaptionWords(media).then(() => syncMediaLiveCaption(media));
    });

    media.addEventListener("click", (ev) => {
      if (ev.target.closest(".topic-check")) return;
      if (!ev.target.closest(".topic-media-frame")) return;
      ev.preventDefault();
      ev.stopPropagation();
      toggleTopicPreview(media);
    });
  }

  function renderTopicPicker(job) {
    const highlights = job.result?.highlights || [];
    state.highlights = highlights;
    const list = $("#topic-list");
    const pick = $("#pick-area");
    if (state.viewStep === 4) pick.hidden = false;

    if (!highlights.length) {
      list.innerHTML = `<p class="empty">Nenhum tópico encontrado.</p>`;
      $("#pick-continue").disabled = true;
      $("#pick-hint").textContent = "Nada para selecionar";
      closeTrimEditor({ silent: true });
      return;
    }

    if (state.selectedIds.size === 0) {
      state.selectedIds = selectedIdsFromJob(job, highlights);
    }

    pauseAllTopicVideos();
    list.innerHTML = "";
    const previewSrc = sourcePreviewUrl(job);
    const aspect = currentAspectRatio();
    const editingId = state.trim.highlightId;
    highlights.forEach((h, i) => {
      const id = Number(h.id ?? i);
      const selected = state.selectedIds.has(id);
      const already = state.renderedIds.has(id);
      const editing = editingId != null && Number(editingId) === id;
      const card = document.createElement("div");
      card.className = `topic-card${selected ? " is-selected" : ""}${
        editing ? " is-editing" : ""
      }`;
      card.dataset.id = String(id);
      card.style.animationDelay = `${i * 0.04}s`;
      const thumbUrl =
        h.preview_thumbnail_url ||
        h.thumbnail_url ||
        (state.activeJobId
          ? `/api/jobs/${state.activeJobId}/preview-thumbs/${id}?v=2`
          : "");
      const thumbLayer = thumbUrl
        ? `<img class="topic-thumb" src="${escapeAttr(thumbUrl)}" alt="" loading="lazy" referrerpolicy="no-referrer" />`
        : `<span class="topic-thumb is-placeholder">frame</span>`;
      const mediaInner = previewSrc
        ? `${thumbLayer}<video class="topic-video" playsinline preload="none"${
            thumbUrl ? ` poster="${escapeAttr(thumbUrl)}"` : ""
          }></video>`
        : thumbLayer;
      card.innerHTML = `
        <div
          class="topic-media${previewSrc ? "" : " is-static"}"
          data-ratio="${escapeAttr(aspect)}"
          data-start="${escapeAttr(String(h.start_time ?? 0))}"
          data-end="${escapeAttr(String(h.end_time ?? 0))}"
          data-src="${escapeAttr(previewSrc)}"
        >
          <input
            class="topic-check"
            type="checkbox"
            ${selected ? "checked" : ""}
            aria-label="Selecionar tópico"
          />
          <div class="topic-media-frame">
            ${mediaInner}
            ${
              previewSrc
                ? `<div class="live-caption" aria-hidden="true"></div><div class="topic-media-overlay" aria-hidden="true"><span class="topic-play-icon" aria-label="Reproduzir"></span></div>`
                : ""
            }
          </div>
        </div>
        <div class="topic-body">
          <div class="score"><strong>${h.score ?? "—"}</strong> / 100</div>
          <h3>${escapeHtml(h.title || `Tópico #${i + 1}`)}${
            already ? ` <span class="topic-ready">pronto</span>` : ""
          }</h3>
          ${
            h.attributed_to
              ? `<p class="meta-row"><strong>Locutor:</strong> ${escapeHtml(h.attributed_to)}</p>`
              : ""
          }
          <p class="meta-row topic-time"><strong>Tempo:</strong> ${fmtTime(h.start_time)} → ${fmtTime(h.end_time)}</p>
          <p class="meta-row"><strong>Hook:</strong> ${escapeHtml(h.hook_sentence || "—")}</p>
          <p class="topic-snippet">${escapeHtml(h.snippet || h.virality_reason || "")}</p>
          <div class="topic-card-actions">
            <button type="button" class="topic-edit" data-edit-id="${id}">
              ${editing ? "Editando corte…" : "Ajustar corte"}
            </button>
          </div>
        </div>
      `;
      const media = $(".topic-media", card);
      if (previewSrc && media) bindTopicMedia(media);
      const check = $(".topic-check", card);
      check?.addEventListener("click", (ev) => ev.stopPropagation());
      check?.addEventListener("change", () => {
        if (check.checked) {
          state.selectedIds.add(id);
          card.classList.add("is-selected");
        } else {
          state.selectedIds.delete(id);
          card.classList.remove("is-selected");
        }
        syncPickContinue();
        persistSelectedIds();
      });
      $(".topic-edit", card)?.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        openTrimEditor(id);
      });
      list.appendChild(card);
    });

    applyPreviewAspect(aspect);
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
    persistSelectedIds();
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
    persistSelectedIds({ immediate: true });
  });

  $("#pick-none").addEventListener("click", () => {
    state.selectedIds.clear();
    $$(".topic-card").forEach((card) => {
      card.classList.remove("is-selected");
      const cb = $(".topic-check", card);
      if (cb) cb.checked = false;
    });
    syncPickContinue();
    persistSelectedIds({ immediate: true });
  });

  /* ---------- Trim editor (step 2) ---------- */
  function parseTimeInput(raw) {
    const s = String(raw || "").trim().replace(",", ".");
    if (!s) return NaN;
    if (/^\d+(\.\d+)?$/.test(s)) return Number(s);
    const parts = s.split(":").map((p) => p.trim());
    if (parts.some((p) => p === "" || Number.isNaN(Number(p)))) return NaN;
    if (parts.length === 2) return Number(parts[0]) * 60 + Number(parts[1]);
    if (parts.length === 3) {
      return Number(parts[0]) * 3600 + Number(parts[1]) * 60 + Number(parts[2]);
    }
    return NaN;
  }

  function fmtTimeInput(t) {
    if (t == null || Number.isNaN(Number(t))) return "";
    const n = Math.max(0, Number(t));
    const m = Math.floor(n / 60);
    const s = (n % 60).toFixed(2);
    return `${m}:${s.padStart(5, "0")}`;
  }

  function highlightById(id) {
    return state.highlights.find((h, i) => Number(h.id ?? i) === Number(id)) || null;
  }

  function videoDurationFromJob(job) {
    const fromResult = float(job?.result?.duration);
    if (fromResult > 0) return fromResult;
    const fromTx = float(job?.result?.transcript?.duration);
    if (fromTx > 0) return fromTx;
    const video = $("#trim-video");
    if (video && Number.isFinite(video.duration) && video.duration > 0) {
      return video.duration;
    }
    let maxEnd = 0;
    (job?.result?.highlights || state.highlights || []).forEach((h) => {
      maxEnd = Math.max(maxEnd, float(h.end_time));
    });
    return maxEnd > 0 ? maxEnd + 5 : 0;
  }

  function sourcePreviewUrl(job) {
    const url = job?.result?.source_preview_url;
    if (url) return url;
    if (state.activeJobId) return `/api/jobs/${state.activeJobId}/source`;
    return "";
  }

  function fmtTimePrecise(t) {
    if (t == null || Number.isNaN(Number(t))) return "—";
    const n = Math.max(0, Number(t));
    const m = Math.floor(n / 60);
    const s = (n % 60).toFixed(2);
    return `${m}:${s.padStart(5, "0")}`;
  }

  function clampTrimTimes(start, end) {
    const t = state.trim;
    const duration = t.duration > 0 ? t.duration : Math.max(end, start + 1);
    let s = Math.max(0, Math.min(start, duration - 1));
    let e = Math.max(s + 1, Math.min(end, duration));
    if (e - s < 1) e = Math.min(duration, s + 1);
    return { start: Math.round(s * 100) / 100, end: Math.round(e * 100) / 100 };
  }

  function recomputeTrimWindow({ force = false } = {}) {
    const t = state.trim;
    // Keep the visible window stable while dragging a single handle —
    // recentering made the opposite handle appear to move.
    if (!force && t.winEnd > t.winStart) {
      const margin = Math.max(2, (t.winEnd - t.winStart) * 0.04);
      let winStart = t.winStart;
      let winEnd = t.winEnd;
      const span = winEnd - winStart;
      if (t.start < winStart + margin) {
        winStart = Math.max(0, t.start - margin);
        winEnd = winStart + span;
      }
      if (t.end > winEnd - margin) {
        winEnd = Math.min(t.duration || t.end + margin, t.end + margin);
        winStart = Math.max(0, winEnd - span);
      }
      if (t.duration > 0 && winEnd > t.duration) {
        winEnd = t.duration;
        winStart = Math.max(0, winEnd - span);
      }
      t.winStart = winStart;
      t.winEnd = Math.max(winStart + 1, winEnd);
      return;
    }
    const clip = Math.max(1, t.end - t.start);
    const pad = Math.max(20, Math.min(120, clip * 1.5));
    let winStart = Math.max(0, t.start - pad);
    let winEnd = t.duration > 0 ? Math.min(t.duration, t.end + pad) : t.end + pad;
    if (winEnd - winStart < 10) {
      winEnd = Math.min(t.duration || winEnd + 10, winStart + Math.max(10, clip * 2));
    }
    t.winStart = winStart;
    t.winEnd = Math.max(winStart + 1, winEnd);
  }

  function syncTrimUI() {
    const t = state.trim;
    const startInput = $("#trim-start-input");
    const endInput = $("#trim-end-input");
    if (startInput && document.activeElement !== startInput) {
      startInput.value = fmtTimeInput(t.start);
    }
    if (endInput && document.activeElement !== endInput) {
      endInput.value = fmtTimeInput(t.end);
    }
    const durEl = $("#trim-duration");
    if (durEl) durEl.textContent = fmtTime(t.end - t.start);
    const winStartEl = $("#trim-win-start");
    const winEndEl = $("#trim-win-end");
    if (winStartEl) winStartEl.textContent = fmtTime(t.winStart);
    if (winEndEl) winEndEl.textContent = fmtTime(t.winEnd);

    const span = Math.max(0.001, t.winEnd - t.winStart);
    const leftPct = ((t.start - t.winStart) / span) * 100;
    const rightPct = ((t.end - t.winStart) / span) * 100;
    const sel = $("#trim-selection");
    const hs = $("#trim-handle-start");
    const he = $("#trim-handle-end");
    if (sel) {
      sel.style.left = `${leftPct}%`;
      sel.style.width = `${Math.max(0.5, rightPct - leftPct)}%`;
    }
    if (hs) hs.style.left = `${leftPct}%`;
    if (he) he.style.left = `${rightPct}%`;

    const hint = $("#trim-hint");
    if (hint) {
      const len = t.end - t.start;
      let msg = "Arraste um handle por vez — o outro lado fica fixo";
      if (len < 15) msg = "Corte curto (<15s) — ok se for intencional";
      else if (len > 90) msg = "Corte longo (>90s) — shorts costumam performar melhor mais curtos";
      if (t.dirty) msg += " · alterações não salvas";
      hint.textContent = msg;
    }
  }

  function updatePlayhead() {
    const video = $("#trim-video");
    const head = $("#trim-playhead");
    const nowEl = $("#trim-current-time");
    const t = state.trim;
    if (!video || !Number.isFinite(video.currentTime)) return;
    if (nowEl) nowEl.textContent = fmtTimePrecise(video.currentTime);
    if (!head) return;
    const span = Math.max(0.001, t.winEnd - t.winStart);
    const pct = ((video.currentTime - t.winStart) / span) * 100;
    if (pct < -2 || pct > 102) {
      head.hidden = true;
      return;
    }
    head.hidden = false;
    head.style.left = `${Math.max(0, Math.min(100, pct))}%`;
  }

  function setTrimRange(start, end, {
    seek = true,
    markDirty = true,
    recomputeWindow = true,
    forceWindow = false,
  } = {}) {
    const clamped = clampTrimTimes(start, end);
    state.trim.start = clamped.start;
    state.trim.end = clamped.end;
    if (markDirty) state.trim.dirty = true;
    if (recomputeWindow) recomputeTrimWindow({ force: forceWindow });
    state.trim._captionKey = null;
    syncTrimUI();
    const video = $("#trim-video");
    if (seek && video) {
      const target = state.trim.dragging === "end" ? state.trim.end : state.trim.start;
      if (Math.abs(video.currentTime - target) > 0.05) {
        try {
          video.currentTime = target;
        } catch (_) {
          /* ignore seek errors while loading */
        }
      }
    }
    updatePlayhead();
    syncTrimLiveCaption();
  }

  function knownSpeakerNames(job) {
    const names = [];
    const seen = new Set();
    for (const sp of job?.result?.speakers || []) {
      const name = String(sp.name || sp.suggested_name || "").trim();
      if (!name) continue;
      const key = name.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      names.push(name);
    }
    return names;
  }

  function escapeRegExp(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function replaceSpeakerInTitle(title, fromName, toName, knownNames = []) {
    const t = String(title || "").trim();
    const to = String(toName || "").trim();
    if (!to) return t;
    if (!t) return to;

    const tryReplace = (from) => {
      const name = String(from || "").trim();
      if (!name) return null;
      const re = new RegExp(escapeRegExp(name), "i");
      if (!re.test(t)) return null;
      return t.replace(new RegExp(escapeRegExp(name), "gi"), to);
    };

    const fromHit = tryReplace(fromName);
    if (fromHit != null) return fromHit;

    const sorted = [...knownNames]
      .filter(Boolean)
      .sort((a, b) => b.length - a.length);
    for (const name of sorted) {
      if (name.toLowerCase() === to.toLowerCase()) continue;
      const hit = tryReplace(name);
      if (hit != null) return hit;
    }

    const colon = t.match(/^([^:]{1,80}):\s*(.+)$/);
    if (colon) return `${to}: ${colon[2].trim()}`;

    return `${to}: ${t}`;
  }

  function populateTrimSpeakerSelect(job, selected) {
    const select = $("#trim-speaker-input");
    if (!select) return [];
    const names = knownSpeakerNames(job);
    const current = String(selected || "").trim();
    if (current && !names.some((n) => n.toLowerCase() === current.toLowerCase())) {
      names.unshift(current);
    }
    const opts = [`<option value="">— Sem locutor —</option>`].concat(
      names.map(
        (n) =>
          `<option value="${escapeAttr(n)}"${
            current && n.toLowerCase() === current.toLowerCase() ? " selected" : ""
          }>${escapeHtml(n)}</option>`
      )
    );
    select.innerHTML = opts.join("");
    if (current) {
      const match = names.find((n) => n.toLowerCase() === current.toLowerCase());
      select.value = match || current;
    } else {
      select.value = "";
    }
    return names;
  }

  function openTrimEditor(highlightId) {
    const job = state.lastJob;
    const h = highlightById(highlightId);
    if (!job || !h || !state.activeJobId) return;

    const preview = sourcePreviewUrl(job);
    if (!preview) {
      $("#pick-hint").textContent = "Vídeo fonte indisponível para pré-visualizar";
      return;
    }

    pauseAllTopicVideos();
    applyPreviewAspect();

    const start = float(h.start_time);
    const end = float(h.end_time);
    const duration = videoDurationFromJob(job);
    state.trim.highlightId = Number(highlightId);
    state.trim.originalStart = start;
    state.trim.originalEnd = end;
    state.trim.originalTitle = String(h.title || "").trim();
    state.trim.originalSpeaker = String(h.attributed_to || "").trim();
    state.trim.titleSpeaker = state.trim.originalSpeaker;
    state.trim.duration = duration;
    state.trim.dirty = false;
    state.trim.previewLoop = false;
    state.trim.dragging = null;

    const editor = $("#trim-editor");
    if (editor) editor.hidden = false;
    document.body.classList.add("trim-modal-open");
    const title = $("#trim-title");
    if (title) title.textContent = `Ajustar: ${h.title || `Tópico #${highlightId}`}`;

    const titleInput = $("#trim-title-input");
    if (titleInput) titleInput.value = state.trim.originalTitle;
    populateTrimSpeakerSelect(job, state.trim.originalSpeaker);

    const video = $("#trim-video");
    if (video) {
      const nextSrc = preview;
      if (video.dataset.src !== nextSrc) {
        video.dataset.src = nextSrc;
        video.src = nextSrc;
      }
      video.pause();
    }
    $("#trim-pause").hidden = true;
    $("#trim-play-clip").hidden = false;

    setTrimRange(start, end, { seek: true, markDirty: false, forceWindow: true });
    if (state.lastJob) renderTopicPicker(state.lastJob);
    $("#trim-close")?.focus();
  }

  function closeTrimEditor({ silent = false } = {}) {
    const video = $("#trim-video");
    if (video) {
      video.pause();
      state.trim.previewLoop = false;
    }
    $("#trim-pause").hidden = true;
    $("#trim-play-clip").hidden = false;
    const had = state.trim.highlightId != null;
    state.trim.highlightId = null;
    state.trim.dragging = null;
    state.trim.dirty = false;
    const editor = $("#trim-editor");
    if (editor) editor.hidden = true;
    document.body.classList.remove("trim-modal-open");
    if (!silent && had && state.lastJob && state.viewStep === 4) {
      renderTopicPicker(state.lastJob);
    }
  }

  async function saveTrimEditor() {
    const id = state.trim.highlightId;
    if (id == null || !state.activeJobId) return;
    const clamped = clampTrimTimes(state.trim.start, state.trim.end);
    const titleVal = ($("#trim-title-input")?.value || "").trim();
    const speakerVal = ($("#trim-speaker-input")?.value || "").trim();
    if (!titleVal) {
      const hint = $("#trim-hint");
      if (hint) hint.textContent = "Informe um título para o corte";
      $("#trim-title-input")?.focus();
      return;
    }
    const btn = $("#trim-save");
    const hint = $("#trim-hint");
    if (btn) btn.disabled = true;
    if (hint) hint.textContent = "salvando ajustes…";
    try {
      const res = await fetch(
        `/api/jobs/${state.activeJobId}/highlights/${id}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            start_time: clamped.start,
            end_time: clamped.end,
            title: titleVal,
            attributed_to: speakerVal,
          }),
        }
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));

      const h = highlightById(id);
      if (h && data.highlight) {
        Object.assign(h, {
          start_time: data.highlight.start_time,
          end_time: data.highlight.end_time,
          title: data.highlight.title ?? titleVal,
          attributed_to: data.highlight.attributed_to ?? speakerVal,
          thumbnail_url: data.highlight.thumbnail_url || h.thumbnail_url,
        });
      } else if (h) {
        h.start_time = clamped.start;
        h.end_time = clamped.end;
        h.title = titleVal;
        h.attributed_to = speakerVal;
      }
      if (state.lastJob?.result?.highlights) {
        state.lastJob.result.highlights = state.highlights;
      }
      if (data.invalidated_short) {
        state.renderedIds.delete(Number(id));
        if (state.lastJob?.result?.shorts) {
          state.lastJob.result.shorts = state.lastJob.result.shorts.filter(
            (s, i) => Number(s.id ?? i) !== Number(id)
          );
        }
        if (state.lastJob) state.lastJob.status = "awaiting_selection";
        state.jobStatus = "awaiting_selection";
      }
      state.trim.start = clamped.start;
      state.trim.end = clamped.end;
      state.trim.originalStart = clamped.start;
      state.trim.originalEnd = clamped.end;
      state.trim.originalTitle = titleVal;
      state.trim.originalSpeaker = speakerVal;
      state.trim.dirty = false;
      const heading = $("#trim-title");
      if (heading) heading.textContent = `Ajustar: ${titleVal}`;
      syncTrimUI();
      if (hint) {
        hint.textContent = data.invalidated_short
          ? "Ajustes salvos — este short será re-cortado"
          : "Ajustes salvos";
      }
      closeTrimEditor();
    } catch (err) {
      if (hint) hint.textContent = `Erro: ${err.message || err}`;
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function timeFromTimelineClientX(clientX) {
    const track = $("#trim-track");
    if (!track) return state.trim.start;
    const rect = track.getBoundingClientRect();
    const ratio = rect.width > 0 ? (clientX - rect.left) / rect.width : 0;
    const t = state.trim;
    return t.winStart + Math.max(0, Math.min(1, ratio)) * (t.winEnd - t.winStart);
  }

  function startTrimDrag(edge, ev) {
    ev.preventDefault();
    ev.stopPropagation();
    state.trim.dragging = edge;
    state.trim.previewLoop = false;
    const video = $("#trim-video");
    video?.pause();
    $("#trim-pause").hidden = true;
    $("#trim-play-clip").hidden = false;
  }

  function onTrimPointerMove(ev) {
    if (!state.trim.dragging) return;
    const t = timeFromTimelineClientX(ev.clientX);
    const opts = { recomputeWindow: false, markDirty: true };
    if (state.trim.dragging === "start") {
      setTrimRange(Math.min(t, state.trim.end - 1), state.trim.end, {
        ...opts,
        seek: true,
      });
    } else if (state.trim.dragging === "end") {
      setTrimRange(state.trim.start, Math.max(t, state.trim.start + 1), {
        ...opts,
        seek: true,
      });
    } else if (state.trim.dragging === "seek") {
      const video = $("#trim-video");
      const clamped = Math.max(state.trim.winStart, Math.min(t, state.trim.winEnd));
      if (video) {
        try {
          video.currentTime = clamped;
        } catch (_) {}
      }
      updatePlayhead();
    }
  }

  function onTrimPointerUp() {
    if (!state.trim.dragging) return;
    const was = state.trim.dragging;
    state.trim.dragging = null;
    // Soft pan only if a handle is near the edge — never recenter both sides.
    if (was === "start" || was === "end") {
      recomputeTrimWindow({ force: false });
      syncTrimUI();
      updatePlayhead();
    }
  }

  $("#trim-handle-start")?.addEventListener("pointerdown", (ev) =>
    startTrimDrag("start", ev)
  );
  $("#trim-handle-end")?.addEventListener("pointerdown", (ev) =>
    startTrimDrag("end", ev)
  );
  $("#trim-track")?.addEventListener("pointerdown", (ev) => {
    if (ev.target.closest(".trim-handle")) return;
    startTrimDrag("seek", ev);
    onTrimPointerMove(ev);
  });
  window.addEventListener("pointermove", onTrimPointerMove);
  window.addEventListener("pointerup", onTrimPointerUp);
  window.addEventListener("pointercancel", onTrimPointerUp);

  $("#trim-start-input")?.addEventListener("change", () => {
    const parsed = parseTimeInput($("#trim-start-input").value);
    if (Number.isNaN(parsed)) {
      syncTrimUI();
      return;
    }
    setTrimRange(parsed, state.trim.end);
  });
  $("#trim-end-input")?.addEventListener("change", () => {
    const parsed = parseTimeInput($("#trim-end-input").value);
    if (Number.isNaN(parsed)) {
      syncTrimUI();
      return;
    }
    setTrimRange(state.trim.start, parsed);
  });

  $$(".trim-nudge-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const edge = btn.dataset.edge;
      const delta = float(btn.dataset.delta);
      if (edge === "start") setTrimRange(state.trim.start + delta, state.trim.end);
      else setTrimRange(state.trim.start, state.trim.end + delta);
    });
  });

  $("#trim-reset")?.addEventListener("click", () => {
    const titleInput = $("#trim-title-input");
    const speakerInput = $("#trim-speaker-input");
    if (titleInput) titleInput.value = state.trim.originalTitle || "";
    if (speakerInput) {
      populateTrimSpeakerSelect(state.lastJob, state.trim.originalSpeaker);
    }
    state.trim.titleSpeaker = state.trim.originalSpeaker || "";
    setTrimRange(state.trim.originalStart, state.trim.originalEnd, {
      markDirty: true,
      forceWindow: true,
    });
  });
  $("#trim-close")?.addEventListener("click", () => closeTrimEditor());
  $("#trim-save")?.addEventListener("click", () => saveTrimEditor());
  $("#trim-editor")?.addEventListener("click", (ev) => {
    if (ev.target.closest("[data-trim-dismiss]")) closeTrimEditor();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    if ($("#trim-editor")?.hidden) return;
    closeTrimEditor();
  });

  function markTrimMetaDirty() {
    if (state.trim.highlightId == null) return;
    const titleVal = ($("#trim-title-input")?.value || "").trim();
    const speakerVal = ($("#trim-speaker-input")?.value || "").trim();
    const metaDirty =
      titleVal !== (state.trim.originalTitle || "") ||
      speakerVal !== (state.trim.originalSpeaker || "");
    const timesDirty =
      Math.abs(state.trim.start - state.trim.originalStart) > 0.01 ||
      Math.abs(state.trim.end - state.trim.originalEnd) > 0.01;
    state.trim.dirty = metaDirty || timesDirty;
    syncTrimUI();
  }
  $("#trim-title-input")?.addEventListener("input", markTrimMetaDirty);
  $("#trim-speaker-input")?.addEventListener("change", () => {
    if (state.trim.highlightId == null) return;
    const select = $("#trim-speaker-input");
    const titleInput = $("#trim-title-input");
    const next = (select?.value || "").trim();
    const prev = String(state.trim.titleSpeaker || state.trim.originalSpeaker || "").trim();
    if (titleInput && next && next.toLowerCase() !== prev.toLowerCase()) {
      titleInput.value = replaceSpeakerInTitle(
        titleInput.value,
        prev,
        next,
        knownSpeakerNames(state.lastJob)
      );
      const heading = $("#trim-title");
      if (heading) heading.textContent = `Ajustar: ${titleInput.value}`;
    } else if (titleInput && !next && prev) {
      // Speaker cleared: strip previous name from title when present
      const stripped = String(titleInput.value || "")
        .replace(new RegExp(`^\\s*${escapeRegExp(prev)}\\s*:\\s*`, "i"), "")
        .replace(new RegExp(escapeRegExp(prev), "gi"), "")
        .replace(/\s{2,}/g, " ")
        .trim();
      if (stripped) titleInput.value = stripped;
      const heading = $("#trim-title");
      if (heading) heading.textContent = `Ajustar: ${titleInput.value || "corte"}`;
    }
    state.trim.titleSpeaker = next;
    markTrimMetaDirty();
  });

  $("#trim-play-clip")?.addEventListener("click", () => {
    const video = $("#trim-video");
    if (!video) return;
    state.trim.previewLoop = true;
    try {
      video.currentTime = state.trim.start;
    } catch (_) {}
    video.play().catch(() => {});
    $("#trim-play-clip").hidden = true;
    $("#trim-pause").hidden = false;
  });
  $("#trim-pause")?.addEventListener("click", () => {
    const video = $("#trim-video");
    state.trim.previewLoop = false;
    video?.pause();
    $("#trim-pause").hidden = true;
    $("#trim-play-clip").hidden = false;
  });

  const trimVideo = $("#trim-video");
  trimVideo?.addEventListener("click", () => {
    if (state.trim.highlightId == null) return;
    if (trimVideo.paused) {
      // If parked past the cut end, restart from start for a useful preview.
      if (trimVideo.currentTime >= state.trim.end - 0.05) {
        try {
          trimVideo.currentTime = state.trim.start;
        } catch (_) {}
      }
      state.trim.previewLoop = true;
      trimVideo.play().catch(() => {});
    } else {
      state.trim.previewLoop = false;
      trimVideo.pause();
    }
  });
  trimVideo?.addEventListener("loadedmetadata", () => {
    if (state.trim.highlightId == null) return;
    if (Number.isFinite(trimVideo.duration) && trimVideo.duration > 0) {
      state.trim.duration = Math.max(state.trim.duration, trimVideo.duration);
      recomputeTrimWindow({ force: true });
      syncTrimUI();
    }
    try {
      trimVideo.currentTime = state.trim.start;
    } catch (_) {}
    updatePlayhead();
  });
  trimVideo?.addEventListener("seeked", () => {
    updatePlayhead();
    syncTrimLiveCaption();
  });
  trimVideo?.addEventListener("timeupdate", () => {
    if (state.trim.highlightId == null) return;
    updatePlayhead();
    syncTrimLiveCaption();
    if (
      state.trim.previewLoop &&
      !trimVideo.paused &&
      trimVideo.currentTime >= state.trim.end - 0.05
    ) {
      trimVideo.pause();
      state.trim.previewLoop = false;
      try {
        trimVideo.currentTime = state.trim.start;
      } catch (_) {}
      $("#trim-pause").hidden = true;
      $("#trim-play-clip").hidden = false;
      updatePlayhead();
      syncTrimLiveCaption();
    }
  });
  trimVideo?.addEventListener("play", () => {
    if (state.trim.highlightId == null) return;
    $("#trim-play-clip").hidden = true;
    $("#trim-pause").hidden = false;
    state.trim._captionKey = null;
    syncTrimLiveCaption();
  });
  trimVideo?.addEventListener("pause", () => {
    if (state.trim.highlightId == null) return;
    if (!state.trim.previewLoop) {
      $("#trim-pause").hidden = true;
      $("#trim-play-clip").hidden = false;
    }
  });

  $("#pick-continue").addEventListener("click", async () => {
    if (!state.activeJobId || state.selectedIds.size === 0) return;
    if (state.trim.highlightId != null && state.trim.dirty) {
      await saveTrimEditor();
      if (state.trim.dirty) return; // save failed
    }
    closeTrimEditor({ silent: true });
    const btn = $("#pick-continue");
    btn.disabled = true;
    state.captionStyle = {
      ...state.captionStyle,
      ...(state.jobParams?.caption_style || {}),
    };
    applyThemeToForm({ ...state.captionStyle, id: state.captionStyle.theme });
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
      setFlowStep(5);
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
      $("#pick-hint").textContent = `erro: ${err.message}`;
      btn.disabled = false;
      pollJob();
    }
  });

  $("#pick-back")?.addEventListener("click", () => {
    state.followJobStep = false;
    closeTrimEditor({ silent: true });
    setFlowStep(3);
    persistUiStep(3);
    syncCaptionForm();
  });

  $("#format-back")?.addEventListener("click", () => {
    state.followJobStep = false;
    setFlowStep(1);
  });

  $("#format-continue")?.addEventListener("click", async () => {
    if (!state.activeJobId) return;
    const btn = $("#format-continue");
    const hint = $("#format-hint");
    const aspect = $("#aspect_ratio")?.value || "9:16";
    const fmt = $("#download_format")?.value || "720";
    if (btn) btn.disabled = true;
    if (hint) hint.textContent = "salvando formato…";
    try {
      if (state.selectedIds.size === 0 && state.lastJob) {
        state.selectedIds = selectedIdsFromJob(
          state.lastJob,
          state.lastJob.result?.highlights || state.highlights
        );
      }
      const selected = [...state.selectedIds]
        .map(Number)
        .filter((n) => !Number.isNaN(n));
      const res = await fetch(`/api/jobs/${state.activeJobId}/params`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          aspect_ratio: aspect,
          download_format: fmt,
          ui_step: 3,
          selected_ids: selected,
          regenerate: false,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      if (state.lastJob) {
        state.lastJob.params = {
          ...state.lastJob.params,
          aspect_ratio: aspect,
          download_format: fmt,
          ui_step: 3,
          flow_version: 2,
          selected_ids: selected,
        };
      }
      state.jobParams = {
        ...(state.jobParams || {}),
        aspect_ratio: aspect,
        download_format: fmt,
        ui_step: 3,
        flow_version: 2,
        selected_ids: selected,
      };
      if (hint) {
        hint.textContent =
          "Escolha a proporção do corte. A resolução vale para novos downloads (análise).";
      }
      state.followJobStep = false;
      setFlowStep(3, { maxStep: 3 });
      syncCaptionForm();
    } catch (err) {
      if (hint) hint.textContent = `erro: ${err.message}`;
    } finally {
      if (btn) btn.disabled = false;
    }
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
    const themeBtn = $(".theme-chip.is-selected");
    return {
      theme: themeBtn?.dataset.theme || state.captionStyle.theme || "bold-white",
      enabled: true,
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
      uppercase: state.captionStyle.uppercase !== false,
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

  const captionPreview = {
    start: 0,
    end: 0,
    words: [],
    key: "",
    bound: false,
  };

  function syncCaptionPreviewUi(playing) {
    const frame = $("#caption-preview-frame");
    const toggle = $("#caption-preview-toggle");
    const playBtn = $("#caption-preview-play");
    frame?.classList.toggle("is-playing", Boolean(playing));
    if (toggle) toggle.textContent = playing ? "❚❚ Pausar" : "▶ Pré-visualizar";
    if (playBtn) playBtn.setAttribute("aria-label", playing ? "Pausar" : "Reproduzir preview");
  }

  async function syncCaptionPreviewCaption() {
    const video = $("#caption-preview-video");
    const caption = $("#caption-preview-caption");
    const frame = $("#caption-preview-frame");
    if (!video || !caption) return;
    paintLiveCaption(caption, captionPreview.words, video.currentTime || 0, frame);
  }

  async function ensureCaptionPreviewWords(start, end) {
    const key = `${Number(start).toFixed(2)}:${Number(end).toFixed(2)}`;
    if (captionPreview.key === key && captionPreview.words.length) {
      return captionPreview.words;
    }
    captionPreview.key = key;
    captionPreview.start = start;
    captionPreview.end = end;
    captionPreview.words = await fetchCaptionWords(start, end);
    return captionPreview.words;
  }

  async function toggleCaptionPreviewPlayback() {
    const video = $("#caption-preview-video");
    if (!video || !video.getAttribute("src")) return;
    if (!video.paused) {
      video.pause();
      syncCaptionPreviewUi(false);
      return;
    }
    pauseAllTopicVideos();
    const start = captionPreview.start;
    const end = Math.max(start + 0.5, captionPreview.end);
    try {
      if (
        !Number.isFinite(video.currentTime) ||
        video.currentTime < start - 0.05 ||
        video.currentTime >= end - 0.15
      ) {
        video.currentTime = start;
      }
    } catch (_) {
      /* ignore */
    }
    await ensureCaptionPreviewWords(start, end);
    try {
      await video.play();
      syncCaptionPreviewUi(true);
      syncCaptionPreviewCaption();
    } catch (_) {
      syncCaptionPreviewUi(false);
    }
  }

  function bindCaptionPreviewVideo() {
    if (captionPreview.bound) return;
    const video = $("#caption-preview-video");
    if (!video) return;
    captionPreview.bound = true;

    video.addEventListener("timeupdate", () => {
      if (state.viewStep !== 3) return;
      const end = captionPreview.end;
      if (end > 0 && video.currentTime >= end - 0.05) {
        try {
          video.currentTime = captionPreview.start;
        } catch (_) {
          /* ignore */
        }
      }
      syncCaptionPreviewCaption();
    });
    video.addEventListener("play", () => syncCaptionPreviewUi(true));
    video.addEventListener("pause", () => {
      if (state.viewStep === 3) syncCaptionPreviewUi(false);
    });
    video.addEventListener("ended", () => syncCaptionPreviewUi(false));

    $("#caption-preview-frame")?.addEventListener("click", (ev) => {
      if (ev.target.closest(".caption-preview-badge")) return;
      ev.preventDefault();
      toggleCaptionPreviewPlayback();
    });
    $("#caption-preview-toggle")?.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      toggleCaptionPreviewPlayback();
    });
  }

  function updateCaptionPreview() {
    const frame = $("#caption-preview-frame");
    const video = $("#caption-preview-video");
    const badge = $("#caption-preview-badge");
    const meta = $("#caption-preview-meta");
    if (!frame || !video) return;

    bindCaptionPreviewVideo();

    const aspect = currentAspectRatio();
    frame.dataset.ratio = aspect;
    if (badge) badge.textContent = aspect;

    const highlight = previewHighlight();
    const start = float(highlight?.start_time);
    const end = Math.max(start + 0.5, float(highlight?.end_time));
    const thumb = highlight?.thumbnail_url || "";
    const previewSrc = sourcePreviewUrl(state.lastJob);
    const rangeKey = `${start.toFixed(2)}:${end.toFixed(2)}`;
    const srcChanged = previewSrc && video.dataset.src !== previewSrc;
    const rangeChanged = captionPreview.key !== rangeKey;

    captionPreview.start = start;
    captionPreview.end = end;

    if (previewSrc) {
      if (srcChanged) {
        video.dataset.src = previewSrc;
        video.src = previewSrc;
      }
      if (thumb) video.setAttribute("poster", thumb);
      else video.removeAttribute("poster");
      if ((srcChanged || rangeChanged) && video.paused) {
        try {
          video.currentTime = start;
        } catch (_) {
          /* ignore until metadata */
        }
      }
    }

    if (meta) {
      const title = highlight?.title || "tópico selecionado";
      meta.textContent = previewSrc
        ? `Preview · ${aspect} · “${title}” · clique para play/pause`
        : `Preview · ${aspect} · vídeo fonte indisponível`;
    }

    const caption = $("#caption-preview-caption");
    if (caption) {
      if (rangeChanged || !captionPreview.words.length) {
        captionPreview.key = "";
        captionPreview.words = [];
        const sample = previewWordsFromHighlight(highlight).map((word, i) => ({
          word,
          start: start + i * 0.35,
          end: start + (i + 1) * 0.35,
        }));
        paintLiveCaption(caption, sample, start + 0.4, frame);
        ensureCaptionPreviewWords(start, end).then(() => {
          syncCaptionPreviewCaption();
        });
      } else {
        syncCaptionPreviewCaption();
      }
    }

    syncCaptionPreviewUi(!video.paused && Boolean(previewSrc));
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
    applyThemeToForm({ ...style, id: style.theme });
    const controls = $("#caption-controls");
    if (controls) controls.hidden = false;
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

  ["caption-font", "caption-size", "caption-outline", "caption-words", "caption-primary", "caption-secondary", "caption-outline-color", "caption-bold"].forEach((id) => {
    $(`#${id}`)?.addEventListener("input", updateCaptionPreview);
    $(`#${id}`)?.addEventListener("change", updateCaptionPreview);
  });

  $("#aspect_ratio")?.addEventListener("change", () => {
    applyPreviewAspect();
    if (state.viewStep === 3) updateCaptionPreview();
  });

  $("#caption-back")?.addEventListener("click", () => {
    state.followJobStep = false;
    setFlowStep(2);
    persistUiStep(2);
  });

  $("#caption-continue")?.addEventListener("click", async () => {
    if (!state.activeJobId) return;
    const btn = $("#caption-continue");
    const hint = $("#caption-hint");
    btn.disabled = true;
    const style = readCaptionForm();
    state.captionStyle = style;
    try {
      const aspect = currentAspectRatio();
      const fmt =
        $("#download_format")?.value ||
        state.jobParams?.download_format ||
        "720";
      const res = await fetch(`/api/jobs/${state.activeJobId}/params`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          aspect_ratio: aspect,
          download_format: fmt,
          ui_step: 4,
          caption_style: style,
          regenerate: false,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      if (state.lastJob?.params) {
        state.lastJob.params.ui_step = 4;
        state.lastJob.params.flow_version = 2;
        state.lastJob.params.caption_style = style;
      }
      state.jobParams = {
        ...(state.jobParams || {}),
        ui_step: 4,
        flow_version: 2,
        caption_style: style,
      };
      state.followJobStep = false;
      setFlowStep(4, { maxStep: 4 });
      if (state.lastJob) renderTopicPicker(state.lastJob);
      if (hint) {
        hint.textContent =
          "Escolha o tema e ajuste tipografia — as palavras destacam no ritmo da fala";
      }
    } catch (err) {
      if (hint) hint.textContent = `erro: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  });

  $("#goto-topics-btn")?.addEventListener("click", () => {
    if (state.maxStep >= 4) {
      state.followJobStep = false;
      setFlowStep(4);
      persistUiStep(4);
      if (state.lastJob) renderTopicPicker(state.lastJob);
    } else if (state.maxStep >= 3) {
      state.followJobStep = false;
      setFlowStep(3);
      persistUiStep(3);
      syncCaptionForm();
    } else if (state.maxStep >= 2 && state.jobStatus === "awaiting_cast") {
      state.followJobStep = false;
      setFlowStep(2);
      if (state.lastJob) renderCastForm(state.lastJob);
    }
  });

  $("#goto-format-btn")?.addEventListener("click", () => {
    if (state.maxStep >= 2 && state.jobStatus !== "awaiting_cast") {
      state.followJobStep = false;
      setFlowStep(2);
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
    // Short poster only after the clip exists (or server already published thumbnail_url).
    // While queued/rendering, reuse the topic preview thumb — never hit short-thumbs 404s.
    const previewThumb =
      highlight.thumbnail_url ||
      highlight.preview_thumbnail_url ||
      (state.activeJobId
        ? `/api/jobs/${state.activeJobId}/preview-thumbs/${id}?v=2`
        : "");
    const shortThumb =
      short?.thumbnail_url ||
      (cardState === "ready" && state.activeJobId
        ? `/api/jobs/${state.activeJobId}/short-thumbs/${id}?v=2`
        : "");
    const poster = (cardState === "ready" ? shortThumb || previewThumb : previewThumb) || "";
    const thumbImg = poster
      ? `<img class="short-skeleton-thumb" src="${escapeAttr(poster)}" alt="" loading="lazy" onerror="this.remove()" />`
      : "";

    let media;
    if (cardState === "ready" && clip) {
      const posterAttr = poster ? ` poster="${escapeAttr(poster)}"` : "";
      media = `<video controls playsinline preload="metadata" src="${escapeAttr(clip)}"${posterAttr}></video>`;
    } else if (cardState === "error") {
      media = `<div class="short-skeleton is-error"><span>${escapeHtml(
        short?.error || "Clip indisponível"
      )}</span></div>`;
    } else if (cardState === "rendering") {
      media = `<div class="short-skeleton is-rendering" aria-busy="true">
        ${thumbImg}
        <div class="short-skeleton-shine"></div>
        <span class="short-skeleton-label">Renderizando…</span>
      </div>`;
    } else if (cardState === "pending") {
      media = `<div class="short-skeleton is-pending" aria-busy="true">
        ${thumbImg}
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

    const ytUrl = short?.youtube_url || "";
    const ytActions =
      cardState === "ready" && clip
        ? `<div class="short-actions" data-short-id="${escapeAttr(String(id))}">
            ${
              ytUrl
                ? `<a class="btn ghost btn-tiny" href="${escapeAttr(
                    ytUrl
                  )}" target="_blank" rel="noopener">Ver no YouTube</a>
                   <button type="button" class="btn ghost btn-tiny yt-upload-btn" data-yt-upload="${escapeAttr(
                     String(id)
                   )}">Reenviar</button>`
                : `<button type="button" class="btn primary btn-tiny yt-upload-btn" data-yt-upload="${escapeAttr(
                    String(id)
                  )}">Enviar ao YouTube</button>`
            }
            <p class="hint yt-upload-hint" hidden></p>
          </div>`
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
        ${ytActions}
      </div>
    `;
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

  async function uploadShortToYoutube(shortId, btn) {
    const jobId = state.activeJobId || state.lastJob?.id;
    if (!jobId) return;
    const actions = btn.closest(".short-actions");
    const hint = actions?.querySelector(".yt-upload-hint");
    const prevLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Enviando…";
    if (hint) {
      hint.hidden = false;
      hint.textContent = "Upload em andamento (pode levar alguns minutos)…";
    }
    try {
      const res = await fetch(`/api/jobs/${jobId}/shorts/${shortId}/youtube`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = data.detail;
        const msg =
          typeof detail === "string"
            ? detail
            : Array.isArray(detail)
              ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
              : data.message || `HTTP ${res.status}`;
        throw new Error(msg);
      }
      if (state.lastJob?.result?.shorts) {
        const shorts = state.lastJob.result.shorts;
        for (let i = 0; i < shorts.length; i++) {
          const sid = Number(shorts[i].id ?? i);
          if (sid === Number(shortId)) {
            shorts[i].youtube_url = data.url;
            shorts[i].youtube_video_id = data.video_id;
            shorts[i].youtube_privacy = data.privacy_status;
            break;
          }
        }
      }
      if (actions) {
        actions.innerHTML = `
          <a class="btn ghost btn-tiny" href="${escapeAttr(
            data.url
          )}" target="_blank" rel="noopener">Ver no YouTube</a>
          <button type="button" class="btn ghost btn-tiny yt-upload-btn" data-yt-upload="${escapeAttr(
            String(shortId)
          )}">Reenviar</button>
          <p class="hint yt-upload-hint">${escapeHtml(
            data.privacy_status
              ? `Publicado como ${data.privacy_status}`
              : "Enviado"
          )}</p>
        `;
      }
    } catch (err) {
      btn.disabled = false;
      btn.textContent = prevLabel;
      if (hint) {
        hint.hidden = false;
        hint.textContent = String(err.message || err);
      }
    }
  }

  $("#results")?.addEventListener("click", (ev) => {
    const btn = ev.target.closest?.("[data-yt-upload]");
    if (!btn || btn.disabled) return;
    ev.preventDefault();
    uploadShortToYoutube(btn.getAttribute("data-yt-upload"), btn);
  });

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
