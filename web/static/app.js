(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const state = {
    activeJobId: null,
    pollTimer: null,
    lastLogCount: 0,
  };

  /* ---------- Tabs ---------- */
  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const name = tab.dataset.tab;
      $$(".tab").forEach((t) => {
        t.classList.toggle("is-active", t === tab);
        t.setAttribute("aria-selected", t === tab ? "true" : "false");
      });
      $$(".panel").forEach((p) => {
        const on = p.id === `panel-${name}`;
        p.classList.toggle("is-active", on);
        p.hidden = !on;
      });
      if (name === "jobs") loadJobs();
      if (name === "config") loadConfig();
    });
  });

  /* ---------- Mode / upload ---------- */
  const modeEl = $("#mode");
  const uploadWrap = $("#upload-wrap");
  const fileInput = $("#file");
  const fileHint = $("#file-hint");

  function syncUploadState() {
    const local = modeEl.value === "local";
    uploadWrap.classList.toggle("is-disabled", !local);
    fileInput.disabled = !local;
  }
  modeEl.addEventListener("change", syncUploadState);
  syncUploadState();

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
    for (const item of data.items) {
      const wrap = document.createElement("label");
      wrap.className = "field config-item";
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
    // Remove empty file so FastAPI doesn't choke
    if (!fileInput.files?.length) fd.delete("file");
    if (!fd.get("language")) fd.set("language", "");

    try {
      const res = await fetch("/api/jobs", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      hint.textContent = `job ${data.id} na fila`;
      watchJob(data.id);
    } catch (err) {
      hint.textContent = `erro: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  });

  function watchJob(jobId) {
    state.activeJobId = jobId;
    state.lastLogCount = 0;
    $("#run-area").hidden = false;
    $("#active-job-title").textContent = jobId;
    $("#job-log").textContent = "";
    $("#results").innerHTML = "";
    setBadge("queued");
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
      if (job.status === "completed" && job.result) {
        renderResults(job);
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        loadJobs();
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
        ${shorts.length} shorts · ${job.result.highlights?.length || 0} candidatos
      </h2>
      <a class="btn ghost" href="/api/jobs/${job.id}/result.json" download>Baixar JSON</a>
    `;
    box.appendChild(head);

    shorts.forEach((s, i) => {
      const card = document.createElement("article");
      card.className = "short-card";
      card.style.animationDelay = `${i * 0.06}s`;
      const clip = s.clip_url || "";
      const isHttp = clip.startsWith("http");
      const videoSrc = isHttp ? clip : clip;
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
          <span class="hint">${j.shorts_count || 0} clips</span>
        `;
        row.addEventListener("click", () => {
          $$(".tab").find((t) => t.dataset.tab === "generate")?.click();
          watchJob(j.id);
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
})();
