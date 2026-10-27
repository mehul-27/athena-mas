/* Pure view-model helpers for the demo page. No DOM access: testable with node.
 *
 * Loaded as a classic script by the page (globalThis.AthenaLogic) and as a
 * CommonJS module by tests (`module.exports` when `module` exists).
 */
"use strict";

(function (root) {
  const FRAMEWORK = {
    planner: "adk", researcher: "adk", rag_researcher: "adk",
    web_researcher: "adk", calc_researcher: "adk", supervisor: "adk",
    analyst: "crewai", writer: "crewai", critic: "crewai",
  };

  function frameworkOf(event) {
    return event.framework || FRAMEWORK[event.agent] || "adk";
  }

  /* One row per agent, derived only from recorded events. */
  function reduceTimeline(events) {
    const rows = new Map();
    const ensure = (agent) => {
      if (!rows.has(agent)) {
        rows.set(agent, {
          agent, framework: "adk", status: "waiting",
          ms: null, model_in: 0, model_out: 0, stage_in: 0, stage_out: 0,
          reasoning: 0, stop_reason: "",
        });
      }
      return rows.get(agent);
    };
    (events || []).forEach((event) => {
      const row = ensure(event.agent || "?");
      row.framework = frameworkOf(event);
      if (event.kind === "stage_end") {
        row.status = event.ok === false ? "failed" : "done";
        row.ms = event.duration_ms == null ? null : Math.round(event.duration_ms);
        // A stage_end carries the stage total, so it replaces the per-call sum.
        if (event.tokens_in || event.tokens_out) {
          row.stage_in = event.tokens_in || 0;
          row.stage_out = event.tokens_out || 0;
        }
        if (event.extra && event.extra.stop_reason) row.stop_reason = event.extra.stop_reason;
      } else if (event.kind === "model_call") {
        row.model_in += event.tokens_in || 0;
        row.model_out += event.tokens_out || 0;
        if (event.extra && event.extra.reasoning_tokens) {
          row.reasoning += event.extra.reasoning_tokens;
        }
        if (row.status === "waiting") row.status = "running";
      } else if (event.kind === "stage_start" && row.status !== "done") {
        row.status = "running";
      } else if (event.kind === "error") {
        row.status = row.status === "done" ? "done" : "failed";
      }
    });
    return Array.from(rows.values()).map((row) => ({
      agent: row.agent, framework: row.framework, status: row.status, ms: row.ms,
      tokens_in: row.stage_in || row.model_in,
      tokens_out: row.stage_out || row.model_out,
      reasoning: row.reasoning, stop_reason: row.stop_reason,
    }));
  }

  function a2aBytes(events) {
    return (events || []).reduce((total, event) => {
      if (event.kind !== "a2a_call") return total;
      const extra = event.extra || {};
      return {
        out: total.out + (extra.request_bytes || 0),
        in: total.in + (extra.response_bytes || 0),
      };
    }, { out: 0, in: 0 });
  }

  const VERDICT_WORD = { true: "approved", false: "rejected" };

  /* "critic approved (low)" | "critic rejected (high)" | "critic unknown" */
  function verdictLabel(approved, severity) {
    if (approved === null || approved === undefined) return "critic unknown";
    const word = VERDICT_WORD[String(approved)];
    return severity ? "critic " + word + " (" + severity + ")" : "critic " + word;
  }

  function verdictFromCritique(critique) {
    if (!critique || !critique.value) return { approved: null, severity: null };
    const value = critique.value || critique;
    return { approved: Boolean(value.approved), severity: value.severity || null };
  }

  /* Citation rows the table can render; the quote is never empty by design. */
  function citationRows(report) {
    const citations = (report && report.citations) || [];
    return citations.map((citation, index) => ({
      index: index + 1,
      source_ref: citation.source_ref || "(no source)",
      quote: citation.quote || citation.text || "(quote not recorded)",
    }));
  }

  function statusStrip(health, replaying) {
    const mode = replaying || !health || !health.provider_configured ? "replay" : "live";
    return {
      crew: health && health.crew_server ? health.crew_server : "unknown",
      model: mode === "replay" ? "n/a (replay)" : (health.model || "not set"),
      host: mode === "replay" ? "n/a (replay)" : (health.api_host || "default"),
      mode,
    };
  }

  /* Readable card views for the blackboard, not raw JSON. */
  function blackboardCards(snapshot) {
    const cards = [];
    const value = (name) => (snapshot[name] ? snapshot[name].value : null);
    const version = (name) => (snapshot[name] ? snapshot[name].version : null);

    const task = value("task");
    if (task) cards.push({ name: "task", version: version("task"),
      lines: [task.question || "", "pattern: " + (task.pattern || "?")] });

    const plan = value("plan");
    if (plan) cards.push({ name: "plan", version: version("plan"),
      lines: (plan.steps || []).map((step, i) =>
        (i + 1) + ". " + (step.description || "") + " -> " + (step.assigned_to || "?")) });

    if (snapshot.evidence && snapshot.evidence.length) {
      cards.push({ name: "evidence", version: null, table: true,
        columns: ["author", "source", "score", "snippet"],
        rows: snapshot.evidence.map((item) => [
          item.retrieved_by || "?", item.source_ref || "?", String(item.score ?? ""),
          String(item.text || "").slice(0, 200)]) });
    }

    const analysis = value("analysis");
    if (analysis) cards.push({ name: "analysis", version: version("analysis"),
      lines: (analysis.key_findings || []).map((finding) => "- " + finding)
        .concat((analysis.gaps || []).map((gap) => "gap: " + gap)) });

    const draft = value("draft");
    if (draft) cards.push({ name: "draft", version: version("draft"),
      lines: [String(draft.text || "").slice(0, 600)] });

    const critique = value("critique");
    if (critique) cards.push({ name: "critique", version: version("critique"),
      verdict: verdictLabel(critique.approved, critique.severity),
      lines: (critique.issues || []).map((issue) => "- " + issue) });

    const report = value("final_report");
    if (report) cards.push({ name: "final_report", version: version("final_report"),
      lines: [(report.answer || "").slice(0, 600)] });

    return cards;
  }

  const api = {
    frameworkOf, reduceTimeline, a2aBytes, verdictLabel, verdictFromCritique,
    citationRows, statusStrip, blackboardCards,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.AthenaLogic = api;
}(typeof globalThis !== "undefined" ? globalThis : this));
