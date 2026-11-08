let DATA = { rows: [], qualitative: {}, notes: {}, flag_counts: {}, flag_meanings: {}, metric_labels: {}, ranking_modes: {}, ranking_locked: [] };
let sortKey = "overall_rank", sortDir = 1, selectedTicker = null, activeFlagChip = "";
const METRIC_COLS = ["pe_ratio", "pb_ratio", "p_dcf", "debt_to_equity", "reinvestment_efficiency", "roe"];

// One switch per metric drives both its ranking AND its column display:
// on = peer z-score, off = absolute value. There used to be a separate
// global "Peer z-scores" display toggle, but it meant the Rank-by switches
// changed the ranking without changing what the columns showed.
function displayZ(col) { return (DATA.ranking_modes || {})[col] === "relative"; }

const $ = (id) => document.getElementById(id);
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toFixed(d));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function qualFor(ticker) { return DATA.qualitative[ticker] || null; }
function notesFor(ticker) { return DATA.notes[ticker] || []; }

function sortVal(r, key) {
  // A metric column sorts by what it shows: its peer z-score when its
  // switch is on (highest = best first), its raw value when off.
  if (METRIC_COLS.includes(key) && displayZ(key)) return r.zscores?.[key]?.z ?? null;
  return r[key];
}

function filteredRows() {
  const q = $("searchInput").value.trim().toUpperCase();
  const sector = $("sectorFilter").value, status = $("statusFilter").value;
  const flag = $("flagFilter").value || activeFlagChip;
  return DATA.rows.filter((r) => {
    if (q && !r.ticker.includes(q)) return false;
    if (sector && r.sector !== sector) return false;
    if (status === "ranked" && !r.is_ranked) return false;
    if (status && status !== "ranked" && r.queue_status !== status) return false;
    if (flag && !r.flags.includes(flag)) return false;
    if ($("hasFlagsOnly").checked && !r.flags.length) return false;
    if ($("staleOnly").checked && !r.is_stale) return false;
    if ($("reviewOnly").checked && !notesFor(r.ticker).length) return false;
    return true;
  }).sort((a, b) => {
    const av = sortVal(a, sortKey), bv = sortVal(b, sortKey);
    if (av === null || av === undefined) return 1;
    if (bv === null || bv === undefined) return -1;
    return (av < bv ? -1 : av > bv ? 1 : 0) * sortDir;
  });
}

function zClass(z) { return z > 0.05 ? "z-pos" : z < -0.05 ? "z-neg" : ""; }
function zText(z) { return (z >= 0 ? "+" : "") + z.toFixed(2) + "σ"; }

function metricCell(r, col, decimals = 2) {
  const raw = r[col];
  // P/DCF and reinvestment efficiency are absolute only: they never show a
  // z-score, whatever the display toggle says (and the server no longer
  // computes one for them either -- this guard is belt and braces).
  const locked = (DATA.ranking_locked || []).includes(col);
  const z = locked ? null : r.zscores && r.zscores[col];
  if (displayZ(col) && z) {
    const rawTxt = col === "roe" && raw != null ? (raw * 100).toFixed(1) + "%" : fmt(raw, decimals);
    return `<td class="num ${zClass(z.z)}" title="Raw ${rawTxt} · ${z.scope} peers: mean ${fmt(z.peer_mean, decimals)} ± ${fmt(z.peer_std, decimals)} (n=${z.n})">${zText(z.z)}</td>`;
  }
  if (col === "roe") return `<td class="num">${raw == null ? "—" : (raw * 100).toFixed(1) + "%"}</td>`;
  return `<td class="num">${fmt(raw, decimals)}</td>`;
}

function peerZCell(r) {
  const z = r.peer_z_avg;
  if (z == null) return `<td class="num">—</td>`;
  return `<td class="num ${zClass(z)}" title="Mean peer z-score across this ticker's available metrics">${zText(z)}</td>`;
}

function renderOverview() {
  const rows = DATA.rows;
  const ranked = rows.filter((r) => r.is_ranked).length;
  const byStatus = (s) => rows.filter((r) => r.queue_status === s).length;
  const flagged = rows.filter((r) => r.flags.length).length;
  const stale = rows.filter((r) => r.is_stale).length;
  const reviewed = Object.keys(DATA.notes).length;
  $("overview").innerHTML = [
    [ranked, "ranked"], [rows.length, "in queue / stats"], [byStatus("success"), "success"],
    [byStatus("partial"), "partial"], [byStatus("failed"), "failed"], [byStatus("queued"), "queued"],
    [flagged, "rows with flags"], [stale, "stale (>45d)"], [reviewed, "tickers you flagged"],
  ].map(([n, label]) => `<div class="card"><b>${n}</b><span>${label}</span></div>`).join("");
}

function renderFilters() {
  // Live refreshes rebuild these lists as new sectors/flags appear; keep
  // whatever the user had selected so a refresh never resets their filters.
  const keep = { sector: $("sectorFilter").value, status: $("statusFilter").value, flag: $("flagFilter").value };
  const sectors = [...new Set(DATA.rows.map((r) => r.sector).filter(Boolean))].sort();
  $("sectorFilter").innerHTML = '<option value="">All sectors</option>' + sectors.map((s) => `<option>${esc(s)}</option>`).join("");
  const flags = Object.entries(DATA.flag_counts).sort((a, b) => b[1] - a[1]);
  $("flagFilter").innerHTML = '<option value="">All flags</option>' + flags.map(([f, n]) => `<option value="${esc(f)}">${esc(f)} (${n})</option>`).join("");
  if (sectors.includes(keep.sector)) $("sectorFilter").value = keep.sector;
  $("statusFilter").value = keep.status;
  if (Object.keys(DATA.flag_counts).includes(keep.flag)) $("flagFilter").value = keep.flag;
  $("flagChips").innerHTML = flags.map(([f, n]) =>
    `<button type="button" class="chip${activeFlagChip === f ? " active" : ""}" data-flag="${esc(f)}">${esc(f)} · ${n}</button>`).join("");
  document.querySelectorAll("#flagChips .chip").forEach((chip) => chip.addEventListener("click", () => {
    activeFlagChip = activeFlagChip === chip.dataset.flag ? "" : chip.dataset.flag;
    $("flagFilter").value = activeFlagChip; render();
  }));
}

function renderRankingControls() {
  const modes = DATA.ranking_modes || {};
  const locked = DATA.ranking_locked || [];
  const switchable = METRIC_COLS.filter((c) => !locked.includes(c));
  // A real switch per metric, with its current state spelled out: on means
  // the metric is ranked AND shown by its peer z-score, off means absolute.
  const parts = switchable.map((c) => {
    const on = modes[c] === "relative";
    return `<label class="switch" title="On: ranked and shown by peer z-score. Off: ranked and shown by absolute value."><input type="checkbox" data-mode="${esc(c)}" ${on ? "checked" : ""}><span class="slider"></span><span>${esc(DATA.metric_labels[c] || c)}: <b>${on ? "peer z" : "absolute"}</b></span></label>`;
  });
  locked.forEach((c) => parts.push(`<span class="muted">${esc(DATA.metric_labels[c] || c)}: absolute only</span>`));
  $("rankingControls").innerHTML = "<b>Rank by:</b> " + parts.join(" ");
  document.querySelectorAll("#rankingControls input[data-mode]").forEach((box) => box.addEventListener("change", async () => {
    const next = { ...modes };
    document.querySelectorAll("#rankingControls input[data-mode]").forEach((b) => { next[b.dataset.mode] = b.checked ? "relative" : "absolute"; });
    await fetch("/api/ranking-modes", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ modes: next }) });
    await load();
  }));
}

function renderReviewList() {
  const entries = Object.entries(DATA.notes).sort((a, b) => b[1].length - a[1].length);
  $("reviewCount").textContent = entries.length ? `(${entries.length} tickers)` : "";
  if (!entries.length) { $("reviewList").innerHTML = '<p class="muted">Nothing flagged yet.</p>'; return; }
  $("reviewList").innerHTML = entries.map(([ticker, notes]) =>
    `<div class="note-item"><b><a href="#" data-ticker="${esc(ticker)}">${esc(ticker)}</a></b> — ${notes.length} note${notes.length > 1 ? "s" : ""}: ` +
    notes.map((n) => esc(n.note)).join(" | ") + "</div>").join("");
  document.querySelectorAll("#reviewList a").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); selectTicker(a.dataset.ticker); }));
}

function renderTable() {
  const rows = filteredRows();
  $("resultCount").textContent = `${rows.length} of ${DATA.rows.length} tickers`;
  $("stocksBody").innerHTML = rows.map((r) => {
    const q = qualFor(r.ticker);
    const qual = q ? `${q.management ? fmt(q.management.management_composite, 1) + "/5" : "—"} mgmt · ${q.macro ? fmt(q.macro.macro_score, 1) + "/10" : "—"} macro` : "—";
    const badFlags = ["pdcf_out_of_range", "neg_equity", "sec_fetch_failed", "no_recent_10k", "dcf_nonpositive_fcf"];
    const flagHtml = r.flags.map((f) => `<span class="flag${badFlags.includes(f) ? " bad" : ""}" title="${esc(DATA.flag_meanings[f] || "")}">${esc(f)}</span>`).join("") || "—";
    return `<tr data-ticker="${esc(r.ticker)}" class="${selectedTicker === r.ticker ? "selected" : ""}">
      <td>${r.overall_rank ?? "—"}</td><td><b>${esc(r.ticker)}</b></td><td>${esc(r.sector || "—")}</td>
      <td class="num">${fmt(r.composite_score, 3)}</td>${peerZCell(r)}${metricCell(r, "pe_ratio")}${metricCell(r, "pb_ratio")}${metricCell(r, "p_dcf")}${metricCell(r, "debt_to_equity")}${metricCell(r, "reinvestment_efficiency")}${metricCell(r, "roe")}
      <td class="num">${r.metrics_present}/6</td><td>${esc(r.date || "—")}${r.is_stale ? " ⚠ stale" : ""}</td>
      <td>${flagHtml}</td><td>${qual}</td><td>${notesFor(r.ticker).length ? "📝 " + notesFor(r.ticker).length : ""}</td></tr>`;
  }).join("") || '<tr><td colspan="16" class="muted">No tickers match these filters.</td></tr>';
  document.querySelectorAll("#stocksBody tr[data-ticker]").forEach((tr) =>
    tr.addEventListener("click", () => selectTicker(tr.dataset.ticker)));
}

let lastDetailKey = "";
function renderDetail(scroll = true) {
  const r = DATA.rows.find((x) => x.ticker === selectedTicker);
  if (!r) { $("detail").classList.add("hidden"); lastDetailKey = ""; return; }
  lastDetailKey = JSON.stringify(r) + "|" + notesFor(r.ticker).length;
  $("detail").classList.remove("hidden");
  $("detailTitle").textContent = `${r.ticker} — ${r.sector || "no sector"} · rank ${r.overall_rank ?? "unranked"} · ${r.queue_status}`;
  const q = qualFor(r.ticker);
  const metricCells = Object.keys(DATA.metric_labels).map((c) => {
    const raw = c === "roe" && r.roe !== null && r.roe !== undefined ? (r.roe * 100).toFixed(1) + "%" : fmt(r[c]);
    const locked = (DATA.ranking_locked || []).includes(c);
    const z = locked ? null : r.zscores && r.zscores[c];
    // The detail view follows the same switch as the column: the z-score
    // line only shows while this metric's switch is on.
    const zLine = locked
      ? `<br><span>absolute only — no z-score for this metric</span>`
      : !displayZ(c)
      ? `<br><span>ranked and shown by absolute value — flip its switch for the peer z-score</span>`
      : z
      ? `<br><span class="${zClass(z.z)}">${zText(z.z)} vs ${esc(z.scope)}</span><span> — peers ${fmt(z.peer_mean)} ± ${fmt(z.peer_std)} (n=${z.n})</span>`
      : `<br><span>no peer z-score (too few peers with this metric)</span>`;
    return `<div><b>${raw}</b><span>${esc(DATA.metric_labels[c])}${r[c] === null || r[c] === undefined ? " — missing" : ""}${zLine}</span></div>`;
  }).join("");
  const peerAvg = r.peer_z_avg == null ? "—" : `<span class="${zClass(r.peer_z_avg)}">${zText(r.peer_z_avg)}</span>`;
  const flagItems = r.flags.length ? r.flags.map((f) =>
    `<li><b>${esc(f)}</b> — ${esc(DATA.flag_meanings[f] || "No explanation recorded.")}</li>`).join("") : "<li>No flags. Missing metrics are still worth a glance.</li>";
  let qualHtml = "<p class='muted'>No qualitative score yet — run Agent_Wardrobe for this ticker.</p>";
  if (q) {
    const m = q.management, mc = q.macro;
    qualHtml = "";
    if (m) qualHtml += `<h3>Management — ${fmt(m.management_composite, 2)}/5 (${esc(m.model_used || "")}, ${esc((m.as_of || "").slice(0, 10))})</h3>
      <p>Rationale ${fmt(m.rationale_score, 1)}/5 — ${esc(m.rationale_justification || "")}</p>
      <p>Transparency ${fmt(m.transparency_score, 1)}/5 — ${esc(m.transparency_justification || "")}</p>
      <p>Credibility ${fmt(m.credibility_score, 1)}/5 — ${esc(m.credibility_justification || "")}</p>`;
    if (mc) qualHtml += `<h3>Macro — ${fmt(mc.macro_score, 1)}/10 (${esc(mc.model_used || "")}, ${esc((mc.as_of || "").slice(0, 10))})</h3>
      <p>${esc(mc.macro_justification || "")}</p>
      <p>Tailwinds: ${esc((mc.tailwinds || []).join("; ") || "—")}<br>Headwinds: ${esc((mc.headwinds || []).join("; ") || "—")}</p>`;
  }
  $("detailBody").innerHTML = `
    <div class="kv">${metricCells}
      <div><b>${fmt(r.composite_score, 3)}</b><span>Composite score (lower is better) — live, from the ranking toggles</span></div>
      <div><b>${peerAvg}</b><span>Mean peer z-score (positive = better than peers)</span></div>
      <div><b>${r.stored_overall_rank ?? "—"}</b><span>Stored Stage 2 rank${r.stored_composite_score != null ? ` (score ${fmt(r.stored_composite_score, 3)})` : ""}</span></div>
      <div><b>${esc(r.fiscal_year_end || "—")}</b><span>Latest fiscal year end</span></div>
      <div><b>${esc(r.date || "—")}</b><span>Evaluated${r.age_days !== null && r.age_days !== undefined ? ` (${r.age_days}d ago)` : ""}</span></div>
    </div>
    <h3>Data flags — what they mean</h3><ul class="flag-list">${flagItems}</ul>
    <p class="muted">Missing metrics: ${r.missing_metrics.length ? r.missing_metrics.map((c) => DATA.metric_labels[c]).join(", ") : "none"}.
    ${r.queue_status === "failed" ? "This ticker failed evaluation — check the flags above, then re-queue it with initial_screen.py --requeue failed." : ""}
    ${r.queue_status === "partial" ? "Partial: too few metrics to rank." : ""}</p>
    <p><a href="https://finance.yahoo.com/quote/${esc(r.ticker)}" target="_blank" rel="noopener">Yahoo Finance: ${esc(r.ticker)}</a> ·
    <a href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&ticker=${esc(r.ticker)}" target="_blank" rel="noopener">SEC EDGAR filings</a></p>
    ${qualHtml}`;
  $("noteMetric").innerHTML = '<option value="">Whole row</option>' + Object.entries(DATA.metric_labels).map(([c, l]) => `<option value="${c}">${esc(l)}</option>`).join("");
  const notes = notesFor(r.ticker);
  $("detailNotes").innerHTML = notes.length ? "<h3>Notes on this ticker</h3>" + notes.map((n) =>
    `<div class="note-item sev-${esc(n.severity)}"><div class="meta">${esc(n.created)} · ${esc(n.severity.replace("_", " "))}${n.metric ? " · " + esc(DATA.metric_labels[n.metric] || n.metric) : ""}</div>${esc(n.note)}
    <div><button type="button" data-del="${esc(n.id)}">Delete</button></div></div>`).join("") : "";
  document.querySelectorAll("#detailNotes [data-del]").forEach((b) => b.addEventListener("click", async () => {
    await fetch("/api/notes/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ticker: r.ticker, id: b.dataset.del }) });
    await load();
  }));
  if (scroll) $("detail").scrollIntoView({ behavior: "smooth", block: "start" });
}

// Live refresh: re-render the open detail panel only if that ticker's data
// actually changed, and never while the user is typing a note in it.
function refreshDetailLive() {
  if (!selectedTicker) return;
  const noteInput = $("noteText");
  if (document.activeElement === noteInput && noteInput.value.trim()) return;
  const r = DATA.rows.find((x) => x.ticker === selectedTicker);
  if (!r) return;
  const key = JSON.stringify(r) + "|" + notesFor(r.ticker).length;
  if (key !== lastDetailKey) renderDetail(false);
}

function selectTicker(ticker) { selectedTicker = ticker; renderTable(); renderDetail(); }

function render() { renderOverview(); renderReviewList(); renderTable(); renderDetail(); }

function exportCSV() {
  const rows = filteredRows();
  const cols = ["overall_rank", "ticker", "sector", "industry", "composite_score", "peer_z_avg", "pe_ratio", "pb_ratio", "p_dcf", "debt_to_equity", "reinvestment_efficiency", "roe", "metrics_present", "date", "queue_status"];
  const head = cols.join(",");
  const lines = rows.map((r) => cols.map((c) => { const v = r[c]; return v === null || v === undefined ? "" : `"${String(v).replace(/"/g, '""')}"`; }).join(","));
  const blob = new Blob([[head, ...lines].join("\n")], { type: "text/csv" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
  a.download = "valuproj-ranked.csv"; a.click(); URL.revokeObjectURL(a.href);
}

async function load(live = false) {
  // A failed /api/overview used to leave the page silently blank (the
  // Decimal crash on 2026-11-06 only showed in the VS Code terminal).
  // Surface it in the banner instead so a refresh that does nothing is
  // visibly an error, not a wait.
  let res;
  try {
    res = await fetch("/api/overview");
    if (!res.ok) throw new Error("server returned HTTP " + res.status);
    DATA = await res.json();
  } catch (e) {
    const banner = $("banner");
    banner.textContent = "Could not load data from the dashboard server (" + e.message + "). "
      + "Check the VS Code terminal where app.py is running for the traceback, then tell Marlon what it says.";
    banner.classList.remove("hidden", "info");
    return;
  }
  $("generatedAt").textContent = "Updated " + (DATA.generated_at || "");
  const banner = $("banner");
  const problems = [];
  if (DATA.quant_error) problems.push("Quantitative data unavailable — " + DATA.quant_error);
  if (DATA.qual_error) problems.push("Qualitative data: " + DATA.qual_error);
  if (problems.length) { banner.textContent = problems.join("\n\n"); banner.classList.remove("hidden"); banner.classList.toggle("info", !DATA.quant_error); }
  else banner.classList.add("hidden");
  renderFilters();
  renderRankingControls();
  if (live) { renderOverview(); renderReviewList(); renderTable(); refreshDetailLive(); }
  else render();
}

// ---------------------------------------------------------------------------
// Pipeline runner: start/stop runs, show live status + log, and refresh the
// table from the database while a run is in progress so finished tickers
// pop in as they are evaluated.
// ---------------------------------------------------------------------------
let runState = null, statusTimer = null, liveTimer = null, wasRunning = false;

function elapsedSince(startedAt) {
  if (!startedAt) return "";
  const s = Math.max(0, Math.round((Date.now() - new Date(startedAt).getTime()) / 1000));
  return s < 60 ? s + "s" : Math.floor(s / 60) + "m " + (s % 60) + "s";
}

function renderRunStatus() {
  const s = runState;
  const running = s && s.state === "running";
  ["runStage2Btn", "runRankBtn", "runStage1Btn"].forEach((id) => { $(id).disabled = !!running; });
  $("stopRunBtn").classList.toggle("hidden", !running);
  const logEl = $("runLog");
  if (!s || !s.job) {
    $("runStatus").textContent = "Idle — nothing running.";
    logEl.classList.add("hidden");
    return;
  }
  const progress = s.total ? ` — ${s.processed}/${s.total} tickers` : (s.processed ? ` — ${s.processed} tickers done` : "");
  const current = s.current_ticker ? ` — now: ${s.current_ticker}` : "";
  if (s.state === "running") {
    $("runStatus").textContent = `● Running: ${s.label}${progress}${current} (${elapsedSince(s.started_at)} elapsed)`;
  } else if (s.state === "done") {
    $("runStatus").textContent = `✓ Finished: ${s.label}${progress} in ${elapsedSince(s.started_at)}. Table refreshed from the database.`;
  } else if (s.state === "stopped") {
    $("runStatus").textContent = `■ Stopped: ${s.label}${progress}. Tickers already finished stay saved.`;
  } else {
    $("runStatus").textContent = `✕ Failed: ${s.label} — ${s.error || "see the log below."}`;
  }
  if (s.log_tail && s.log_tail.length) {
    logEl.textContent = s.log_tail.join("\n");
    logEl.classList.remove("hidden");
    logEl.scrollTop = logEl.scrollHeight;
  }
}

async function refreshRunStatus() {
  try {
    const res = await fetch("/api/run/status");
    if (!res.ok) return;
    const prevRunning = wasRunning;
    runState = await res.json();
    wasRunning = runState.state === "running";
    renderRunStatus();
    if (wasRunning) {
      if ($("liveToggle").checked && !liveTimer) {
        liveTimer = setInterval(() => load(true), 3000);
      }
      if (!$("liveToggle").checked && liveTimer) { clearInterval(liveTimer); liveTimer = null; }
    } else {
      if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
      if (prevRunning) { await load(); renderRunStatus(); } // final refresh once the run ends
      if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
    }
  } catch (e) { /* status endpoint failing is non-fatal; the overview load reports it */ }
}

async function startRun(job, extra = {}) {
  const res = await fetch("/api/run", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job, ...extra }) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    runState = data.run || runState;
    $("runStatus").textContent = "Could not start: " + (data.message || data.error || res.status);
    return;
  }
  runState = data.run;
  wasRunning = true;
  renderRunStatus();
  if (!statusTimer) statusTimer = setInterval(refreshRunStatus, 2000);
  if ($("liveToggle").checked && !liveTimer) liveTimer = setInterval(() => load(true), 3000);
  await load(true);
}

$("runStage2Btn").addEventListener("click", () => startRun("stage2", { limit: parseInt($("stage2Limit").value, 10) }));
$("runRankBtn").addEventListener("click", () => startRun("rank-only"));
$("runStage1Btn").addEventListener("click", () => startRun("stage1", { requeue: $("stage1Requeue").value }));
$("stopRunBtn").addEventListener("click", async () => {
  await fetch("/api/run/stop", { method: "POST" });
  await refreshRunStatus();
});
$("liveToggle").addEventListener("change", () => {
  if (!$("liveToggle").checked && liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  else if ($("liveToggle").checked && wasRunning && !liveTimer) liveTimer = setInterval(() => load(true), 3000);
});

document.querySelectorAll("#stocksTable th[data-sort]").forEach((th) => th.addEventListener("click", () => {
  const key = th.dataset.sort;
  if (sortKey === key) { sortDir *= -1; }
  else {
    sortKey = key;
    // Best first by default: lowest rank/score/raw value, highest z-score.
    sortDir = ((METRIC_COLS.includes(key) && displayZ(key)) || key === "peer_z_avg") ? -1 : 1;
  }
  renderTable();
}));
["searchInput", "sectorFilter", "statusFilter", "flagFilter", "hasFlagsOnly", "staleOnly", "reviewOnly"].forEach((id) =>
  $(id).addEventListener("input", renderTable));
$("refreshBtn").addEventListener("click", load);
$("exportBtn").addEventListener("click", exportCSV);
$("detailClose").addEventListener("click", () => { selectedTicker = null; $("detail").classList.add("hidden"); renderTable(); });
$("noteForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!selectedTicker) return;
  await fetch("/api/notes", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticker: selectedTicker, severity: $("noteSeverity").value, metric: $("noteMetric").value, note: $("noteText").value }) });
  $("noteText").value = ""; await load(); selectTicker(selectedTicker);
});
// ---------------------------------------------------------------------------
// Portfolio panel: top-10 holdings from the pipeline's stored ranking, plus
// the backtest (point-in-time snapshots vs S&P 500).
// ---------------------------------------------------------------------------
async function loadPortfolio() {
  const weighting = $("pfWeighting").value;
  let data;
  try {
    const res = await fetch(`/api/portfolio?weighting=${encodeURIComponent(weighting)}&top_n=10`);
    data = await res.json();
  } catch (e) {
    $("pfSummary").textContent = "Could not load the portfolio (" + e.message + ").";
    return;
  }
  if (!data.ok) {
    $("pfSummary").textContent = "Portfolio unavailable — " + (data.error || "unknown error.");
    $("pfBody").innerHTML = "";
    return;
  }
  const pf = data.portfolio;
  $("pfSummary").textContent = pf.warning
    ? pf.warning
    : `Top ${pf.count} holdings, ${pf.weighting}-weighted, as of ${pf.as_of} — portfolio score ${fmt(pf.portfolio_score, 3)} (weight-weighted average composite, lower is better).`;
  $("pfBody").innerHTML = pf.holdings.map((h) =>
    `<tr><td>${h.overall_rank ?? "—"}</td><td><b>${esc(h.ticker)}</b></td><td>${esc(h.sector || "—")}</td>` +
    `<td class="num">${fmt(h.composite_score, 3)}</td><td class="num">${(h.weight * 100).toFixed(2)}%</td>` +
    `<td class="num">${fmt(h.pe_ratio)}</td><td class="num">${fmt(h.pb_ratio)}</td>` +
    `<td class="num">${fmt(h.p_dcf)}</td><td class="num">${fmt(h.debt_to_equity)}</td>` +
    `<td class="num">${fmt(h.reinvestment_efficiency)}</td>` +
    `<td class="num">${h.roe == null ? "—" : (h.roe * 100).toFixed(1) + "%"}</td></tr>`).join("")
    || '<tr><td colspan="11" class="muted">No ranked holdings — run Stage 2 first.</td></tr>';
}

let btTimer = null;
function renderBacktest(data) {
  const running = data.state === "running";
  $("btRunBtn").disabled = running;
  const logEl = $("btLog");
  if (data.state === "running") {
    $("btStatus").textContent = "● Backtest running…";
  } else if (data.state === "done") {
    $("btStatus").textContent = "✓ Backtest finished" + (data.finished_at ? ` at ${data.finished_at}.` : ".");
  } else if (data.state === "error") {
    $("btStatus").textContent = "✕ Backtest failed — " + (data.error || "see the log below.");
  } else if (data.has_results) {
    $("btStatus").textContent = "Showing the last backtest run.";
  }
  if (data.log_tail && data.log_tail.length) {
    logEl.textContent = data.log_tail.join("\n");
    logEl.classList.remove("hidden");
    logEl.scrollTop = logEl.scrollHeight;
  }
  const res = data.results;
  if (!res || !res.stats) {
    $("btStats").innerHTML = "";
    $("equityChart").classList.add("hidden");
    $("equityLegend").classList.add("hidden");
    $("ddChart").classList.add("hidden");
    renderRebalances([]);
    renderAnalysis(null);
    return;
  }
  const p = res.stats.portfolio, b = res.stats.benchmark;
  const pct = (v) => (v == null ? "—" : (v * 100).toFixed(1) + "%");
  const money = (v) => `$${(v || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  const statCards = [
    [pct(p.total_return), "portfolio total return"], [pct(b.total_return), "S&P 500 total return"],
    [pct(p.cagr), "portfolio CAGR"], [pct(b.cagr), "S&P 500 CAGR"],
    [pct(p.ann_vol), "portfolio ann. vol"], [p.sharpe_rf0.toFixed(2), "portfolio Sharpe (rf=0)"],
    [pct(p.max_drawdown), "portfolio max drawdown"],
    [`${p.end_value.toLocaleString(undefined, { maximumFractionDigits: 0 })}`, `portfolio value (from ${p.start_value.toLocaleString(undefined, { maximumFractionDigits: 0 })})`],
  ];
  // Dividends are already inside the total return (Adj Close is a
  // total-return price); these cards split the gain into income vs
  // price appreciation. Older results files have no dividends block.
  if (res.dividends) {
    const d = res.dividends;
    statCards.push([money(d.total_received), "dividends received"]);
    statCards.push([pct(d.pct_of_total_return), "dividends as % of total gain"]);
    statCards.push([pct(d.annual_yield), "portfolio dividend yield / yr"]);
  }
  $("btStats").innerHTML = statCards
    .map(([n, label]) => `<div class="card"><b>${n}</b><span>${label}</span></div>`).join("");
  $("btWarnings").textContent = (res.warnings || []).join(" ");
  drawEquityChart(res.equity_curve || []);
  drawDrawdownChart(res.equity_curve || []);
  renderRebalances(res.rebalances || []);
  renderAnalysis(res.analysis, res.dividends);
}

// What was held at each rebalance, and which point-in-time ranking it
// came from -- the visible proof the backtest picks at each date, not
// today's ranking replayed over the past.
function renderRebalances(list) {
  const el = $("btRebalances");
  if (!list || !list.length) { el.innerHTML = ""; return; }
  const rows = list.map((r) => {
    const holdings = (r.holdings || []).map((h) => `${esc(h.ticker)} ${(h.weight * 100).toFixed(1)}%`).join(", ");
    return `<tr><td>${esc(r.date)}</td><td>${esc(r.snapshot_date)}</td><td>${holdings || "—"}</td>` +
      `<td class="num">${(r.turnover * 100).toFixed(1)}%</td>` +
      `<td class="num">$${(r.cost || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}</td></tr>`;
  }).join("");
  el.innerHTML = `<h4>Rebalance history — what was held, and when it was picked</h4>` +
    `<p class="muted chart-note">Each row buys from the ranking as it stood on that date (the "Ranking used" column). A stock only appears once its own ranking exists — nothing is ever picked from today's list and replayed backwards.</p>` +
    `<div class="table-wrap scroll-table"><table>` +
    `<thead><tr><th>Date</th><th>Ranking used</th><th>Holdings</th><th>Turnover</th><th>Cost</th></tr></thead>` +
    `<tbody>${rows}</tbody></table></div>`;
}

function renderAnalysis(a, div) {
  const el = $("btAnalysis"), monthlyEl = $("btMonthly"), ddEl = $("btDrawdowns");
  if (!a) { el.innerHTML = ""; monthlyEl.innerHTML = ""; ddEl.innerHTML = ""; return; }
  const pct = (v) => (v == null ? "—" : (v * 100).toFixed(1) + "%");
  const cards = [
    [a.beta.toFixed(2), "beta vs S&P 500"], [`${(a.alpha_annualized * 100).toFixed(1)}%`, "alpha (annualised)"],
    [a.correlation.toFixed(2), "correlation with S&P 500"], [pct(a.tracking_error), "tracking error"],
    [a.information_ratio.toFixed(2), "information ratio"], [a.sortino.toFixed(2), "Sortino (rf=0)"],
    [a.calmar.toFixed(2), "Calmar (CAGR / max drawdown)"], [pct(a.avg_turnover), "avg turnover per rebalance"],
    [`$${(a.total_cost || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}`, "total trading costs"],
    [a.avg_holdings == null ? "—" : a.avg_holdings.toFixed(1), "avg holdings"],
    [a.best_month ? `${a.best_month.month} ${pct(a.best_month.portfolio)}` : "—", "best month"],
    [a.worst_month ? `${a.worst_month.month} ${pct(a.worst_month.portfolio)}` : "—", "worst month"],
    [a.positive_months_pct == null ? "—" : pct(a.positive_months_pct), "positive months"],
  ];
  if (div && div.benchmark_total_received != null) {
    cards.push([pct(div.benchmark_pct_of_total_return), "S&P 500 dividends as % of its gain"]);
  }
  el.innerHTML = "<h4>Risk &amp; benchmark-relative</h4><div class=\"cards\">" +
    cards.map(([n, label]) => `<div class="card"><b>${n}</b><span>${label}</span></div>`).join("") + "</div>";
  const months = a.monthly_returns || [];
  if (months.length) {
    const rows = months.map((m) =>
      `<tr><td>${m.month}</td><td class="num">${pct(m.portfolio)}</td><td class="num">${pct(m.benchmark)}</td></tr>`).join("");
    monthlyEl.innerHTML = `<h4>Monthly returns</h4><div class="table-wrap scroll-table"><table>` +
      `<thead><tr><th>Month</th><th>Portfolio</th><th>S&amp;P 500</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  } else { monthlyEl.innerHTML = ""; }
  const dds = a.drawdowns || [];
  if (dds.length) {
    ddEl.innerHTML = "<h4>Worst drawdowns</h4>" + dds.map((d) =>
      `<p>${pct(d.depth)} — from ${d.start} (trough ${d.trough}${d.recovery ? `, recovered ${d.recovery}` : ", not yet recovered"}, ${d.duration_days} days)</p>`).join("");
  } else { ddEl.innerHTML = ""; }
}

function drawEquityChart(curve) {
  const canvas = $("equityChart");
  const ctx = canvas.getContext && canvas.getContext("2d");
  if (!ctx || !curve.length) { canvas.classList.add("hidden"); $("equityLegend").classList.add("hidden"); return; }
  canvas.classList.remove("hidden");
  $("equityLegend").classList.remove("hidden");
  const W = canvas.width, H = canvas.height, pad = { l: 56, r: 12, t: 14, b: 26 };
  const norm = (v, first) => (v / first) * 100;
  const p0 = curve[0].portfolio, b0 = curve[0].benchmark;
  const vals = curve.flatMap((e) => [norm(e.portfolio, p0), norm(e.benchmark, b0)]);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const span = (hi - lo) || 1;
  const x = (i) => pad.l + (i / (curve.length - 1 || 1)) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - lo) / span) * (H - pad.t - pad.b);
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = "#d8dde3"; ctx.fillStyle = "#5b6b7c"; ctx.font = "12px sans-serif";
  ctx.textAlign = "right";
  [lo, (lo + hi) / 2, hi].forEach((v) => {
    ctx.beginPath(); ctx.moveTo(pad.l, y(v)); ctx.lineTo(W - pad.r, y(v)); ctx.stroke();
    ctx.fillText(v.toFixed(0), pad.l - 6, y(v) + 4);
  });
  ctx.textAlign = "center";
  ctx.fillText(curve[0].date, pad.l + 30, H - 8);
  ctx.fillText(curve[curve.length - 1].date, W - pad.r - 40, H - 8);
  const line = (key, first, color) => {
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
    curve.forEach((e, i) => { const px = x(i), py = y(norm(e[key], first)); i ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
    ctx.stroke();
  };
  line("portfolio", p0, "#14684a");
  line("benchmark", b0, "#98a2b3");
  ctx.lineWidth = 1;
}

// Pure helper (tested): [{date, drawdown}] where drawdown = value/peak - 1 (<= 0).
function drawdownSeries(curve) {
  const out = [];
  let peak = -Infinity;
  for (const e of curve || []) {
    if (e.portfolio == null) continue;
    peak = Math.max(peak, e.portfolio);
    out.push({ date: e.date, drawdown: peak > 0 ? e.portfolio / peak - 1 : 0 });
  }
  return out;
}

function drawDrawdownChart(curve) {
  const canvas = $("ddChart");
  const ctx = canvas.getContext && canvas.getContext("2d");
  const series = drawdownSeries(curve);
  if (!ctx || series.length < 2) { canvas.classList.add("hidden"); return; }
  canvas.classList.remove("hidden");
  const W = canvas.width, H = canvas.height, pad = { l: 56, r: 12, t: 14, b: 26 };
  const min = Math.min(...series.map((e) => e.drawdown), 0);
  const span = Math.abs(min) || 1;
  const x = (i) => pad.l + (i / (series.length - 1 || 1)) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (1 - Math.abs(v) / span) * (H - pad.t - pad.b);
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = "#d8dde3"; ctx.fillStyle = "#5b6b7c"; ctx.font = "12px sans-serif";
  ctx.textAlign = "right";
  [0, min / 2, min].forEach((v) => {
    ctx.beginPath(); ctx.moveTo(pad.l, y(v)); ctx.lineTo(W - pad.r, y(v)); ctx.stroke();
    ctx.fillText((v * 100).toFixed(0) + "%", pad.l - 6, y(v) + 4);
  });
  ctx.textAlign = "center";
  ctx.fillText(series[0].date, pad.l + 30, H - 8);
  ctx.fillText(series[series.length - 1].date, W - pad.r - 40, H - 8);
  ctx.beginPath();
  series.forEach((e, i) => { const px = x(i), py = y(e.drawdown); i ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
  ctx.lineTo(x(series.length - 1), y(0));
  ctx.lineTo(x(0), y(0));
  ctx.closePath();
  ctx.fillStyle = "rgba(169, 50, 38, 0.18)";
  ctx.fill();
  ctx.beginPath();
  series.forEach((e, i) => { const px = x(i), py = y(e.drawdown); i ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
  ctx.strokeStyle = "#a93226"; ctx.lineWidth = 2; ctx.stroke();
  ctx.lineWidth = 1;
}

async function refreshBacktest() {
  try {
    const res = await fetch("/api/backtest");
    if (!res.ok) return;
    const data = await res.json();
    renderBacktest(data);
    if (data.state === "running" && !btTimer) btTimer = setInterval(refreshBacktest, 2000);
    if (data.state !== "running" && btTimer) { clearInterval(btTimer); btTimer = null; }
  } catch (e) { /* backtest status failing is non-fatal */ }
}

let bfTimer = null;
function renderBackfill(data) {
  const running = data.state === "running";
  $("bfRunBtn").disabled = running;
  const logEl = $("bfLog");
  if (data.state === "running") {
    $("bfStatus").textContent = "● Backfill running — it downloads years of prices, so this takes a while. It is resumable: stop the server any time and it picks up where it left off.";
  } else if (data.state === "done") {
    $("bfStatus").textContent = "✓ Backfill finished" + (data.finished_at ? ` at ${data.finished_at}.` : ".");
  } else if (data.state === "error") {
    $("bfStatus").textContent = "✕ Backfill failed — " + (data.error || "see the log below.");
  } else if (!data.coverage || !data.coverage.dates || !data.coverage.dates.length) {
    $("bfStatus").textContent = "No snapshots yet — run the backfill (or Stage 2) to build history.";
  }
  const cov = data.coverage || {};
  COVERAGE_DATES = cov.dates || [];
  if (cov.dates && cov.dates.length) {
    $("snapshotCoverage").textContent = `Snapshots: ${cov.dates.length} dates (${cov.dates[0]} … ${cov.dates[cov.dates.length - 1]}), ` +
      `${cov.backfilled_dates || 0} backfilled, ${cov.live_dates || 0} live.`;
  } else {
    $("snapshotCoverage").textContent = "";
  }
  if (data.log_tail && data.log_tail.length) {
    logEl.textContent = data.log_tail.join("\n");
    logEl.classList.remove("hidden");
    logEl.scrollTop = logEl.scrollHeight;
  }
}

async function refreshBackfill() {
  try {
    const res = await fetch("/api/backfill");
    if (!res.ok) return;
    const data = await res.json();
    renderBackfill(data);
    if (data.state === "running" && !bfTimer) bfTimer = setInterval(refreshBackfill, 3000);
    if (data.state !== "running" && bfTimer) { clearInterval(bfTimer); bfTimer = null; }
  } catch (e) { /* backfill status failing is non-fatal */ }
}

$("bfRunBtn").addEventListener("click", async () => {
  const res = await fetch("/api/backfill/run", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ start_year: parseInt($("bfStartYear").value, 10),
      rebalance: $("bfRebalance").value, max_tickers: 0, force: $("bfForce").checked }) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { $("bfStatus").textContent = "Could not start backfill: " + (data.message || res.status); return; }
  renderBackfill(data.backfill);
  if (!bfTimer) bfTimer = setInterval(refreshBackfill, 3000);
});

// ---------------------------------------------------------------------------
// Tabs: Shortlist / Backtest. Remembered in the URL hash and localStorage;
// the backtest tab's data loads the first time it is opened.
// ---------------------------------------------------------------------------
let btTabLoaded = false;
function showTab(name, { push = true } = {}) {
  const backtest = name === "backtest";
  $("tab-shortlist").hidden = backtest;
  $("tab-backtest").hidden = !backtest;
  $("tabBtnShortlist").classList.toggle("active", !backtest);
  $("tabBtnBacktest").classList.toggle("active", backtest);
  $("tabBtnShortlist").setAttribute("aria-selected", String(!backtest));
  $("tabBtnBacktest").setAttribute("aria-selected", String(backtest));
  document.body.classList.toggle("tab-backtest", backtest);
  if (push) {
    history.replaceState(null, "", backtest ? "#backtest" : "#shortlist");
    try { localStorage.setItem("valuproj-tab", backtest ? "backtest" : "shortlist"); } catch (e) { /* private mode */ }
  }
  if (backtest && !btTabLoaded) {
    btTabLoaded = true;
    loadPortfolio();
    refreshBacktest();
    refreshBackfill();
    if (!$("btStart").value) setBtRange(5);
  }
}
$("tabBtnShortlist").addEventListener("click", () => showTab("shortlist"));
$("tabBtnBacktest").addEventListener("click", () => showTab("backtest"));

// ---------------------------------------------------------------------------
// Backtest time presets: 1Y / 3Y / 5Y / 10Y / All, from the earliest
// snapshot date (from the coverage line) to today. Editing a date by hand
// clears the active preset.
// ---------------------------------------------------------------------------
let COVERAGE_DATES = [];  // snapshot dates, kept current by renderBackfill
function fmtDate(d) { return d.toISOString().slice(0, 10); }
function setBtRange(years) {
  const end = new Date();
  let start;
  if (years === 0) {
    const first = COVERAGE_DATES[0] || "2016-01-01";
    start = new Date(first + "T00:00:00");
  } else {
    start = new Date(end);
    start.setFullYear(start.getFullYear() - years);
  }
  $("btStart").value = fmtDate(start);
  $("btEnd").value = fmtDate(end);
  document.querySelectorAll("#btPresets button").forEach((b) =>
    b.classList.toggle("active", parseInt(b.dataset.years, 10) === years));
}
document.querySelectorAll("#btPresets button").forEach((b) =>
  b.addEventListener("click", () => setBtRange(parseInt(b.dataset.years, 10))));
["btStart", "btEnd"].forEach((id) => $(id).addEventListener("change", () =>
  document.querySelectorAll("#btPresets button").forEach((b) => b.classList.remove("active"))));

$("pfRebuildBtn").addEventListener("click", loadPortfolio);
$("btRunBtn").addEventListener("click", async () => {
  const start = $("btStart").value, end = $("btEnd").value;
  if (!start || !end || start > end) {
    $("btStatus").textContent = "Pick a valid period first — the From date has to be before the To date.";
    return;
  }
  const res = await fetch("/api/backtest/run", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ top_n: parseInt($("btTopN").value, 10), weighting: $("pfWeighting").value,
      rebalance: $("btRebalance").value, cost_bps: parseFloat($("btCost").value) || 0,
      start, end }) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { $("btStatus").textContent = "Could not start backtest: " + (data.message || res.status); return; }
  renderBacktest(data.backtest);
  if (!btTimer) btTimer = setInterval(refreshBacktest, 2000);
});

load();
refreshRunStatus();
(function initTab() {
  let tab = "shortlist";
  if (location.hash === "#backtest") tab = "backtest";
  else {
    try { if (localStorage.getItem("valuproj-tab") === "backtest") tab = "backtest"; } catch (e) { /* private mode */ }
  }
  showTab(tab, { push: false });
})();
