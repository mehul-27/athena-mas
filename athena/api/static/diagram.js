/* Hand-written SVG architecture diagram. No libraries, no remote assets.
 *
 * The layout is data-driven: nodes are placed in lanes from a per-pattern
 * position map, then connectors are routed on a Manhattan grid and scored
 * against the node, lane-title and previously placed label boxes. The live
 * variants use the same layout; only state classes and edge activity change.
 */
"use strict";

(function (root) {
  const NS = "http://www.w3.org/2000/svg";

  const VIEW = { width: 1200, height: 570 };

  const LANES = [
    { id: "adk", label: "Google ADK", x: 18, y: 18, w: 640, h: 330 },
    { id: "crew", label: "CrewAI · A2A :9001", x: 704, y: 18, w: 478, h: 330 },
    { id: "mcp", label: "MCP tools (stdio)", x: 18, y: 368, w: 640, h: 180 },
    { id: "board", label: "Blackboard · shared state", x: 704, y: 368, w: 478, h: 180 },
  ];

  const NODE_META = {
    user: { label: "User", lane: "adk", w: 120, h: 54 },
    supervisor: { label: "Supervisor", lane: "adk", badge: "ADK", w: 156, h: 54 },
    planner: { label: "Planner", lane: "adk", badge: "ADK", w: 156, h: 54 },
    rag_researcher: { label: "RAG worker", lane: "adk", badge: "ADK", w: 156, h: 54 },
    web_researcher: { label: "Web worker", lane: "adk", badge: "ADK", w: 156, h: 54 },
    calc_researcher: { label: "Calc worker", lane: "adk", badge: "ADK", w: 156, h: 54 },
    analyst: { label: "Analyst", lane: "crew", badge: "CrewAI", w: 188, h: 54 },
    writer: { label: "Writer", lane: "crew", badge: "CrewAI", w: 188, h: 54 },
    critic: { label: "Critic", lane: "crew", badge: "CrewAI", w: 188, h: 54 },
    rag_tool: { label: "RAG", lane: "mcp", w: 156, h: 54 },
    web_tool: { label: "Web search", lane: "mcp", w: 156, h: 54 },
    calc_tool: { label: "Calculator", lane: "mcp", w: 156, h: 54 },
    blackboard: { label: "Blackboard", lane: "board", w: 284, h: 90 },
  };

  const BASE_POS = {
    user: { x: 52, y: 72 },
    supervisor: { x: 236, y: 72 },
    planner: { x: 236, y: 174 },
    rag_researcher: { x: 52, y: 278 },
    web_researcher: { x: 250, y: 278 },
    calc_researcher: { x: 448, y: 278 },
    analyst: { x: 800, y: 72 },
    writer: { x: 800, y: 156 },
    critic: { x: 800, y: 240 },
    rag_tool: { x: 76, y: 430 },
    web_tool: { x: 274, y: 430 },
    calc_tool: { x: 472, y: 430 },
    blackboard: { x: 800, y: 430 },
  };

  /* p2p is a chain: keep RAG worker beside the first A2A hop and move its
     tool underneath it; all other nodes retain their lane positions. */
  const PATTERN_POS = {
    p2p: {
      rag_researcher: { x: 448, y: 72 },
      rag_tool: { x: 472, y: 430 },
      web_tool: { x: 76, y: 430 },
      calc_tool: { x: 274, y: 430 },
    },
  };

  const LABELS = {};
  Object.keys(NODE_META).forEach((id) => { LABELS[id] = NODE_META[id].label; });

  function el(name, attrs, textContent) {
    const node = document.createElementNS(NS, name);
    Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, value));
    if (textContent != null) node.textContent = String(textContent);
    return node;
  }

  const centre = (box) => ({ x: box.x + box.w / 2, y: box.y + box.h / 2 });
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

  function estimate(value, size, factor) {
    return String(value || "").length * size * (factor || 0.58);
  }

  function positionsFor(pattern) {
    const extra = PATTERN_POS[pattern] || {};
    const result = {};
    Object.keys(NODE_META).forEach((id) => {
      result[id] = { ...BASE_POS[id], w: NODE_META[id].w, h: NODE_META[id].h, ...extra[id] };
    });
    return result;
  }

  function nodeLabels(id, box, info, audit) {
    const meta = NODE_META[id];
    const labels = [];
    if (meta.badge) {
      labels.push({
        kind: "badge",
        text: meta.badge,
        box: { x: box.x + 12, y: box.y + 6, w: estimate(meta.badge, 8.5, 0.7) + 2, h: 10 },
      });
    }
    labels.push({
      kind: "title",
      text: meta.label,
      box: {
        x: box.x + 12,
        y: box.y + (meta.badge ? 20 : 16),
        w: estimate(meta.label, 12, 0.58),
        h: 15,
      },
    });
    let metaText = "";
    if (id === "blackboard") metaText = audit ? "audit only" : "read + write";
    else if (info && info.label) metaText = info.label;
    if (metaText) {
      labels.push({
        kind: "meta",
        text: metaText,
        box: { x: box.x + 12, y: box.y + box.h - 17, w: estimate(metaText, 10, 0.58), h: 12 },
      });
    }
    if (info && info.calls) {
      const count = info.calls + "x";
      labels.push({
        kind: "count",
        text: count,
        box: { x: box.x + box.w - 12 - estimate(count, 9.5, 0.65), y: box.y + 6, w: estimate(count, 9.5, 0.65), h: 11 },
      });
    }
    return labels;
  }

  function buildNodes(spec, state, pattern) {
    const positions = positionsFor(pattern);
    const stateNodes = (state && state.nodes) || {};
    return (spec.nodes || []).map((id) => {
      if (!NODE_META[id] || !positions[id]) return null;
      const box = { ...positions[id] };
      const info = stateNodes[id] || { state: "idle", label: "", calls: 0 };
      const audit = id === "blackboard" && Boolean(spec.blackboardAuditOnly);
      return {
        id, box, lane: NODE_META[id].lane, meta: NODE_META[id],
        info, audit, labels: nodeLabels(id, box, info, audit),
      };
    }).filter(Boolean);
  }

  function buildLanes() {
    return LANES.map((lane) => {
      const labelWidth = estimate(lane.label, 10.5, 0.64) + 12;
      const titleX = lane.id === "mcp"
        ? lane.x + lane.w - 14 - labelWidth
        : lane.x + 14;
      return {
        ...lane,
        titleBox: { x: titleX, y: lane.y + 8, w: labelWidth, h: 14 },
      };
    });
  }

  function port(box, side) {
    const c = centre(box);
    if (side === "left") return { x: box.x, y: c.y };
    if (side === "right") return { x: box.x + box.w, y: c.y };
    if (side === "top") return { x: c.x, y: box.y };
    return { x: c.x, y: box.y + box.h };
  }

  function sideCost(box, side, other) {
    const c = centre(box);
    const o = centre(other);
    const dx = o.x - c.x;
    const dy = o.y - c.y;
    if (side === "right") return dx >= 0 ? 0 : 260;
    if (side === "left") return dx <= 0 ? 0 : 260;
    if (side === "bottom") return dy >= 0 ? 0 : 260;
    return dy <= 0 ? 0 : 260;
  }

  function approachPenalty(points, side, box) {
    const approach = points[points.length - 2] || points[0];
    if (side === "left") return approach.x > box.x - 2 ? 5000 : 0;
    if (side === "right") return approach.x < box.x + box.w + 2 ? 5000 : 0;
    if (side === "top") return approach.y > box.y - 2 ? 5000 : 0;
    return approach.y < box.y + box.h + 2 ? 5000 : 0;
  }

  function targetPortPenalty(prior, toId, targetSide) {
    return prior.some((edge) => edge.to === toId && edge.toSide === targetSide) ? 1e9 : 0;
  }

  function cleanPoints(points) {
    const cleaned = [];
    points.forEach((point) => {
      const last = cleaned[cleaned.length - 1];
      if (!last || Math.abs(last.x - point.x) > 0.01 || Math.abs(last.y - point.y) > 0.01) {
        cleaned.push({ x: Math.round(point.x * 10) / 10, y: Math.round(point.y * 10) / 10 });
      }
    });
    for (let i = 1; i < cleaned.length - 1; i += 1) {
      const a = cleaned[i - 1];
      const b = cleaned[i];
      const c = cleaned[i + 1];
      const collinear = (Math.abs(a.x - b.x) < 0.01 && Math.abs(b.x - c.x) < 0.01)
        || (Math.abs(a.y - b.y) < 0.01 && Math.abs(b.y - c.y) < 0.01);
      if (collinear) {
        cleaned.splice(i, 1);
        i -= 1;
      }
    }
    return cleaned;
  }

  function segmentLength(a, b) {
    return Math.abs(a.x - b.x) + Math.abs(a.y - b.y);
  }

  function pathLength(points) {
    let total = 0;
    for (let i = 1; i < points.length; i += 1) total += segmentLength(points[i - 1], points[i]);
    return total;
  }

  function segmentHitsBox(a, b, box, pad) {
    const x1 = Math.min(a.x, b.x);
    const x2 = Math.max(a.x, b.x);
    const y1 = Math.min(a.y, b.y);
    const y2 = Math.max(a.y, b.y);
    const left = box.x - pad;
    const right = box.x + box.w + pad;
    const top = box.y - pad;
    const bottom = box.y + box.h + pad;
    if (Math.abs(a.y - b.y) < 0.01) {
      return a.y >= top && a.y <= bottom && x2 >= left && x1 <= right;
    }
    if (Math.abs(a.x - b.x) < 0.01) {
      return a.x >= left && a.x <= right && y2 >= top && y1 <= bottom;
    }
    return false;
  }

  function segmentCrossesBox(a, b, box) {
    return segmentHitsBox(a, b, box, 0);
  }

  function segmentsCross(a, b, c, d) {
    const aH = Math.abs(a.y - b.y) < 0.01;
    const cH = Math.abs(c.y - d.y) < 0.01;
    if (aH === cH) return false;
    const h1 = aH ? a : c;
    const h2 = aH ? b : d;
    const v1 = aH ? c : a;
    const v2 = aH ? d : b;
    const hx1 = Math.min(h1.x, h2.x);
    const hx2 = Math.max(h1.x, h2.x);
    const vy1 = Math.min(v1.y, v2.y);
    const vy2 = Math.max(v1.y, v2.y);
    return v1.x >= hx1 && v1.x <= hx2 && h1.y >= vy1 && h1.y <= vy2;
  }

  function segmentsOverlap(a, b, c, d) {
    const aH = Math.abs(a.y - b.y) < 0.01;
    const cH = Math.abs(c.y - d.y) < 0.01;
    if (aH !== cH) return false;
    if (aH) {
      if (Math.abs(a.y - c.y) > 0.01) return false;
      return Math.max(Math.min(a.x, b.x), Math.min(c.x, d.x))
        < Math.min(Math.max(a.x, b.x), Math.max(c.x, d.x)) - 4;
    }
    if (Math.abs(a.x - c.x) > 0.01) return false;
    return Math.max(Math.min(a.y, b.y), Math.min(c.y, d.y))
      < Math.min(Math.max(a.y, b.y), Math.max(c.y, d.y)) - 4;
  }

  function pathScore(points, fromId, toId, nodeMap, tokens, prior) {
    if (points.length < 2) return Infinity;
    const obstacles = [];
    nodeMap.forEach((node) => {
      if (node.id !== fromId && node.id !== toId) obstacles.push(node.box);
    });
    let score = pathLength(points) + Math.max(0, points.length - 2) * 18;
    const lastLength = segmentLength(points[points.length - 2], points[points.length - 1]);
    if (lastLength < 36) score += 50;
    let outside = false;
    points.forEach((point) => {
      if (point.x < 8 || point.x > VIEW.width - 8 || point.y < 8 || point.y > VIEW.height - 8) outside = true;
    });
    if (outside) score += 100000;
    for (let i = 1; i < points.length; i += 1) {
      const a = points[i - 1];
      const b = points[i];
      if (segmentLength(a, b) < 12) score += 40;
      const fromBox = nodeMap.get(fromId) && nodeMap.get(fromId).box;
      const toBox = nodeMap.get(toId) && nodeMap.get(toId).box;
      if (fromBox && segmentHitsBox(a, b, fromBox, -2)) score += 200000;
      if (toBox && segmentHitsBox(a, b, toBox, -2)) score += 200000;
      obstacles.forEach((box) => {
        if (segmentHitsBox(a, b, box, 6)) score += 100000;
      });
      tokens.forEach((box) => {
        if (segmentHitsBox(a, b, box, 4)) score += 60000;
      });
      prior.forEach((edge) => {
        for (let j = 1; j < edge.points.length; j += 1) {
          if (segmentsCross(a, b, edge.points[j - 1], edge.points[j])) score += 35;
          if (edge.to === toId && segmentsOverlap(a, b, edge.points[j - 1], edge.points[j])) score += 400;
        }
      });
    }
    return score;
  }

  function channelXs(from, to) {
    return Array.from(new Set([
      681,
      (from.x + from.w + to.x) / 2,
      (from.x + to.x + to.w) / 2,
      from.x - 24,
      from.x + from.w + 24,
      to.x - 24,
      to.x + to.w + 24,
      Math.min(from.x, to.x) - 24,
      Math.max(from.x + from.w, to.x + to.w) + 24,
      24,
      VIEW.width - 24,
    ])).map((x) => clamp(x, 12, VIEW.width - 12));
  }

  function channelYs(from, to) {
    return Array.from(new Set([
      150,
      225,
      253,
      410,
      (from.y + from.h + to.y) / 2,
      (from.y + to.y + to.h) / 2,
      from.y - 24,
      from.y + from.h + 24,
      to.y - 24,
      to.y + to.h + 24,
      Math.min(from.y, to.y) - 24,
      Math.max(from.y + from.h, to.y + to.h) + 24,
      24,
      VIEW.height - 24,
    ])).map((y) => clamp(y, 12, VIEW.height - 12));
  }

  function pathsForSides(from, to, side, targetSide) {
    const start = port(from, side);
    const end = port(to, targetSide);
    const sourceHorizontal = side === "left" || side === "right";
    const targetHorizontal = targetSide === "left" || targetSide === "right";
    const paths = [];
    if (sourceHorizontal && targetHorizontal) {
      if (Math.abs(start.y - end.y) < 0.01) paths.push([start, end]);
      channelXs(from, to).forEach((x) => {
        paths.push([start, { x, y: start.y }, { x, y: end.y }, end]);
      });
    } else if (!sourceHorizontal && !targetHorizontal) {
      if (Math.abs(start.x - end.x) < 0.01) paths.push([start, end]);
      channelYs(from, to).forEach((y) => {
        paths.push([start, { x: start.x, y }, { x: end.x, y }, end]);
      });
    } else if (sourceHorizontal) {
      paths.push([start, { x: end.x, y: start.y }, end]);
      const direction = targetSide === "top" ? -22 : 22;
      channelXs(from, to).forEach((x) => {
        const y = clamp(end.y + direction, 12, VIEW.height - 12);
        paths.push([start, { x, y: start.y }, { x, y }, { x: end.x, y }, end]);
      });
    } else {
      paths.push([start, { x: start.x, y: end.y }, end]);
      const direction = targetSide === "left" ? -22 : 22;
      channelYs(from, to).forEach((y) => {
        const x = clamp(end.x + direction, 12, VIEW.width - 12);
        paths.push([start, { x: start.x, y }, { x, y }, { x, y: end.y }, end]);
      });
    }
    return paths;
  }

  function routeEdge(edge, fromNode, toNode, nodeMap, laneTitles, prior) {
    const candidates = [];
    ["right", "left", "top", "bottom"].forEach((side) => {
      ["left", "right", "top", "bottom"].forEach((targetSide) => {
        pathsForSides(fromNode.box, toNode.box, side, targetSide).forEach((raw) => {
          const points = cleanPoints(raw);
          if (points.length < 2) return;
          const score = sideCost(fromNode.box, side, toNode.box)
            + sideCost(toNode.box, targetSide, fromNode.box)
            + approachPenalty(points, targetSide, toNode.box)
            + targetPortPenalty(prior, toNode.id, targetSide)
            + pathScore(points, fromNode.id, toNode.id, nodeMap, laneTitles, prior);
          candidates.push({ points, score, fromSide: side, toSide: targetSide });
        });
      });
    });
    candidates.sort((a, b) => a.score - b.score);
    const chosen = candidates[0] || {
      points: [port(fromNode.box, "right"), port(toNode.box, "left")],
      score: Infinity, fromSide: "right", toSide: "left",
    };
    return {
      from: edge.from, to: edge.to, label: edge.label || "",
      fromSide: chosen.fromSide, toSide: chosen.toSide,
      points: chosen.points, start: chosen.points[0], end: chosen.points[chosen.points.length - 1],
      score: chosen.score,
    };
  }

  function labelLines(edge) {
    const lines = [];
    if (edge.label) lines.push({ kind: "label", text: edge.label, size: 10.5 });
    if (edge.bytes) lines.push({ kind: "bytes", text: edge.bytes, size: 9.5 });
    return lines;
  }

  function labelBoxAt(segment, width, height, orientation, offset, t) {
    const ratio = t == null ? 0.5 : t;
    const mx = segment.a.x + (segment.b.x - segment.a.x) * ratio;
    const my = segment.a.y + (segment.b.y - segment.a.y) * ratio;
    if (orientation === "horizontal") {
      return { x: mx - width / 2, y: my + offset, w: width, h: height };
    }
    return { x: mx + offset, y: my - height / 2, w: width, h: height };
  }

  function boxHitsBox(a, b, pad) {
    const p = pad || 0;
    return a.x < b.x + b.w + p && a.x + a.w > b.x - p
      && a.y < b.y + b.h + p && a.y + a.h > b.y - p;
  }

  function placeEdgeLabels(edges, nodes, lanes) {
    const placed = [];
    const nodeBoxes = nodes.map((node) => node.box);
    const laneBoxes = lanes.map((lane) => lane.titleBox);
    edges.forEach((edge) => {
      const lines = labelLines(edge);
      if (!lines.length) {
        edge.labelBox = null;
        edge.labelLines = [];
        return;
      }
      const width = Math.max(...lines.map((line) => estimate(line.text, line.size, 0.62))) + 12;
      const height = lines.length === 2 ? 24 : 18;
      const candidates = [];
      for (let i = 1; i < edge.points.length; i += 1) {
        const a = edge.points[i - 1];
        const b = edge.points[i];
        const horizontal = Math.abs(a.y - b.y) < 0.01;
        const length = segmentLength(a, b);
        if (length < 24) continue;
        const offsets = horizontal
          ? [-height - 4, 4, -height - 14, 14]
          : [7, -width - 7, 17, -width - 17];
        const ratios = length < 80 ? [0.5] : [0.5, 0.3, 0.7];
        ratios.forEach((ratio) => {
          offsets.forEach((offset) => {
            const box = labelBoxAt(
              { a, b }, width, height, horizontal ? "horizontal" : "vertical", offset, ratio,
            );
            let score = Math.abs(ratio - 0.5) * 30;
            if (box.x < 6 || box.y < 6 || box.x + box.w > VIEW.width - 6 || box.y + box.h > VIEW.height - 6) {
              score += 100000;
            }
            nodeBoxes.forEach((nodeBox) => {
              if (boxHitsBox(box, nodeBox, 4)) score += 90000;
            });
            laneBoxes.forEach((laneBox) => {
              if (boxHitsBox(box, laneBox, 3)) score += 50000;
            });
            placed.forEach((other) => {
              if (boxHitsBox(box, other, 3)) score += 80000;
            });
            score -= length * 0.02;
            score += (edge.points.length - i) * 4;
            score += Math.abs(offset) * 0.2;
            candidates.push({ box, score });
          });
        });
      }
      candidates.sort((a, b) => a.score - b.score);
      let chosen = candidates[0] || {
        box: { x: 12, y: 12, w: width, h: height }, score: Infinity,
      };
      chosen.box.x = clamp(chosen.box.x, 6, VIEW.width - 6 - chosen.box.w);
      chosen.box.y = clamp(chosen.box.y, 6, VIEW.height - 6 - chosen.box.h);
      edge.labelBox = chosen.box;
      edge.labelLines = lines.map((line, index) => ({
        ...line,
        x: chosen.box.x + chosen.box.w / 2,
        y: chosen.box.y + (lines.length === 2 ? 10 + index * 9 : 13),
      }));
      placed.push(chosen.box);
    });
  }

  function buildLayout(spec, state) {
    const pattern = spec.pattern || "sequential";
    const nodes = buildNodes(spec, state, pattern);
    const lanes = buildLanes();
    const nodeMap = new Map(nodes.map((node) => [node.id, node]));
    const laneTitles = lanes.map((lane) => lane.titleBox);
    const routed = [];
    (spec.edges || []).forEach((edge) => {
      const fromNode = nodeMap.get(edge.from);
      const toNode = nodeMap.get(edge.to);
      if (!fromNode || !toNode) return;
      const route = routeEdge(edge, fromNode, toNode, nodeMap, laneTitles, routed);
      const activity = (state && state.edges && state.edges[edge.from + "->" + edge.to]) || {};
      route.active = Boolean(activity.active);
      route.bytes = activity.bytes || "";
      route.revision = edge.label === "revision";
      routed.push(route);
    });
    placeEdgeLabels(routed, nodes, lanes);
    return {
      width: VIEW.width, height: VIEW.height, pattern, lanes, nodes, edges: routed,
    };
  }

  function layoutSignature(spec, state, options) {
    return JSON.stringify({
      pattern: spec.pattern || "sequential",
      nodes: spec.nodes || [],
      edges: (spec.edges || []).map((edge) => [edge.from, edge.to, edge.label || ""]),
      stateNodes: (state && state.nodes) || {},
      stateEdges: (state && state.edges) || {},
      audit: Boolean(spec.blackboardAuditOnly),
      compact: Boolean(options && options.compact),
    });
  }

  function roundedPath(points) {
    if (!points.length) return "";
    if (points.length === 1) return "M" + points[0].x + "," + points[0].y;
    let d = "M" + points[0].x + "," + points[0].y;
    for (let i = 1; i < points.length - 1; i += 1) {
      const prev = points[i - 1];
      const cur = points[i];
      const next = points[i + 1];
      const inLen = segmentLength(prev, cur);
      const outLen = segmentLength(cur, next);
      const radius = Math.min(7, inLen / 2, outLen / 2);
      if (radius < 1) {
        d += " L" + cur.x + "," + cur.y;
        continue;
      }
      const inDx = Math.sign(cur.x - prev.x);
      const inDy = Math.sign(cur.y - prev.y);
      const outDx = Math.sign(next.x - cur.x);
      const outDy = Math.sign(next.y - cur.y);
      const before = { x: cur.x - inDx * radius, y: cur.y - inDy * radius };
      const after = { x: cur.x + outDx * radius, y: cur.y + outDy * radius };
      d += " L" + before.x + "," + before.y + " Q" + cur.x + "," + cur.y + " " + after.x + "," + after.y;
    }
    const last = points[points.length - 1];
    d += " L" + last.x + "," + last.y;
    return d;
  }

  let renderCount = 0;

  function render(target, spec, state, options) {
    if (!target) return null;
    const opts = options || {};
    const signature = layoutSignature(spec, state, opts);
    const cached = target.__athenaDiagram;
    if (cached && cached.signature === signature && cached.svg.parentNode === target) {
      return cached.svg;
    }
    const layout = buildLayout(spec, state);
    const id = "athena-diagram-" + (++renderCount);
    const titleId = id + "-title";
    const descId = id + "-desc";
    const arrowId = id + "-arrow";
    const svg = el("svg", {
      viewBox: "0 0 " + layout.width + " " + layout.height,
      class: "diagram" + (opts.compact ? " compact" : ""),
      role: "img", "aria-labelledby": titleId + " " + descId,
      "data-pattern": layout.pattern,
    });
    svg.appendChild(el("title", { id: titleId }, "athena architecture — " + layout.pattern));
    svg.appendChild(el("desc", { id: descId },
      "Pattern " + layout.pattern
      + ". Google ADK plans and gathers evidence; CrewAI roles answer over A2A; "
      + "MCP tools provide RAG, web search and calculation; the blackboard holds shared state."));

    const defs = el("defs", {});
    const marker = el("marker", {
      id: arrowId, markerWidth: "8", markerHeight: "8", refX: "7", refY: "3", orient: "auto",
    });
    marker.appendChild(el("path", { d: "M0,0 L7,3 L0,6 z", class: "arrowhead" }));
    defs.appendChild(marker);
    svg.appendChild(defs);

    layout.lanes.forEach((lane) => {
      svg.appendChild(el("rect", {
        x: lane.x, y: lane.y, width: lane.w, height: lane.h, rx: 10,
        class: "lane lane-" + lane.id, "data-lane": lane.id,
      }));
      svg.appendChild(el("text", {
        x: lane.titleBox.x + 2, y: lane.titleBox.y + 12, class: "lane-label",
      }, lane.label));
    });

    layout.edges.forEach((edge) => {
      const group = el("g", {
        class: "edge-group" + (edge.active ? " active" : ""),
        "data-edge": edge.from + "->" + edge.to,
      });
      group.appendChild(el("path", {
        d: roundedPath(edge.points),
        class: "edge" + (edge.revision ? " revision" : ""),
        "marker-end": "url(#" + arrowId + ")",
      }));
      svg.appendChild(group);
    });

    layout.edges.forEach((edge) => {
      if (!edge.labelBox) return;
      const group = el("g", { class: "edge-label-group" });
      group.appendChild(el("rect", {
        x: edge.labelBox.x, y: edge.labelBox.y, width: edge.labelBox.w, height: edge.labelBox.h,
        rx: 4, class: "edge-label-bg",
      }));
      edge.labelLines.forEach((line) => {
        group.appendChild(el("text", {
          x: line.x, y: line.y, class: line.kind === "bytes" ? "edge-bytes" : "edge-label",
        }, line.text));
      });
      svg.appendChild(group);
    });

    layout.nodes.forEach((node) => {
      const info = node.info || {};
      const label = node.meta.label
        + (node.meta.badge ? ", " + node.meta.badge : "")
        + (info.state && info.state !== "idle" ? ", " + info.state : "")
        + (node.id === "blackboard" && node.audit ? ", audit only" : "");
      const group = el("g", {
        class: "node state-" + (info.state || "idle") + (node.audit ? " audit" : ""),
        "data-node": node.id, tabindex: "0", role: "group", "aria-label": label,
      });
      if (node.id === "blackboard") {
        group.appendChild(el("path", {
          d: `M${node.box.x},${node.box.y} h${node.box.w - 18} l18,18 v${node.box.h - 18} h-${node.box.w} z`,
          class: "board",
        }));
      } else {
        group.appendChild(el("rect", {
          x: node.box.x, y: node.box.y, width: node.box.w, height: node.box.h,
          rx: 8, class: "box",
        }));
      }
      node.labels.forEach((item) => {
        if (item.kind === "count") {
          group.appendChild(el("text", {
            x: item.box.x + item.box.w, y: item.box.y + 9,
            class: "node-count", "text-anchor": "end",
          }, item.text));
        } else {
          const className = item.kind === "badge" ? "node-badge " + node.meta.badge.toLowerCase()
            : item.kind === "title" ? "node-label" : "node-meta";
          group.appendChild(el("text", {
            x: item.box.x, y: item.box.y + (item.kind === "title" ? 12 : 10), class: className,
          }, item.text));
        }
      });
      svg.appendChild(group);
    });

    target.replaceChildren(svg);
    target.__athenaDiagram = { signature, svg };
    return svg;
  }

  const api = {
    render, buildLayout, layoutSignature, LANES, LABELS, NODE_META, NODE_POS: BASE_POS, VIEW,
  };
  root.AthenaDiagram = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
}(typeof globalThis !== "undefined" ? globalThis : this));
