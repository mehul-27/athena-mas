/* Hand-written SVG architecture diagram. No libraries, no remote assets.
 * Layout is fixed; variants come from the edge list that logic.js produces.
 */
"use strict";

(function (root) {
  const NS = "http://www.w3.org/2000/svg";

  const LANES = [
    { id: "adk", label: "Google ADK", x: 14, y: 14, w: 424, h: 250 },
    { id: "crew", label: "CrewAI · A2A :9001", x: 452, y: 14, w: 434, h: 172 },
    { id: "mcp", label: "MCP tools (stdio)", x: 452, y: 200, w: 434, h: 118 },
  ];

  const NODE_POS = {
    user: { x: 62, y: 62, w: 74, h: 34 },
    supervisor: { x: 196, y: 62, w: 104, h: 40 },
    planner: { x: 196, y: 130, w: 104, h: 36 },
    rag_researcher: { x: 84, y: 200, w: 116, h: 40 },
    web_researcher: { x: 214, y: 200, w: 116, h: 40 },
    calc_researcher: { x: 344, y: 200, w: 116, h: 40 },
    analyst: { x: 520, y: 58, w: 100, h: 40 },
    writer: { x: 632, y: 58, w: 100, h: 40 },
    critic: { x: 744, y: 58, w: 100, h: 40 },
    rag_tool: { x: 500, y: 246, w: 104, h: 34 },
    web_tool: { x: 616, y: 246, w: 104, h: 34 },
    calc_tool: { x: 732, y: 246, w: 104, h: 34 },
    blackboard: { x: 330, y: 330, w: 220, h: 96 },
  };

  const LABELS = {
    user: "User", supervisor: "Supervisor", planner: "Planner",
    rag_researcher: "RAG worker", web_researcher: "Web worker",
    calc_researcher: "Calc worker", analyst: "Analyst", writer: "Writer",
    critic: "Critic", rag_tool: "RAG", web_tool: "Web search",
    calc_tool: "Calculator", blackboard: "Blackboard",
  };

  const FRAMEWORK = {
    supervisor: "ADK", planner: "ADK", rag_researcher: "ADK",
    web_researcher: "ADK", calc_researcher: "ADK",
    analyst: "CrewAI", writer: "CrewAI", critic: "CrewAI",
  };

  function el(name, attrs, textContent) {
    const node = document.createElementNS(NS, name);
    Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, value));
    if (textContent != null) node.textContent = String(textContent);
    return node;
  }

  const centre = (box) => ({ x: box.x + box.w / 2, y: box.y + box.h / 2 });

  function port(box, towards) {
    const c = centre(box);
    const dx = towards.x - c.x;
    const dy = towards.y - c.y;
    if (Math.abs(dx) > Math.abs(dy)) {
      return { x: c.x + (dx > 0 ? box.w / 2 : -box.w / 2), y: c.y };
    }
    return { x: c.x, y: c.y + (dy > 0 ? box.h / 2 : -box.h / 2) };
  }

  function edgePath(fromBox, toBox) {
    const start = port(fromBox, centre(toBox));
    const end = port(toBox, centre(fromBox));
    const mx = (start.x + end.x) / 2;
    return `M${start.x},${start.y} C${mx},${start.y} ${mx},${end.y} ${end.x},${end.y}`;
  }

  function render(target, spec, state, options) {
    const opts = options || {};
    target.replaceChildren();
    const svg = el("svg", {
      viewBox: "0 0 900 460", class: "diagram" + (opts.compact ? " compact" : ""),
      role: "img", "aria-labelledby": "diagram-title diagram-desc",
    });
    svg.appendChild(el("title", { id: "diagram-title" }, "Athena architecture"));
    svg.appendChild(el("desc", { id: "diagram-desc" },
      "User to supervisor to planner in Google ADK, researchers calling MCP tools, "
      + "analyst writer and critic reached over A2A, all sharing a blackboard."));

    const defs = el("defs", {});
    const marker = el("marker", { id: "arrow", markerWidth: "9", markerHeight: "9",
      refX: "7", refY: "3", orient: "auto" });
    marker.appendChild(el("path", { d: "M0,0 L7,3 L0,6 z", class: "arrowhead" }));
    defs.appendChild(marker);
    svg.appendChild(defs);

    LANES.forEach((lane) => {
      svg.appendChild(el("rect", { x: lane.x, y: lane.y, width: lane.w, height: lane.h,
        rx: 10, class: "lane lane-" + lane.id }));
      svg.appendChild(el("text", { x: lane.x + 10, y: lane.y + 18,
        class: "lane-label" }, lane.label));
    });

    const active = (state && state.edges) || {};
    (spec.edges || []).forEach((edge) => {
      const from = NODE_POS[edge.from];
      const to = NODE_POS[edge.to];
      if (!from || !to) return;
      const activity = active[edge.from + "->" + edge.to] || {};
      const group = el("g", { class: "edge-group" + (activity.active ? " active" : "") });
      group.appendChild(el("path", { d: edgePath(from, to), class: "edge" + (edge.label === "revision" ? " revision" : ""),
        "marker-end": "url(#arrow)" }));
      const mid = { x: (centre(from).x + centre(to).x) / 2, y: (centre(from).y + centre(to).y) / 2 };
      if (edge.label) {
        group.appendChild(el("text", { x: mid.x, y: mid.y - 4, class: "edge-label" }, edge.label));
      }
      if (activity.bytes) {
        group.appendChild(el("text", { x: mid.x, y: mid.y + 12, class: "edge-bytes" }, activity.bytes));
      }
      svg.appendChild(group);
    });

    const nodes = (state && state.nodes) || {};
    (spec.nodes || []).forEach((id) => {
      const box = NODE_POS[id];
      if (!box) return;
      const info = nodes[id] || { state: "idle", label: "", calls: 0 };
      const group = el("g", { class: "node state-" + info.state + (id === "blackboard" && spec.blackboardAuditOnly ? " audit" : ""),
        "data-node": id, tabindex: "0" });
      if (id === "blackboard") {
        group.appendChild(el("path", {
          d: `M${box.x},${box.y} h${box.w - 18} l18,18 v${box.h - 18} h-${box.w} z`,
          class: "board" }));
      } else {
        group.appendChild(el("rect", { x: box.x, y: box.y, width: box.w, height: box.h,
          rx: 8, class: "box" }));
      }
      group.appendChild(el("text", { x: centre(box).x, y: centre(box).y + 4,
        class: "node-label" }, LABELS[id] || id));
      const badge = FRAMEWORK[id];
      if (badge) {
        group.appendChild(el("text", { x: centre(box).x, y: box.y + 14,
          class: "node-badge " + badge.toLowerCase() }, badge));
      }
      if (info.calls) {
        group.appendChild(el("text", { x: box.x + box.w - 8, y: centre(box).y + 4,
          class: "node-count", "text-anchor": "end" }, info.calls + "x"));
      }
      if (info.label) {
        group.appendChild(el("text", { x: centre(box).x, y: box.y + box.h - 6,
          class: "node-meta" }, info.label));
      }
      if (id === "blackboard" && spec.blackboardAuditOnly) {
        group.appendChild(el("text", { x: centre(box).x, y: box.y + 20,
          class: "node-meta" }, "audit only"));
      }
      svg.appendChild(group);
    });

    target.appendChild(svg);
    return svg;
  }

  root.AthenaDiagram = { render, NODE_POS, LABELS, LANES };
}(typeof globalThis !== "undefined" ? globalThis : this));
