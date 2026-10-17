/* CD Appreciation Leaderboard - dashboard logic (plain JavaScript, no build step).
 *
 * Talks to the Flask JSON API:
 *   /api/summary, /api/genres, /api/leaderboard?..., /api/releases/<id>
 * All API strings are inserted with textContent (never innerHTML).
 */
"use strict";

(() => {
  const GROWTH_MIN = -50; // slider ends mean "no bound"
  const GROWTH_MAX = 50;
  // Column sorting: click 1 sorts in the column's most useful direction, click 2 reverses
  // it, click 3 returns to the table's normal ordering. FIRST_* is each column's click-1
  // direction, written as [order, hint for click 1, hint for click 2].
  const SCREENER_FIRST = {
    rank: ["desc", "lowest-ranked first", "normal order"],
    title: ["asc", "A to Z", "Z to A"],
    artist: ["asc", "A to Z", "Z to A"],
    year: ["asc", "oldest first", "newest first"],
    ratio: ["desc", "scarcest first", "least scarce first"],
    price: ["desc", "highest first", "lowest first"],
    listed: ["asc", "cheapest first", "most expensive first"],
    growth: ["desc", "biggest rise first", "biggest fall first"],
    velocity: ["desc", "fastest rising first", "fastest falling first"],
    score: ["asc", "lowest first", "highest first"],
  };

  /** Next state of the 3-click cycle; null means "back to the normal ordering". */
  function nextSort(current, key, first) {
    if (!current || current.key !== key) return { key, order: first };
    if (current.order === first) return { key, order: first === "asc" ? "desc" : "asc" };
    return null;
  }

  /** Add "Click to sort: …" to each sortable heading's tooltip. */
  function addSortHints(table, attribute, columns) {
    table.querySelectorAll(`button[${attribute}]`).forEach((button) => {
      const [, one, two] = columns[button.getAttribute(attribute)];
      const hint = two === "normal order"
        ? `Click to sort: ${one}, then back to normal order`
        : `Click to sort: ${one}, then ${two}, then back to normal order`;
      button.title = button.title ? `${button.title}\n${hint}` : hint;
    });
  }

  const state = {
    q: "",
    genre: "",
    flag: "",
    minGrowth: null,
    maxGrowth: null,
    sort: "rank",
    order: "asc",
    selectedId: null,
  };

  // ---------------------------------------------------------------- helpers

  const $ = (id) => document.getElementById(id);
  const SVG_NS = "http://www.w3.org/2000/svg";

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value == null || value === false) continue;
      if (key === "text") node.textContent = value;
      else if (key === "class") node.className = value;
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children) if (child != null) node.append(child);
    return node;
  }

  function svgEl(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
    return node;
  }

  /** Render "text with `code` spans" safely (API messages use backticks for commands). */
  function richText(message) {
    const frag = document.createDocumentFragment();
    String(message).split("`").forEach((part, i) => {
      frag.append(i % 2 ? el("code", { text: part }) : document.createTextNode(part));
    });
    return frag;
  }

  function debounce(fn, ms) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  }

  // ------------------------------------------------------------- formatting

  const intFmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const moneyFormats = new Map();

  function fmtMoney(value, currency, digits = 2) {
    if (value == null || Number.isNaN(value)) return "—";
    const key = `${currency}|${digits}`;
    if (!moneyFormats.has(key)) {
      try {
        moneyFormats.set(key, new Intl.NumberFormat(undefined, {
          style: "currency", currency: currency || "USD",
          minimumFractionDigits: digits, maximumFractionDigits: digits,
        }));
      } catch (e) { // unknown currency code
        moneyFormats.set(key, { format: (v) => `${v.toFixed(digits)} ${currency}` });
      }
    }
    return moneyFormats.get(key).format(value);
  }

  function fmtSignedMoney(value, currency) {
    if (value == null) return "—";
    const sign = value > 0 ? "+" : value < 0 ? "−" : "";
    return sign + fmtMoney(Math.abs(value), currency);
  }

  function fmtSignedPct(value, digits = 1) {
    if (value == null) return "—";
    const abs = Math.abs(value).toFixed(digits);
    if (Number(abs) === 0) return `${abs}%`;
    return `${value > 0 ? "+" : "−"}${abs}%`;
  }

  function fmtDate(value) {
    if (!value) return "—";
    const d = value instanceof Date ? value : new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

  function fmtDateTime(value) {
    if (!value) return "—";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }

  function ordinal(n) {
    const suffixes = ["th", "st", "nd", "rd"];
    const v = n % 100;
    return n + (suffixes[(v - 20) % 10] || suffixes[v] || suffixes[0]);
  }

  /** Signed % with a direction icon. Color is never the only cue: icon + sign carry it too. */
  function deltaNode(value) {
    if (value == null) return el("span", { class: "muted", text: "—" });
    const dir = value > 0.05 ? "up" : value < -0.05 ? "down" : "flat";
    const node = el("span", { class: `delta is-${dir}` });
    if (dir !== "flat") {
      node.append(el("span", { class: "delta-icon", "aria-hidden": "true", text: dir === "up" ? "▲" : "▼" }));
    }
    node.append(fmtSignedPct(value));
    return node;
  }

  // -------------------------------------------------------------------- API

  async function fetchJSON(url, signal) {
    const response = await fetch(url, { signal, headers: { Accept: "application/json" } });
    let body = null;
    try {
      body = await response.json();
    } catch (e) { /* not JSON */ }
    if (!response.ok) {
      const error = new Error((body && body.error) || `Request failed (HTTP ${response.status})`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function showAlert(message) {
    $("alert").replaceChildren(el("strong", { text: "Can’t load data. " }), richText(message));
    $("alert").hidden = false;
  }

  function clearAlert() {
    $("alert").hidden = true;
  }

  // ---------------------------------------------------------------- summary

  async function loadSummary() {
    const s = await fetchJSON("/api/summary");
    const ratio = Number(s.min_ratio).toFixed(1);
    $("ratio-threshold").textContent = ratio;
    $("kpi-releases").textContent = intFmt.format(s.releases);
    $("kpi-cds").textContent = intFmt.format(s.cds);
    $("kpi-cds-sub").textContent = `format = ${s.format}`;
    $("kpi-flagged").textContent = intFmt.format(s.flagged);
    $("kpi-flagged-sub").textContent = `want/have > ${ratio} · ≥ ${s.min_wants} wants`;
    $("kpi-priced").textContent = intFmt.format(s.priced);

    const gaps = [];
    if (s.missing_price_data) gaps.push(`${intFmt.format(s.missing_price_data)} no price data`);
    if (s.not_found) gaps.push(`${intFmt.format(s.not_found)} not found`);
    if (s.fetch_errors) gaps.push(`${intFmt.format(s.fetch_errors)} errors`);
    $("kpi-priced-sub").textContent = gaps.length ? gaps.join(" · ") : "Near Mint price suggestions";

    $("kpi-growth").replaceChildren(deltaNode(s.median_growth_30d));
    $("kpi-growth-sub").textContent = s.with_growth_30d
      ? `${intFmt.format(s.rising_30d)} of ${intFmt.format(s.with_growth_30d)} rising`
      : "needs ~30 days of history";

    const flagSelect = $("flag");
    flagSelect.options[1].textContent = `Deals only (${intFmt.format(s.deals || 0)})`;
    flagSelect.options[2].textContent = `Reissue alerts (${intFmt.format(s.reissue_risks || 0)})`;

    const badge = $("mode-badge");
    badge.textContent = s.mode === "live" ? "Live Discogs data" : "Simulated market data";
    badge.classList.toggle("is-live", s.mode === "live");
    badge.hidden = false;

    $("footer-source").replaceChildren(richText(s.mode === "live"
      ? "Prices: Discogs marketplace price suggestions for Near Mint (NM or M-) copies."
      : "Demo mode: releases, want/have counts and prices are simulated. Add `DISCOGS_TOKEN` to .env and run `ingest` + `fetch-prices` for live data."));
    $("footer-times").textContent = s.last_price_at
      ? `Last price observation ${fmtDateTime(s.last_price_at)} · metrics computed ${fmtDateTime(s.computed_at)}`
      : "";
  }

  async function loadGenres() {
    const genres = await fetchJSON("/api/genres");
    const select = $("genre");
    const current = select.value;
    select.replaceChildren(
      el("option", { value: "", text: "All genres" }),
      ...genres.map((g) => el("option", { value: g.genre, text: `${g.genre} (${g.count})` })),
    );
    select.value = current;
  }

  // ------------------------------------------------------------ leaderboard

  let boardRequest = null;

  async function loadLeaderboard() {
    if (boardRequest) boardRequest.abort();
    const controller = new AbortController();
    boardRequest = controller;

    const params = filterParams();
    params.set("sort", state.sort);
    params.set("order", state.order);
    params.set("limit", "1000");

    const wrap = $("table-wrap");
    wrap.classList.add("is-loading"); // keep the previous rows visible while refetching
    wrap.setAttribute("aria-busy", "true");
    try {
      const data = await fetchJSON(`/api/leaderboard?${params}`, controller.signal);
      clearAlert();
      renderRows(data);
      renderOverview(data.items);
      loadIndex();
    } catch (error) {
      if (error.name !== "AbortError") showAlert(error.message);
    } finally {
      if (boardRequest === controller) {
        wrap.classList.remove("is-loading");
        wrap.removeAttribute("aria-busy");
        boardRequest = null;
      }
    }
  }

  function renderRows(data) {
    const tbody = $("rows");
    const frag = document.createDocumentFragment();
    for (const item of data.items) frag.append(rowFor(item));
    tbody.replaceChildren(frag);

    // Roving tabindex: the table is a single Tab stop; arrow keys move between rows.
    const rows = tbody.rows;
    if (rows.length) {
      const selected = tbody.querySelector("tr.is-selected");
      (selected || rows[0]).tabIndex = 0;
    }

    const filtered = data.matched !== data.total;
    $("result-count").textContent = data.total === 0
      ? "No priced releases yet"
      : filtered
        ? `Showing ${intFmt.format(data.matched)} of ${intFmt.format(data.total)} CDs`
        : `${intFmt.format(data.total)} CDs`;

    const empty = $("empty");
    empty.hidden = data.items.length > 0;
    if (!data.items.length) {
      const noData = data.total === 0;
      $("empty-message").replaceChildren(richText(noData
        ? "No priced releases yet. Run `python -m cd_screener demo` (offline) or `fetch-prices`, then reload this page."
        : "No CDs match these filters."));
      $("empty-reset").hidden = noData;
    }
    updateSortIndicators();
  }

  function rowFor(item) {
    const tr = el("tr", { "data-id": item.release_id, tabindex: "-1" });
    if (item.release_id === state.selectedId) tr.classList.add("is-selected");
    const sub = [item.genre, item.style].filter(Boolean).join(" · ");
    tr.append(
      el("td", { class: "num rank", text: item.rank }),
      el("td", { class: "title-cell" },
        el("div", { class: "title", text: item.title }),
        sub ? el("div", { class: "sub", text: sub }) : null,
        pressingLine(item),
        flagBadges(item)),
      el("td", { class: "artist", text: item.artist }),
      el("td", { class: "num col-year", text: item.year ?? "—" }),
      el("td", {
        class: "num",
        title: `${intFmt.format(item.wants)} wants / ${intFmt.format(item.haves)} haves`,
        text: `${item.want_have_ratio.toFixed(2)}×`,
      }),
      el("td", { class: "num", text: fmtMoney(item.current_price, item.currency) }),
      listedCell(item),
      el("td", { class: "num" }, deltaNode(item.growth_30d_pct)),
      el("td", { class: "col-trend" }, sparkline(item.sparkline, item.currency)),
      el("td", { class: "num col-score" },
        el("span", { class: "score-value", text: item.scarcity_index.toFixed(1) }),
        meter(item.scarcity_index)),
    );
    return tr;
  }

  /** "Brazil · [PIAS] Recordings · MBB20036" - which exact pressing this row is. */
  function pressingLine(item) {
    const parts = [item.country, item.label, item.catno].filter(Boolean);
    return parts.length ? el("div", { class: "sub pressing", text: parts.join(" · ") }) : null;
  }

  /** Text badges (never colour alone) warning that the NM estimate needs a second look. */
  function flagBadges(item) {
    const badges = [];
    if (item.rare_pressing) {
      badges.push(el("span", {
        class: "flag flag-rare",
        title: `Estimate is ${item.pressing_premium}× the cheapest copy of this album in any pressing (${fmtMoney(item.album_lowest_price, item.currency)})`,
        text: `Rare pressing · album from ${fmtMoney(item.album_lowest_price, item.currency)}`,
      }));
    }
    if (item.deal) {
      badges.push(el("span", {
        class: "flag flag-deal",
        title: `${item.deal_reason}. Cheapest listing ${fmtMoney(item.listed_price, item.currency)}.`,
        text: item.discount_vs_usual_pct >= 20
          ? `Deal · ${Math.round(item.discount_vs_usual_pct)}% below usual`
          : `Deal · ${Math.round(item.discount_vs_estimate_pct)}% below estimate`,
      }));
    }
    if (item.reissue_risk) {
      badges.push(el("span", { class: "flag flag-reissue", title: item.reissue_reason, text: `Reissue alert · ${item.reissue_reason}` }));
    }
    return badges.length ? el("div", { class: "flags" }, ...badges) : null;
  }

  function listedCell(item) {
    const td = el("td", { class: "num col-listed" });
    if (item.listed_count == null) {
      td.append(el("span", { class: "muted", text: "—", title: "Not checked yet" }));
    } else if (!item.listed_count) {
      td.append(el("span", { class: "muted", text: "none for sale" }));
    } else {
      td.append(
        el("div", { text: fmtMoney(item.listed_price, item.listed_currency || item.currency) }),
        el("div", { class: "sub", text: `${intFmt.format(item.listed_count)} for sale` }));
    }
    return td;
  }

  function meter(value) {
    const fill = el("span", { class: "meter-fill" });
    fill.style.width = `${Math.max(0, Math.min(100, value))}%`;
    return el("span", { class: "meter", "aria-hidden": "true" }, fill);
  }

  /** Trend glyph: de-emphasised line, current value as an accent end-dot. */
  function sparkline(values, currency) {
    const W = 104;
    const H = 32;
    const PAD = 6;
    const node = svgEl("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, class: "spark", role: "img" });
    if (!values || values.length < 2) {
      node.setAttribute("aria-label", "Not enough history for a trend");
      return node;
    }
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const x = (i) => PAD + (i * (W - 2 * PAD)) / (values.length - 1);
    const y = (v) => H - PAD - ((v - min) * (H - 2 * PAD)) / span;
    const d = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
    const last = values.length - 1;
    const label = `90-day trend: ${fmtMoney(values[0], currency)} to ${fmtMoney(values[last], currency)}`;
    const title = svgEl("title");
    title.textContent = label;
    node.setAttribute("aria-label", label);
    node.append(title, svgEl("path", { d, class: "spark-line" }),
      svgEl("circle", { cx: x(last).toFixed(1), cy: y(values[last]).toFixed(1), r: 4, class: "spark-dot" }));
    return node;
  }

  function updateSortIndicators() {
    $("board-table").querySelectorAll("thead th[data-col]").forEach((th) => {
      if (th.dataset.col === state.sort) {
        th.setAttribute("aria-sort", state.order === "asc" ? "ascending" : "descending");
      } else {
        th.removeAttribute("aria-sort");
      }
    });
  }

  // ---------------------------------------------------------- detail drawer

  let detailRequest = null;
  let chartObserver = null;
  let lastTrigger = null;

  function selectRow(id) {
    document.querySelectorAll("#rows tr.is-selected").forEach((tr) => tr.classList.remove("is-selected"));
    const tr = document.querySelector(`#rows tr[data-id="${id}"]`);
    if (tr) tr.classList.add("is-selected");
    return tr;
  }

  async function openDetail(id) {
    state.selectedId = id;
    lastTrigger = selectRow(id) || lastTrigger;
    history.replaceState(null, "", `#release-${id}`); // shareable deep link
    $("drawer").hidden = false;
    document.body.classList.add("drawer-open");
    $("drawer-body").classList.add("is-loading");

    if (detailRequest) detailRequest.abort();
    const controller = new AbortController();
    detailRequest = controller;
    try {
      renderDetail(await fetchJSON(`/api/releases/${id}`, controller.signal));
    } catch (error) {
      if (error.name === "AbortError") return;
      $("drawer-body").replaceChildren(el("p", { class: "alert" }, richText(error.message)));
    } finally {
      if (detailRequest === controller) {
        $("drawer-body").classList.remove("is-loading");
        detailRequest = null;
      }
    }
  }

  function closeDetail() {
    const drawer = $("drawer");
    if (drawer.hidden) return;
    const focusWasInside = drawer.contains(document.activeElement);
    drawer.hidden = true;
    document.body.classList.remove("drawer-open");
    history.replaceState(null, "", location.pathname + location.search);
    if (chartObserver) chartObserver.disconnect();
    state.selectedId = null;
    selectRow(null);
    if (focusWasInside && lastTrigger && lastTrigger.isConnected) lastTrigger.focus();
  }

  function renderDetail({ release, metrics, history, recent_fetches: fetches, listing, album, checks,
    listing_history: listingHistory }) {
    $("drawer-eyebrow").textContent = metrics
      ? `Rank #${metrics.rank} · Scarcity index ${metrics.scarcity_index.toFixed(1)}`
      : "Not ranked yet";
    $("drawer-title").textContent = release.title;
    $("drawer-artist").textContent = release.artist;

    const parts = [el("div", { class: "chips" },
      ...[release.year, release.format, release.genre, release.style, release.country]
        .filter(Boolean).map((text) => el("span", { class: "chip", text })))];

    const lastObs = history.length ? history[history.length - 1] : null;
    const currency = (metrics && metrics.currency) || (lastObs && lastObs.currency) || "USD";
    parts.push(pressingSection(release));
    if (metrics) parts.push(marketSection(metrics, listing, album, checks, release.discogs_url));
    parts.push(metrics
      ? metricsGrid(release, metrics)
      : el("p", { class: "note", text: "No Near Mint price data for this release yet." }));

    if (chartObserver) chartObserver.disconnect();
    const points = history
      .filter((h) => h.currency === currency)
      .map((h) => ({ t: new Date(h.fetched_at), v: h.price }));
    if (points.length >= 2) parts.push(chartFigure(points, currency));
    if (listingHistory && listingHistory.length) parts.push(supplyFigure(listingHistory));

    if (fetches.length) parts.push(fetchSection(fetches));
    if (release.source === "simulated") {
      parts.push(el("p", { class: "note", text: "Simulated release — not a real Discogs listing." }));
    }
    $("drawer-body").replaceChildren(...parts);
  }

  function pressingSection(release) {
    const rows = [
      ["Pressing", [release.country, release.year].filter(Boolean).join(", ") || "—"],
      ["Label", release.label || "—"],
      ["Catalogue no.", release.catno || "—"],
    ];
    const links = [];
    if (release.discogs_url) links.push(el("a", { href: release.discogs_url, target: "_blank", rel: "noopener", text: "This pressing on Discogs ↗" }));
    if (release.album_url) links.push(el("a", { href: release.album_url, target: "_blank", rel: "noopener", text: "All pressings of this album ↗" }));
    return el("section", {},
      el("h3", { class: "section-title", text: "Which pressing this is" }),
      el("dl", { class: "kv" }, ...rows.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })])),
      links.length ? el("p", { class: "links" }, ...links) : null);
  }

  /** NM estimate vs. what copies are really listed for - this pressing and the whole album. */
  /** "Data provided by Discogs", linked to the Discogs page the data comes from (API terms). */
  function attribution(url) {
    return el("p", { class: "attribution" }, "Data provided by ",
      el("a", { href: url || "https://www.discogs.com", target: "_blank", rel: "noopener", text: "Discogs" }), ".");
  }

  function marketSection(m, listing, album, checks, discogsUrl) {
    const cur = m.currency;
    const row = (label, value, note) => el("tr", {},
      el("th", { scope: "row", text: label }),
      el("td", { class: "num", text: value }),
      el("td", { class: "note-cell", text: note || "" }));
    const listed = !listing ? ["—", "not checked yet"]
      : !listing.num_for_sale ? ["none", "no copies for sale right now"]
        : [fmtMoney(listing.lowest_price, listing.currency || cur), `${intFmt.format(listing.num_for_sale)} for sale, any condition`];
    const albumRow = !album ? ["—", "not part of an album group, or not checked yet"]
      : album.lowest_price == null ? ["none", "no copies of any pressing for sale"]
        : [fmtMoney(album.lowest_price, cur), `${intFmt.format(album.num_for_sale)} for sale across all pressings & formats`];

    const notes = [];
    if (m.deal) notes.push(`Deal: ${m.deal_reason}.`);
    if (m.usual_listed_price != null) {
      notes.push(`Usual listing price over the last 30 days: ${fmtMoney(m.usual_listed_price, cur)}.`);
    }
    if (m.reissue_risk) notes.push(`Reissue alert: ${m.reissue_reason}. New pressings can lower the value of existing ones.`);
    if (checks && checks.rare_pressing) {
      notes.push(`This pressing's estimate is ${checks.pressing_premium}× the cheapest copy of the album. Other pressings of the same album are far cheaper — the value is tied to this specific variant.`);
    }
    if (checks && checks.estimate_high) {
      notes.push(`Discogs' estimate is ${checks.estimate_vs_listing}× the cheapest listed copy of this pressing. Estimates for rarely-sold pressings can be well off; treat the listing price as the reality check.`);
    }
    return el("section", {},
      el("h3", { class: "section-title", text: "Market check" }),
      el("table", { class: "market-table" }, el("tbody", {},
        row("Discogs NM estimate", fmtMoney(m.current_price, cur), "suggested price, Near Mint, this pressing"),
        row("Cheapest listing", listed[0], listed[1]),
        row("Album, any pressing", albumRow[0], albumRow[1]))),
      attribution(discogsUrl),
      ...notes.map((text) => el("p", { class: "callout", text })));
  }

  function metricsGrid(release, m) {
    const item = (label, value, note) => el("div", { class: "metric" },
      el("dt", { text: label }),
      el("dd", {}, value, note ? el("span", { class: "metric-note", text: note }) : null));
    return el("dl", { class: "metrics" },
      item("Current value (NM)", fmtMoney(m.current_price, m.currency), `as of ${fmtDate(m.current_at)}`),
      item("Baseline price", fmtMoney(m.baseline_price, m.currency), `first observed ${fmtDate(m.baseline_at)}`),
      item("Appreciation since baseline", deltaNode(m.pct_appreciation),
        `currency moves removed · raw ${fmtSignedPct(m.raw_pct_appreciation)} (${fmtSignedMoney(m.abs_change, m.currency)})`),
      item("30-day growth", deltaNode(m.growth_30d_pct),
        m.growth_30d_pct == null ? "needs ~30 days of history"
          : `currency moves removed · raw ${fmtSignedPct(m.raw_growth_30d_pct)}`),
      item("Trend velocity", deltaNode(m.velocity_30d_pct), "per 30 days, fitted over 90 days"),
      item("Want / have", `${release.want_have_ratio.toFixed(2)}×`,
        `${intFmt.format(release.wants)} wants · ${intFmt.format(release.haves)} haves`),
      item("Demand percentile", ordinal(Math.round(m.demand_pctile * 100)), "want/have vs. other flagged CDs"),
      item("Momentum percentile", ordinal(Math.round(m.momentum_pctile * 100)), "trend vs. other flagged CDs"),
    );
  }

  function fetchSection(fetches) {
    const labels = { ok: "Priced", no_data: "No price data", not_found: "Not found", error: "Error" };
    return el("section", {},
      el("h3", { class: "section-title", text: "Recent price fetches" }),
      el("ul", { class: "fetch-list" }, ...fetches.map((f) => el("li", {},
        el("span", { class: `status-pill status-${f.status}`, text: labels[f.status] || f.status }),
        ` ${fmtDateTime(f.attempted_at)}`,
        f.status !== "ok" && f.message ? ` — ${f.message}` : null))));
  }

  // ------------------------------------------------------ price history chart

  function chartFigure(points, currency, title = "Near Mint price") {
    const days = Math.max(1, Math.round((points[points.length - 1].t - points[0].t) / 86400000));
    const container = el("div", {
      class: "chart",
      tabindex: "0",
      role: "group",
      "aria-label": `${title} over ${days} days, ${points.length} observations. Use the arrow keys to read values.`,
    });
    const live = el("p", { class: "sr-only", "aria-live": "polite" });
    const table = historyTable(points, currency, title);
    table.id = `history-table-${Date.now()}`;
    table.hidden = true;
    const toggle = el("button", {
      type: "button", class: "link-button", "aria-expanded": "false", "aria-controls": table.id,
      text: "Show data table",
    });
    toggle.addEventListener("click", () => {
      table.hidden = !table.hidden;
      toggle.setAttribute("aria-expanded", String(!table.hidden));
      toggle.textContent = table.hidden ? "Show data table" : "Hide data table";
    });
    chartObserver = attachChart(container, live, points, currency);
    return el("figure", { class: "chart-card" },
      el("figcaption", {},
        el("span", { class: "chart-title", text: title }),
        el("span", { class: "chart-sub", text: `last ${days} day${days === 1 ? "" : "s"} · ${currency}` })),
      container, live, toggle, table);
  }

  /** Copies for sale over time (one series, so no legend; the title names it). */
  function supplyFigure(rows) {
    const pts = rows.map((r) => ({ t: new Date(r.fetched_at), v: r.num_for_sale, price: r.lowest_price, cur: r.currency }));
    const box = el("div", { class: "chart" });
    const table = el("table", { class: "history-table", hidden: true },
      el("thead", {}, el("tr", {}, el("th", { scope: "col", text: "Date" }),
        el("th", { scope: "col", class: "num", text: "Copies for sale" }),
        el("th", { scope: "col", class: "num", text: "Cheapest" }))),
      el("tbody", {}, ...pts.slice().reverse().map((p) => el("tr", {},
        el("td", { text: fmtDate(p.t) }), el("td", { class: "num", text: p.v }),
        el("td", { class: "num", text: p.price == null ? "—" : fmtMoney(p.price, p.cur) })))));
    const toggle = el("button", { type: "button", class: "link-button", "aria-expanded": "false", text: "Show data table" });
    toggle.addEventListener("click", () => {
      table.hidden = !table.hidden;
      toggle.setAttribute("aria-expanded", String(!table.hidden));
      toggle.textContent = table.hidden ? "Show data table" : "Hide data table";
    });
    const latest = pts[pts.length - 1];
    const figure = el("figure", { class: "chart-card" },
      el("figcaption", {},
        el("span", { class: "chart-title", text: "Copies for sale" }),
        el("span", { class: "chart-sub", text: `${latest.v} now · shrinking supply often comes before price rises` })),
      box, toggle, table);
    requestAnimationFrame(() => drawSupply(box, pts));
    return figure;
  }

  function drawSupply(box, pts) {
    if (pts.length < 2) {
      box.replaceChildren(el("p", { class: "note", text: "The trend appears after a second day of listings." }));
      return;
    }
    const width = Math.max(260, Math.floor(box.clientWidth));
    const height = 120;
    const m = { top: 10, right: 30, bottom: 24, left: 36 };
    const max = Math.max(...pts.map((p) => p.v), 1);
    const { hi, ticks } = niceTicks(0, max, 2);
    const x = (i) => m.left + (i / (pts.length - 1)) * (width - m.left - m.right);
    const y = (v) => m.top + (1 - v / hi) * (height - m.top - m.bottom);
    const svg = svgEl("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": `Copies for sale went from ${pts[0].v} to ${pts[pts.length - 1].v}` });
    for (const t of ticks) {
      const yy = Math.round(y(t)) + 0.5;
      svg.append(svgEl("line", { x1: m.left, x2: width - m.right, y1: yy, y2: yy, class: t === 0 ? "axis-line" : "grid-line" }));
      const label = svgEl("text", { x: m.left - 6, y: yy, class: "tick-label", "text-anchor": "end", "dominant-baseline": "middle" });
      label.textContent = String(t);
      svg.append(label);
    }
    [0, pts.length - 1].forEach((i) => {
      const label = svgEl("text", { x: x(i), y: height - 6, class: "tick-label", "text-anchor": i ? "end" : "start" });
      label.textContent = pts[i].t.toLocaleDateString(undefined, { day: "numeric", month: "short" });
      svg.append(label);
    });
    svg.append(svgEl("path", { d: pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(""), class: "line" }));
    pts.forEach((p, i) => {
      const dot = svgEl("circle", { cx: x(i), cy: y(p.v), r: 4, class: "marker-1" });
      const tip = svgEl("title");
      tip.textContent = `${fmtDate(p.t)}: ${p.v} for sale`;
      dot.append(tip);
      svg.append(dot);
    });
    const end = svgEl("text", { x: x(pts.length - 1) + 8, y: y(pts[pts.length - 1].v), class: "end-label", "dominant-baseline": "middle" });
    end.textContent = String(pts[pts.length - 1].v);
    svg.append(end);
    box.replaceChildren(svg);
  }

  function historyTable(points, currency, valueLabel = "Near Mint price") {
    const rows = points.slice().reverse().map((p) => el("tr", {},
      el("td", { text: fmtDate(p.t) }),
      el("td", { class: "num", text: fmtMoney(p.v, currency) })));
    return el("table", { class: "history-table" },
      el("thead", {}, el("tr", {},
        el("th", { scope: "col", text: "Date" }),
        el("th", { scope: "col", class: "num", text: valueLabel }))),
      el("tbody", {}, ...rows));
  }

  /** "Nice" axis ticks: steps of 1, 2, 2.5 or 5 x 10^n covering [min, max]. */
  function niceTicks(min, max, count) {
    if (min === max) { min -= 1; max += 1; }
    const raw = (max - min) / count;
    const mag = 10 ** Math.floor(Math.log10(raw));
    const norm = raw / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
    const lo = Math.floor(min / step) * step;
    const hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Number(v.toFixed(10)));
    return { lo, hi, step, ticks };
  }

  function fmtTick(value, currency, step) {
    if (Math.abs(value) >= 10000) {
      try {
        return new Intl.NumberFormat(undefined, { style: "currency", currency, notation: "compact", maximumFractionDigits: 1 }).format(value);
      } catch (e) { /* fall through */ }
    }
    return fmtMoney(value, currency, step >= 1 && Number.isInteger(step) ? 0 : step >= 1 ? 1 : 2);
  }

  /** Up to `max` month-start ticks between t0 and t1. */
  function monthTicks(t0, t1, max) {
    const ticks = [];
    const d = new Date(t0);
    d.setDate(1);
    d.setHours(0, 0, 0, 0);
    d.setMonth(d.getMonth() + 1);
    while (d.getTime() <= t1) {
      ticks.push(new Date(d));
      d.setMonth(d.getMonth() + 1);
    }
    const stride = Math.ceil(ticks.length / max);
    return ticks.filter((_, i) => i % stride === 0);
  }

  function attachChart(container, live, points, currency) {
    const tooltip = el("div", { class: "chart-tooltip", hidden: true });
    let geo = null;
    let focusIndex = null;
    let drawnWidth = 0;

    function draw() {
      const width = Math.max(280, Math.floor(container.clientWidth));
      if (width === drawnWidth) return;
      drawnWidth = width;
      const height = 230;
      const m = { top: 14, right: 72, bottom: 28, left: 56 };
      const plotW = width - m.left - m.right;
      const plotH = height - m.top - m.bottom;

      const values = points.map((p) => p.v);
      const { lo, hi, step, ticks } = niceTicks(Math.min(...values), Math.max(...values), 4);
      const t0 = points[0].t.getTime();
      const t1 = points[points.length - 1].t.getTime();
      const x = (t) => m.left + ((t - t0) / (t1 - t0 || 1)) * plotW;
      const y = (v) => m.top + (1 - (v - lo) / (hi - lo || 1)) * plotH;

      const svg = svgEl("svg", { width, height, viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });

      // Recessive hairline grid; the bottom tick doubles as the baseline.
      for (const tv of ticks) {
        const yy = Math.round(y(tv)) + 0.5;
        svg.append(svgEl("line", {
          x1: m.left, x2: width - m.right, y1: yy, y2: yy,
          class: tv === lo ? "axis-line" : "grid-line",
        }));
        const label = svgEl("text", { x: m.left - 8, y: yy, class: "tick-label", "text-anchor": "end", "dominant-baseline": "middle" });
        label.textContent = fmtTick(tv, currency, step);
        svg.append(label);
      }
      const multiYear = new Date(t0).getFullYear() !== new Date(t1).getFullYear();
      for (const tick of monthTicks(t0, t1, Math.max(2, Math.floor(plotW / 70)))) {
        const label = svgEl("text", { x: x(tick.getTime()), y: height - 8, class: "tick-label", "text-anchor": "middle" });
        label.textContent = tick.toLocaleDateString(undefined, multiYear ? { month: "short", year: "2-digit" } : { month: "short" });
        svg.append(label);
      }

      const xs = points.map((p) => x(p.t.getTime()));
      const line = points.map((p, i) => `${i ? "L" : "M"}${xs[i].toFixed(1)},${y(p.v).toFixed(1)}`).join("");
      const base = y(lo).toFixed(1);
      svg.append(
        svgEl("path", { d: `${line}L${xs[xs.length - 1].toFixed(1)},${base}L${xs[0].toFixed(1)},${base}Z`, class: "area" }),
        svgEl("path", { d: line, class: "line" }),
      );

      // Direct label at the line end only (never a number on every point).
      const last = points[points.length - 1];
      const endX = xs[xs.length - 1];
      const endY = y(last.v);
      svg.append(svgEl("circle", { cx: endX, cy: endY, r: 4, class: "end-dot" }));
      const endLabel = svgEl("text", { x: endX + 10, y: endY, class: "end-label", "dominant-baseline": "middle" });
      endLabel.textContent = fmtMoney(last.v, currency);
      svg.append(endLabel);

      const crosshair = svgEl("line", { y1: m.top, y2: m.top + plotH, class: "crosshair", visibility: "hidden" });
      const focusDot = svgEl("circle", { r: 4, class: "focus-dot", visibility: "hidden" });
      svg.append(crosshair, focusDot);

      container.replaceChildren(svg, tooltip);
      geo = { xs, y, crosshair, focusDot, width };
      if (focusIndex != null) showPoint(focusIndex, false);
    }

    function nearestIndex(px) {
      const xs = geo.xs;
      let lo = 0;
      let hi = xs.length - 1;
      while (hi - lo > 1) {
        const mid = (lo + hi) >> 1;
        if (xs[mid] < px) lo = mid; else hi = mid;
      }
      return Math.abs(xs[lo] - px) <= Math.abs(xs[hi] - px) ? lo : hi;
    }

    function showPoint(i, announce) {
      if (!geo) return;
      focusIndex = i;
      const p = points[i];
      const px = geo.xs[i];
      const py = geo.y(p.v);
      geo.crosshair.setAttribute("x1", px);
      geo.crosshair.setAttribute("x2", px);
      geo.crosshair.setAttribute("visibility", "visible");
      geo.focusDot.setAttribute("cx", px);
      geo.focusDot.setAttribute("cy", py);
      geo.focusDot.setAttribute("visibility", "visible");
      tooltip.replaceChildren(el("strong", { text: fmtMoney(p.v, currency) }), el("span", { text: fmtDate(p.t) }));
      tooltip.hidden = false;
      const half = tooltip.offsetWidth / 2;
      tooltip.style.left = `${Math.min(Math.max(px, half), geo.width - half)}px`;
      tooltip.style.top = `${py}px`;
      if (announce) live.textContent = `${fmtDate(p.t)}: ${fmtMoney(p.v, currency)}`;
    }

    function hidePoint() {
      focusIndex = null;
      if (!geo) return;
      geo.crosshair.setAttribute("visibility", "hidden");
      geo.focusDot.setAttribute("visibility", "hidden");
      tooltip.hidden = true;
    }

    container.addEventListener("pointermove", (event) => {
      if (!geo) return;
      const rect = container.getBoundingClientRect();
      const scale = geo.width / (container.querySelector("svg").getBoundingClientRect().width || geo.width);
      showPoint(nearestIndex((event.clientX - rect.left) * scale), false);
    });
    container.addEventListener("pointerleave", () => {
      if (document.activeElement !== container) hidePoint();
    });
    container.addEventListener("focus", () => showPoint(focusIndex ?? points.length - 1, true));
    container.addEventListener("blur", hidePoint);
    container.addEventListener("keydown", (event) => {
      let i = focusIndex ?? points.length - 1;
      switch (event.key) {
        case "ArrowLeft": i -= 1; break;
        case "ArrowRight": i += 1; break;
        case "PageUp": i -= 7; break;
        case "PageDown": i += 7; break;
        case "Home": i = 0; break;
        case "End": i = points.length - 1; break;
        default: return;
      }
      event.preventDefault();
      showPoint(Math.max(0, Math.min(points.length - 1, i)), true);
    });

    const observer = new ResizeObserver(() => draw());
    observer.observe(container);
    return observer;
  }

  // ------------------------------------------------------------------- tabs

  const TABS = ["screener", "wantlist", "collection"];
  const loadedViews = new Set(["screener"]);

  function showTab(name) {
    for (const t of TABS) {
      const tab = $(`tab-${t}`);
      const active = t === name;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      $(`view-${t}`).hidden = !active;
    }
    try { localStorage.setItem("cd-screener-tab", name); } catch (e) { /* storage unavailable */ }
    if (!loadedViews.has(name)) {
      loadedViews.add(name);
      if (name === "wantlist") loadWantlist();
      if (name === "collection") loadCollection();
    }
  }

  for (const t of TABS) {
    $(`tab-${t}`).addEventListener("click", () => showTab(t));
    $(`tab-${t}`).addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      const next = TABS[(TABS.indexOf(t) + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length];
      showTab(next);
      $(`tab-${next}`).focus();
    });
  }

  // --------------------------------------------------------------- wantlist

  let wantAlbums = [];

  async function loadWantlist() {
    try {
      const data = await fetchJSON("/api/me/wantlist");
      wantAlbums = data.albums;
      $("want-albums").textContent = intFmt.format(data.albums.length);
      $("want-items").textContent = `${intFmt.format(data.items)} variants on your Discogs wantlist`;
      $("want-deals").textContent = intFmt.format(data.albums.filter((a) => a.deal).length);
      const checked = data.albums.map((a) => a.checked_at).filter(Boolean).sort().pop();
      $("want-checked").textContent = checked ? fmtDateTime(checked) : "not yet";
      renderWantlist();
    } catch (error) {
      showAlert(error.message);
    }
  }

  // Wantlist sorting happens in the browser (all albums are already loaded). The normal
  // ordering is the server's: deals first, then the rest. Empty values always sort last.
  const WANT_FIRST = {
    title: ["asc", "A to Z", "Z to A"],
    artist: ["asc", "A to Z", "Z to A"],
    variants: ["desc", "most first", "fewest first"],
    cheapest: ["asc", "cheapest first", "most expensive first"],
    usual: ["asc", "cheapest first", "most expensive first"],
    change: ["asc", "biggest drop first", "biggest rise first"],
  };
  const WANT_VALUE = {
    title: (a) => a.title.toLowerCase(),
    artist: (a) => a.artist.toLowerCase(),
    variants: (a) => a.variants,
    cheapest: (a) => (a.num_for_sale === 0 ? null : a.lowest_price),
    usual: (a) => a.usual_price,
    change: (a) => (a.days_tracked > 1 ? a.change_pct : null),
  };
  let wantSort = null; // { key, order } or null for the normal ordering

  function sortWantAlbums(rows) {
    if (!wantSort) return rows;
    const value = WANT_VALUE[wantSort.key];
    const direction = wantSort.order === "asc" ? 1 : -1;
    return rows
      .map((album, index) => ({ album, index, v: value(album) }))
      .sort((x, y) => {
        if (x.v == null || y.v == null) return (x.v == null) - (y.v == null) || x.index - y.index;
        const cmp = typeof x.v === "string" ? x.v.localeCompare(y.v) : x.v - y.v;
        return direction * cmp || x.index - y.index; // ties keep the normal ordering
      })
      .map((row) => row.album);
  }

  function updateWantSortIndicators() {
    $("want-table").querySelectorAll("thead th[data-wcol]").forEach((th) => {
      if (wantSort && th.dataset.wcol === wantSort.key) {
        th.setAttribute("aria-sort", wantSort.order === "asc" ? "ascending" : "descending");
      } else {
        th.removeAttribute("aria-sort");
      }
    });
  }

  $("want-table").querySelector("thead").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-wsort]");
    if (!button) return;
    const key = button.dataset.wsort;
    wantSort = nextSort(wantSort, key, WANT_FIRST[key][0]);
    updateWantSortIndicators();
    renderWantlist();
  });
  addSortHints($("want-table"), "data-wsort", WANT_FIRST);

  function renderWantlist() {
    const q = $("want-search").value.trim().toLowerCase();
    const dealsOnly = $("want-deals-only").checked;
    const rows = sortWantAlbums(wantAlbums.filter((a) => (!dealsOnly || a.deal)
      && (!q || `${a.artist} ${a.title}`.toLowerCase().includes(q))));
    const link = (href, text) => el("a", { href, target: "_blank", rel: "noopener", text });
    const sinceDay = (iso) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
    $("want-rows").replaceChildren(...rows.map((a) => {
      const tr = el("tr", { "data-album": a.album_key, tabindex: "0" });
      const days = a.days_tracked;
      tr.append(
        el("td", { class: "title-cell" },
          el("div", { class: "title", text: a.title }),
          a.deal ? el("div", { class: "flags" }, el("span", { class: "flag flag-deal",
            text: `Deal · ${Math.round(a.below_usual_pct)}% below usual` })) : null),
        el("td", { class: "artist", text: a.artist }),
        el("td", { class: "num", text: a.variants, title: `${a.cd_variants} of them on CD` }),
        el("td", { class: "num" },
          el("div", { text: a.num_for_sale === 0 ? "none for sale" : fmtMoney(a.lowest_price, a.currency) }),
          a.num_for_sale ? el("div", { class: "sub", text: `${intFmt.format(a.num_for_sale)} for sale` }) : null),
        el("td", { class: "num", text: a.usual_price != null ? fmtMoney(a.usual_price, a.currency) : "—",
          title: a.usual_price == null ? `Needs ${3 - Math.min(days - 1, 3)} more day(s) of prices` : "Median of the previous 30 days" }),
        el("td", { class: "num" },
          days > 1 ? deltaNode(a.change_pct) : el("span", { class: "muted", text: "—", title: "Needs a second day of checks" }),
          a.first_checked_at ? el("div", { class: "sub", text: `since ${sinceDay(a.first_checked_at)}` }) : null),
        el("td", {}, el("div", { class: "cell-links" },
          link(a.cd_listings_url, "CD listings ↗"), link(a.album_url, "Album ↗"))));
      return tr;
    }));
    const empty = $("want-empty");
    empty.hidden = rows.length > 0;
    if (!rows.length) {
      $("want-empty-text").replaceChildren(richText(wantAlbums.length
        ? "No albums match."
        : "No wantlist synced yet. Run `python -m cd_screener sync-me` (the daily run does this automatically)."));
    }
  }

  $("want-search").addEventListener("input", debounce(renderWantlist, 150));
  $("want-deals-only").addEventListener("change", renderWantlist);
  $("want-rows").addEventListener("click", (e) => {
    if (e.target.closest("a")) return; // links open Discogs
    const tr = e.target.closest("tr[data-album]");
    if (tr) openAlbum(tr.dataset.album);
  });
  $("want-rows").addEventListener("keydown", (e) => {
    const tr = e.target.closest("tr[data-album]");
    if (tr && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openAlbum(tr.dataset.album); }
  });

  async function openAlbum(key) {
    $("drawer").hidden = false;
    document.body.classList.add("drawer-open");
    $("drawer-body").classList.add("is-loading");
    try {
      const a = await fetchJSON(`/api/me/wantlist/${encodeURIComponent(key)}`);
      $("drawer-eyebrow").textContent = `On your wantlist · ${a.variants} variant${a.variants === 1 ? "" : "s"}`;
      $("drawer-title").textContent = a.title;
      $("drawer-artist").textContent = a.artist;
      if (chartObserver) chartObserver.disconnect();
      const cur = a.currency;
      const row = (label, value, note) => el("tr", {},
        el("th", { scope: "row", text: label }), el("td", { class: "num", text: value }),
        el("td", { class: "note-cell", text: note || "" }));
      const parts = [
        el("section", {},
          el("h3", { class: "section-title", text: "Price check" }),
          el("table", { class: "market-table" }, el("tbody", {},
            row("Cheapest copy now", a.num_for_sale === 0 ? "none" : fmtMoney(a.lowest_price, cur),
              a.num_for_sale ? `${intFmt.format(a.num_for_sale)} for sale, any pressing or format` : "nothing for sale"),
            row("Usual price", a.usual_price != null ? fmtMoney(a.usual_price, cur) : "—",
              a.usual_price != null ? "median of the previous 30 days" : "after 3+ days of checks"),
            row("Change since first check", a.change_pct != null ? fmtSignedPct(a.change_pct) : "—",
              `${a.first_checked_at ? `since ${fmtDate(a.first_checked_at)} · ` : ""}${a.days_tracked} day${a.days_tracked === 1 ? "" : "s"} of checks`))),
          attribution(a.album_url),
          a.deal ? el("p", { class: "callout", text: `Deal: the cheapest copy is ${Math.round(a.below_usual_pct)}% below its usual price. Check that it's a CD you want.` }) : null,
          el("p", { class: "links" },
            el("a", { href: a.cd_listings_url, target: "_blank", rel: "noopener", text: "CD listings for this album ↗" }),
            el("a", { href: a.album_url, target: "_blank", rel: "noopener", text: "Album on Discogs ↗" }))),
      ];
      const points = a.history.filter((h) => h.lowest_price != null)
        .map((h) => ({ t: new Date(h.fetched_at), v: h.lowest_price }));
      if (points.length >= 2) parts.push(chartFigure(points, cur, "Cheapest copy"));
      parts.push(el("section", {},
        el("h3", { class: "section-title", text: `Variants on your wantlist (${a.variants_list.length})` }),
        el("table", { class: "history-table" },
          el("thead", {}, el("tr", {}, el("th", { scope: "col", text: "Year" }), el("th", { scope: "col", text: "Format" }),
            el("th", { scope: "col", text: "Label · Cat. no." }))),
          el("tbody", {}, ...a.variants_list.map((v) => el("tr", {},
            el("td", { text: v.year || "—" }), el("td", { text: v.format || "—" }),
            el("td", {}, el("a", { href: `https://www.discogs.com/release/${v.release_id}`, target: "_blank", rel: "noopener",
              text: [v.label, v.catno].filter(Boolean).join(" · ") || `Release ${v.release_id}` }))))))));
      $("drawer-body").replaceChildren(...parts);
    } catch (error) {
      $("drawer-body").replaceChildren(el("p", { class: "alert" }, richText(error.message)));
    } finally {
      $("drawer-body").classList.remove("is-loading");
    }
  }

  // ------------------------------------------------------------- collection

  let collectionObserver = null;

  async function loadCollection() {
    try {
      const data = await fetchJSON("/api/me/collection");
      const v = data.latest_value;
      const cur = v && v.currency;
      $("col-median").textContent = v ? fmtMoney(v.median, cur) : "—";
      $("col-range").textContent = v ? `range ${fmtMoney(v.minimum, cur)} – ${fmtMoney(v.maximum, cur)}` : "not synced yet";
      $("col-items").textContent = intFmt.format(data.items.length);
      const first = data.value_history.find((h) => h.median != null);
      if (v && first && data.value_history.length > 1) {
        $("col-change").replaceChildren(deltaNode((v.median / first.median - 1) * 100));
        $("col-since").textContent = `since ${fmtDate(first.fetched_at)} (${fmtMoney(first.median, cur)})`;
      } else {
        $("col-change").textContent = "—";
        $("col-since").textContent = "shows after a second day of tracking";
      }
      const points = data.value_history.filter((h) => h.median != null)
        .map((h) => ({ t: new Date(h.fetched_at), v: h.median }));
      const box = $("col-chart");
      if (collectionObserver) collectionObserver.disconnect();
      if (points.length >= 2) {
        const live = el("p", { class: "sr-only", "aria-live": "polite" });
        const chart = el("div", { class: "chart", tabindex: "0", role: "group",
          "aria-label": "Collection value over time. Use the arrow keys to read values." });
        box.replaceChildren(chart, live);
        collectionObserver = attachChart(chart, live, points, cur);
      } else {
        box.replaceChildren(el("p", { class: "note", text: "The chart appears after a second day of tracking." }));
      }
      $("col-rows").replaceChildren(...data.items.map((i) => el("tr", {},
        el("td", { class: "title-cell" },
          el("div", { class: "title" }, el("a", { href: `https://www.discogs.com/release/${i.release_id}`,
            target: "_blank", rel: "noopener", text: i.title })),
          el("div", { class: "sub", text: i.artist })),
        el("td", { text: [i.format, i.label, i.catno].filter(Boolean).join(" · ") || "—" }),
        el("td", { class: "num", text: i.year || "—" }),
        el("td", { text: i.date_added ? fmtDate(i.date_added) : "—" }))));
    } catch (error) {
      showAlert(error.message);
    }
  }

  // ------------------------------------------------- weekly report (news card)

  async function loadReport() {
    let r;
    try {
      r = await fetchJSON("/api/reports/latest");
    } catch (error) {
      $("news").hidden = true; // 404 until the first report is published
      return;
    }
    const day = (iso, withYear) => new Date(iso).toLocaleDateString(undefined,
      withYear ? { day: "numeric", month: "short", year: "numeric" } : { day: "numeric", month: "short" });
    $("news-title").textContent = `${day(r.period_start)} – ${day(r.period_end, true)}`;
    const next = new Date(new Date(r.period_end).getTime() + 7 * 86400000);
    $("news-sub").textContent = `Published ${day(r.published_at, true)} · ${r.tracked} CDs tracked · next report around ${day(next.toISOString(), true)}`;

    const item = (i, detail) => el("li", {}, el("button", { type: "button", class: "news-item", "data-id": i.release_id, title: `${i.artist} – ${i.title}${i.pressing ? ` (${i.pressing})` : ""}` },
      el("span", { class: "name", text: `${i.artist} – ${i.title}` }),
      el("span", { class: "detail", text: detail })));
    const column = (title, count, items, emptyText, more) => el("section", { class: "news-col" },
      el("h3", {}, title, count != null ? el("span", { class: "count", text: ` (${count})` }) : null),
      items.length ? el("ul", { class: "news-list" }, ...items) : el("p", { class: "news-empty", text: emptyText }),
      more || null);
    const seeAll = (flag, label) => {
      const button = el("button", { type: "button", class: "link-button news-more", text: label });
      button.addEventListener("click", () => {
        state.flag = flag;
        $("flag").value = flag;
        loadLeaderboard();
        document.querySelector(".board").scrollIntoView({ behavior: "smooth", block: "start" });
      });
      return button;
    };

    const movers = [
      ...r.rising.slice(0, 3).map((i) => item(i, `▲ ${fmtSignedPct(i.change_pct)} · ${fmtMoney(i.price, i.currency)}`)),
      ...r.falling.slice(0, 3).map((i) => item(i, `▼ ${fmtSignedPct(i.change_pct)} · ${fmtMoney(i.price, i.currency)}`)),
    ];
    $("news-grid").replaceChildren(
      column("Biggest movers", null, movers, "No CD moved 0.5% or more on its own this week."),
      column("Deals", r.deals_total, r.deals.slice(0, 3).map((i) =>
        item(i, `${fmtMoney(i.listed_price, i.currency)} listed · est. ${fmtMoney(i.price, i.currency)}`)),
      "No deals this week.", r.deals_total > 3 ? seeAll("deal", `See all ${r.deals_total} current deals`) : null),
      column("Reissue alerts", r.reissues.length, r.reissues.slice(0, 3).map((i) => item(i, i.reason)),
        "No new reissues for tracked albums.",
        r.reissues.length > 3 ? seeAll("reissue", `See all reissue alerts`) : null),
      column("Supply changes", r.supply.length, r.supply.slice(0, 3).map((i) =>
        item(i, `${i.copies_before} → ${i.copies_now} copies for sale`)), "No changes in copies for sale."));
    $("news").hidden = false;
  }

  $("news-grid").addEventListener("click", (event) => {
    const button = event.target.closest(".news-item");
    if (button) openDetail(Number(button.dataset.id));
  });

  // -------------------------------------------------------- market overview

  const overview = { items: [], index: [] };

  function filterParams() {
    const params = new URLSearchParams();
    if (state.q) params.set("q", state.q);
    if (state.genre) params.set("genre", state.genre);
    if (state.flag) params.set("flag", state.flag);
    if (state.minGrowth != null) params.set("min_growth", state.minGrowth);
    if (state.maxGrowth != null) params.set("max_growth", state.maxGrowth);
    return params;
  }

  /** Log-scale ticks at 1, 2, 5 x 10^n inside [lo, hi]. */
  function logTicks(lo, hi) {
    const ticks = [];
    for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e += 1) {
      for (const f of [1, 2, 5]) {
        const v = f * 10 ** e;
        if (v >= lo && v <= hi) ticks.push(v);
      }
    }
    return ticks;
  }

  function placeTooltip(tooltip, x, y, width) {
    tooltip.hidden = false;
    const half = tooltip.offsetWidth / 2;
    tooltip.style.left = `${Math.min(Math.max(x, half), width - half)}px`;
    tooltip.style.top = `${y}px`;
  }

  // ---- 1. scarcity vs price scatter
  const scatterBox = $("scatter-chart");
  const scatterState = { pts: [], focus: null, geo: null };

  function renderScatter() {
    const pts = overview.items.filter((i) => i.current_price > 0 && i.want_have_ratio > 0);
    scatterState.pts = pts;
    scatterState.focus = null;
    if (!pts.length) {
      scatterBox.replaceChildren(el("p", { class: "note", text: "No priced CDs match these filters." }));
      scatterState.geo = null;
      return;
    }
    const width = Math.max(300, Math.floor(scatterBox.clientWidth));
    const height = 320;
    const m = { top: 10, right: 14, bottom: 40, left: 58 };
    const ratios = pts.map((p) => p.want_have_ratio);
    const prices = pts.map((p) => p.current_price);
    const x0 = Math.min(...ratios) * 0.9, x1 = Math.max(...ratios) * 1.1;
    const y0 = Math.min(...prices) * 0.8, y1 = Math.max(...prices) * 1.25;
    const x = (v) => m.left + (Math.log(v / x0) / Math.log(x1 / x0)) * (width - m.left - m.right);
    const y = (v) => height - m.bottom - (Math.log(v / y0) / Math.log(y1 / y0)) * (height - m.top - m.bottom);
    const cur = pts[0].currency;

    const svg = svgEl("svg", { width, height, viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
    for (const t of logTicks(y0, y1)) {
      const yy = Math.round(y(t)) + 0.5;
      svg.append(svgEl("line", { x1: m.left, x2: width - m.right, y1: yy, y2: yy, class: "grid-line" }));
      const label = svgEl("text", { x: m.left - 8, y: yy, class: "tick-label", "text-anchor": "end", "dominant-baseline": "middle" });
      label.textContent = fmtMoney(t, cur, t < 10 ? 2 : 0);
      svg.append(label);
    }
    for (const t of logTicks(x0, x1)) {
      const xx = Math.round(x(t)) + 0.5;
      svg.append(svgEl("line", { x1: xx, x2: xx, y1: m.top, y2: height - m.bottom, class: "grid-line" }));
      const label = svgEl("text", { x: xx, y: height - m.bottom + 16, class: "tick-label", "text-anchor": "middle" });
      label.textContent = `${t}×`;
      svg.append(label);
    }
    const xTitle = svgEl("text", { x: width - m.right, y: height - 4, class: "axis-title", "text-anchor": "end" });
    xTitle.textContent = "Want/have ratio → scarcer";
    const yTitle = svgEl("text", { x: 4, y: m.top + 4, class: "axis-title" });
    yTitle.textContent = "NM estimate";
    svg.append(xTitle, yTitle);

    const coords = pts.map((p) => ({ p, cx: x(p.want_have_ratio), cy: y(p.current_price) }));
    // Regular pressings first so the (rarer) orange dots sit on top.
    for (const c of [...coords].sort((a, b) => Number(a.p.rare_pressing) - Number(b.p.rare_pressing))) {
      svg.append(svgEl("circle", { cx: c.cx, cy: c.cy, r: 4, class: `dot ${c.p.rare_pressing ? "dot-2" : "dot-1"}` }));
    }
    const ring = svgEl("circle", { r: 8, class: "dot-focus", visibility: "hidden" });
    svg.append(ring);
    const tooltip = el("div", { class: "chart-tooltip", hidden: true });
    scatterBox.replaceChildren(svg, tooltip);
    scatterState.geo = { coords: [...coords].sort((a, b) => a.cx - b.cx), ring, tooltip, width };
  }

  function focusScatter(c) {
    const g = scatterState.geo;
    if (!g) return;
    scatterState.focus = c;
    if (!c) { g.ring.setAttribute("visibility", "hidden"); g.tooltip.hidden = true; return; }
    g.ring.setAttribute("cx", c.cx);
    g.ring.setAttribute("cy", c.cy);
    g.ring.setAttribute("visibility", "visible");
    const p = c.p;
    g.tooltip.replaceChildren(
      el("strong", { text: fmtMoney(p.current_price, p.currency) }),
      el("span", { text: `${p.want_have_ratio.toFixed(2)}× want/have${p.rare_pressing ? " · rare pressing" : ""}` }),
      el("span", { class: "tooltip-row", text: `${p.artist} – ${p.title}` }));
    placeTooltip(g.tooltip, c.cx, c.cy, g.width);
  }

  function nearestDot(event) {
    const g = scatterState.geo;
    if (!g) return null;
    const rect = scatterBox.getBoundingClientRect();
    const px = event.clientX - rect.left, py = event.clientY - rect.top;
    let best = null, bestD = 24; // hit target bigger than the 8px dot
    for (const c of g.coords) {
      const d = Math.hypot(c.cx - px, c.cy - py);
      if (d < bestD) { best = c; bestD = d; }
    }
    return best;
  }

  scatterBox.addEventListener("pointermove", (e) => focusScatter(nearestDot(e)));
  scatterBox.addEventListener("pointerleave", () => focusScatter(null));
  scatterBox.addEventListener("click", (e) => {
    const c = nearestDot(e);
    if (c) openDetail(c.p.release_id);
  });
  scatterBox.addEventListener("keydown", (e) => {
    const g = scatterState.geo;
    if (!g) return;
    const i = scatterState.focus ? g.coords.indexOf(scatterState.focus) : -1;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const next = e.key === "ArrowRight" ? Math.min(g.coords.length - 1, i + 1) : Math.max(0, i - 1);
      focusScatter(g.coords[next]);
    } else if (e.key === "Enter" && scatterState.focus) {
      openDetail(scatterState.focus.p.release_id);
    }
  });
  scatterBox.addEventListener("blur", () => focusScatter(null));

  // ---- 2. median NM estimate by style
  function median(values) {
    const s = [...values].sort((a, b) => a - b);
    const mid = Math.floor(s.length / 2);
    return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
  }

  function renderStyles() {
    const groups = new Map();
    for (const i of overview.items) {
      const key = i.style || i.genre || "Unknown";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(i);
    }
    const rows = [...groups.entries()]
      .filter(([, list]) => list.length >= 3)
      .map(([style, list]) => ({
        style,
        count: list.length,
        price: median(list.map((i) => i.current_price)),
        ratio: median(list.map((i) => i.want_have_ratio)),
        currency: list[0].currency,
      }))
      .sort((a, b) => b.price - a.price)
      .slice(0, 10);
    const box = $("style-chart");
    if (!rows.length) {
      box.replaceChildren(el("p", { class: "note", text: "Not enough CDs per style for these filters." }));
      $("style-table").replaceChildren();
      return;
    }
    const max = Math.max(...rows.map((r) => r.price));
    box.replaceChildren(...rows.map((r) => {
      const bar = el("span", { class: "bar" });
      bar.style.width = `${(r.price / max) * 78}%`; // leave room for the value label
      return el("div", {
        class: "bar-row",
        title: `${r.style}: median ${fmtMoney(r.price, r.currency)} · median want/have ${r.ratio.toFixed(2)}× · ${r.count} CDs`,
      },
        el("span", { class: "bar-name", text: `${r.style} (${r.count})` }),
        el("span", { class: "bar-track" }, bar, el("span", { class: "bar-value", text: fmtMoney(r.price, r.currency, 0) })));
    }));
    $("style-table").replaceChildren(
      el("thead", {}, el("tr", {},
        el("th", { scope: "col", text: "Style" }), el("th", { scope: "col", class: "num", text: "CDs" }),
        el("th", { scope: "col", class: "num", text: "Median NM est." }),
        el("th", { scope: "col", class: "num", text: "Median want/have" }))),
      el("tbody", {}, ...rows.map((r) => el("tr", {},
        el("td", { text: r.style }), el("td", { class: "num", text: r.count }),
        el("td", { class: "num", text: fmtMoney(r.price, r.currency) }),
        el("td", { class: "num", text: `${r.ratio.toFixed(2)}×` })))));
  }

  // ---- 3. market index
  const indexBox = $("index-chart");
  const indexState = { geo: null, focus: null };
  let indexRequest = null;

  async function loadIndex() {
    if (indexRequest) indexRequest.abort();
    const controller = new AbortController();
    indexRequest = controller;
    try {
      const data = await fetchJSON(`/api/market-index?${filterParams()}`, controller.signal);
      overview.index = data.points;
      renderIndex();
    } catch (error) {
      if (error.name !== "AbortError") indexBox.replaceChildren(el("p", { class: "note", text: error.message }));
    }
  }

  function renderIndex() {
    const pts = overview.index;
    const note = $("index-note");
    $("index-table").replaceChildren(
      el("thead", {}, el("tr", {},
        el("th", { scope: "col", text: "Date" }), el("th", { scope: "col", class: "num", text: "Own moves" }),
        el("th", { scope: "col", class: "num", text: "Raw" }), el("th", { scope: "col", class: "num", text: "CDs" }))),
      el("tbody", {}, ...pts.slice().reverse().map((p) => el("tr", {},
        el("td", { text: fmtDate(p.date) }), el("td", { class: "num", text: p.index.toFixed(2) }),
        el("td", { class: "num", text: p.raw_index.toFixed(2) }), el("td", { class: "num", text: p.cds })))));
    if (pts.length < 2) {
      indexBox.replaceChildren(el("p", { class: "note", text: "The index needs at least two days of prices." }));
      note.textContent = "";
      indexState.geo = null;
      return;
    }
    note.textContent = pts.length < 14
      ? `${pts.length} days of data so far — the trend becomes meaningful after a few weeks of daily runs.`
      : `Base 100 on ${fmtDate(pts[0].date)}.`;
    const width = Math.max(260, Math.floor(indexBox.clientWidth));
    const height = 180;
    const m = { top: 10, right: 14, bottom: 26, left: 44 };
    const values = pts.flatMap((p) => [p.index, p.raw_index]);
    const spread = Math.max(Math.max(...values) - 100, 100 - Math.min(...values), 0.5);
    const { lo, hi, ticks } = niceTicks(100 - spread, 100 + spread, 4);
    const x = (i) => m.left + (i / (pts.length - 1)) * (width - m.left - m.right);
    const y = (v) => m.top + (1 - (v - lo) / (hi - lo)) * (height - m.top - m.bottom);
    const svg = svgEl("svg", { width, height, viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
    for (const t of ticks) {
      const yy = Math.round(y(t)) + 0.5;
      svg.append(svgEl("line", { x1: m.left, x2: width - m.right, y1: yy, y2: yy, class: t === 100 ? "axis-line" : "grid-line" }));
      const label = svgEl("text", { x: m.left - 6, y: yy, class: "tick-label", "text-anchor": "end", "dominant-baseline": "middle" });
      label.textContent = Number(t.toFixed(2)).toString();
      svg.append(label);
    }
    const stride = Math.ceil(pts.length / Math.max(2, Math.floor((width - m.left) / 80)));
    pts.forEach((p, i) => {
      if (i % stride && i !== pts.length - 1) return;
      const label = svgEl("text", { x: x(i), y: height - 6, class: "tick-label", "text-anchor": i === 0 ? "start" : i === pts.length - 1 ? "end" : "middle" });
      label.textContent = new Date(p.date).toLocaleDateString(undefined, { day: "numeric", month: "short" });
      svg.append(label);
    });
    const path = (key) => pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join("");
    svg.append(svgEl("path", { d: path("raw_index"), class: "line-2" }), svgEl("path", { d: path("index"), class: "line" }));
    if (pts.length <= 12) { // with few points, show the observations themselves
      pts.forEach((p, i) => svg.append(
        svgEl("circle", { cx: x(i), cy: y(p.raw_index), r: 4, class: "marker-2" }),
        svgEl("circle", { cx: x(i), cy: y(p.index), r: 4, class: "marker-1" })));
    }
    const crosshair = svgEl("line", { y1: m.top, y2: height - m.bottom, class: "crosshair", visibility: "hidden" });
    svg.append(crosshair);
    const tooltip = el("div", { class: "chart-tooltip", hidden: true });
    indexBox.replaceChildren(svg, tooltip);
    indexState.geo = { x, y, crosshair, tooltip, width, n: pts.length };
    if (indexState.focus != null) showIndexPoint(Math.min(indexState.focus, pts.length - 1));
  }

  function showIndexPoint(i) {
    const g = indexState.geo;
    if (!g) return;
    indexState.focus = i;
    const p = overview.index[i];
    g.crosshair.setAttribute("x1", g.x(i));
    g.crosshair.setAttribute("x2", g.x(i));
    g.crosshair.setAttribute("visibility", "visible");
    const row = (cls, value, label) => el("div", { class: "tooltip-row" },
      el("span", { class: `key-line ${cls}` }), el("strong", { text: value.toFixed(2) }), el("span", { text: label }));
    g.tooltip.replaceChildren(
      row("key-1", p.index, "own moves"), row("key-2", p.raw_index, "raw prices"),
      el("span", { text: `${fmtDate(p.date)} · ${p.cds} CDs` }));
    placeTooltip(g.tooltip, g.x(i), g.y(Math.max(p.index, p.raw_index)), g.width);
  }

  function hideIndexPoint() {
    indexState.focus = null;
    if (!indexState.geo) return;
    indexState.geo.crosshair.setAttribute("visibility", "hidden");
    indexState.geo.tooltip.hidden = true;
  }

  indexBox.addEventListener("pointermove", (e) => {
    const g = indexState.geo;
    if (!g) return;
    const px = e.clientX - indexBox.getBoundingClientRect().left;
    let best = 0;
    for (let i = 1; i < g.n; i += 1) if (Math.abs(g.x(i) - px) < Math.abs(g.x(best) - px)) best = i;
    showIndexPoint(best);
  });
  indexBox.addEventListener("pointerleave", () => { if (document.activeElement !== indexBox) hideIndexPoint(); });
  indexBox.addEventListener("focus", () => indexState.geo && showIndexPoint(indexState.geo.n - 1));
  indexBox.addEventListener("blur", hideIndexPoint);
  indexBox.addEventListener("keydown", (e) => {
    const g = indexState.geo;
    if (!g || (e.key !== "ArrowLeft" && e.key !== "ArrowRight")) return;
    e.preventDefault();
    const i = indexState.focus ?? g.n - 1;
    showIndexPoint(Math.max(0, Math.min(g.n - 1, i + (e.key === "ArrowRight" ? 1 : -1))));
  });

  function wireTableToggle(buttonId, tableId) {
    const button = $(buttonId), table = $(tableId);
    button.addEventListener("click", () => {
      table.hidden = !table.hidden;
      button.setAttribute("aria-expanded", String(!table.hidden));
      button.textContent = table.hidden ? "Show data table" : "Hide data table";
    });
  }
  wireTableToggle("style-table-toggle", "style-table");
  wireTableToggle("index-table-toggle", "index-table");

  function renderOverview(items) {
    overview.items = items;
    renderScatter();
    renderStyles();
  }

  let overviewWidth = 0;
  new ResizeObserver(() => {
    const w = $("overview-grid").clientWidth;
    if (w === overviewWidth) return;
    overviewWidth = w;
    renderScatter();
    renderIndex();
  }).observe($("overview-grid"));

  // ---------------------------------------------------------------- filters

  const minInput = $("growth-min");
  const maxInput = $("growth-max");
  const reloadSoon = debounce(loadLeaderboard, 200);

  function growthLabel() {
    const { minGrowth: lo, maxGrowth: hi } = state;
    if (lo == null && hi == null) return "Any";
    if (hi == null) return `≥ ${fmtSignedPct(lo, 0)}`;
    if (lo == null) return `≤ ${fmtSignedPct(hi, 0)}`;
    return `${fmtSignedPct(lo, 0)} to ${fmtSignedPct(hi, 0)}`;
  }

  function syncGrowth(changed) {
    let lo = Number(minInput.value);
    let hi = Number(maxInput.value);
    if (lo > hi) { // thumbs may not cross
      if (changed === maxInput) { maxInput.value = lo; hi = lo; } else { minInput.value = hi; lo = hi; }
    }
    state.minGrowth = lo <= GROWTH_MIN ? null : lo;
    state.maxGrowth = hi >= GROWTH_MAX ? null : hi;

    const span = GROWTH_MAX - GROWTH_MIN;
    const range = $("growth-range");
    range.style.setProperty("--lo", `calc(8px + (100% - 16px) * ${(lo - GROWTH_MIN) / span})`);
    range.style.setProperty("--hi", `calc(8px + (100% - 16px) * ${(hi - GROWTH_MIN) / span})`);
    minInput.classList.toggle("on-top", lo > GROWTH_MAX - 10); // keep the min thumb reachable

    $("growth-readout").textContent = growthLabel();
    minInput.setAttribute("aria-valuetext", state.minGrowth == null ? "No minimum" : fmtSignedPct(lo, 0));
    maxInput.setAttribute("aria-valuetext", state.maxGrowth == null ? "No maximum" : fmtSignedPct(hi, 0));
  }

  function resetFilters() {
    state.q = "";
    state.genre = "";
    state.flag = "";
    $("search").value = "";
    $("genre").value = "";
    $("flag").value = "";
    minInput.value = String(GROWTH_MIN);
    maxInput.value = String(GROWTH_MAX);
    syncGrowth();
    loadLeaderboard();
  }

  $("search").addEventListener("input", (event) => {
    state.q = event.target.value.trim();
    reloadSoon();
  });
  $("genre").addEventListener("change", (event) => {
    state.genre = event.target.value;
    loadLeaderboard();
  });
  $("flag").addEventListener("change", (event) => {
    state.flag = event.target.value;
    loadLeaderboard();
  });
  minInput.addEventListener("input", () => { syncGrowth(minInput); reloadSoon(); });
  maxInput.addEventListener("input", () => { syncGrowth(maxInput); reloadSoon(); });
  $("reset").addEventListener("click", resetFilters);
  $("empty-reset").addEventListener("click", resetFilters);

  // ----------------------------------------------------------- table events

  // The leaderboard's normal ordering is by rank (Scarcity Index), so "rank ascending" is
  // treated as "not sorted by a column".
  $("board-table").querySelector("thead").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-sort]");
    if (!button) return;
    const key = button.dataset.sort;
    const current = state.sort === "rank" && state.order === "asc"
      ? null : { key: state.sort, order: state.order };
    const next = nextSort(current, key, SCREENER_FIRST[key][0]);
    state.sort = next ? next.key : "rank";
    state.order = next ? next.order : "asc";
    updateSortIndicators();
    loadLeaderboard();
  });
  addSortHints($("board-table"), "data-sort", SCREENER_FIRST);

  const tbody = $("rows");
  tbody.addEventListener("click", (event) => {
    const tr = event.target.closest("tr[data-id]");
    if (tr) {
      tr.focus({ preventScroll: true });
      openDetail(Number(tr.dataset.id));
    }
  });
  tbody.addEventListener("keydown", (event) => {
    const tr = event.target.closest("tr[data-id]");
    if (!tr) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openDetail(Number(tr.dataset.id));
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      const next = event.key === "ArrowDown" ? tr.nextElementSibling : tr.previousElementSibling;
      if (!next) return;
      event.preventDefault();
      next.focus();
      if (!$("drawer").hidden) openDetail(Number(next.dataset.id)); // browse with the drawer open
    }
  });
  tbody.addEventListener("focusin", (event) => {
    const tr = event.target.closest("tr[data-id]");
    if (!tr) return;
    for (const row of tbody.querySelectorAll('tr[tabindex="0"]')) if (row !== tr) row.tabIndex = -1;
    tr.tabIndex = 0;
  });

  $("drawer-close").addEventListener("click", closeDetail);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDetail();
  });

  // ------------------------------------------------------------------ theme

  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
  const effectiveTheme = () => document.documentElement.dataset.theme || (darkQuery.matches ? "dark" : "light");

  function updateThemeButton() {
    const next = effectiveTheme() === "dark" ? "light" : "dark";
    $("theme-toggle").setAttribute("aria-label", `Switch to ${next} theme`);
    $("theme-toggle").title = `Switch to ${next} theme`;
  }

  $("theme-toggle").addEventListener("click", () => {
    const next = effectiveTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("cd-screener-theme", next);
    } catch (e) { /* storage unavailable: the choice lasts for this page view */ }
    updateThemeButton();
  });
  if (darkQuery.addEventListener) darkQuery.addEventListener("change", updateThemeButton);

  // ------------------------------------------------------------------- init

  async function init() {
    syncGrowth();
    updateThemeButton();
    updateSortIndicators();
    try {
      await Promise.all([loadSummary(), loadGenres()]);
    } catch (error) {
      showAlert(error.message);
    }
    loadLeaderboard();
    loadReport();
    let savedTab = location.hash.slice(1); // e.g. /#wantlist
    if (location.hash.startsWith("#release-")) {
      savedTab = "screener"; // a link to a release always shows it over the screener
    } else if (!TABS.includes(savedTab)) {
      try { savedTab = localStorage.getItem("cd-screener-tab"); } catch (e) { /* storage unavailable */ }
    }
    if (TABS.includes(savedTab) && savedTab !== "screener") showTab(savedTab);
    const deepLink = /^#release-(\d+)$/.exec(location.hash);
    if (deepLink) openDetail(Number(deepLink[1]));
  }

  init();
})();
