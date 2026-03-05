let data;
let populationData;
let profiles;
let byKey;
let raw;
let ACTIVE_ROLE;
let currentPopulationKey = "random";
const populationCache = new Map();

const POPULATION_SOURCES = {
  random: "./data/population_random.json",
  umea: "./data/population_umea.json",
  default: "./data/population_report.json",
};

async function loadData() {
  const predictions = await fetch("./data/predictions.json").then((r) => {
    if (!r.ok) throw new Error("Kunde inte läsa ./data/predictions.json");
    return r.json();
  });
  return { predictions };
}

async function loadPopulationDataset(key) {
  const resolved = POPULATION_SOURCES[key] ? key : "random";
  if (populationCache.has(resolved)) return populationCache.get(resolved);
  const primary = POPULATION_SOURCES[resolved];
  const fallback = POPULATION_SOURCES.default;
  const response = await fetch(primary);
  if (!response.ok) {
    if (resolved !== "default") {
      const fallbackResp = await fetch(fallback);
      if (!fallbackResp.ok) {
        throw new Error(`Kunde inte läsa ${primary} eller ${fallback}`);
      }
      const fallbackData = await fallbackResp.json();
      populationCache.set(resolved, fallbackData);
      return fallbackData;
    }
    throw new Error(`Kunde inte läsa ${primary}`);
  }
  const parsed = await response.json();
  populationCache.set(resolved, parsed);
  return parsed;
}

const els = {
  tabIndivid: document.getElementById("tabIndivid"),
  tabPopulation: document.getElementById("tabPopulation"),
  viewIndivid: document.getElementById("viewIndivid"),
  viewPopulation: document.getElementById("viewPopulation"),

  workplace: document.getElementById("workplace"),
  specialist: document.getElementById("specialist"),
  phd: document.getElementById("phd"),
  years: document.getElementById("years"),
  yearsValue: document.getElementById("yearsValue"),
  actualSalary: document.getElementById("actualSalary"),
  q10: document.getElementById("q10"),
  q50: document.getElementById("q50"),
  q90: document.getElementById("q90"),
  actualVsMedian: document.getElementById("actualVsMedian"),
  supportInfo: document.getElementById("supportInfo"),
  chart: document.getElementById("chart"),

  popNNational: document.getElementById("popNNational"),
  popDataset: document.getElementById("popDataset"),
  popNPopulation: document.getElementById("popNPopulation"),
  popMedianPred: document.getElementById("popMedianPred"),
  popMedianGap: document.getElementById("popMedianGap"),
  popMeta: document.getElementById("popMeta"),
  popUnseen: document.getElementById("popUnseen"),
  popChartDecile: document.getElementById("popChartDecile"),
  popChartEcdf: document.getElementById("popChartEcdf"),
  popChartScatter: document.getElementById("popChartScatter"),
  popChartGap: document.getElementById("popChartGap"),
  popTopPeople: document.getElementById("popTopPeople"),
  popBottomPeople: document.getElementById("popBottomPeople"),
  popGroupRankings: document.getElementById("popGroupRankings"),
};

function profileKey(role, workplace, specialist, phd) {
  return [role, workplace, specialist, phd].join("||");
}

function fillSelect(selectEl, values, preferredValue = null) {
  const previous = preferredValue ?? selectEl.value;
  selectEl.innerHTML = "";
  for (const value of values) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = value;
    selectEl.appendChild(opt);
  }
  if (values.includes(previous)) {
    selectEl.value = previous;
  } else {
    selectEl.value = values[0] ?? "";
  }
}

function allowedValues(field, current) {
  const matches = profiles.filter((p) => {
    return (
      p.role === ACTIVE_ROLE &&
      (!current.workplace || p.workplace === current.workplace) &&
      (!current.specialist || p.specialist === current.specialist) &&
      (!current.phd || p.phd === current.phd)
    );
  });
  return [...new Set(matches.map((p) => p[field]))].sort((a, b) =>
    a.localeCompare(b, "sv")
  );
}

function syncFilters() {
  const state = {
    workplace: els.workplace.value || null,
    specialist: els.specialist.value || null,
    phd: els.phd.value || null,
  };

  const workplaceValues = allowedValues("workplace", {
    ...state,
    workplace: null,
  });
  fillSelect(els.workplace, workplaceValues, state.workplace);
  state.workplace = els.workplace.value || null;

  const specialistValues = allowedValues("specialist", {
    ...state,
    specialist: null,
  });
  fillSelect(els.specialist, specialistValues, state.specialist);
  state.specialist = els.specialist.value || null;

  const phdValues = allowedValues("phd", {
    ...state,
    phd: null,
  });
  fillSelect(els.phd, phdValues, state.phd);
}

function lerpAt(xArr, yArr, x) {
  if (x <= xArr[0]) return yArr[0];
  if (x >= xArr[xArr.length - 1]) return yArr[yArr.length - 1];
  let lo = 0;
  let hi = xArr.length - 1;
  while (hi - lo > 1) {
    const mid = Math.floor((lo + hi) / 2);
    if (xArr[mid] <= x) lo = mid;
    else hi = mid;
  }
  const x0 = xArr[lo];
  const x1 = xArr[hi];
  const y0 = yArr[lo];
  const y1 = yArr[hi];
  const t = (x - x0) / (x1 - x0);
  return y0 + t * (y1 - y0);
}

function sek(v) {
  return `${Math.round(v).toLocaleString("sv-SE")} kr/mån`;
}

function sekSimple(v) {
  if (!Number.isFinite(v)) return "-";
  return `${Math.round(v).toLocaleString("sv-SE")} kr`;
}

function fmtSignedSek(v) {
  const rounded = Math.round(v);
  const abs = Math.abs(rounded).toLocaleString("sv-SE");
  return `${rounded >= 0 ? "+" : "-"}${abs} kr/mån`;
}

function quantile(arr, q) {
  if (!arr.length) return 0;
  const sorted = [...arr].sort((a, b) => a - b);
  const pos = (sorted.length - 1) * q;
  const base = Math.floor(pos);
  const rest = pos - base;
  if (sorted[base + 1] === undefined) return sorted[base];
  return sorted[base] + rest * (sorted[base + 1] - sorted[base]);
}

function normalizeSupport(support) {
  if (!support.length) return { t: [], lo: 0, hi: 1 };
  let lo = quantile(support, 0.1);
  let hi = quantile(support, 0.9);
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) {
    lo = Math.min(...support);
    hi = Math.max(...support);
    if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) {
      return { t: support.map(() => 0), lo: 0, hi: 1 };
    }
  }
  const t = support.map((s) => Math.max(0, Math.min(1, (s - lo) / (hi - lo))));
  return { t, lo, hi };
}

function interpRgb(t) {
  const scale = [
    [0.0, [230, 245, 255]],
    [0.5, [158, 202, 225]],
    [1.0, [49, 130, 189]],
  ];
  const u = Math.max(0, Math.min(1, t));
  for (let i = 0; i < scale.length - 1; i += 1) {
    const [p0, c0] = scale[i];
    const [p1, c1] = scale[i + 1];
    if (u <= p1) {
      const f = p1 === p0 ? 0 : (u - p0) / (p1 - p0);
      return [
        Math.round(c0[0] + f * (c1[0] - c0[0])),
        Math.round(c0[1] + f * (c1[1] - c0[1])),
        Math.round(c0[2] + f * (c1[2] - c0[2])),
      ];
    }
  }
  return scale[scale.length - 1][1];
}

function supportFillColor(t) {
  const [r, g, b] = interpRgb(t);
  const alpha = 0.08 + t * (0.30 - 0.08);
  return `rgba(${r},${g},${b},${alpha.toFixed(3)})`;
}

function extraQuantileTraces(profile, x) {
  const extras = profile.curve.extra_quantiles;
  if (!extras || typeof extras !== "object") return [];
  const keys = Object.keys(extras).sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));
  const traces = [];
  for (const key of keys) {
    const y = extras[key];
    if (!Array.isArray(y) || y.length !== x.length) continue;
    traces.push({
      x,
      y,
      mode: "lines",
      name: `${key.slice(1)}e percentil`,
      line: { width: 1.25, color: "rgba(31,42,68,0.45)", dash: "dot" },
      visible: "legendonly",
    });
  }
  return traces;
}

function buildBandSegmentTraces(x, q10, q90, tNorm) {
  const traces = [];
  for (let i = 0; i < x.length - 1; i += 1) {
    traces.push({
      x: [x[i], x[i + 1], x[i + 1], x[i]],
      y: [q90[i], q90[i + 1], q10[i + 1], q10[i]],
      mode: "lines",
      line: { width: 0 },
      fill: "toself",
      fillcolor: supportFillColor(tNorm[i]),
      hoverinfo: "skip",
      showlegend: false,
    });
  }
  return traces;
}

function smoothSeries(values, windowSize = 11) {
  const n = values.length;
  if (n === 0 || windowSize <= 1) return [...values];
  const half = Math.floor(windowSize / 2);
  const out = new Array(n);
  for (let i = 0; i < n; i += 1) {
    let sum = 0;
    let count = 0;
    const start = Math.max(0, i - half);
    const end = Math.min(n - 1, i + half);
    for (let j = start; j <= end; j += 1) {
      sum += values[j];
      count += 1;
    }
    out[i] = count > 0 ? sum / count : values[i];
  }
  return out;
}

function uncertaintyOuterLines(q10, q50, q90, tNorm) {
  const tSmooth = smoothSeries(tNorm, 15);
  const lowOuter = [];
  const highOuter = [];
  for (let i = 0; i < q50.length; i += 1) {
    const extra = (1.0 - tSmooth[i]) * 0.75;
    const dLow = q50[i] - q10[i];
    const dHigh = q90[i] - q50[i];
    lowOuter.push(Math.max(0, q10[i] - dLow * extra));
    highOuter.push(q90[i] + dHigh * extra);
  }
  const lowSmooth = smoothSeries(lowOuter, 11).map((v, i) => Math.min(v, q10[i]));
  const highSmooth = smoothSeries(highOuter, 11).map((v, i) => Math.max(v, q90[i]));
  return { lowOuter: lowSmooth, highOuter: highSmooth };
}

function bootstrapOuterLines(profile, q10, q90) {
  const b = profile.bootstrap;
  if (!b || !Array.isArray(b.q50_low95) || !Array.isArray(b.q50_high95)) return null;
  if (b.q50_low95.length !== q10.length || b.q50_high95.length !== q90.length) return null;
  const low = smoothSeries(b.q50_low95, 9).map((v, i) => Math.min(v, q10[i]));
  const high = smoothSeries(b.q50_high95, 9).map((v, i) => Math.max(v, q90[i]));
  return { lowOuter: low, highOuter: high };
}

function peerCustomData(indices) {
  return indices.map((idx) => [
    raw.role[idx],
    raw.workplace[idx],
    raw.specialist[idx],
    raw.phd[idx],
  ]);
}

function selectedProfile() {
  return byKey.get(
    profileKey(
      ACTIVE_ROLE,
      els.workplace.value,
      els.specialist.value,
      els.phd.value
    )
  );
}

function xAxisPadding() {
  const span = Number(data.years.max) - Number(data.years.min);
  return Math.max(0.4, span * 0.015);
}

function updateIndividualView() {
  const profile = selectedProfile();
  if (!profile) return;

  const years = Number(els.years.value);
  els.yearsValue.textContent = years.toFixed(1);

  const x = profile.curve.years;
  const q10 = profile.curve.q10;
  const q50 = profile.curve.q50;
  const q90 = profile.curve.q90;
  const support = profile.curve.support;
  const peerIdx = profile.strict_peer_indices || profile.peer_indices || [];
  const peersX = peerIdx.map((i) => raw.years[i]);
  const peersY = peerIdx.map((i) => raw.salary[i]);
  const peersCustom = peerCustomData(peerIdx);
  const { t: tNorm, lo: supportLo, hi: supportHi } = normalizeSupport(support);
  const bootLines = bootstrapOuterLines(profile, q10, q90);
  const uLines = bootLines || uncertaintyOuterLines(q10, q50, q90, tNorm);

  const y10 = lerpAt(x, q10, years);
  const y50 = lerpAt(x, q50, years);
  const y90 = lerpAt(x, q90, years);
  const xPad = xAxisPadding();
  const actualSalary = Number(els.actualSalary.value);
  const hasActual = Number.isFinite(actualSalary) && actualSalary > 0;

  els.q10.textContent = sek(y10);
  els.q50.textContent = sek(y50);
  els.q90.textContent = sek(y90);
  if (hasActual) {
    const delta = actualSalary - y50;
    const pct = y50 !== 0 ? (delta / y50) * 100 : 0;
    const direction = delta >= 0 ? "över" : "under";
    els.actualVsMedian.textContent = `${fmtSignedSek(delta)} (${Math.abs(pct).toFixed(1)}% ${direction})`;
  } else {
    els.actualVsMedian.textContent = "Ange egen lön";
  }
  const hasBootstrap = !!bootLines;
  const strictCount = Number.isFinite(profile.strict_peer_count) ? profile.strict_peer_count : peerIdx.length;
  els.supportInfo.textContent = hasBootstrap
    ? `Markerade blå punkter: exakt profilmatchning (n=${strictCount}). Datastöd för band: ${profile.support_desc} (n=${profile.peer_count}). Yttre osäkerhetslinjer visar bootstrap-baserat 95%-intervall kring medianen.`
    : `Markerade blå punkter: exakt profilmatchning (n=${strictCount}). Datastöd för band: ${profile.support_desc} (n=${profile.peer_count}). Yttre osäkerhetslinjer breddas där lokalt N är lågt.`;

  const traces = [
    {
      x: raw.years,
      y: raw.salary,
      mode: "markers",
      name: "Data",
      marker: { size: 5, color: "rgba(120,120,120,0.20)" },
      hoverinfo: "skip",
    },
    {
      x: peersX,
      y: peersY,
      mode: "markers",
      name: "Jämförbara datapunkter",
      marker: { size: 7, color: "rgba(107,174,214,0.75)" },
      customdata: peersCustom,
      hovertemplate:
        `${data.columns.years}: %{x:.1f}<br>` +
        `${data.columns.salary}: %{y:,.0f} kr/mån<br>` +
        `${data.columns.role}: %{customdata[0]}<br>` +
        `${data.columns.workplace}: %{customdata[1]}<br>` +
        `${data.columns.specialist}: %{customdata[2]}<br>` +
        `${data.columns.phd}: %{customdata[3]}<extra></extra>`,
    },
    {
      x,
      y: q90,
      mode: "lines",
      line: { width: 2, color: "rgba(253,174,107,0.95)" },
      name: "90e percentil",
    },
    ...buildBandSegmentTraces(x, q10, q90, tNorm),
    {
      x,
      y: q10,
      mode: "lines",
      line: { width: 2, color: "rgba(116,196,118,0.95)" },
      name: "10e percentil",
    },
    {
      x,
      y: q50,
      mode: "lines",
      line: { width: 4, color: "rgba(107,174,214,1.0)" },
      name: "50e percentil (median)",
    },
    ...extraQuantileTraces(profile, x),
    {
      x,
      y: uLines.highOuter,
      mode: "lines",
      line: { width: 2, color: "rgba(253,174,107,0.7)", dash: "dot", shape: "spline" },
      name: "Övre osäkerhetslinje",
    },
    {
      x,
      y: uLines.lowOuter,
      mode: "lines",
      line: { width: 2, color: "rgba(116,196,118,0.7)", dash: "dot", shape: "spline" },
      name: "Nedre osäkerhetslinje",
    },
    {
      x: [years],
      y: [y50],
      mode: "markers",
      marker: { size: 12, color: "#1f2a44" },
      name: "Vald punkt",
      hovertemplate: "År: %{x:.1f}<br>Median: %{y:,.0f} kr/mån<extra></extra>",
    },
    ...(hasActual
      ? [
          {
            x: [years],
            y: [actualSalary],
            mode: "markers",
            marker: { size: 12, color: "#FDD0A2", symbol: "diamond" },
            name: "Egen lön",
            hovertemplate: "År: %{x:.1f}<br>Egen lön: %{y:,.0f} kr/mån<extra></extra>",
          },
        ]
      : []),
    {
      x: [null],
      y: [null],
      mode: "markers",
      marker: {
        size: 0.1,
        color: [supportLo],
        cmin: supportLo,
        cmax: supportHi,
        colorscale: [
          [0.0, "rgb(230,245,255)"],
          [0.5, "rgb(158,202,225)"],
          [1.0, "rgb(49,130,189)"],
        ],
        showscale: true,
        colorbar: {
          title: "Datatäthet (lokalt N)<br><sup>fler = bättre stöd</sup>",
          thickness: 14,
          len: 0.55,
          y: 0.55,
          x: 1.02,
        },
      },
      hoverinfo: "skip",
      showlegend: false,
    },
  ];

  const layout = {
    title: {
      text: "Predikterat lönespann",
      x: 0.01,
      xanchor: "left",
      font: { family: "Space Grotesk, sans-serif", size: 28 },
    },
    margin: { l: 80, r: 95, t: 80, b: 70 },
    paper_bgcolor: "transparent",
    plot_bgcolor: "rgba(255,255,255,0.75)",
    hovermode: "x unified",
    legend: { orientation: "h", y: -0.2 },
    xaxis: {
      title: data.columns.years,
      showgrid: true,
      gridcolor: "rgba(0,0,0,0.08)",
      zeroline: false,
      range: [Number(data.years.min) - xPad, Number(data.years.max) + xPad],
      fixedrange: true,
    },
    yaxis: {
      title: `${data.columns.salary} (kr/mån)`,
      tickformat: ",.0f",
      showgrid: true,
      gridcolor: "rgba(0,0,0,0.08)",
      zeroline: false,
    },
  };

  Plotly.react(els.chart, traces, layout, { responsive: true, displaylogo: false });
}

function populationPlotLayout(title, yTitle, xTitle = "") {
  return {
    title: { text: title, x: 0.02, xanchor: "left", font: { family: "Space Grotesk, sans-serif", size: 18 } },
    margin: { l: 62, r: 24, t: 54, b: 58 },
    paper_bgcolor: "transparent",
    plot_bgcolor: "rgba(255,255,255,0.75)",
    xaxis: { title: xTitle, showgrid: true, gridcolor: "rgba(0,0,0,0.08)", zeroline: false },
    yaxis: { title: yTitle, showgrid: true, gridcolor: "rgba(0,0,0,0.08)", zeroline: false },
    showlegend: false,
  };
}

function renderPopulationView() {
  if (!populationData) {
    els.popMeta.textContent = "Kunde inte läsa populationsunderlag. Kontrollera JSON-filer i docs/data.";
    return;
  }

  const summary = populationData.summary;
  const meta = populationData.meta;
  const bins = populationData.gap_bins || populationData.percentile_bins || populationData.deciles;
  const scatter = populationData.scatter;
  const individuals = Array.isArray(populationData.individuals) ? populationData.individuals : [];
  const groupRankings = populationData.group_rankings || {};

  els.popNNational.textContent = `${summary.n_national}`;
  els.popNPopulation.textContent = `${summary.n_population}`;
  els.popMedianPred.textContent = sekSimple(summary.median_pred_q50);
  els.popMedianGap.textContent = sekSimple(summary.median_gap_actual_minus_pred50);
  els.popMeta.textContent = `Rollfilter: ${meta.role}. Källor: nationellt (${meta.national_source}), population (${meta.cohort_source}).`;
  els.popUnseen.textContent = `Okända kategorier i populationen: ${meta.unseen_categories_summary}.`;

  const binCounts = bins.labels.map((label) => bins.counts[label] || 0);
  const gapTickText = bins.labels.map((label) =>
    label
      .replace(" till ", "<br>till ")
      .replace("-5000", "-5 000")
      .replace("-2500", "-2 500")
      .replace("+2500", "+2 500")
      .replace("+5000", "+5 000")
  );
  Plotly.react(
    els.popChartDecile,
    [
      {
        x: bins.labels,
        y: binCounts,
        type: "bar",
        name: "Antal personer",
        marker: { color: "rgba(49,130,189,0.78)" },
      },
    ],
    {
      ...populationPlotLayout(
        "Populationens lönegap mot predikterad median (q50)",
        "Antal personer",
        "Intervall för faktisk - predikterad q50 (kr/mån)"
      ),
      margin: { l: 62, r: 24, t: 54, b: 88 },
      xaxis: {
        title: { text: "Intervall för faktisk - predikterad q50 (kr/mån)", standoff: 10 },
        tickmode: "array",
        tickvals: bins.labels,
        ticktext: gapTickText,
        tickangle: 0,
        showgrid: true,
        gridcolor: "rgba(0,0,0,0.08)",
        zeroline: false,
        automargin: true,
      },
    },
    { responsive: true, displaylogo: false }
  );

  const sortedByPct = [...individuals].sort((a, b) => a.pred_percentile - b.pred_percentile);
  const personLabels = sortedByPct.map((p) => `Person ${p.id}`);
  const personPercentiles = sortedByPct.map((p) => p.pred_percentile);
  const groupMedianPercentile =
    personPercentiles.length > 0
      ? personPercentiles.slice().sort((a, b) => a - b)[Math.floor(personPercentiles.length / 2)]
      : 50;

  Plotly.react(
    els.popChartEcdf,
    [
      {
        x: personPercentiles,
        y: personLabels,
        mode: "markers",
        name: "Person",
        marker: { color: "rgba(49,130,189,0.95)", size: 9 },
        customdata: sortedByPct.map((p) => [p.pred_q50, p.gap_actual_minus_pred50]),
        hovertemplate:
          "%{y}<br>Nationell percentil: %{x:.0f}<br>" +
          "Pred q50: %{customdata[0]:,.0f} kr/mån<br>" +
          "Gap (faktisk - q50): %{customdata[1]:,.0f} kr/mån<extra></extra>",
      },
    ],
    {
      ...populationPlotLayout(
        "Individers nationella percentilrank (predikterad q50)",
        "Person (sorterad)",
        "Nationell percentil (0-100)"
      ),
      xaxis: {
        title: "Nationell percentil (0-100)",
        range: [0, 100],
        showgrid: true,
        gridcolor: "rgba(0,0,0,0.08)",
        zeroline: false,
      },
      yaxis: {
        title: "Person (sorterad)",
        automargin: true,
      },
      shapes: [
        {
          type: "line",
          x0: 50,
          x1: 50,
          y0: -0.5,
          y1: Math.max(0, personLabels.length - 0.5),
          line: { color: "rgba(140,140,140,0.85)", width: 2, dash: "dash" },
        },
        {
          type: "line",
          x0: groupMedianPercentile,
          x1: groupMedianPercentile,
          y0: -0.5,
          y1: Math.max(0, personLabels.length - 0.5),
          line: { color: "rgba(214,39,40,0.9)", width: 2 },
        },
      ],
      annotations: [
        {
          x: 50,
          y: 1.02,
          xref: "x",
          yref: "paper",
          text: "Nationell median (50)",
          showarrow: false,
          font: { size: 11, color: "rgba(110,110,110,1)" },
        },
        {
          x: groupMedianPercentile,
          y: 1.08,
          xref: "x",
          yref: "paper",
          text: `Gruppens medianpercentil (${Math.round(groupMedianPercentile)})`,
          showarrow: false,
          font: { size: 11, color: "rgba(214,39,40,1)" },
        },
      ],
    },
    { responsive: true, displaylogo: false }
  );

  Plotly.react(
    els.popChartScatter,
    [
      {
        x: scatter.national_years,
        y: scatter.national_salary,
        mode: "markers",
        name: "Nationella löner",
        marker: { color: "rgba(120,120,120,0.24)", size: 6 },
      },
      {
        x: scatter.population_years,
        y: scatter.population_salary,
        mode: "markers",
        name: "Populationens löner",
        marker: { color: "rgba(49,130,189,0.95)", size: 9, line: { color: "white", width: 1 } },
      },
      {
        x: scatter.trend_years,
        y: scatter.trend_q50,
        mode: "lines",
        name: "Mediantrend (typisk profil)",
        line: { color: "rgba(214,39,40,0.9)", width: 2 },
      },
    ],
    populationPlotLayout("Faktisk lön mot erfarenhet", "Lön (kr/mån)", "Erfarenhet (år)"),
    { responsive: true, displaylogo: false }
  );

  const devPct = sortedByPct.map((p) =>
    p.pred_q50 > 0 ? (100.0 * p.gap_actual_minus_pred50) / p.pred_q50 : 0
  );
  Plotly.react(
    els.popChartGap,
    [
      {
        x: sortedByPct.map((p) => p.years),
        y: devPct,
        mode: "markers",
        name: "Individ",
        marker: { color: "rgba(49,130,189,0.9)", size: 9, line: { color: "#fff", width: 1 } },
        text: sortedByPct.map((p) => `Person ${p.id}`),
        customdata: sortedByPct.map((p, i) => [p.gap_actual_minus_pred50, p.pred_q50, devPct[i]]),
        hovertemplate:
          "%{text}<br>Erfarenhet: %{x:.1f} år<br>" +
          "Avvikelse: %{customdata[2]:.1f}%<br>" +
          "Gap: %{customdata[0]:,.0f} kr/mån<br>" +
          "Pred q50: %{customdata[1]:,.0f} kr/mån<extra></extra>",
      },
    ],
    {
      ...populationPlotLayout(
        "Avvikelse mot predikterad median (%) per individ",
        "Avvikelse i % av predikterad q50",
        "Erfarenhet (år)"
      ),
      yaxis: {
        title: "Avvikelse i % av predikterad q50",
        ticksuffix: "%",
        showgrid: true,
        gridcolor: "rgba(0,0,0,0.08)",
        zeroline: true,
        zerolinecolor: "rgba(214,39,40,0.9)",
        zerolinewidth: 2,
      },
    },
    { responsive: true, displaylogo: false }
  );

  const fmtPercentile = (v) => `${Math.round(v)}p`;
  const fmtGap = (v) => `${v >= 0 ? "+" : ""}${Math.round(v).toLocaleString("sv-SE")} kr`;
  const byHigh = [...individuals].sort((a, b) => b.pred_percentile - a.pred_percentile);
  const byLow = [...individuals].sort((a, b) => a.pred_percentile - b.pred_percentile);
  const top = byHigh.slice(0, 5);
  const bottom = byLow.slice(0, 5);

  els.popTopPeople.innerHTML = top
    .map(
      (p) =>
        `<li>Person ${p.id}: ${fmtPercentile(p.pred_percentile)} (pred q50 ${sekSimple(
          p.pred_q50
        )}, avvikelse ${fmtGap(p.gap_actual_minus_pred50)})</li>`
    )
    .join("");
  els.popBottomPeople.innerHTML = bottom
    .map(
      (p) =>
        `<li>Person ${p.id}: ${fmtPercentile(p.pred_percentile)} (pred q50 ${sekSimple(
          p.pred_q50
        )}, avvikelse ${fmtGap(p.gap_actual_minus_pred50)})</li>`
    )
    .join("");

  const rankingSections = Object.entries(groupRankings).map(([name, rows]) => {
    const text = rows
      .map(
        (r) =>
          `${r.group} (n=${r.n}): medianpercentil ${Math.round(r.median_percentile)}p, medianavvikelse ${fmtGap(
            r.median_gap
          )}`
      )
      .join("<br>");
    return `<p><strong>${name}</strong><br>${text}</p>`;
  });
  els.popGroupRankings.innerHTML = rankingSections.join("");
}

async function switchPopulationDataset(key) {
  els.popMeta.textContent = "Laddar population...";
  try {
    currentPopulationKey = POPULATION_SOURCES[key] ? key : "random";
    populationData = await loadPopulationDataset(currentPopulationKey);
    renderPopulationView();
  } catch (err) {
    console.error(err);
    populationData = null;
    renderPopulationView();
  }
}

function setTab(tab) {
  const isIndivid = tab === "individ";
  els.tabIndivid.classList.toggle("is-active", isIndivid);
  els.tabPopulation.classList.toggle("is-active", !isIndivid);
  els.viewIndivid.classList.toggle("is-active", isIndivid);
  els.viewPopulation.classList.toggle("is-active", !isIndivid);

  if (isIndivid) {
    updateIndividualView();
  } else {
    void switchPopulationDataset(els.popDataset.value || currentPopulationKey);
  }
}

async function init() {
  const loaded = await loadData();
  data = loaded.predictions;
  profiles = data.profiles;
  byKey = new Map(
    profiles.map((p) => [profileKey(p.role, p.workplace, p.specialist, p.phd), p])
  );
  raw = data.raw_data;
  ACTIVE_ROLE = data.default_profile.role;

  fillSelect(els.workplace, data.options.workplace);
  fillSelect(els.specialist, data.options.specialist);
  fillSelect(els.phd, data.options.phd);

  els.workplace.value = data.default_profile.workplace;
  els.specialist.value = data.default_profile.specialist;
  els.phd.value = data.default_profile.phd;

  els.years.min = String(data.years.min);
  els.years.max = String(data.years.max);
  els.years.step = "0.1";
  els.years.value = String(data.default_profile.years);

  syncFilters();
  updateIndividualView();

  const onFilterChange = () => {
    syncFilters();
    updateIndividualView();
  };
  els.workplace.addEventListener("change", onFilterChange);
  els.specialist.addEventListener("change", onFilterChange);
  els.phd.addEventListener("change", onFilterChange);
  els.years.addEventListener("input", updateIndividualView);
  els.actualSalary.addEventListener("input", updateIndividualView);

  els.tabIndivid.addEventListener("click", () => setTab("individ"));
  els.tabPopulation.addEventListener("click", () => setTab("population"));
  els.popDataset.addEventListener("change", () => {
    if (els.viewPopulation.classList.contains("is-active")) {
      void switchPopulationDataset(els.popDataset.value || "random");
    }
  });
}

init().catch((err) => {
  console.error(err);
  els.supportInfo.textContent =
    "Kunde inte ladda data till sidan. Kontrollera att JSON-filerna finns i docs/data och ladda om sidan.";
});
