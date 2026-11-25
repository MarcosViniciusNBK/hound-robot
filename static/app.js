"use strict";

const state = {
  selectedDate: null,
  selectedTrack: "ALL",
  outcomeFilter: "ALL",
  searchQuery: "",
  chartMode: "backtest",
  overview: null,
  races: [],
  expandedRaces: new Set(),
  ws: null,
  dogCache: [],
  bankrollInitialized: false,
};

const byId = (id) => document.getElementById(id);

function node(tag, className, text) {
  const elem = document.createElement(tag);
  if (className) elem.className = className;
  if (text !== undefined && text !== null) elem.textContent = String(text);
  return elem;
}

function fmtBRL(val, showSign = false) {
  const num = Number(val || 0);
  const absFormatted = Math.abs(num).toLocaleString("pt-BR", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  if (num < 0) return `-R$ ${absFormatted}`;
  if (showSign && num > 0) return `+R$ ${absFormatted}`;
  return `R$ ${absFormatted}`;
}

function fmtTime(isoStr) {
  if (!isoStr) return "—";
  const d = new Date(isoStr);
  if (Number.isNaN(d.getTime())) return isoStr;
  return d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function makeTrapBadge(trapNum) {
  const n = Number(trapNum) || 1;
  const cls = n >= 1 && n <= 6 ? `trap-${n}` : "trap-4";
  return node("span", `trap-badge ${cls}`, n);
}

// Sidebar tabs
const tabButtons = Array.from(document.querySelectorAll('[role="tab"]'));
function activateTab(targetBtn) {
  tabButtons.forEach((btn) => {
    const isActive = btn === targetBtn;
    btn.classList.toggle("is-active", isActive);
    btn.setAttribute("aria-selected", String(isActive));
    btn.tabIndex = isActive ? 0 : -1;
    const panel = byId(btn.getAttribute("aria-controls"));
    if (panel) {
      panel.classList.toggle("is-active", isActive);
      panel.hidden = !isActive;
    }
  });
  byId("pageTitle").textContent = targetBtn.dataset.title || "Dashboard";
  byId("sidebar").classList.remove("is-open");
}

tabButtons.forEach((btn, idx) => {
  btn.addEventListener("click", () => activateTab(btn));
  btn.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowRight") {
      e.preventDefault();
      const next = tabButtons[(idx + 1) % tabButtons.length];
      activateTab(next);
      next.focus();
    } else if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
      e.preventDefault();
      const prev = tabButtons[(idx - 1 + tabButtons.length) % tabButtons.length];
      activateTab(prev);
      prev.focus();
    }
  });
});

byId("btnCollapseSidebar").addEventListener("click", () => {
  const shell = document.querySelector(".shell");
  const collapsed = shell.classList.toggle("is-collapsed");
  byId("btnCollapseSidebar").setAttribute("aria-expanded", String(!collapsed));
});

byId("btnMobileMenu").addEventListener("click", () => {
  byId("sidebar").classList.toggle("is-open");
});

byId("btnJumpRaces").addEventListener("click", () => activateTab(byId("tabBtn-races")));
byId("btnJumpPerf").addEventListener("click", () => activateTab(byId("tabBtn-performance")));

const modal = byId("bankrollModal");
byId("btnOpenBankrollModal").addEventListener("click", () => modal.showModal());
byId("btnCloseModal").addEventListener("click", () => modal.close());
byId("btnCancelModal").addEventListener("click", () => modal.close());

function updateStatusBanner(status) {
  if (!status) return;
  const running = Boolean(status.running);
  byId("phasePill").textContent = String(status.phase || "idle").toUpperCase();
  byId("statusMessage").textContent = status.message || "—";
  byId("studyProgress").value = Number(status.progress ?? 0);
  byId("cycleCounter").textContent = `#${status.cycle_count ?? 0}`;
  byId("lastRunLabel").textContent = status.last_run_at ? fmtTime(status.last_run_at) : "—";
  byId("botPulse").classList.toggle("is-busy", running);
  byId("btnRunStudy").disabled = running;
}

function createLogElement(log) {
  const row = node("div", `log-item ${log.level || "INFO"}`);
  row.appendChild(node("span", "log-time", fmtTime(log.created_at)));
  row.appendChild(node("span", "log-phase", log.phase || "sys"));
  row.appendChild(node("span", "", log.message || ""));
  return row;
}

function appendLog(log) {
  const list = byId("liveLogsList");
  list.prepend(createLogElement(log));
  while (list.children.length > 80) {
    list.removeChild(list.lastChild);
  }
}

function renderLogs(logs) {
  const list = byId("liveLogsList");
  list.replaceChildren();
  (logs || []).forEach((log) => list.appendChild(createLogElement(log)));
}

function renderBankrollChart(data) {
  const box = byId("bankrollChart");
  box.replaceChildren();

  const paper = data.paper_summary || {};
  const bt = data.backtest_summary || {};
  const base = Number(paper.start_bankroll || 100);
  const isLive = state.chartMode === "live";

  let curve = [];
  if (isLive) {
    byId("chartHeaderStat").textContent = `${fmtBRL(paper.total_pnl ?? 0, true)} · ${paper.total_bets ?? 0} entradas`;
    curve = (data.live_curve || []).map((c) => Number(c.bankroll_after));
  } else {
    byId("chartHeaderStat").textContent = `${fmtBRL(bt.total_pnl ?? 0, true)} · ROI ${bt.roi_pct ?? 0}% · DD -${fmtBRL(bt.max_drawdown_brl ?? 0)}`;
    curve = (data.bankroll_curve || []).map((c) => Number(c.bankroll_after));
  }

  if (!curve.length) {
    box.appendChild(node("p", "empty-cell", "Aguardando liquidações."));
    return;
  }

  const values = [base, ...curve];
  const minV = Math.min(...values) - 8;
  const maxV = Math.max(...values) + 8;
  const w = 640;
  const h = 225;
  const pad = 22;

  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Evolução da banca em Reais");

  const defs = document.createElementNS(ns, "defs");
  const grad = document.createElementNS(ns, "linearGradient");
  grad.setAttribute("id", "chartGrad");
  grad.setAttribute("x1", "0");
  grad.setAttribute("y1", "0");
  grad.setAttribute("x2", "0");
  grad.setAttribute("y2", "1");
  const s1 = document.createElementNS(ns, "stop");
  s1.setAttribute("offset", "0%");
  s1.setAttribute("stop-color", "#f5a623");
  s1.setAttribute("stop-opacity", "0.18");
  const s2 = document.createElementNS(ns, "stop");
  s2.setAttribute("offset", "100%");
  s2.setAttribute("stop-color", "#f5a623");
  s2.setAttribute("stop-opacity", "0");
  grad.appendChild(s1);
  grad.appendChild(s2);
  defs.appendChild(grad);
  svg.appendChild(defs);

  const baseY = h - pad - ((base - minV) / Math.max(maxV - minV, 1)) * (h - pad * 2);
  const baseline = document.createElementNS(ns, "line");
  baseline.setAttribute("x1", String(pad));
  baseline.setAttribute("y1", baseY.toFixed(1));
  baseline.setAttribute("x2", String(w - pad));
  baseline.setAttribute("y2", baseY.toFixed(1));
  baseline.setAttribute("stroke", "rgba(255,255,255,0.08)");
  baseline.setAttribute("stroke-dasharray", "3 4");
  svg.appendChild(baseline);

  const coords = values.map((v, i) => {
    const x = pad + (i / Math.max(values.length - 1, 1)) * (w - pad * 2);
    const y = h - pad - ((v - minV) / Math.max(maxV - minV, 1)) * (h - pad * 2);
    return [x, y];
  });

  const linePts = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const areaPts = `${pad},${h - pad} ${linePts} ${coords[coords.length - 1][0].toFixed(1)},${h - pad}`;

  const area = document.createElementNS(ns, "polygon");
  area.setAttribute("fill", "url(#chartGrad)");
  area.setAttribute("points", areaPts);
  svg.appendChild(area);

  const poly = document.createElementNS(ns, "polyline");
  poly.setAttribute("fill", "none");
  poly.setAttribute("stroke", "#f5a623");
  poly.setAttribute("stroke-width", "1.75");
  poly.setAttribute("points", linePts);
  svg.appendChild(poly);

  const lastVal = values[values.length - 1];
  const topTxt = document.createElementNS(ns, "text");
  topTxt.setAttribute("x", String(pad));
  topTxt.setAttribute("y", "14");
  topTxt.setAttribute("fill", "#8b93a3");
  topTxt.setAttribute("font-size", "10.5");
  topTxt.setAttribute("font-family", "JetBrains Mono, monospace");
  topTxt.textContent = `${fmtBRL(lastVal)}  (máx ${fmtBRL(Math.max(...values))})`;
  svg.appendChild(topTxt);

  const botTxt = document.createElementNS(ns, "text");
  botTxt.setAttribute("x", String(pad));
  botTxt.setAttribute("y", String(h - 5));
  botTxt.setAttribute("fill", "#59606e");
  botTxt.setAttribute("font-size", "10.5");
  botTxt.setAttribute("font-family", "JetBrains Mono, monospace");
  botTxt.textContent = `mín ${fmtBRL(Math.min(...values))} · ${curve.length} entradas`;
  svg.appendChild(botTxt);

  box.appendChild(svg);
}

function renderQuickUpcoming(upcoming) {
  const box = byId("upcomingQuickList");
  box.replaceChildren();
  if (!upcoming || !upcoming.length) {
    box.appendChild(node("p", "empty-cell", "Sem corridas futuras abertas."));
    return;
  }

  upcoming.slice(0, 6).forEach((b) => {
    const row = node("div", "compact-row");
    const left = node("div", "compact-left");
    const timePart = (b.race_dt || "").split(" ")[1] || "";
    left.appendChild(node("span", "compact-time mono", timePart.slice(0, 5)));
    left.appendChild(makeTrapBadge(b.trap_number));

    const info = node("div", "compact-info");
    info.appendChild(node("span", "compact-title", b.dog_name));
    info.appendChild(node("span", "compact-sub", `${b.track_name} · P${b.race_number}`));
    left.appendChild(info);

    const right = node("div", "compact-right mono");
    right.appendChild(node("div", "", `Odd ${Number(b.lay_odds).toFixed(2)}`));
    right.appendChild(node("div", "sub-line", fmtBRL(b.liability)));

    row.appendChild(left);
    row.appendChild(right);
    box.appendChild(row);
  });
}

function renderRecentSettled(races) {
  const tbody = byId("recentSettledBody");
  tbody.replaceChildren();
  const settled = (races || [])
    .filter((r) => r.paper_bet && (r.paper_bet.status === "WON_LAY" || r.paper_bet.status === "LOST_LAY"))
    .slice(0, 8);

  if (!settled.length) {
    const tr = document.createElement("tr");
    const td = node("td", "empty-cell", "Aguardando liquidações.");
    td.colSpan = 6;
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  settled.forEach((r) => {
    const pb = r.paper_bet;
    const isWin = pb.status === "WON_LAY";
    const tr = document.createElement("tr");
    tr.appendChild(node("td", "mono", (r.race_time || "").slice(0, 5)));
    tr.appendChild(node("td", "", `${r.track_name} · P${r.race_number}`));

    const dogTd = document.createElement("td");
    const cell = node("div", "runner-cell");
    if (r.lay_pick) {
      cell.appendChild(makeTrapBadge(r.lay_pick.trap_number));
      cell.appendChild(node("span", "", r.lay_pick.dog_name));
    }
    dogTd.appendChild(cell);
    tr.appendChild(dogTd);

    tr.appendChild(node("td", "num", pb.lay_odds ? Number(pb.lay_odds).toFixed(2) : "—"));

    const stTd = document.createElement("td");
    stTd.appendChild(node("span", `tag ${isWin ? "tag-green" : "tag-red"}`, isWin ? "Acertou" : "Errou"));
    tr.appendChild(stTd);

    tr.appendChild(node("td", `num ${isWin ? "pos" : "neg"}`, fmtBRL(pb.pnl, true)));
    tbody.appendChild(tr);
  });
}

function renderUpcomingTable(upcoming) {
  const card = byId("upcomingCard");
  const tbody = byId("upcomingBody");
  tbody.replaceChildren();

  if (!upcoming || !upcoming.length) {
    card.hidden = true;
    return;
  }

  card.hidden = false;
  byId("upcomingCountBadge").textContent = String(upcoming.length);

  upcoming.forEach((b) => {
    const tr = document.createElement("tr");
    const timePart = (b.race_dt || "").split(" ")[1] || "";
    tr.appendChild(node("td", "mono", timePart.slice(0, 5)));
    tr.appendChild(node("td", "", `${b.track_name} · P${b.race_number}`));

    const dogTd = document.createElement("td");
    const cell = node("div", "runner-cell");
    cell.appendChild(makeTrapBadge(b.trap_number));
    cell.appendChild(node("span", "", b.dog_name));
    dogTd.appendChild(cell);
    tr.appendChild(dogTd);

    tr.appendChild(node("td", "num", `${(Number(b.lose_prob || 0) * 100).toFixed(1)}%`));
    tr.appendChild(node("td", "num", Number(b.lay_odds).toFixed(2)));
    tr.appendChild(node("td", "num", `${fmtBRL(b.stake)} / ${fmtBRL(b.liability)}`));
    tbody.appendChild(tr);
  });
}

function renderAdaptiveLearning(al) {
  const badge = byId("adaptiveModeBadge");
  const bar = byId("adaptiveMetaBar");
  const tbody = byId("adaptiveBody");
  bar.replaceChildren();
  tbody.replaceChildren();

  if (!al || !al.contexts) {
    badge.className = "tag mono";
    badge.textContent = "—";
    return;
  }

  const defensive = Boolean(al.defensive_mode);
  badge.className = `tag mono ${defensive ? "tag-amber" : "tag-green"}`;
  badge.textContent = defensive ? "Defensivo" : "Normal";

  [
    `Bloqueios: ${al.blocked_contexts_count ?? 0}`,
    `Evitadas: ${al.races_protected_count ?? 0}`,
    `+EV mín: ${((al.active_min_ev ?? 0.01) * 100).toFixed(1)}%`,
    `Risco máx: ${al.active_max_risk_pct ?? 15}%`,
  ].forEach((txt) => bar.appendChild(node("span", "", txt)));

  (al.contexts || []).forEach((c) => {
    const tr = document.createElement("tr");
    tr.appendChild(node("td", "", c.context));
    tr.appendChild(node("td", "num", c.bets));
    tr.appendChild(node("td", "num", `${c.win_pct}%`));
    tr.appendChild(node("td", `num ${c.roi_pct >= 0 ? "pos" : "neg"}`, `${c.roi_pct}%`));
    tr.appendChild(node("td", "num", `${c.pred_win_pct}% / ${c.actual_win_pct}%`));
    const stTd = document.createElement("td");
    const isBlocked = c.status === "BLOQUEADO";
    stTd.appendChild(node("span", `tag ${isBlocked ? "tag-red" : "tag-green"}`, c.status));
    tr.appendChild(stTd);
    tbody.appendChild(tr);
  });
}

function renderRiskAndCalibration(rules, bt, buckets) {
  const bar = byId("simRulesBar");
  bar.replaceChildren();
  const r = rules || {};
  const b = bt || {};
  [
    `Comissão ${r.commission_pct ?? 5}%`,
    `Spread +${r.spread_pct ?? 5}% + ${r.slippage_ticks ?? 1} tick`,
    `Drawdown -${fmtBRL(b.max_drawdown_brl ?? 0)} (${b.max_drawdown_pct ?? 0}%)`,
    `Profit Factor ${b.profit_factor ?? 0}`,
    `Expectativa ${fmtBRL(b.expectancy_brl ?? 0, true)}`,
  ].forEach((txt) => bar.appendChild(node("span", "", txt)));

  const calBody = byId("calibrationBody");
  calBody.replaceChildren();
  (buckets || []).forEach((bk) => {
    const tr = document.createElement("tr");
    tr.appendChild(node("td", "mono", bk.band));
    tr.appendChild(node("td", "num", bk.count));
    tr.appendChild(node("td", "num", `${bk.pred_win_pct}%`));
    tr.appendChild(node("td", "num", `${bk.actual_win_pct}%`));
    tr.appendChild(node("td", "num pos", `${bk.actual_lose_pct}%`));
    calBody.appendChild(tr);
  });
}

function renderFeatures(features) {
  const list = byId("featureList");
  list.replaceChildren();
  const maxW = Math.max(...(features || []).map((f) => Number(f.weight || 0)), 1);
  (features || []).forEach((f) => {
    const row = node("div", "feat-row");
    row.appendChild(node("span", "", f.label || f.feature));
    const meter = document.createElement("meter");
    meter.className = "feat-meter";
    meter.min = 0;
    meter.max = maxW;
    meter.value = Number(f.weight || 0);
    row.appendChild(meter);
    row.appendChild(node("span", "mono", `${Number(f.weight || 0).toFixed(1)}%`));
    list.appendChild(row);
  });
}

function renderOverview(data) {
  state.overview = data;
  updateStatusBanner(data.status);

  const paper = data.paper_summary || {};
  const bt = data.backtest_summary || {};
  const totals = data.totals || {};
  const model = data.latest_model;
  const al = data.adaptive_learning || {};

  const startBankroll = paper.start_bankroll ?? 100;
  const layStake = paper.lay_stake ?? 2;
  if (!state.bankrollInitialized) {
    byId("cfgBankrollStart").value = String(startBankroll);
    byId("cfgLayStake").value = String(layStake);
    state.bankrollInitialized = true;
  }

  const currentBankroll = paper.current_bankroll ?? startBankroll;
  const availBalance = paper.available_balance ?? currentBankroll;
  const committedRisk = paper.committed_liability ?? 0;
  const netPnl = paper.total_pnl ?? 0;
  const isProfit = netPnl >= 0;

  byId("topBankrollValue").textContent = fmtBRL(currentBankroll);
  byId("kpiBankroll").textContent = fmtBRL(currentBankroll);

  const badge = byId("kpiProfitBadge");
  badge.className = `tag mono ${isProfit ? "tag-green" : "tag-red"}`;
  badge.textContent = fmtBRL(netPnl, true);
  byId("kpiBankrollSub").textContent = `Livre ${fmtBRL(availBalance)} · Risco ${fmtBRL(committedRisk)}`;

  byId("kpiTotalWon").textContent = fmtBRL(paper.total_won_amount ?? 0);
  byId("kpiTotalLost").textContent = fmtBRL(paper.total_lost_amount ?? 0);
  byId("kpiRoiBadge").textContent = `ROI ${paper.roi_pct ?? 0}%`;
  byId("kpiWinLossSub").textContent = `${paper.won_bets ?? 0}W · ${paper.lost_bets ?? 0}L · Stake ${fmtBRL(layStake)}`;

  const hasLive = (paper.total_bets ?? 0) > 0;
  byId("kpiLayAcc").textContent = hasLive
    ? `${paper.win_rate_pct}%`
    : `${bt.win_rate_pct ?? (model?.lay_accuracy ?? 0)}%`;
  byId("kpiLaySub").textContent = `Backtest 30d: ${bt.win_rate_pct ?? 0}% · ROI ${bt.roi_pct ?? 0}%`;

  const pendingCount = paper.pending_bets ?? 0;
  byId("kpiPendingBadge").textContent = `${pendingCount} abertas`;
  const navPill = byId("navUpcomingPill");
  navPill.textContent = String(pendingCount);
  navPill.hidden = pendingCount === 0;

  byId("kpiRaces").textContent = `${totals.total_races ?? 0} corr.`;
  byId("kpiModelSub").textContent = model
    ? `${model.model_name} · LL ${Number(model.log_loss).toFixed(4)}`
    : "Aguardando treino";
  const adapPill = byId("kpiAdaptivePill");
  adapPill.className = `tag mono ${al.defensive_mode ? "tag-amber" : "tag-green"}`;
  adapPill.textContent = al.defensive_mode ? "Defensivo" : "Normal";

  renderQuickUpcoming(data.upcoming_bets);
  renderUpcomingTable(data.upcoming_bets);
  renderBankrollChart(data);
  renderFeatures(data.feature_importances);
  renderRiskAndCalibration(data.simulation_rules, bt, data.calibration_buckets);
  renderAdaptiveLearning(al);

  if (data.simulation_rules?.betfair_hist_dir) {
    byId("bfHistDesc").textContent = `${data.simulation_rules.betfair_hist_dir} (${data.simulation_rules.exchange_entries_count} entradas)`;
  }

  const dateSel = byId("dateSelect");
  const prev = state.selectedDate || data.active_date;
  dateSel.replaceChildren();
  (data.available_dates || []).forEach((d) => {
    const opt = document.createElement("option");
    opt.value = d.iso_date;
    opt.textContent = `${d.iso_date} (${d.race_count})`;
    dateSel.appendChild(opt);
  });
  if (prev && Array.from(dateSel.options).some((o) => o.value === prev)) {
    dateSel.value = prev;
    state.selectedDate = prev;
  } else if (dateSel.options.length > 0) {
    state.selectedDate = dateSel.options[0].value;
    dateSel.value = state.selectedDate;
  }
  if (state.selectedDate) byId("customDateInput").value = state.selectedDate;

  const mhBody = byId("modelHistoryBody");
  mhBody.replaceChildren();
  (data.model_history || []).slice().reverse().forEach((m) => {
    const tr = document.createElement("tr");
    [
      [`#${m.id}`, "mono"],
      [m.model_name, ""],
      [m.val_races, "num"],
      [Number(m.log_loss).toFixed(4), "num"],
      [Number(m.brier_score).toFixed(4), "num"],
      [`${m.lay_accuracy}%`, "num"],
      [`${m.lay_roi_pct}%`, "num"],
    ].forEach(([val, cls]) => tr.appendChild(node("td", cls, val)));
    mhBody.appendChild(tr);
  });

  const errBody = byId("errorAuditBody");
  errBody.replaceChildren();
  const errs = data.error_audit || [];
  if (!errs.length) {
    const tr = document.createElement("tr");
    const td = node("td", "empty-cell", "Nenhum erro registrado.");
    td.colSpan = 7;
    tr.appendChild(td);
    errBody.appendChild(tr);
  } else {
    errs.forEach((e) => {
      const tr = document.createElement("tr");
      tr.appendChild(node("td", "mono", e.iso_date));
      tr.appendChild(node("td", "", `${e.track_name} · P${e.race_number}`));
      const dogTd = document.createElement("td");
      const wrap = node("div", "runner-cell");
      wrap.appendChild(makeTrapBadge(e.trap_number));
      wrap.appendChild(node("span", "", e.dog_name));
      dogTd.appendChild(wrap);
      tr.appendChild(dogTd);
      tr.appendChild(node("td", "num", `${(Number(e.win_prob) * 100).toFixed(1)}%`));
      tr.appendChild(node("td", "num", Number(e.lay_odds).toFixed(2)));
      tr.appendChild(node("td", "num neg", `-${fmtBRL(e.liability)}`));
      tr.appendChild(node("td", "", e.error_reason || "—"));
      errBody.appendChild(tr);
    });
  }

  if (!byId("liveLogsList").children.length && data.logs) renderLogs(data.logs);
}

function buildDetailRow(race) {
  const tr = node("tr", "detail-row");
  const td = document.createElement("td");
  td.colSpan = 6;

  const box = node("div", "detail-box");
  const lp = race.lay_pick;
  if (lp && lp.reasons && lp.reasons.length) {
    box.appendChild(node("div", "detail-reason", `${lp.dog_name}: ${lp.reasons.join(" · ")}`));
  }

  const wrap = node("div", "table-wrap");
  const tbl = node("table", "tbl");
  const thead = document.createElement("thead");
  const hrow = document.createElement("tr");
  [
    ["Rank", ""],
    ["Galgo", ""],
    ["Vitória", "num"],
    ["Derrota", "num"],
    ["Forma", ""],
    ["SP", "num"],
    ["Chegada", ""],
  ].forEach(([h, cls]) => {
    const th = node("th", cls, h);
    th.scope = "col";
    hrow.appendChild(th);
  });
  thead.appendChild(hrow);
  tbl.appendChild(thead);

  const tbody = document.createElement("tbody");
  (race.runners || []).forEach((d) => {
    const rtr = document.createElement("tr");
    rtr.appendChild(node("td", "mono", `${d.predicted_rank ?? "—"}º`));

    const gtd = document.createElement("td");
    const cell = node("div", "runner-cell");
    cell.appendChild(makeTrapBadge(d.trap_number));
    cell.appendChild(node("span", "", d.dog_name));
    if (d.is_lay_pick === 1) cell.appendChild(node("span", "tag tag-amber mono", "LAY"));
    gtd.appendChild(cell);
    rtr.appendChild(gtd);

    rtr.appendChild(node("td", "num", d.win_prob != null ? `${(d.win_prob * 100).toFixed(1)}%` : "—"));
    rtr.appendChild(node("td", "num", d.lose_prob != null ? `${(d.lose_prob * 100).toFixed(1)}%` : "—"));
    rtr.appendChild(node("td", "mono", d.stats?.recent_form || "—"));
    rtr.appendChild(node("td", "num", d.decimal_odds ? Number(d.decimal_odds).toFixed(2) : "—"));
    const posText = d.result_position ? (d.result_position === 1 ? "1º" : `${d.result_position}º`) : "—";
    rtr.appendChild(node("td", `mono ${d.result_position === 1 ? "pos" : ""}`, posText));
    tbody.appendChild(rtr);
  });
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
  box.appendChild(wrap);
  td.appendChild(box);
  tr.appendChild(td);
  return tr;
}

function renderRaces() {
  const tbody = byId("racesTableBody");
  tbody.replaceChildren();
  const q = state.searchQuery.trim().toLowerCase();
  const outF = state.outcomeFilter;

  const filtered = state.races.filter((r) => {
    if (outF !== "ALL" && r.lay_outcome !== outF) return false;
    if (!q) return true;
    const inHeader = (r.track_name || "").toLowerCase().includes(q) || String(r.race_number).includes(q);
    const inRunners = (r.runners || []).some((d) => (d.dog_name || "").toLowerCase().includes(q));
    return inHeader || inRunners;
  });

  if (!filtered.length) {
    const tr = document.createElement("tr");
    const td = node("td", "empty-cell", "Sem corridas para os filtros atuais.");
    td.colSpan = 6;
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  const frag = document.createDocumentFragment();
  filtered.forEach((r) => {
    const tr = document.createElement("tr");

    const tdRace = document.createElement("td");
    tdRace.appendChild(node("span", "mono", `${(r.race_time || "").slice(0, 5)} `));
    tdRace.appendChild(node("strong", "", r.track_name));
    tdRace.appendChild(node("div", "sub-line mono", `P${r.race_number} · ${r.race_class || "—"} · ${r.race_distance ? Math.round(r.race_distance) + "m" : "—"}`));
    tr.appendChild(tdRace);

    const tdLay = document.createElement("td");
    const lp = r.lay_pick;
    if (lp) {
      const cell = node("div", "runner-cell");
      cell.appendChild(makeTrapBadge(lp.trap_number));
      cell.appendChild(node("span", "", lp.dog_name));
      tdLay.appendChild(cell);
      const pb = r.paper_bet;
      const oddShow = lp.decimal_odds || pb?.lay_odds;
      const oddTxt = oddShow ? ` · Odd ${Number(oddShow).toFixed(2)}` : "";
      tdLay.appendChild(node("div", "sub-line mono", `${lp.predicted_rank ?? "—"}º/${r.active_runners || (r.runners || []).length} · ${((lp.lose_prob || 0) * 100).toFixed(0)}%${oddTxt}`));
    }
    tr.appendChild(tdLay);

    const tdWin = document.createElement("td");
    const pw = r.probable_winner;
    if (pw) {
      const cell = node("div", "runner-cell");
      cell.appendChild(makeTrapBadge(pw.trap_number));
      cell.appendChild(node("span", "", pw.dog_name));
      tdWin.appendChild(cell);
      tdWin.appendChild(node("div", "sub-line mono", `${((pw.win_prob || 0) * 100).toFixed(1)}% · Fair ${pw.fair_odds ?? "—"}`));
    }
    tr.appendChild(tdWin);

    const tdSt = document.createElement("td");
    if (r.lay_outcome === "ACERTOU_LAY") {
      tdSt.appendChild(node("span", "tag tag-green", "Acertou"));
    } else if (r.lay_outcome === "ERROU_LAY") {
      tdSt.appendChild(node("span", "tag tag-red", "Errou"));
    } else {
      tdSt.appendChild(node("span", "tag tag-amber", "Aberta"));
    }
    if (r.actual_winner) {
      tdSt.appendChild(node("div", "sub-line", `1º T${r.actual_winner.trap_number} ${r.actual_winner.dog_name}`));
    }
    tr.appendChild(tdSt);

    const tdPnl = document.createElement("td");
    tdPnl.className = "num";
    const pb = r.paper_bet;
    if (pb && pb.status === "WON_LAY") {
      tdPnl.appendChild(node("strong", "pos", fmtBRL(pb.pnl, true)));
      tdPnl.appendChild(node("div", "sub-line", pb.placed_before_race ? "Ao vivo" : "Backtest"));
    } else if (pb && pb.status === "LOST_LAY") {
      tdPnl.appendChild(node("strong", "neg", fmtBRL(pb.pnl)));
      tdPnl.appendChild(node("div", "sub-line", pb.placed_before_race ? "Ao vivo" : "Backtest"));
    } else if (pb && pb.status === "PENDING") {
      tdPnl.appendChild(node("span", "", fmtBRL(pb.stake)));
      tdPnl.appendChild(node("div", "sub-line", `Risco ${fmtBRL(pb.liability)}`));
    } else {
      tdPnl.appendChild(node("span", "sub-line", "—"));
    }
    tr.appendChild(tdPnl);

    const tdBtn = document.createElement("td");
    tdBtn.className = "num";
    const isExp = state.expandedRaces.has(r.race_id);
    const btn = node("button", "text-btn", isExp ? "Fechar" : "Traps");
    btn.type = "button";
    btn.setAttribute("aria-expanded", String(isExp));
    btn.addEventListener("click", () => {
      if (isExp) state.expandedRaces.delete(r.race_id);
      else state.expandedRaces.add(r.race_id);
      renderRaces();
    });
    tdBtn.appendChild(btn);
    tr.appendChild(tdBtn);

    frag.appendChild(tr);
    if (isExp) frag.appendChild(buildDetailRow(r));
  });

  tbody.appendChild(frag);
}

async function loadOverview() {
  const qs = state.selectedDate ? `?date=${encodeURIComponent(state.selectedDate)}` : "";
  const resp = await fetch(`/api/overview${qs}`);
  const data = await resp.json();
  renderOverview(data);
}

async function loadRaces() {
  const params = new URLSearchParams();
  if (state.selectedDate) params.set("date", state.selectedDate);
  if (state.selectedTrack && state.selectedTrack !== "ALL") params.set("track", state.selectedTrack);

  const resp = await fetch(`/api/races?${params.toString()}`);
  const data = await resp.json();
  state.races = data.races || [];
  renderRecentSettled(state.races);

  const trackSel = byId("trackSelect");
  const prevTrack = state.selectedTrack;
  trackSel.replaceChildren();
  const allOpt = document.createElement("option");
  allOpt.value = "ALL";
  allOpt.textContent = "Todas as pistas";
  trackSel.appendChild(allOpt);
  (data.tracks || []).forEach((t) => {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = t;
    trackSel.appendChild(opt);
  });
  trackSel.value = Array.from(trackSel.options).some((o) => o.value === prevTrack) ? prevTrack : "ALL";
  state.selectedTrack = trackSel.value;

  const ds = data.day_summary || {};
  const bar = byId("daySummaryBar");
  bar.replaceChildren();
  [
    `${data.date || "—"}`,
    `${ds.total_races ?? 0} corridas`,
    `Lay ${ds.lay_hits ?? 0}/${ds.lay_total ?? 0} (${ds.lay_pct ?? 0}%)`,
    `Saldo dia ${fmtBRL(ds.day_net_pnl ?? 0, true)}`,
  ].forEach((txt) => bar.appendChild(node("span", "", txt)));

  renderRaces();
}

async function refreshAll() {
  await loadOverview();
  await loadRaces();
}

function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  state.ws = ws;

  ws.onopen = () => {
    byId("wsBadge").className = "ws-status is-online";
    byId("wsLabel").textContent = "Ao vivo";
  };

  ws.onmessage = async (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "snapshot") {
      updateStatusBanner(msg.status);
      renderLogs(msg.logs);
    } else if (msg.type === "status") {
      updateStatusBanner(msg.status);
    } else if (msg.type === "log") {
      appendLog(msg.log);
    } else if (msg.type === "study_complete") {
      updateStatusBanner(msg.status);
      await refreshAll();
    }
  };

  ws.onclose = () => {
    byId("wsBadge").className = "ws-status is-offline";
    byId("wsLabel").textContent = "Reconectando";
    setTimeout(connectWebSocket, 3000);
  };
}

function buildSimulatorGrid() {
  const grid = byId("simRunnersGrid");
  grid.replaceChildren();
  let searchTimer = null;

  for (let trap = 1; trap <= 6; trap++) {
    const card = node("div", "sim-runner");
    const head = node("div", "sim-runner-head");
    head.appendChild(makeTrapBadge(trap));
    head.appendChild(node("span", "", `Trap ${trap}`));
    card.appendChild(head);

    const inputs = node("div", "sim-runner-inputs");
    const nameInp = document.createElement("input");
    nameInp.type = "text";
    nameInp.className = "sim-dog-name";
    nameInp.dataset.trap = String(trap);
    nameInp.setAttribute("list", "dogsDatalist");
    nameInp.setAttribute("aria-label", `Galgo Trap ${trap}`);
    nameInp.placeholder = "Nome do galgo";

    const oddsInp = document.createElement("input");
    oddsInp.type = "number";
    oddsInp.step = "0.05";
    oddsInp.min = "1.05";
    oddsInp.className = "sim-dog-odds";
    oddsInp.setAttribute("aria-label", `Odd Trap ${trap}`);
    oddsInp.placeholder = "Odd";

    nameInp.addEventListener("input", () => {
      const q = nameInp.value.trim();
      if (q.length < 2) return;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(async () => {
        const res = await fetch(`/api/dogs/search?q=${encodeURIComponent(q)}`);
        const items = await res.json();
        state.dogCache = items;
        const dl = byId("dogsDatalist");
        dl.replaceChildren();
        items.forEach((d) => {
          const opt = document.createElement("option");
          opt.value = d.dog_name;
          opt.textContent = `${d.dog_name} (${d.runs})`;
          dl.appendChild(opt);
        });
      }, 250);
    });

    inputs.appendChild(nameInp);
    inputs.appendChild(oddsInp);
    card.appendChild(inputs);
    grid.appendChild(card);
  }
}

byId("btnChartBacktest").addEventListener("click", () => {
  state.chartMode = "backtest";
  byId("btnChartBacktest").classList.add("is-active");
  byId("btnChartLive").classList.remove("is-active");
  if (state.overview) renderBankrollChart(state.overview);
});

byId("btnChartLive").addEventListener("click", () => {
  state.chartMode = "live";
  byId("btnChartLive").classList.add("is-active");
  byId("btnChartBacktest").classList.remove("is-active");
  if (state.overview) renderBankrollChart(state.overview);
});

byId("bankrollForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const bankrollStart = parseFloat(byId("cfgBankrollStart").value);
  const layStake = parseFloat(byId("cfgLayStake").value);
  if (!Number.isFinite(bankrollStart) || bankrollStart < 10 || !Number.isFinite(layStake) || layStake < 1) return;
  const btn = byId("btnSaveBankroll");
  btn.disabled = true;
  try {
    await fetch("/api/settings/bankroll", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bankroll_start: bankrollStart, lay_stake: layStake, reset_live: true }),
    });
    modal.close();
    await refreshAll();
  } finally {
    btn.disabled = false;
  }
});

byId("btnRunSim").addEventListener("click", async () => {
  const runners = [];
  document.querySelectorAll(".sim-runner").forEach((row) => {
    const nameInp = row.querySelector(".sim-dog-name");
    const oddsInp = row.querySelector(".sim-dog-odds");
    const name = nameInp.value.trim();
    if (!name) return;
    const match = state.dogCache.find((d) => d.dog_name.toLowerCase() === name.toLowerCase());
    const oddsVal = parseFloat(oddsInp.value);
    runners.push({
      trap_number: Number(nameInp.dataset.trap),
      dog_name: name,
      dog_id: match ? match.dog_id : null,
      decimal_odds: Number.isFinite(oddsVal) && oddsVal > 1.01 ? oddsVal : null,
    });
  });

  const resultBox = byId("simResultBox");
  resultBox.replaceChildren();
  if (runners.length < 3) {
    resultBox.appendChild(node("p", "empty-cell", "Informe pelo menos 3 galgos."));
    return;
  }

  const resp = await fetch("/api/simulate-race", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      track_name: byId("simTrack").value.trim() || "Nottingham",
      race_distance: parseFloat(byId("simDistance").value) || 480,
      race_class: byId("simClass").value.trim() || "A1",
      runners,
    }),
  });
  const data = await resp.json();

  const grid = node("div", "sim-cards");
  const layBox = node("div", "sim-out-card");
  layBox.appendChild(node("span", "kpi-name", "Lay (Perderá)"));
  const layCell = node("div", "runner-cell");
  layCell.appendChild(makeTrapBadge(data.lay_pick.trap_number));
  layCell.appendChild(node("strong", "", `${data.lay_pick.dog_name} (${data.lay_pick.predicted_rank}º/${data.runners.length})`));
  layBox.appendChild(layCell);
  layBox.appendChild(node("span", "sub-line mono", `${data.lay_pick.lose_prob_pct}% derrota · ${data.lay_pick.reasons.join(" · ")}`));

  const winBox = node("div", "sim-out-card");
  winBox.appendChild(node("span", "kpi-name", `Favorito (${data.model_name})`));
  const winCell = node("div", "runner-cell");
  winCell.appendChild(makeTrapBadge(data.probable_winner.trap_number));
  winCell.appendChild(node("strong", "", data.probable_winner.dog_name));
  winBox.appendChild(winCell);
  winBox.appendChild(node("span", "sub-line mono", `${data.probable_winner.win_prob_pct}% vitória · Fair ${data.probable_winner.fair_odds}`));

  grid.appendChild(layBox);
  grid.appendChild(winBox);
  resultBox.appendChild(grid);
});

byId("btnRunStudy").addEventListener("click", async () => {
  await fetch("/api/study/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
});

byId("btnSyncDate").addEventListener("click", async () => {
  const dt = byId("customDateInput").value;
  if (!dt) return;
  state.selectedDate = dt;
  await fetch("/api/study/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ days: 5, target_date: dt }),
  });
});

byId("btnImportBf").addEventListener("click", async () => {
  const btn = byId("btnImportBf");
  const st = byId("bfImportStatus");
  btn.disabled = true;
  st.textContent = "Importando...";
  try {
    const resp = await fetch("/api/import/betfair-historical", { method: "POST" });
    const data = await resp.json();
    st.textContent = `${data.files_scanned} arq · ${data.prices_parsed} preços · ${data.entries_updated} atualizadas`;
    await refreshAll();
  } finally {
    btn.disabled = false;
  }
});

byId("dateSelect").addEventListener("change", async (e) => {
  state.selectedDate = e.target.value;
  state.selectedTrack = "ALL";
  await refreshAll();
});

byId("trackSelect").addEventListener("change", async (e) => {
  state.selectedTrack = e.target.value;
  await loadRaces();
});

byId("outcomeFilter").addEventListener("change", (e) => {
  state.outcomeFilter = e.target.value;
  renderRaces();
});

byId("searchInput").addEventListener("input", (e) => {
  state.searchQuery = e.target.value;
  renderRaces();
});

buildSimulatorGrid();
connectWebSocket();
refreshAll();
