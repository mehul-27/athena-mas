/* Telemetry view helpers: pure shaping, layout and formatting for the run's
 * Metrics tab (cost, heat grids, waterfall, provenance drill-down).
 *
 * No DOM access anywhere in this file: `app.js` builds the elements and these
 * functions decide what to draw. Loaded in the browser as
 * `globalThis.AthenaTelemetry` and required by node in `telemetry.test.js`.
 */
"use strict";

(function (root) {
  const METRIC_LABELS = Object.freeze({
    stage_ms: "stage ms",
    model_calls: "model calls",
    tokens_in: "tokens in",
    tokens_out: "tokens out",
    reasoning_tokens: "reasoning",
    tool_calls: "tool calls",
    tool_errors: "tool errors",
    errors: "errors",
    retries: "retries",
    retries_inferred: "retries (inferred)",
    guardrails: "guardrails",
    handoffs: "handoffs",
    recoveries: "recoveries",
    limiter_wait_ms: "limiter wait ms",
    a2a_bytes: "a2a bytes",
    evidence_items: "evidence",
    cost_usd: "cost",
  });

  const UNITS = Object.freeze({
    ms: "ms",
    count: "n",
    tokens: "tok",
    bytes: "B",
    usd: "USD",
  });

  function isObject(value) {
    return Boolean(value) && typeof value === "object" && !Array.isArray(value);
  }

  function isNumber(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function clamp01(value) {
    if (!isNumber(value)) return 0;
    if (value < 0) return 0;
    if (value > 1) return 1;
    return value;
  }

  function formatMs(value) {
    if (!isNumber(value)) return "\u2014";
    if (Math.abs(value) < 1000) return Math.round(value) + " ms";
    if (Math.abs(value) < 60000) return (value / 1000).toFixed(1) + " s";
    const minutes = Math.floor(value / 60000);
    const seconds = Math.round((value % 60000) / 1000);
    return minutes + "m " + seconds + "s";
  }

  function formatCount(value) {
    if (!isNumber(value)) return "\u2014";
    const abs = Math.abs(value);
    if (abs >= 1e6) return (value / 1e6).toFixed(1) + "M";
    if (abs >= 1e4) return (value / 1e3).toFixed(0) + "k";
    if (abs >= 1e3) return (value / 1e3).toFixed(1) + "k";
    return Number.isInteger(value) ? String(value) : value.toFixed(1);
  }

  function formatBytes(value) {
    if (!isNumber(value)) return "\u2014";
    if (Math.abs(value) < 1024) return Math.round(value) + " B";
    if (Math.abs(value) < 1024 * 1024) return (value / 1024).toFixed(1) + " kB";
    return (value / (1024 * 1024)).toFixed(1) + " MB";
  }

  /* A cost is either a number, or a stated reason there is no number. It is
     never rendered as $0.00 for an unpriced call: that would be a claim. */
  function formatCost(costUsd, basis, currency) {
    const code = currency === "EUR" ? "\u20ac" : "$";
    if (isNumber(costUsd)) {
      if (costUsd === 0) return code + "0";
      if (Math.abs(costUsd) < 0.01) return code + costUsd.toFixed(6).replace(/0+$/, "");
      if (Math.abs(costUsd) < 1) return code + costUsd.toFixed(4);
      return code + costUsd.toFixed(2);
    }
    if (basis === "unpriced") return "unpriced";
    if (basis === "partly_unpriced") return "partly unpriced";
    if (basis === "no_model_calls") return "no model calls";
    return "\u2014";
  }

  function formatValue(raw, unit) {
    if (!isNumber(raw)) return "\u2014";
    if (unit === "ms") return formatMs(raw);
    if (unit === "usd") return formatCost(raw, "", "USD");
    if (unit === "bytes") return formatBytes(raw);
    if (unit === "tokens") return formatCount(raw);
    return formatCount(raw);
  }

  function metricLabel(name) {
    return METRIC_LABELS[name] || String(name || "");
  }

  /* ------------------------------------------------- recorded heatmap PNG
   * A replay bundle carries the heatmap it recorded as base64 PNG. That blob
   * is untrusted storage: a recording written by an older build can hold
   * characters outside the base64 alphabet, a truncated body, or bytes that
   * are not a PNG at all. Handing such a string straight to `img.src` renders
   * a broken image and logs a console error, so the payload is sniffed here
   * and the caller only ever sees either a usable data URL or a reason to say
   * the image is unavailable.
   */

  const BASE64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

  /* The eight-byte PNG signature, as decimal so it survives minifiers. */
  const PNG_SIGNATURE = Object.freeze([137, 80, 78, 71, 13, 10, 26, 10]);

  function base64Head(text, count) {
    const bytes = [];
    let buffer = 0;
    let bits = 0;
    for (let index = 0; index < text.length; index += 1) {
      const value = BASE64_ALPHABET.indexOf(text[index]);
      if (value < 0) return null;
      buffer = (buffer << 6) | value;
      bits += 6;
      if (bits >= 8) {
        bits -= 8;
        bytes.push((buffer >> bits) & 0xff);
        if (bytes.length >= count) break;
      }
    }
    return bytes;
  }

  function pngBase64(payload) {
    if (typeof payload !== "string" || !payload.trim()) {
      return { ok: false, url: "", reason: "this recording stored no heatmap image" };
    }
    const body = payload.trim().replace(/^data:image\/png;base64,/, "");
    if (!body) {
      return { ok: false, url: "", reason: "this recording stored no heatmap image" };
    }
    const stray = [];
    for (const char of body) {
      if (BASE64_ALPHABET.indexOf(char) < 0 && char !== "=" && !stray.includes(char)) {
        stray.push(char);
      }
    }
    if (stray.length) {
      return {
        ok: false,
        url: "",
        reason: "the recorded heatmap is not valid base64 (it contains "
          + stray.length + " character" + (stray.length === 1 ? "" : "s")
          + " outside the base64 alphabet)",
      };
    }
    if (body.length % 4 !== 0) {
      return {
        ok: false,
        url: "",
        reason: "the recorded heatmap base64 is truncated (its length is not a multiple of 4)",
      };
    }
    const head = base64Head(body, PNG_SIGNATURE.length);
    if (!head || head.length < PNG_SIGNATURE.length
        || PNG_SIGNATURE.some((byte, position) => head[position] !== byte)) {
      return { ok: false, url: "", reason: "the recorded heatmap bytes are not a PNG" };
    }
    return { ok: true, url: "data:image/png;base64," + body, reason: "" };
  }

  /* ------------------------------------------------------------ shaping */

  function shapeTelemetry(payload) {
    if (!isObject(payload)) {
      return {
        ok: false, agents: [], spans: [], totals: {}, costBasis: {},
        reason: "no telemetry payload", missing: [], exists: false,
      };
    }
    const agents = Array.isArray(payload.agents)
      ? payload.agents.filter(isObject).map((row) => ({
        agent: typeof row.agent === "string" && row.agent ? row.agent : "(unknown)",
        framework: typeof row.framework === "string" ? row.framework : "",
        start: isNumber(row.start_ms) ? row.start_ms : null,
        end: isNumber(row.end_ms) ? row.end_ms : null,
        duration: isNumber(row.duration_ms) ? row.duration_ms : null,
        tokensIn: isNumber(row.tokens_in) ? row.tokens_in : 0,
        tokensOut: isNumber(row.tokens_out) ? row.tokens_out : 0,
        reasoning: isNumber(row.reasoning_tokens) ? row.reasoning_tokens : 0,
        tokensSource: row.tokens_source || "",
        costUsd: isNumber(row.cost_usd) ? row.cost_usd : null,
        costBasis: row.cost_basis || "",
        costReason: row.cost_reason || "",
        modelCalls: isNumber(row.model_calls) ? row.model_calls : 0,
        toolCalls: isNumber(row.tool_calls) ? row.tool_calls : 0,
        toolErrors: isNumber(row.tool_errors) ? row.tool_errors : 0,
        errors: isNumber(row.errors) ? row.errors : 0,
        retries: isNumber(row.retries) ? row.retries : 0,
        retriesInferred: isNumber(row.retries_inferred) ? row.retries_inferred : 0,
        guardrails: isNumber(row.guardrails) ? row.guardrails : 0,
        handoffs: isNumber(row.handoffs) ? row.handoffs : 0,
        recoveries: isNumber(row.recoveries) ? row.recoveries : 0,
        limiterWait: isNumber(row.limiter_wait_ms) ? row.limiter_wait_ms : 0,
        a2aBytes: isNumber(row.a2a_bytes) ? row.a2a_bytes : 0,
        evidence: isObject(row.evidence_contributions)
          ? (isNumber(row.evidence_contributions.count) ? row.evidence_contributions.count : 0)
          : 0,
        evidenceSource: isObject(row.evidence_contributions)
          ? (row.evidence_contributions.source || "")
          : "",
        terminalState: row.terminal_state || "",
        models: Array.isArray(row.models) ? row.models.slice() : [],
      }))
      : [];
    const spans = Array.isArray(payload.spans) ? payload.spans.filter(isObject) : [];
    const totals = isObject(payload.totals) ? payload.totals : {};
    return {
      ok: true,
      exists: payload.exists === true,
      runId: payload.run_id || "",
      pattern: payload.pattern || "",
      agents: agents,
      spans: spans,
      totals: {
        wallMs: isNumber(totals.wall_ms) ? totals.wall_ms : null,
        tokensIn: isNumber(totals.tokens_in) ? totals.tokens_in : 0,
        tokensOut: isNumber(totals.tokens_out) ? totals.tokens_out : 0,
        costUsd: isNumber(totals.cost_usd) ? totals.cost_usd : null,
        costBasis: totals.cost_basis || "",
        unpricedCalls: isNumber(totals.unpriced_calls) ? totals.unpriced_calls : 0,
        events: isNumber(totals.events) ? totals.events : 0,
        terminalState: totals.terminal_state || "",
        toolCalls: isNumber(totals.tool_calls) ? totals.tool_calls : 0,
        retries: isNumber(totals.retries) ? totals.retries : 0,
        retriesInferred: isNumber(totals.retries_inferred) ? totals.retries_inferred : 0,
        guardrails: isNumber(totals.guardrails) ? totals.guardrails : 0,
        handoffs: isNumber(totals.handoffs) ? totals.handoffs : 0,
      },
      costBasis: isObject(payload.cost_basis) ? payload.cost_basis : {},
      reason: typeof payload.reason === "string" ? payload.reason : "",
      missing: Array.isArray(payload.missing) ? payload.missing.slice() : [],
      notes: Array.isArray(payload.notes) ? payload.notes.slice() : [],
    };
  }

  function telemetrySummary(shape) {
    if (!isObject(shape) || !shape.ok) return "Telemetry unavailable";
    if (!shape.agents.length) {
      return shape.reason || "No agent telemetry was recorded for this run.";
    }
    const totals = shape.totals || {};
    const parts = [
      shape.agents.length + " agent" + (shape.agents.length === 1 ? "" : "s"),
      formatCount(totals.tokensIn) + " in / " + formatCount(totals.tokensOut) + " out",
      "cost " + formatCost(totals.costUsd, totals.costBasis, "USD"),
      formatMs(totals.wallMs),
    ];
    if (totals.unpricedCalls) {
      parts.push(totals.unpricedCalls + " unpriced call" + (totals.unpricedCalls === 1 ? "" : "s"));
    }
    return parts.join(" \u00b7 ");
  }

  /* -------------------------------------------------------------- colour */

  function heatLevel(raw, min, max) {
    if (!isNumber(raw) || !isNumber(min) || !isNumber(max)) return null;
    if (max <= min) return raw === 0 ? 0 : 0.5;
    return clamp01((raw - min) / (max - min));
  }

  /* Grey -> amber -> red, warm on either theme, so a cell reads the same in
     the light and dark surfaces. Alpha carries the magnitude as well. */
  const RAMP = Object.freeze([
    [143, 143, 138],
    [212, 176, 106],
    [224, 113, 111],
  ]);

  function rampColour(level) {
    const value = clamp01(level);
    const scaled = value * (RAMP.length - 1);
    const index = Math.min(RAMP.length - 2, Math.floor(scaled));
    const t = scaled - index;
    const from = RAMP[index];
    const to = RAMP[index + 1];
    return from.map((channel, position) => Math.round(channel + (to[position] - channel) * t));
  }

  function heatColour(level) {
    if (!isNumber(level)) return "rgba(0, 0, 0, 0)";
    const rgb = rampColour(level);
    const alpha = level <= 0 ? 0.06 : 0.16 + clamp01(level) * 0.72;
    return "rgba(" + rgb.join(", ") + ", " + alpha.toFixed(2) + ")";
  }

  /* ---------------------------------------------------------- heat grids */

  function cellDisplay(cell) {
    if (!isObject(cell) || !isNumber(cell.raw)) return "\u2014";
    return formatValue(cell.raw, cell.unit);
  }

  function buildCell(agent, column, cell) {
    const raw = isObject(cell) && isNumber(cell.raw) ? cell.raw : null;
    const level = isObject(cell) && isNumber(cell.normalised) ? cell.normalised : null;
    const eventIds = isObject(cell) && Array.isArray(cell.event_ids) ? cell.event_ids.slice() : [];
    return {
      agent: agent,
      column: column.name,
      label: column.label || metricLabel(column.name),
      unit: column.unit || "",
      raw: raw,
      level: level,
      display: cellDisplay(cell),
      eventIds: eventIds,
      hasValue: raw !== null,
      /* An unpriced cost cell has no value but does have model calls behind it;
         those events are the reason it is null, so they stay drillable. */
      drillable: eventIds.length > 0,
      key: agent + "::" + column.name,
    };
  }

  function metricGrid(heat) {
    if (!isObject(heat)) {
      return { ok: false, columns: [], rows: [], empty: true, reason: "no heatmap payload" };
    }
    const agents = Array.isArray(heat.agents) ? heat.agents.slice() : [];
    const metrics = Array.isArray(heat.metrics) ? heat.metrics.filter(isObject) : [];
    const matrix = isObject(heat.metric_matrix) ? heat.metric_matrix : {};
    const ranges = isObject(heat.metric_normalisation) && isObject(heat.metric_normalisation.ranges)
      ? heat.metric_normalisation.ranges
      : {};
    const columns = metrics.map((metric) => ({
      name: metric.name,
      label: metric.label || metricLabel(metric.name),
      unit: metric.unit || "",
      min: isObject(ranges[metric.name]) && isNumber(ranges[metric.name].min) ? ranges[metric.name].min : null,
      max: isObject(ranges[metric.name]) && isNumber(ranges[metric.name].max) ? ranges[metric.name].max : null,
      title: (metric.label || metricLabel(metric.name))
        + (isObject(ranges[metric.name]) && isNumber(ranges[metric.name].max)
          ? " (max " + formatValue(ranges[metric.name].max, metric.unit) + ")" : ""),
    }));
    const rows = agents.map((agent) => {
      const cells = {};
      columns.forEach((column) => {
        const source = isObject(matrix[agent]) ? matrix[agent][column.name] : null;
        cells[column.name] = buildCell(agent, column, source);
      });
      return { agent: agent, cells: cells };
    });
    return {
      ok: true,
      columns: columns,
      rows: rows,
      empty: !columns.length || !rows.length,
      reason: heat.reason || (rows.length ? "" : "no agent rows recorded"),
      normalisation: isObject(heat.metric_normalisation) ? (heat.metric_normalisation.basis || "") : "",
      pngFallback: heat.png_fallback || "",
    };
  }

  function bucketGrid(heat) {
    if (!isObject(heat)) {
      return { ok: false, columns: [], rows: [], empty: true, reason: "no heatmap payload" };
    }
    const agents = Array.isArray(heat.agents) ? heat.agents.slice() : [];
    const buckets = Array.isArray(heat.buckets) ? heat.buckets.filter(isObject) : [];
    const matrix = isObject(heat.bucket_matrix) ? heat.bucket_matrix : {};
    const columns = buckets.map((bucket) => ({
      name: String(bucket.index),
      label: bucket.label || "stage " + bucket.index,
      unit: "ms",
      min: null,
      max: null,
      title: (bucket.label || "") + (isNumber(bucket.duration_ms) ? " \u2014 " + formatMs(bucket.duration_ms) : ""),
      agent: bucket.agent || "",
      startMs: isNumber(bucket.start_offset_ms) ? bucket.start_offset_ms : null,
      durationMs: isNumber(bucket.duration_ms) ? bucket.duration_ms : null,
    }));
    const rows = agents.map((agent) => {
      const cells = {};
      columns.forEach((column) => {
        const source = isObject(matrix[agent]) ? matrix[agent][column.name] : null;
        cells[column.name] = buildCell(agent, column, source);
      });
      return { agent: agent, cells: cells };
    });
    return {
      ok: true,
      columns: columns,
      rows: rows,
      empty: !columns.length || !rows.length,
      reason: heat.reason || (buckets.length ? "" : "no completed stages recorded"),
      normalisation: isObject(heat.bucket_normalisation) ? (heat.bucket_normalisation.basis || "") : "",
      pngFallback: heat.png_fallback || "",
    };
  }

  /* ---------------------------------------------------------- waterfall */

  function waterfallGeometry(spans, width, options) {
    const settings = isObject(options) ? options : {};
    const trackWidth = isNumber(width) && width > 40 ? width : 640;
    const rowHeight = isNumber(settings.rowHeight) ? settings.rowHeight : 18;
    const gap = isNumber(settings.gap) ? settings.gap : 4;
    const list = Array.isArray(spans) ? spans.filter(isObject) : [];
    const agents = [];
    list.forEach((span) => {
      const agent = typeof span.agent === "string" && span.agent ? span.agent : "(unknown)";
      if (!agents.includes(agent)) agents.push(agent);
    });
    let total = 0;
    list.forEach((span) => {
      const end = isNumber(span.end_offset_ms) ? span.end_offset_ms : null;
      const start = isNumber(span.start_offset_ms) ? span.start_offset_ms : null;
      const stop = end !== null ? end : start;
      if (stop !== null && stop > total) total = stop;
    });
    if (total <= 0) total = 1;
    const scale = trackWidth / total;
    const rowIndex = {};
    agents.forEach((agent, index) => { rowIndex[agent] = index; });
    const bars = [];
    list.forEach((span, order) => {
      const agent = typeof span.agent === "string" && span.agent ? span.agent : "(unknown)";
      const start = isNumber(span.start_offset_ms) ? span.start_offset_ms : null;
      const end = isNumber(span.end_offset_ms) ? span.end_offset_ms : null;
      if (start === null && end === null) return;
      const from = start === null ? end : start;
      const to = end === null ? total : end;
      const barWidth = Math.max(2, Math.round((to - from) * scale));
      bars.push({
        key: span.span_id || span.event_id || agent + ":" + order,
        agent: agent,
        kind: span.kind || "",
        label: span.label || span.kind || "",
        ok: span.ok !== false,
        open: span.open === true,
        startMs: from,
        durationMs: isNumber(span.duration_ms) ? span.duration_ms : null,
        x: Math.max(0, Math.min(trackWidth - 2, Math.round(from * scale))),
        width: Math.min(barWidth, trackWidth),
        row: rowIndex[agent],
        y: rowIndex[agent] * (rowHeight + gap),
        height: rowHeight,
        offsetLabel: formatMs(from),
        durationLabel: isNumber(span.duration_ms) ? formatMs(span.duration_ms) : "open",
        title: (span.label || span.kind || "span") + " \u2014 " + (agent)
          + " \u2014 " + (isNumber(span.duration_ms) ? formatMs(span.duration_ms) : "no duration recorded"),
      });
    });
    const ticks = [0, 0.25, 0.5, 0.75, 1].map((fraction) => ({
      x: Math.round(trackWidth * fraction),
      label: formatMs(total * fraction),
    }));
    return {
      width: trackWidth,
      totalMs: total,
      rowHeight: rowHeight,
      gap: gap,
      height: Math.max(1, agents.length) * (rowHeight + gap),
      rows: agents.map((agent, index) => ({
        agent: agent, y: index * (rowHeight + gap), height: rowHeight,
      })),
      bars: bars,
      ticks: ticks,
      scale: scale,
    };
  }

  /* ------------------------------------------------------- cell drill-down */

  function cellsQuery(target) {
    const spec = isObject(target) ? target : {};
    const parts = [];
    if (spec.agent) parts.push("agent=" + encodeURIComponent(spec.agent));
    if (isNumber(spec.bucket)) parts.push("bucket=" + encodeURIComponent(String(spec.bucket)));
    else if (spec.metric) parts.push("metric=" + encodeURIComponent(spec.metric));
    return parts.join("&");
  }

  function cellsUrl(runId, target) {
    const query = cellsQuery(target);
    if (!runId || !query) return "";
    return "/api/runs/" + encodeURIComponent(runId) + "/cells?" + query;
  }

  function eventRows(payload) {
    if (!isObject(payload) || !Array.isArray(payload.events)) return [];
    return payload.events.filter(isObject).map((event, index) => {
      if (event.kind === "evidence_item") {
        return {
          key: "evidence:" + (event.id || index),
          kind: "evidence",
          agent: event.retrieved_by || "",
          text: (event.source_ref || "") + (isNumber(event.score) ? " (score " + event.score.toFixed(2) + ")" : ""),
          detail: event.snippet || "",
          ts: "",
          ok: true,
        };
      }
      const seconds = typeof event.ts === "string" ? event.ts.slice(11, 19) : "";
      const bits = [];
      if (isNumber(event.duration_ms)) bits.push(formatMs(event.duration_ms));
      if (isNumber(event.tokens_in) || isNumber(event.tokens_out)) {
        bits.push(formatCount(event.tokens_in || 0) + " in / " + formatCount(event.tokens_out || 0) + " out");
      }
      if (event.payload_summary) bits.push(String(event.payload_summary));
      if (event.error_message) bits.push(String(event.error_message));
      return {
        key: event.event_id || "event:" + index,
        kind: String(event.kind || ""),
        agent: event.agent || "",
        text: "#" + (isNumber(event.seq) ? event.seq : index + 1) + " " + (event.kind || ""),
        detail: bits.filter(Boolean).join(" \u00b7 "),
        ts: seconds,
        ok: event.ok !== false,
      };
    });
  }

  function cellListTitle(payload) {
    if (!isObject(payload)) return "";
    const agent = payload.agent || "agent";
    if (payload.metric) return agent + " \u00b7 " + metricLabel(payload.metric);
    if (isNumber(payload.bucket)) return agent + " \u00b7 stage " + (payload.bucket + 1);
    return agent;
  }

  /* --------------------------------------------------------- provenance */

  const HOP_LABELS = Object.freeze({
    citation: "citation",
    evidence: "evidence item",
    source: "source",
    tool_call: "tool call",
    agent: "agent",
    run: "run",
  });

  function provenanceHops(payload) {
    if (!isObject(payload) || !Array.isArray(payload.chain)) return [];
    return payload.chain.filter(isObject).map((hop, index) => {
      const kind = hop.hop || "";
      let detail = "";
      if (kind === "citation") {
        detail = (hop.source_ref || "") + (hop.quote ? " \u2014 " + hop.quote : "");
      } else if (kind === "evidence") {
        detail = (hop.evidence_id || "") + " retrieved by " + (hop.retrieved_by || "?")
          + (hop.match ? " (" + hop.match + " match)" : "");
      } else if (kind === "source") {
        detail = hop.url || hop.document || "";
        if (hop.page) detail += " page " + hop.page;
      } else if (kind === "tool_call") {
        detail = (hop.tool || "tool") + " by " + (hop.agent || "?")
          + (hop.ok === false ? " (failed)" : "")
          + (hop.match ? " \u00b7 " + hop.match : "");
      } else if (kind === "agent") {
        detail = hop.agent || "";
      } else if (kind === "run") {
        detail = hop.run_id || "";
      }
      return { key: kind + ":" + index, kind: kind, label: HOP_LABELS[kind] || kind, detail: detail };
    });
  }

  function provenanceUnavailable(payload) {
    if (!isObject(payload)) return "provenance unavailable";
    if (payload.reason) return payload.reason;
    if (Array.isArray(payload.missing) && payload.missing.length) return payload.missing[0];
    return "";
  }

  const api = {
    METRIC_LABELS, UNITS,
    formatMs, formatCount, formatBytes, formatCost, formatValue, metricLabel,
    shapeTelemetry, telemetrySummary,
    heatLevel, heatColour,
    metricGrid, bucketGrid,
    pngBase64,
    waterfallGeometry,
    cellsQuery, cellsUrl, eventRows, cellListTitle,
    provenanceHops, provenanceUnavailable,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.AthenaTelemetry = api;
}(typeof globalThis !== "undefined" ? globalThis : this));
