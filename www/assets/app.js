/* BerylSolver Control Center — tab router, live solver stats, solve, history. */
(function () {
  const TITLES = {
    overview: ["Overview", "Pemantauan solver model secara real-time."],
    docs: ["Docs", "Dokumentasi endpoint API solver."],
    playground: ["Playground", "Uji solver langsung dari browser."],
    history: ["History", "Riwayat semua request solver."],
    models: ["Models", "Performa per model solver: berhasil, gagal, persen, ms."],
    engines: ["Engine", "Mesin stealth & mode solving."],
  };

  const $ = (id) => document.getElementById(id);

  function switchPage(name) {
    document.querySelectorAll(".side-link").forEach((a) => {
      const active = a.dataset.page === name;
      a.classList.toggle("active", active);
      if (active) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    document.querySelectorAll(".page").forEach((p) => {
      p.classList.toggle("hidden", p.id !== "page-" + name);
    });
    $("ptitle").textContent = TITLES[name][0];
    $("psub").textContent = TITLES[name][1];
  }

  function route() {
    const requested = location.hash.slice(1);
    const name = Object.hasOwn(TITLES, requested) ? requested : "overview";
    switchPage(name);
    if (["history", "overview", "models"].includes(name)) loadStats();
  }

  window.addEventListener("hashchange", route);

  // ── Stats / history rendering ─────────────────────────────
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>]/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }
  function badge(solved) {
    return solved ? '<span class="badge ok">OK</span>' : '<span class="badge fail">FAIL</span>';
  }
  function fmtMs(ms) {
    if (ms == null || isNaN(ms)) return "—";
    if (ms >= 1000) return (ms / 1000).toFixed(1) + "s";
    return Math.round(ms) + " ms";
  }

  function fillStats(d) {
    $("st-total").textContent = d.total;
    $("st-ok").textContent = d.ok;
    $("st-fail").textContent = d.fail;
    $("st-rate").textContent = Math.round(d.success_rate || 0) + "%";
    $("st-avg").textContent = fmtMs(d.avg_ms);
    $("st-max").textContent = fmtMs(d.max_ms);
    $("st-bar").style.width = (d.success_rate || 0) + "%";
    const up = d.uptime_s || 0;
    let upTxt;
    if (up >= 3600) upTxt = Math.floor(up / 3600) + "j " + Math.floor((up % 3600) / 60) + "m";
    else if (up >= 60) upTxt = Math.floor(up / 60) + "m " + Math.floor(up % 60) + "d";
    else upTxt = Math.floor(up) + "d";
    $("uptime").textContent = "up " + upTxt;
  }

  function fillOverviewTable(history) {
    $("ov-n").textContent = history.length + " entry";
    $("ov-body").innerHTML = history.slice(0, 10).map((h) =>
      "<tr><td>" + esc(h.time) + "</td><td><span class='tag cyan'>" + esc(h.type) +
      "</span></td><td>" + esc(h.engine) + "</td><td>" + esc(fmtMs(h.ms)) +
      "</td><td>" + badge(h.solved) + "</td></tr>").join("") ||
      "<tr><td colspan='5' class='muted'>Belum ada request.</td></tr>";
  }

  function fillHistoryTable(history) {
    $("hist-n").textContent = history.length + " entry";
    $("hist-body").innerHTML = history.map((h) =>
      "<tr><td>" + esc(h.time) + "</td><td><span class='tag cyan'>" + esc(h.type) +
      "</span></td><td>" + esc(h.engine) + "</td><td class='mono' style='font-size:.78rem'>" + esc(h.url) +
      "</td><td>" + esc(fmtMs(h.ms)) + "</td><td>" + (h.token ? "✔" : "—") +
      "</td><td>" + (h.cache ? "<span class='badge cyan'>cache</span> " : "") + badge(h.solved) + "</td></tr>").join("") ||
      "<tr><td colspan='7' class='muted'>Belum ada request.</td></tr>";
  }

  function fillModelsTable(models) {
    models = models || [];
    $("models-n").textContent = models.length + " model";
    $("models-body").innerHTML = models.map((m) =>
      "<tr><td><b>" + esc(m.type) + "</b></td><td>" + esc(m.engine) +
      "</td><td>" + m.total + "</td><td><span class='badge ok'>" + m.ok + "</span>" +
      "</td><td><span class='badge fail'>" + m.fail + "</span>" +
      "</td><td>" + Math.round(m.success_rate) + "%" +
      "</td><td>" + fmtMs(m.avg_ms) + "</td><td>" + fmtMs(m.max_ms) + "</td></tr>").join("") ||
      "<tr><td colspan='8' class='muted'>Belum ada data model.</td></tr>";
  }

  let statsLoading = false;
  let lastUpdated = null;

  async function loadStats() {
    if (statsLoading) return;
    statsLoading = true;
    const refresh = $("btn-refresh");
    refresh.disabled = true;
    refresh.textContent = "↻ Memuat…";
    const ctrl = new AbortController();
    const timeout = setTimeout(() => ctrl.abort(), 10000);
    try {
      const r = await fetch("/solver/stats", { signal: ctrl.signal });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      fillStats(d);
      fillOverviewTable(d.history || []);
      fillHistoryTable(d.history || []);
      fillModelsTable(d.models || []);
      lastUpdated = new Date().toLocaleTimeString("id-ID");
      $("stats-status").textContent = "Diperbarui: " + lastUpdated;
      $("stats-status").classList.remove("stats-error");
      $("connection-state").textContent = "Terhubung · live";
      $("stats-badge").textContent = "live";
      $("stats-badge").dataset.state = "live";
    } catch (e) {
      $("stats-status").textContent = "Gagal memuat statistik. " +
        (lastUpdated ? "Data terakhir: " + lastUpdated + ". " : "Data belum tersedia. ") +
        "Klik Refresh untuk mencoba lagi; otomatis dicoba tiap 5 detik.";
      $("stats-status").classList.add("stats-error");
      $("connection-state").textContent = "Statistik tidak tersedia";
      $("stats-badge").textContent = lastUpdated ? "Data lama" : "Tidak tersedia";
      $("stats-badge").dataset.state = "error";
    } finally {
      clearTimeout(timeout);
      statsLoading = false;
      refresh.disabled = false;
      refresh.textContent = "↻ Refresh";
    }
  }

  $("btn-refresh").addEventListener("click", loadStats);

  // ── Playground solve ─────────────────────────────────────
  const result = $("result");
  const btn = $("btn-solve");

  function render(obj, ok) {
    const txt = JSON.stringify(obj, null, 2);
    result.classList.remove("err");
    result.innerHTML = (ok ? '<span class="ok">✔ solved</span>\n'
      : '<span class="err">✖ ' + esc(obj.error || "request failed") + "</span>\n") + esc(txt);
  }

  btn.addEventListener("click", async () => {
    const type = $("f-type").value;
    const url = $("f-url").value.trim();
    const sitekey = $("f-sitekey").value.trim();
    const backend = $("f-backend").value;
    const payload = { type, url, backend };
    if (sitekey) payload.sitekey = sitekey;

    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span> Solving…';
    result.innerHTML = "// mengirim…";

    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 120000);
      const r = await fetch("/solve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: ctrl.signal,
      });
      clearTimeout(t);
      const data = await r.json();
      render(data, !!(r.ok && data.token && !data.test_key));
      setTimeout(loadStats, 300); // refresh live stats after solve
    } catch (e) {
      render({ error: "Jaringan gagal: " + e.message }, false);
    } finally {
      btn.disabled = false;
      btn.textContent = "▶ Solve";
    }
  });

  // ── Init ─────────────────────────────────────────────────
  route();
  loadStats();
  setInterval(loadStats, 5000); // live auto-refresh
})();
