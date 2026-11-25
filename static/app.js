"use strict";

const state = {
  selectedDate: null,
  selectedTrack: "ALL",
  searchQuery: "",
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

// Accessible tabs (WAI-ARIA tablist pattern)
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
}

tabButtons.forEach((btn, idx) => {
  btn.addEventListener("click", () => activateTab(btn));
  btn.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight") {
      e.preventDefault();
      const next = tabButtons[(idx + 1) % tabButtons.length];
      activateTab(next);
      next.focus();
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      const prev = tabButtons[(idx - 1 + tabButtons.length) % tabButtons.length];
      activateTab(prev);
      prev.focus();
    }
  });
});

function updateStatusBanner(status) {
  if (!status) return;
  byId("phasePill").textContent = String(status.phase || "idle").toUpperCase();
  byId("statusMessage").textContent = status.message || "—";
  byId("studyProgress").value = Number(status.progress ?? 0);
  byId("cycleCounter").textContent = `Ciclos: ${status.cycle_count ?? 0}`;
  byId("lastRunLabel").textContent = `Última atualização: ${status.last_run_at ? fmtTime(status.last_run_at) : "—"}`;
  byId("btnRunStudy").disabled = Boolean(status.running);
}

function createLogElement(log) {
  const row = node("div", `log-item ${log.level || "INFO"}`);
  row.appendChild(node("span", "log-time", fmtTime(log.created_at)));
  row.appendChild(node("span", "log-phase", log.phase || "sistema"));
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

function renderUpcomingBets(upcoming) {
  const card = byId("upcomingCard");
  const tbody = byId("upcomingBody");
  tbody.replaceChildren();

  if (!upcoming || !upcoming.length) {
    card.hidden = true;
    return;
  }

  card.hidden = false;
  byId("upcomingCountBadge").textContent = `${upcoming.length} aberta${upcoming.length > 1 ? "s" : ""}`;

  upcoming.forEach((b) => {
    const tr = document.createElement("tr");
    const timePart = (b.race_dt || "").split(" ")[1] || "";
    tr.appendChild(node("td", "", `${timePart.slice(0, 5)} • ${b.track_name} (P${b.race_number})`));

    const dogTd = document.createElement("td");
    const cell = node("div", "runner-cell");
    cell.appendChild(makeTrapBadge(b.trap_number));
    cell.appendChild(node("strong", "", b.dog_name));
    dogTd.appendChild(cell);
    tr.appendChild(dogTd);

    tr.appendChild(node("td", "", `${(Number(b.lose_prob || 0) * 100).toFixed(1)}%`));
    tr.appendChild(node("td", "", Number(b.lay_odds).toFixed(2)));
    tr.appendChild(node("td", "", `${fmtBRL(b.stake)} / Risco ${fmtBRL(b.liability)}`));

    const stTd = document.createElement("td");
    stTd.appendChild(node("span", "badge badge-warning", "Aposta Travada • Aguardando"));
    tr.appendChild(stTd);

    tbody.appendChild(tr);
  });
}

function renderBankrollChart(curve, startBankroll) {
  const box = byId("bankrollChart");
  box.replaceChildren();
  if (!curve || curve.length < 2) {
    box.appendChild(node("p", "empty-cell", "Aguardando mais simulações para traçar o gráfico da banca."));
    return;
  }

  const base = Number(startBankroll || 1000);
  const ns = "http://www.w3.org/2000/svg";
  const values = [base, ...curve.map((c) => Number(c.bankroll_after))];
  const minV = Math.min(...values) - 20;
  const maxV = Math.max(...values) + 20;
  const w = 620;
  const h = 210;
  const pad = 28;

  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Gráfico da evolução da banca de testes em Reais");

  const baseY = h - pad - ((base - minV) / Math.max(maxV - minV, 1)) * (h - pad * 2);
  const baseline = document.createElementNS(ns, "line");
  baseline.setAttribute("x1", String(pad));
  baseline.setAttribute("y1", baseY.toFixed(1));
  baseline.setAttribute("x2", String(w - pad));
  baseline.setAttribute("y2", baseY.toFixed(1));
  baseline.setAttribute("stroke", "#cbd5e1");
  baseline.setAttribute("stroke-dasharray", "4 4");
  svg.appendChild(baseline);

  const pts = values.map((v, i) => {
    const x = pad + (i / (values.length - 1)) * (w - pad * 2);
    const y = h - pad - ((v - minV) / Math.max(maxV - minV, 1)) * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });

  const lastVal = values[values.length - 1];
  const color = lastVal >= base ? "#047857" : "#b91c1c";

  const polyline = document.createElementNS(ns, "polyline");
  polyline.setAttribute("fill", "none");
  polyline.setAttribute("stroke", color);
  polyline.setAttribute("stroke-width", "2.5");
  polyline.setAttribute("points", pts.join(" "));
  svg.appendChild(polyline);

  const topTxt = document.createElementNS(ns, "text");
  topTxt.setAttribute("x", String(pad));
  topTxt.setAttribute("y", "16");
  topTxt.setAttribute("fill", "#475569");
  topTxt.setAttribute("font-size", "11");
  topTxt.textContent = `Máx: ${fmtBRL(Math.max(...values))} • Atual: ${fmtBRL(lastVal)}`;
  svg.appendChild(topTxt);

  const botTxt = document.createElementNS(ns, "text");
  botTxt.setAttribute("x", String(pad));
  botTxt.setAttribute("y", String(h - 6));
  botTxt.setAttribute("fill", "#475569");
  botTxt.setAttribute("font-size", "11");
  botTxt.textContent = `Mín: ${fmtBRL(Math.min(...values))} (${curve.length} liquidações reais)`;
  svg.appendChild(botTxt);

  box.appendChild(svg);
}

function renderFeatures(features) {
  const list = byId("featureList");
  list.replaceChildren();
  if (!features || !features.length) {
    list.appendChild(node("p", "empty-cell", "Aguardando treinamento do modelo."));
    return;
  }
  const maxW = Math.max(...features.map((f) => Number(f.weight || 0)), 1);
  features.forEach((f) => {
    const row = node("div", "feat-row");
    row.appendChild(node("span", "", f.label || f.feature));
    const meter = document.createElement("meter");
    meter.className = "feat-meter";
    meter.min = 0;
    meter.max = maxW;
    meter.value = Number(f.weight || 0);
    row.appendChild(meter);
    row.appendChild(node("strong", "", `${Number(f.weight || 0).toFixed(1)}%`));
    list.appendChild(row);
  });
}

function renderAdaptiveLearning(al) {
  const badge = byId("adaptiveModeBadge");
  const bar = byId("adaptiveMetaBar");
  const tbody = byId("adaptiveBody");
  bar.replaceChildren();
  tbody.replaceChildren();

  if (!al || !al.contexts) {
    badge.className = "badge badge-neutral";
    badge.textContent = "Aguardando dados";
    const tr = document.createElement("tr");
    const td = node("td", "empty-cell", "Aguardando ciclo de aprendizado adaptativo.");
    td.colSpan = 6;
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  const defensive = Boolean(al.defensive_mode);
  badge.className = `badge ${defensive ? "badge-warning" : "badge-success"}`;
  badge.textContent = defensive ? "Modo Defensivo (Drawdown)" : "Modo Normal";

  [
    `Perfis bloqueados: ${al.blocked_contexts_count ?? 0}`,
    `Entradas evitadas pelos bloqueios: ${al.races_protected_count ?? 0}`,
    `+EV mínimo ativo: ${((al.active_min_ev ?? 0.01) * 100).toFixed(1)}%`,
    `Risco máx ativo/aposta: ${al.active_max_risk_pct ?? 15}%`,
  ].forEach((txt) => bar.appendChild(node("span", "", txt)));

  if (!al.contexts.length) {
    const tr = document.createElement("tr");
    const td = node("td", "empty-cell", "Nenhum contexto atingiu amostra mínima ainda.");
    td.colSpan = 6;
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  al.contexts.forEach((c) => {
    const tr = document.createElement("tr");
    tr.appendChild(node("td", "", c.context));
    tr.appendChild(node("td", "", c.bets));
    tr.appendChild(node("td", "", `${c.win_pct}%`));
    tr.appendChild(node("td", c.roi_pct >= 0 ? "text-success" : "text-danger", `${c.roi_pct}%`));
    tr.appendChild(node("td", "", `${c.pred_win_pct}% vs ${c.actual_win_pct}%`));
    const stTd = document.createElement("td");
    const isBlocked = c.status === "BLOQUEADO";
    stTd.appendChild(node("span", `badge ${isBlocked ? "badge-danger" : "badge-success"}`, c.status));
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
    `Comissão Exchange: ${r.commission_pct ?? 5}%`,
    `Spread + Slippage: +${r.spread_pct ?? 5}% + ${r.slippage_ticks ?? 1} tick`,
    `Risco máx/aposta: ${r.max_risk_pct ?? 15}%`,
    `Drawdown Máx: -${fmtBRL(b.max_drawdown_brl ?? 0)} (${b.max_drawdown_pct ?? 0}%)`,
    `Profit Factor: ${b.profit_factor ?? 0}`,
    `Expectativa/Aposta: ${fmtBRL(b.expectancy_brl ?? 0, true)}`,
  ].forEach((txt) => bar.appendChild(node("span", "", txt)));

  const calBody = byId("calibrationBody");
  calBody.replaceChildren();
  if (!buckets || !buckets.length) {
    const tr = document.createElement("tr");
    const td = node("td", "empty-cell", "Aguardando ciclo de calibração.");
    td.colSpan = 5;
    tr.appendChild(td);
    calBody.appendChild(tr);
    return;
  }
  buckets.forEach((bk) => {
    const tr = document.createElement("tr");
    tr.appendChild(node("td", "", bk.band));
    tr.appendChild(node("td", "", bk.count));
    tr.appendChild(node("td", "", `${bk.pred_win_pct}%`));
    tr.appendChild(node("td", "", `${bk.actual_win_pct}%`));
    tr.appendChild(node("td", "text-success", `${bk.actual_lose_pct}%`));
    calBody.appendChild(tr);
  });
}

function renderOverview(data) {
  updateStatusBanner(data.status);

  const paper = data.paper_summary || {};
  const bt = data.backtest_summary || {};
  const totals = data.totals || {};
  const model = data.latest_model;

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

  byId("kpiBankroll").textContent = fmtBRL(currentBankroll);
  const badge = byId("kpiProfitBadge");
  badge.className = `badge ${isProfit ? "badge-success" : "badge-danger"}`;
  badge.textContent = isProfit ? `LUCRO ${fmtBRL(netPnl, true)}` : `PREJUÍZO ${fmtBRL(netPnl)}`;
  byId("kpiBankrollSub").textContent = `Saldo livre: ${fmtBRL(availBalance)} • Em risco: ${fmtBRL(committedRisk)} • Stake: ${fmtBRL(layStake)}`;

  byId("kpiTotalWon").textContent = fmtBRL(paper.total_won_amount ?? 0, true);
  byId("kpiTotalLost").textContent = `-${fmtBRL(paper.total_lost_amount ?? 0)}`;
  byId("kpiWinLossSub").textContent = `${paper.won_bets ?? 0} acertos / ${paper.lost_bets ?? 0} erros ao vivo (ROI: ${paper.roi_pct ?? 0}%)`;

  const hasLiveSettled = (paper.total_bets ?? 0) > 0;
  byId("kpiLayAcc").textContent = hasLiveSettled
    ? `${paper.win_rate_pct}%`
    : `${bt.win_rate_pct ?? (model?.lay_accuracy ?? 0)}%`;
  byId("kpiLaySub").textContent = hasLiveSettled
    ? `Banca ao vivo (${paper.total_bets} liquidadas) • Estudo: ${bt.win_rate_pct ?? 0}%`
    : `Estudo histórico (${bt.total_bets ?? 0} corridas) • Aguardando 1ª corrida ao vivo`;
  const pendingCount = paper.pending_bets ?? 0;
  const pBadge = byId("kpiPendingBadge");
  pBadge.className = `badge ${pendingCount > 0 ? "badge-warning" : "badge-neutral"}`;
  pBadge.textContent = `${pendingCount} aberta${pendingCount === 1 ? "" : "s"}`;

  byId("kpiRaces").textContent = `${totals.total_races ?? 0} corridas`;
  byId("kpiModelSub").textContent = model
    ? `${model.model_name} • Log-Loss: ${Number(model.log_loss).toFixed(4)} • ${totals.total_dogs ?? 0} galgos`
    : "Aguardando primeiro ciclo";

  renderUpcomingBets(data.upcoming_bets);

  // Dates dropdown
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
  if (state.selectedDate) {
    byId("customDateInput").value = state.selectedDate;
  }

  renderBankrollChart(data.bankroll_curve, startBankroll);
  renderFeatures(data.feature_importances);
  renderRiskAndCalibration(data.simulation_rules, bt, data.calibration_buckets);
  renderAdaptiveLearning(data.adaptive_learning);

  if (data.simulation_rules?.betfair_hist_dir) {
    byId("bfHistDesc").textContent = `Coloque arquivos gratuitos (.tar.bz2 / .bz2) do Betfair Historical Data em "${data.simulation_rules.betfair_hist_dir}" (${data.simulation_rules.exchange_entries_count} entradas já vinculadas).`;
  }

  // Model history
  const mhBody = byId("modelHistoryBody");
  mhBody.replaceChildren();
  (data.model_history || []).slice().reverse().forEach((m) => {
    const tr = document.createElement("tr");
    [
      `#${m.id}`,
      m.model_name,
      `${m.val_races} corridas`,
      Number(m.log_loss).toFixed(4),
      Number(m.brier_score).toFixed(4),
      `${m.lay_accuracy}%`,
      `${m.lay_roi_pct}%`,
    ].forEach((val) => tr.appendChild(node("td", "", val)));
    mhBody.appendChild(tr);
  });

  // Error audit
  const errBody = byId("errorAuditBody");
  errBody.replaceChildren();
  const errs = data.error_audit || [];
  if (!errs.length) {
    const tr = document.createElement("tr");
    const td = node("td", "empty-cell", "Nenhum erro registrado ainda.");
    td.colSpan = 7;
    tr.appendChild(td);
    errBody.appendChild(tr);
  } else {
    errs.forEach((e) => {
      const tr = document.createElement("tr");
      tr.appendChild(node("td", "", e.iso_date));
      tr.appendChild(node("td", "", `${e.track_name} • P${e.race_number}`));
      const dogTd = document.createElement("td");
      const wrap = node("div", "runner-cell");
      wrap.appendChild(makeTrapBadge(e.trap_number));
      wrap.appendChild(node("span", "", e.dog_name));
      dogTd.appendChild(wrap);
      tr.appendChild(dogTd);
      tr.appendChild(node("td", "", `${(Number(e.win_prob) * 100).toFixed(1)}%`));
      tr.appendChild(node("td", "", Number(e.lay_odds).toFixed(2)));
      tr.appendChild(node("td", "text-danger", `-${fmtBRL(e.liability)}`));
      tr.appendChild(node("td", "", e.error_reason || "—"));
      errBody.appendChild(tr);
    });
  }

  if (!byId("liveLogsList").children.length && data.logs) {
    renderLogs(data.logs);
  }
}

function buildDetailRow(race) {
  const tr = node("tr", "detail-row");
  const td = document.createElement("td");
  td.colSpan = 6;

  const panel = node("div", "detail-panel");
  const lp = race.lay_pick;
  if (lp && lp.reasons && lp.reasons.length) {
    const reasonBar = node(
      "div",
      "detail-reasons",
      `Motivo da seleção Lay (${lp.dog_name}): ${lp.reasons.join(" • ")}`
    );
    panel.appendChild(reasonBar);
  }

  const wrap = node("div", "table-wrap");
  const tbl = node("table", "data-table");
  const thead = document.createElement("thead");
  const hrow = document.createElement("tr");
  ["Rank", "Trap / Galgo", "Prob. Ganhar", "Prob. Perder", "Forma", "Odd SP", "Chegada Real"].forEach((h) => {
    const th = node("th", "", h);
    th.scope = "col";
    hrow.appendChild(th);
  });
  thead.appendChild(hrow);
  tbl.appendChild(thead);

  const tbody = document.createElement("tbody");
  (race.runners || []).forEach((d) => {
    const rtr = document.createElement("tr");
    rtr.appendChild(node("td", "", `${d.predicted_rank ?? "—"}º`));

    const gtd = document.createElement("td");
    const cell = node("div", "runner-cell");
    cell.appendChild(makeTrapBadge(d.trap_number));
    cell.appendChild(node("span", "", d.dog_name));
    if (d.is_lay_pick === 1) {
      cell.appendChild(node("span", "badge badge-danger", "LAY"));
    }
    gtd.appendChild(cell);
    rtr.appendChild(gtd);

    rtr.appendChild(node("td", "", d.win_prob != null ? `${(d.win_prob * 100).toFixed(1)}%` : "—"));
    rtr.appendChild(node("td", "", d.lose_prob != null ? `${(d.lose_prob * 100).toFixed(1)}%` : "—"));
    rtr.appendChild(node("td", "", d.stats?.recent_form || "—"));
    rtr.appendChild(node("td", "", d.decimal_odds ? Number(d.decimal_odds).toFixed(2) : "—"));
    const posText = d.result_position ? (d.result_position === 1 ? "1º (Vencedor)" : `${d.result_position}º`) : "Aguardando";
    rtr.appendChild(node("td", d.result_position === 1 ? "text-success" : "", posText));
    tbody.appendChild(rtr);
  });
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
  panel.appendChild(wrap);
  td.appendChild(panel);
  tr.appendChild(td);
  return tr;
}

function renderRaces() {
  const tbody = byId("racesTableBody");
  tbody.replaceChildren();
  const q = state.searchQuery.trim().toLowerCase();

  const filtered = state.races.filter((r) => {
    if (!q) return true;
    const inHeader =
      (r.track_name || "").toLowerCase().includes(q) ||
      String(r.race_number).includes(q);
    const inRunners = (r.runners || []).some((d) => (d.dog_name || "").toLowerCase().includes(q));
    return inHeader || inRunners;
  });

  if (!filtered.length) {
    const tr = document.createElement("tr");
    const td = node("td", "empty-cell", "Nenhuma corrida encontrada para os filtros selecionados.");
    td.colSpan = 6;
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  const frag = document.createDocumentFragment();
  filtered.forEach((r) => {
    const tr = document.createElement("tr");

    // 1. Horário / Pista
    const tdRace = document.createElement("td");
    tdRace.appendChild(node("strong", "", `${(r.race_time || "").slice(0, 5)} • ${r.track_name}`));
    const kindTxt = r.is_completed ? "" : " • FUTURA";
    tdRace.appendChild(
      node(
        "div",
        "meta-inline",
        `Páreo ${r.race_number} • ${r.race_class || "—"} • ${r.race_distance ? Math.round(r.race_distance) + "m" : "—"}${kindTxt}`
      )
    );
    tr.appendChild(tdRace);

    // 2. Galgo para Apostar (Perderá)
    const tdLay = document.createElement("td");
    const lp = r.lay_pick;
    if (lp) {
      const cell = node("div", "runner-cell");
      cell.appendChild(makeTrapBadge(lp.trap_number));
      cell.appendChild(node("strong", "", lp.dog_name));
      tdLay.appendChild(cell);
      const rankInfo = `${lp.predicted_rank ?? "—"}º de ${r.active_runners || (r.runners || []).length}`;
      const losePct = lp.lose_prob != null ? `${(lp.lose_prob * 100).toFixed(1)}% perde` : "";
      const pb = r.paper_bet;
      const oddShow = lp.decimal_odds || pb?.lay_odds;
      const oddTxt = oddShow ? ` • Odd ${Number(oddShow).toFixed(2)}` : "";
      tdLay.appendChild(node("div", "meta-inline", `${rankInfo} • ${losePct}${oddTxt}`));
    } else {
      tdLay.textContent = "—";
    }
    tr.appendChild(tdLay);

    // 3. Vencedor Provável
    const tdWin = document.createElement("td");
    const pw = r.probable_winner;
    if (pw) {
      const cell = node("div", "runner-cell");
      cell.appendChild(makeTrapBadge(pw.trap_number));
      cell.appendChild(node("span", "", pw.dog_name));
      tdWin.appendChild(cell);
      tdWin.appendChild(
        node("div", "meta-inline", `Chance: ${((pw.win_prob || 0) * 100).toFixed(1)}% • Fair ${pw.fair_odds ?? "—"}`)
      );
    } else {
      tdWin.textContent = "—";
    }
    tr.appendChild(tdWin);

    // 4. Status do Bot
    const tdStatus = document.createElement("td");
    if (r.lay_outcome === "ACERTOU_LAY") {
      tdStatus.appendChild(node("span", "badge badge-success", "Acertou"));
    } else if (r.lay_outcome === "ERROU_LAY") {
      tdStatus.appendChild(node("span", "badge badge-danger", "Errou"));
    } else {
      tdStatus.appendChild(node("span", "badge badge-warning", "Aposta Aberta"));
    }
    if (r.actual_winner) {
      tdStatus.appendChild(node("div", "meta-inline", `Vencedor: T${r.actual_winner.trap_number} ${r.actual_winner.dog_name}`));
    } else {
      tdStatus.appendChild(node("div", "meta-inline", "Aguardando resultado real"));
    }
    tr.appendChild(tdStatus);

    // 5. Resultado (R$)
    const tdPnl = document.createElement("td");
    const pb = r.paper_bet;
    if (pb && pb.status === "WON_LAY") {
      tdPnl.appendChild(node("strong", "text-success", fmtBRL(pb.pnl, true)));
      const tag = pb.placed_before_race ? "Ao vivo pré-corrida" : "Backtest histórico";
      tdPnl.appendChild(node("div", "meta-inline", tag));
    } else if (pb && pb.status === "LOST_LAY") {
      tdPnl.appendChild(node("strong", "text-danger", fmtBRL(pb.pnl)));
      const tag = pb.placed_before_race ? "Ao vivo pré-corrida" : "Backtest histórico";
      tdPnl.appendChild(node("div", "meta-inline", tag));
    } else if (pb && pb.status === "PENDING") {
      tdPnl.appendChild(node("strong", "", `Stake ${fmtBRL(pb.stake)}`));
      tdPnl.appendChild(node("div", "meta-inline", `Risco ${fmtBRL(pb.liability)}`));
    } else if (!r.is_completed) {
      tdPnl.appendChild(node("span", "meta-inline", "Aguardando saldo livre"));
    } else if (r.lay_outcome !== "PENDENTE") {
      tdPnl.appendChild(node("span", "meta-inline", "Fora da faixa de odd"));
    } else {
      tdPnl.textContent = "—";
    }
    tr.appendChild(tdPnl);

    // 6. Detalhes toggle
    const tdBtn = document.createElement("td");
    const isExpanded = state.expandedRaces.has(r.race_id);
    const btn = node("button", "btn btn-link", isExpanded ? "Ocultar" : "Ver traps");
    btn.type = "button";
    btn.setAttribute("aria-expanded", String(isExpanded));
    btn.addEventListener("click", () => {
      if (state.expandedRaces.has(r.race_id)) {
        state.expandedRaces.delete(r.race_id);
      } else {
        state.expandedRaces.add(r.race_id);
      }
      renderRaces();
    });
    tdBtn.appendChild(btn);
    tr.appendChild(tdBtn);

    frag.appendChild(tr);
    if (isExpanded) {
      frag.appendChild(buildDetailRow(r));
    }
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
    `Data: ${data.date || "—"}`,
    `Corridas: ${ds.total_races ?? 0}`,
    `Acertos Lay: ${ds.lay_hits ?? 0}/${ds.lay_total ?? 0} (${ds.lay_pct ?? 0}%)`,
    `Ganhou no dia: ${fmtBRL(ds.day_won_amount ?? 0, true)}`,
    `Perdeu no dia: -${fmtBRL(ds.day_lost_amount ?? 0)}`,
    `Saldo do dia: ${fmtBRL(ds.day_net_pnl ?? 0, true)}`,
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
    const pill = byId("wsBadge");
    pill.className = "status-pill is-online";
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
    const pill = byId("wsBadge");
    pill.className = "status-pill is-offline";
    byId("wsLabel").textContent = "Reconectando...";
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
    nameInp.setAttribute("aria-label", `Nome do galgo na Trap ${trap}`);
    nameInp.placeholder = "Nome do galgo...";

    const oddsInp = document.createElement("input");
    oddsInp.type = "number";
    oddsInp.step = "0.05";
    oddsInp.min = "1.05";
    oddsInp.className = "sim-dog-odds";
    oddsInp.setAttribute("aria-label", `Odd de Lay na Trap ${trap}`);
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
          opt.textContent = `${d.dog_name} (${d.runs} corridas)`;
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

byId("bankrollForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const bankrollStart = parseFloat(byId("cfgBankrollStart").value);
  const layStake = parseFloat(byId("cfgLayStake").value);
  if (!Number.isFinite(bankrollStart) || bankrollStart < 10 || !Number.isFinite(layStake) || layStake < 1) {
    return;
  }
  const btn = byId("btnSaveBankroll");
  btn.disabled = true;
  try {
    await fetch("/api/settings/bankroll", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bankroll_start: bankrollStart, lay_stake: layStake, reset_live: true }),
    });
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
    resultBox.appendChild(node("p", "empty-cell", "Informe pelo menos 3 galgos para evitar seleção de 1º/último."));
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

  const grid = node("div", "sim-summary-grid");
  const layBox = node("div", "sim-box");
  layBox.appendChild(node("div", "kpi-label", "Galgo para Apostar (Perderá / Lay)"));
  const layCell = node("div", "runner-cell");
  layCell.appendChild(makeTrapBadge(data.lay_pick.trap_number));
  layCell.appendChild(node("strong", "", `${data.lay_pick.dog_name} (${data.lay_pick.predicted_rank}º de ${data.runners.length})`));
  layBox.appendChild(layCell);
  layBox.appendChild(node("p", "kpi-sub", `Chance de perder: ${data.lay_pick.lose_prob_pct}% • ${data.lay_pick.reasons.join(" • ")}`));

  const winBox = node("div", "sim-box");
  winBox.appendChild(node("div", "kpi-label", `Vencedor Provável (${data.model_name})`));
  const winCell = node("div", "runner-cell");
  winCell.appendChild(makeTrapBadge(data.probable_winner.trap_number));
  winCell.appendChild(node("strong", "", data.probable_winner.dog_name));
  winBox.appendChild(winCell);
  winBox.appendChild(node("p", "kpi-sub", `Prob. vitória: ${data.probable_winner.win_prob_pct}% • Fair Odd: ${data.probable_winner.fair_odds}`));

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

byId("dateSelect").addEventListener("change", async (e) => {
  state.selectedDate = e.target.value;
  state.selectedTrack = "ALL";
  await refreshAll();
});

byId("trackSelect").addEventListener("change", async (e) => {
  state.selectedTrack = e.target.value;
  await loadRaces();
});

byId("searchInput").addEventListener("input", (e) => {
  state.searchQuery = e.target.value;
  renderRaces();
});

byId("btnImportBf").addEventListener("click", async () => {
  const btn = byId("btnImportBf");
  const st = byId("bfImportStatus");
  btn.disabled = true;
  st.textContent = "Escaneando arquivos...";
  try {
    const resp = await fetch("/api/import/betfair-historical", { method: "POST" });
    const data = await resp.json();
    st.textContent = `${data.files_scanned} arquivo(s) • ${data.prices_parsed} preços • ${data.entries_updated} entradas atualizadas.`;
    await refreshAll();
  } finally {
    btn.disabled = false;
  }
});

buildSimulatorGrid();
connectWebSocket();
refreshAll();
