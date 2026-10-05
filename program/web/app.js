/* BiliMusicTool 前端逻辑（原生 JS，无构建步骤） */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    results: [],
    selected: null,
    upload: null,     // {upload_id, name, size}
    jobId: null,
    pollTimer: null,
    busy: false,
    searching: false,
  };

  /* ------------------------------------------------------------------ */
  /* 通用工具                                                             */
  /* ------------------------------------------------------------------ */
  function toast(message, kind = "") {
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = message;
    $("toasts").appendChild(el);
    setTimeout(() => {
      el.style.transition = "opacity .3s ease, transform .3s ease";
      el.style.opacity = "0";
      el.style.transform = "translateX(24px)";
      setTimeout(() => el.remove(), 320);
    }, kind === "error" ? 7000 : 4200);
  }

  function setStatus(text, kind = "") {
    const el = $("search-status");
    if (!text) { el.hidden = true; return; }
    el.hidden = false;
    el.className = "status-line" + (kind ? " " + kind : "");
    el.textContent = text;
  }

  async function api(path, options = {}) {
    const res = await fetch(path, options);
    let data = null;
    try { data = await res.json(); } catch (_) { /* 非 JSON */ }
    if (!res.ok || (data && data.ok === false)) {
      throw new Error((data && data.error) || `请求失败（HTTP ${res.status}）`);
    }
    return data;
  }

  // timeoutMs > 0 时给请求加超时，超时抛 AbortError（调用方可据此给专门文案）
  function postJSON(path, body, timeoutMs = 0) {
    const init = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
    if (!timeoutMs) return api(path, init);
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    init.signal = ctrl.signal;
    return api(path, init).finally(() => clearTimeout(timer));
  }

  function humanSize(bytes) {
    if (!bytes && bytes !== 0) return "";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1048576).toFixed(1) + " MB";
  }

  function fmtDuration(seconds) {
    if (!seconds && seconds !== 0) return "—";
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  function fmtViews(n) {
    if (!n) return "—";
    if (n >= 100000000) return (n / 100000000).toFixed(1) + " 亿";
    if (n >= 10000) return (n / 10000).toFixed(1) + " 万";
    return String(n);
  }

  /* ------------------------------------------------------------------ */
  /* 环境状态                                                             */
  /* ------------------------------------------------------------------ */
  async function loadHealth() {
    try {
      const data = await api("/api/health");
      const ff = $("env-ffmpeg");
      ff.textContent = data.ffmpeg ? "ffmpeg ✓" : "ffmpeg 缺失";
      ff.className = "pill " + (data.ffmpeg ? "ok" : "bad");
      const yt = $("env-ytdlp");
      yt.textContent = "yt-dlp ✓";
      yt.className = "pill ok";
      yt.title = data.yt_dlp || "";
    } catch (err) {
      $("env-ffmpeg").textContent = "后端不可用";
      $("env-ffmpeg").className = "pill bad";
    }
  }

  /* ------------------------------------------------------------------ */
  /* 标签页                                                               */
  /* ------------------------------------------------------------------ */
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      $("panel-" + tab.dataset.tab).classList.add("active");
      updateStartButton();
    });
  });

  function activeTab() {
    return document.querySelector(".tab.active").dataset.tab;
  }

  /* ------------------------------------------------------------------ */
  /* 搜索                                                                 */
  /* ------------------------------------------------------------------ */
  // 搜索整体超时（前端兜底）。后端也有 45 秒超时，这里比它略长，
  // 好让用户看到后端给出的具体原因，而不是一个笼统的前端超时。
  const SEARCH_TIMEOUT_MS = 60000;

  $("search-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const keyword = $("keyword").value.trim();
    if (!keyword) return;
    if (state.searching) return;         // 连点保护
    state.searching = true;

    const btn = $("search-btn");
    btn.classList.add("loading");
    btn.disabled = true;
    $("results").innerHTML = "";
    state.results = [];
    state.selected = null;
    updateStartButton();

    // 带秒数的等待提示：让用户分得清"在跑"还是"卡死了"
    const t0 = Date.now();
    const tick = () => {
      const s = ((Date.now() - t0) / 1000).toFixed(0);
      setStatus(`正在搜索「${keyword}」… ${s}s` +
        (Number(s) > 8 ? "（较慢：B 站可能在限流，或网络不稳）" : ""));
    };
    tick();
    const timer = setInterval(tick, 500);

    try {
      const data = await postJSON("/api/search", {
        keyword,
        count: Number($("count").value),
        smart: $("smart").checked,
        cookies: $("cookies").value || null,
      }, SEARCH_TIMEOUT_MS);
      state.results = data.results || [];
      if (!state.results.length) {
        setStatus("没有搜到结果，换个关键词或换一个已登录的浏览器再试。", "error");
      } else {
        setStatus(`共 ${state.results.length} 条结果，用时 ${data.elapsed}s。点击卡片选中你要用的那一条。`, "ok");
      }
      renderResults();
    } catch (err) {
      const msg = err.name === "AbortError"
        ? `搜索超过 ${SEARCH_TIMEOUT_MS / 1000} 秒仍未返回，已取消。请稍后重试，或换更短的关键词。`
        : err.message;
      setStatus(msg, "error");
      toast(msg, "error");
    } finally {
      clearInterval(timer);
      state.searching = false;
      btn.classList.remove("loading");
      btn.disabled = false;
    }
  });

  /* ------------------------------------------------------------------ */
  /* Cookie 可用性检查                                                    */
  /* ------------------------------------------------------------------ */
  $("cookies-check").addEventListener("click", async () => {
    const browser = $("cookies").value;
    const note = $("cookies-note");
    if (!browser) {
      note.hidden = false;
      note.className = "cookie-note warn";
      note.textContent = "先在左边选一个浏览器，再点「检查」。";
      return;
    }
    const btn = $("cookies-check");
    btn.classList.add("loading", "ghost");
    btn.disabled = true;
    note.hidden = false;
    note.className = "cookie-note";
    note.textContent = `正在检查 ${browser} 的 Cookie …`;
    try {
      const data = await postJSON("/api/cookies", { browser });
      const r = data.result;
      note.hidden = false;
      if (r.ok && r.has_login) {
        note.className = "cookie-note ok";
        note.textContent = `${r.reason}。会员/受限视频可以正常下载了。`;
      } else if (r.ok) {
        note.className = "cookie-note warn";
        note.textContent = `${r.reason}`;
        if (r.advice) note.insertAdjacentHTML("beforeend",
          `<span class="advice">建议：${r.advice}</span>`);
      } else {
        note.className = "cookie-note bad";
        note.textContent = `不可用：${r.reason}`;
        if (r.advice) note.insertAdjacentHTML("beforeend",
          `<span class="advice">建议：${r.advice}</span>`);
        note.insertAdjacentHTML("beforeend",
          `<span class="advice">程序会自动改用匿名方式下载，普通视频不受影响。</span>`);
      }
    } catch (err) {
      note.className = "cookie-note bad";
      note.textContent = "检查失败：" + err.message;
    } finally {
      btn.classList.remove("loading");
      btn.disabled = false;
    }
  });

  // 切换浏览器时清掉上一次的检查结论，避免误以为还是同一个浏览器的结果
  $("cookies").addEventListener("change", () => {
    $("cookies-note").hidden = true;
  });

  function renderResults() {
    const wrap = $("results");
    wrap.innerHTML = "";
    state.results.forEach((item) => {
      const card = document.createElement("div");
      card.className = "card";
      card.dataset.videoId = item.video_id;

      const thumb = document.createElement("div");
      thumb.className = "card-thumb";
      if (item.thumbnail) {
        const img = document.createElement("img");
        img.loading = "lazy";
        img.referrerPolicy = "no-referrer";
        img.src = item.thumbnail;
        img.alt = "";
        img.addEventListener("error", () => { img.remove(); showFallback(thumb); });
        thumb.appendChild(img);
      } else {
        showFallback(thumb);
      }
      const dur = document.createElement("span");
      dur.className = "card-dur";
      dur.textContent = item.duration_text || "?:??";
      thumb.appendChild(dur);
      const rank = document.createElement("span");
      rank.className = "card-rank";
      rank.textContent = `#${item.index}`;
      thumb.appendChild(rank);

      const body = document.createElement("div");
      body.className = "card-body";
      const title = document.createElement("div");
      title.className = "card-title";
      title.textContent = item.title || "(无标题)";
      title.title = item.title || "";
      const meta = document.createElement("div");
      meta.className = "card-meta";
      const up = document.createElement("span");
      up.textContent = item.uploader || "未知 UP";
      const views = document.createElement("span");
      views.className = "views";
      views.textContent = fmtViews(item.view_count);
      const id = document.createElement("span");
      id.textContent = item.video_id;
      meta.append(up, views, id);
      body.append(title, meta);
      card.append(thumb, body);

      card.addEventListener("click", () => selectResult(item, card));
      wrap.appendChild(card);
    });
  }

  function showFallback(container) {
    const fb = document.createElement("div");
    fb.className = "fallback";
    fb.textContent = "无封面";
    container.appendChild(fb);
  }

  function selectResult(item, card) {
    document.querySelectorAll(".card").forEach((c) => c.classList.remove("selected"));
    card.classList.add("selected");
    state.selected = item;
    updateStartButton();
    $("settings").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  /* ------------------------------------------------------------------ */
  /* 本地文件                                                             */
  /* ------------------------------------------------------------------ */
  const dz = $("dropzone");

  dz.addEventListener("click", () => $("file-input").click());
  dz.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("file-input").click(); }
  });
  $("file-input").addEventListener("change", (e) => {
    if (e.target.files && e.target.files[0]) uploadFile(e.target.files[0]);
  });

  ["dragenter", "dragover"].forEach((evt) =>
    dz.addEventListener(evt, (e) => { e.preventDefault(); dz.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((evt) =>
    dz.addEventListener(evt, (e) => { e.preventDefault(); dz.classList.remove("dragover"); }));
  dz.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) uploadFile(file);
  });

  async function uploadFile(file) {
    const box = $("dz-file");
    box.hidden = false;
    box.innerHTML = "";
    box.append(Object.assign(document.createElement("span"), { textContent: "上传中…" }));
    state.upload = null;
    updateStartButton();

    const form = new FormData();
    form.append("file", file, file.name);
    try {
      const res = await fetch("/api/upload", { method: "POST", body: form });
      const data = await res.json();
      if (!res.ok || data.ok === false) throw new Error((data && data.error) || "上传失败");

      state.upload = data;
      box.innerHTML = "";
      const name = document.createElement("span");
      name.textContent = data.name;
      const size = document.createElement("span");
      size.className = "size";
      size.textContent = humanSize(data.size);
      box.append(name, size);
      updateStartButton();
      toast("文件已就绪：" + data.name, "ok");
      // 给个合理的默认标题（去掉扩展名）
      if (!$("settings-target").dataset.locked) {
        $("settings-target").textContent = "本地文件：" + data.name.replace(/\.[^.]+$/, "");
      }
    } catch (err) {
      box.innerHTML = "";
      box.append(Object.assign(document.createElement("span"), { textContent: "上传失败" }));
      toast(err.message, "error");
    }
  }

  /* ------------------------------------------------------------------ */
  /* 设置与启动                                                           */
  /* ------------------------------------------------------------------ */
  $("bpm-mode").addEventListener("change", () => {
    $("bpm-manual-wrap").hidden = $("bpm-mode").value !== "manual";
  });

  function updateStartButton() {
    const tab = activeTab();
    const ready = tab === "search" ? !!state.selected : !!state.upload;
    $("start-btn").disabled = !ready || state.busy;
    const target = $("settings-target");
    if (tab === "search") {
      target.textContent = state.selected
        ? `已选择：${state.selected.title}`
        : "先搜索并选择一首歌";
    } else if (state.upload) {
      target.textContent = `本地文件：${state.upload.name}`;
    } else {
      target.textContent = "先上传一个音乐文件";
    }
  }

  $("start-btn").addEventListener("click", async () => {
    const tab = activeTab();
    if (tab === "search" && !state.selected) return toast("请先选择一条搜索结果", "error");
    if (tab === "local" && !state.upload) return toast("请先上传音乐文件", "error");

    const manualMode = $("bpm-mode").value === "manual";
    const manualBpm = manualMode ? Number($("bpm-manual").value) : null;
    if (manualMode && (!manualBpm || manualBpm < 30 || manualBpm > 300)) {
      return toast("请输入 30–300 之间的 BPM", "error");
    }

    const common = {
      beats_per_bar: Number($("beats-per-bar").value),
      manual_bpm: manualBpm,
      keep_source: $("keep-source").checked,
    };

    const payload = tab === "search"
      ? Object.assign({}, common, {
          video_id: state.selected.video_id,
          url: state.selected.url,
          title: state.selected.title,
          uploader: state.selected.uploader,
          duration: state.selected.duration,
          view_count: state.selected.view_count,
          thumbnail: state.selected.thumbnail,
          index: state.selected.index,
          keyword: $("keyword").value.trim(),
          cookies: $("cookies").value || null,
        })
      : Object.assign({}, common, {
          upload_id: state.upload.upload_id,
          title: state.upload.name.replace(/\.[^.]+$/, ""),
        });

    setBusy(true);
    try {
      const data = await postJSON(tab === "search" ? "/api/download" : "/api/process", payload);
      state.jobId = data.job.id;
      renderJob(data.job);
      startPolling();
    } catch (err) {
      toast(err.message, "error");
      setBusy(false);
    }
  });

  $("cancel-btn").addEventListener("click", async () => {
    if (!state.jobId) return;
    try {
      await postJSON(`/api/job/${state.jobId}/cancel`, {});
      toast("已发送取消请求");
    } catch (err) {
      toast(err.message, "error");
    }
  });

  function setBusy(busy) {
    state.busy = busy;
    $("start-btn").disabled = busy || (activeTab() === "search" ? !state.selected : !state.upload);
    $("cancel-btn").hidden = !busy;
  }

  /* ------------------------------------------------------------------ */
  /* 任务轮询与渲染                                                       */
  /* ------------------------------------------------------------------ */
  function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(pollJob, 800);
    pollJob();
  }

  function stopPolling() {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }

  async function pollJob() {
    if (!state.jobId) return;
    try {
      const data = await api(`/api/job/${state.jobId}`);
      renderJob(data.job);
      if (["done", "error", "cancelled"].includes(data.job.status)) {
        stopPolling();
        setBusy(false);
        if (data.job.status === "done") toast("处理完成！", "ok");
        if (data.job.status === "error") toast("处理失败：" + (data.job.error || ""), "error");
      }
    } catch (err) {
      stopPolling();
      setBusy(false);
    }
  }

  const BADGE_TEXT = {
    queued: "排队中", running: "处理中", done: "已完成",
    error: "失败", cancelled: "已取消",
  };

  function renderJob(job) {
    $("job").hidden = false;
    $("job-title").textContent = job.title || "处理任务";
    const badge = $("job-badge");
    badge.textContent = BADGE_TEXT[job.status] || job.status;
    badge.className = "badge " + job.status;

    const bar = $("progress-bar");
    if (job.status === "running" || job.status === "queued") {
      bar.classList.add("indeterminate");
      bar.style.width = "";
    } else {
      bar.classList.remove("indeterminate");
      bar.style.width = (job.status === "done" ? 100 : Math.round((job.progress || 0) * 100)) + "%";
    }
    if (job.status === "done") bar.style.width = "100%";

    $("job-stage").textContent = job.error ? ("失败原因：" + job.error) : (job.stage || "");

    renderSummary(job);
    renderArtifacts(job);

    const log = $("job-log");
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
    log.textContent = (job.log || []).join("\n");
    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  function renderSummary(job) {
    const wrap = $("job-summary");
    wrap.innerHTML = "";
    const s = job.summary || {};
    if (!s.bpm) return;

    const bpmText = Number.isInteger(s.bpm) ? String(s.bpm) : s.bpm.toFixed(2);

    addStat("BPM", bpmText, s.bpm_quantized_from
      ? `已取整（原值 ${s.bpm_quantized_from}）`
      : (s.bpm_source === "manual" ? "手动指定" : "本地分析"));

    addStat("每拍", (s.beat_seconds != null ? s.beat_seconds.toFixed(4) : "—") + " s",
      `一小节空隙 ${s.gap_seconds} s`);

    if (s.output_duration != null) {
      addStat("输出时长", fmtDuration(s.output_duration),
        s.source_duration != null ? `源 ${fmtDuration(s.source_duration)}` : "");
    }
    if (s.bpm_confidence != null) {
      addStat("置信度", Number(s.bpm_confidence).toFixed(2), "本地分析");
    }
    if (s.bpm_candidates && s.bpm_candidates.length) {
      addStat("候选速度", s.bpm_candidates.join(" / "), "", true);
    }
    if (s.bpm_reason) {
      addStat("判定依据", s.bpm_reason, "", true);
    }
  }

  function addStat(k, v, extra = "", small = false) {
    const box = document.createElement("div");
    box.className = "stat";
    const kk = document.createElement("span");
    kk.className = "k";
    kk.textContent = k;
    const vv = document.createElement("span");
    vv.className = "v" + (small ? " small" : "");
    vv.textContent = v;
    box.append(kk, vv);
    if (extra) {
      const e = document.createElement("span");
      e.className = "extra";
      e.textContent = extra;
      box.appendChild(e);
    }
    $("job-summary").appendChild(box);
  }

  const KIND_ICON = { main: "♪", audio: "♫", cover: "▣", manifest: "☰" };

  function renderArtifacts(job) {
    const wrap = $("job-artifacts");
    const existing = wrap.querySelectorAll(".artifact").length;
    wrap.innerHTML = "";
    const list = job.artifacts_list || [];
    // artifacts_list 由后端按需生成；这里回退到 job.artifacts 的原始结构
    const items = list.length ? list : (job.artifacts || []).map((a) => ({
      kind: a.kind, label: a.label, name: a.name, size: null,
      preview: a.kind === "main" || a.kind === "audio",
      url: `/api/file?job=${job.id}&kind=${a.kind}`,
      download_url: `/api/file?job=${job.id}&kind=${a.kind}&dl=1`,
    }));

    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "artifact";

      if (item.kind === "cover") {
        const img = document.createElement("img");
        img.className = "cover-thumb";
        img.src = item.url;
        img.alt = "曲绘";
        row.appendChild(img);
      } else {
        const icon = document.createElement("div");
        icon.className = "icon";
        icon.textContent = KIND_ICON[item.kind] || "•";
        row.appendChild(icon);
      }

      const info = document.createElement("div");
      info.className = "info";
      const label = document.createElement("div");
      label.className = "label";
      label.textContent = item.label || item.name;
      const fname = document.createElement("div");
      fname.className = "fname";
      fname.textContent = item.name + (item.size ? ` · ${humanSize(item.size)}` : "");
      info.append(label, fname);
      row.appendChild(info);

      const links = document.createElement("div");
      links.className = "links";
      const dl = document.createElement("a");
      dl.className = "btn tiny";
      dl.href = item.download_url;
      dl.textContent = "下载";
      links.appendChild(dl);
      row.appendChild(links);

      if (item.preview) {
        const box = document.createElement("div");
        box.className = "audio-preview";
        const audio = document.createElement("audio");
        audio.controls = true;
        audio.preload = "none";
        audio.src = item.url;
        box.appendChild(audio);
        row.appendChild(box);
      }
      wrap.appendChild(row);
    });
    if (existing && !items.length) wrap.innerHTML = "";
  }

  /* ------------------------------------------------------------------ */
  /* 初始化                                                               */
  /* ------------------------------------------------------------------ */
  loadHealth();

  // 开发便利：?demo=1 用假数据渲染结果区，方便调样式（不影响真实流程）
  const params = new URLSearchParams(location.search);
  if (params.get("demo") === "1") {
    state.results = [
      { index: 1, video_id: "BV1sM4y1V7x1", id_kind: "BV", title: "【4K超高清】千本桜原版MV feat 初音ミク",
        uploader: "SILVIA____", duration: 245, duration_text: "4:05", view_count: 9938077,
        url: "", thumbnail: "https://i1.hdslb.com/bfs/archive/d2003cf57978aa71231fc6ce2ff9a0b2a1119c89.jpg" },
      { index: 2, video_id: "BV1STHY6AEEe", id_kind: "BV", title: "【二创投稿】镜音连调 千本桜【千本樱】第84版 feat.初音ミク",
        uploader: "月下的樱雪", duration: 245, duration_text: "4:05", view_count: 910,
        url: "", thumbnail: null },
      { index: 3, video_id: "BV1jfu26QEG8", id_kind: "BV", title: "【钢琴】千本樱（这个强度你受得了吗）",
        uploader: "喵喵Aileen", duration: 69, duration_text: "1:09", view_count: 7577668,
        url: "", thumbnail: "https://i1.hdslb.com/bfs/archive/d2003cf57978aa71231fc6ce2ff9a0b2a1119c89.jpg" },
    ];
    renderResults();
    setStatus("演示数据：这是结果卡片的外观预览（点击卡片右下的按钮才是真实操作）。", "ok");
  }

  // 开发便利：?e2e=1&q=歌名 自动走完整流程（搜索 -> 选中第一条 -> 开始处理），
  // 便于用无头浏览器截图验证真实交互链路。
  if (params.get("e2e") === "1") {
    const q = params.get("q") || "千本樱";
    $("keyword").value = q;
    setTimeout(() => {
      $("search-form").dispatchEvent(new Event("submit", { cancelable: true }));
      const timer = setInterval(() => {
        const card = document.querySelector(".card");
        if (card) {
          clearInterval(timer);
          card.click();
          setTimeout(() => $("start-btn").click(), 250);
        }
      }, 400);
    }, 200);
  }

  // 开发便利：?job=<id> 直接载入某个已完成任务的完成态界面
  if (params.get("job")) {
    setTimeout(async () => {
      try {
        const data = await api(`/api/job/${encodeURIComponent(params.get("job"))}`);
        state.jobId = data.job.id;
        renderJob(data.job);
        if (data.job.status === "running") startPolling();
      } catch (err) {
        toast(err.message, "error");
      }
    }, 200);
  }

  updateStartButton();
})();
