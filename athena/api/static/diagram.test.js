/* Layout contracts for every research pattern. Run with `node diagram.test.js`. */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const logic = require("./logic.js");
const diagram = require("./diagram.js");

const EPS = 0.8;

function emptyState() {
  return { nodes: {}, edges: {} };
}

function layoutFor(pattern, state = emptyState()) {
  return diagram.buildLayout(logic.diagramFor(pattern), state);
}

function overlaps(a, b, pad = 0) {
  return a.x < b.x + b.w + pad && a.x + a.w > b.x - pad
    && a.y < b.y + b.h + pad && a.y + a.h > b.y - pad;
}

function inside(inner, outer, pad = 0.01) {
  return inner.x >= outer.x - pad && inner.y >= outer.y - pad
    && inner.x + inner.w <= outer.x + outer.w + pad
    && inner.y + inner.h <= outer.y + outer.h + pad;
}

function onBoundary(point, box) {
  const onVertical = Math.abs(point.x - box.x) < EPS || Math.abs(point.x - (box.x + box.w)) < EPS;
  const onHorizontal = Math.abs(point.y - box.y) < EPS || Math.abs(point.y - (box.y + box.h)) < EPS;
  return ((onVertical) && point.y >= box.y - EPS && point.y <= box.y + box.h + EPS)
    || ((onHorizontal) && point.x >= box.x - EPS && point.x <= box.x + box.w + EPS);
}

function hitsBoxInterior(a, b, box) {
  const x1 = Math.min(a.x, b.x);
  const x2 = Math.max(a.x, b.x);
  const y1 = Math.min(a.y, b.y);
  const y2 = Math.max(a.y, b.y);
  if (Math.abs(a.y - b.y) < 0.01) {
    return a.y > box.y + 2 && a.y < box.y + box.h - 2
      && x2 > box.x + 2 && x1 < box.x + box.w - 2;
  }
  return a.x > box.x + 2 && a.x < box.x + box.w - 2
    && y2 > box.y + 2 && y1 < box.y + box.h - 2;
}

function axisAligned(points) {
  return points.every((point, index) => {
    if (index === 0) return true;
    const prev = points[index - 1];
    return Math.abs(point.x - prev.x) < 0.01 || Math.abs(point.y - prev.y) < 0.01;
  });
}

function assertLayout(pattern, state = emptyState()) {
  const spec = logic.diagramFor(pattern);
  const layout = layoutFor(pattern, state);
  const lanes = new Map(layout.lanes.map((lane) => [lane.id, lane]));
  const nodes = new Map(layout.nodes.map((node) => [node.id, node]));

  assert.equal(layout.pattern, spec.pattern);
  assert.equal(layout.nodes.length, logic.NODES.length);
  assert.equal(layout.edges.length, spec.edges.length);
  assert.equal(layout.lanes.length, 4);
  assert.ok(layout.width >= 1000 && layout.height >= 500);

  layout.lanes.forEach((lane) => {
    assert.ok(inside(lane.titleBox, lane), `${pattern}: ${lane.id} title leaves its lane`);
    layout.nodes.forEach((node) => {
      if (node.lane === lane.id) assert.ok(inside(node.box, lane), `${pattern}: ${node.id} leaves ${lane.id}`);
      assert.ok(!overlaps(lane.titleBox, node.box), `${pattern}: ${lane.id} title overlaps ${node.id}`);
    });
  });

  for (let i = 0; i < layout.nodes.length; i += 1) {
    const node = layout.nodes[i];
    assert.ok(lanes.has(node.lane), `${pattern}: ${node.id} has no lane`);
    assert.ok(node.box.w >= 110 && node.box.h >= 44, `${pattern}: ${node.id} is too small`);
    node.labels.forEach((label) => {
      assert.ok(inside(label.box, node.box), `${pattern}: ${node.id} ${label.kind} label leaves its node`);
      assert.ok(label.text, `${pattern}: ${node.id} has an empty ${label.kind} label`);
    });
    for (let j = i + 1; j < layout.nodes.length; j += 1) {
      assert.ok(!overlaps(node.box, layout.nodes[j].box), `${pattern}: ${node.id} overlaps ${layout.nodes[j].id}`);
    }
    for (let j = 0; j < node.labels.length; j += 1) {
      for (let k = j + 1; k < node.labels.length; k += 1) {
        assert.ok(!overlaps(node.labels[j].box, node.labels[k].box),
          `${pattern}: ${node.id} labels overlap`);
      }
    }
  }

  layout.edges.forEach((edge) => {
    const from = nodes.get(edge.from);
    const to = nodes.get(edge.to);
    assert.ok(from && to, `${pattern}: ${edge.from}->${edge.to} is missing a node`);
    assert.ok(edge.points.length >= 2, `${pattern}: ${edge.from}->${edge.to} has no path`);
    assert.ok(axisAligned(edge.points), `${pattern}: ${edge.from}->${edge.to} is not orthogonal`);
    assert.ok(onBoundary(edge.start, from.box), `${pattern}: ${edge.from}->${edge.to} start is off-box`);
    assert.ok(onBoundary(edge.end, to.box), `${pattern}: ${edge.from}->${edge.to} end is off-box`);
    for (let i = 1; i < edge.points.length; i += 1) {
      const a = edge.points[i - 1];
      const b = edge.points[i];
      layout.nodes.forEach((node) => {
        if (node.id === edge.from || node.id === edge.to) return;
        assert.ok(!hitsBoxInterior(a, b, node.box), `${pattern}: ${edge.from}->${edge.to} crosses ${node.id}`);
      });
      layout.lanes.forEach((lane) => {
        assert.ok(!hitsBoxInterior(a, b, lane.titleBox), `${pattern}: ${edge.from}->${edge.to} crosses ${lane.id} title`);
      });
    }
    if (edge.labelBox) {
      assert.ok(inside(edge.labelBox, { x: 0, y: 0, w: layout.width, h: layout.height }),
        `${pattern}: ${edge.from}->${edge.to} label leaves the SVG`);
      layout.nodes.forEach((node) => {
        assert.ok(!overlaps(edge.labelBox, node.box, 3),
          `${pattern}: ${edge.from}->${edge.to} label overlaps ${node.id}`);
      });
      layout.lanes.forEach((lane) => {
        assert.ok(!overlaps(edge.labelBox, lane.titleBox, 3),
          `${pattern}: ${edge.from}->${edge.to} label overlaps ${lane.id} title`);
      });
      edge.labelLines.forEach((line) => {
        assert.ok(line.x >= edge.labelBox.x && line.x <= edge.labelBox.x + edge.labelBox.w);
        assert.ok(line.y >= edge.labelBox.y && line.y <= edge.labelBox.y + edge.labelBox.h);
      });
    }
  });

  for (let i = 0; i < layout.edges.length; i += 1) {
    const a = layout.edges[i];
    for (let j = i + 1; j < layout.edges.length; j += 1) {
      const b = layout.edges[j];
      if (a.labelBox && b.labelBox) {
        assert.ok(!overlaps(a.labelBox, b.labelBox, 3),
          `${pattern}: edge labels overlap (${a.from}->${a.to}, ${b.from}->${b.to})`);
      }
    }
  }

  return layout;
}

test("every pattern keeps all nodes inside its lane without collisions", () => {
  for (const pattern of logic.PATTERNS) assertLayout(pattern);
});

test("every pattern has a distinct, meaningful topology", () => {
  const layouts = new Map(logic.PATTERNS.map((pattern) => [pattern, layoutFor(pattern)]));
  const rg = (pattern) => layouts.get(pattern).nodes.find((node) => node.id === "rag_researcher").box;
  assert.notDeepEqual(rg("p2p"), rg("sequential"), "p2p must move RAG into the A2A chain");
  assert.deepEqual(
    layouts.get("parallel").edges.filter((edge) => edge.from === "planner").map((edge) => edge.to).sort(),
    ["calc_researcher", "rag_researcher", "web_researcher"],
  );
  assert.ok(layouts.get("hierarchical").edges.some((edge) => edge.label === "revision"));
  assert.deepEqual(
    layouts.get("p2p").edges.map((edge) => `${edge.from}->${edge.to}`),
    ["user->supervisor", "supervisor->planner", "planner->rag_researcher",
     "rag_researcher->analyst", "analyst->writer", "writer->critic",
     "rag_researcher->rag_tool", "analyst->blackboard"],
  );
});

test("live state and metered A2A labels remain inside the layout", () => {
  const events = [
    { kind: "stage_start", agent: "planner" },
    { kind: "stage_end", agent: "planner", duration_ms: 1200, ok: true },
    { kind: "tool_call", agent: "rag_researcher" },
    { kind: "a2a_call", agent: "writer", extra: { request_bytes: 156, response_bytes: 90 } },
    { kind: "a2a_call", agent: "analyst", extra: { request_bytes: 128, response_bytes: 64 } },
    { kind: "a2a_call", agent: "critic", extra: { request_bytes: 149, response_bytes: 83 } },
    { kind: "a2a_call", agent: "analyst", extra: { request_bytes: 128, response_bytes: 64 } },
    { kind: "blackboard_write", agent: "analyst" },
  ];
  for (const pattern of logic.PATTERNS) {
    const state = logic.diagramState(events, pattern);
    const layout = assertLayout(pattern, state);
    const planner = layout.nodes.find((node) => node.id === "planner");
    assert.equal(planner.info.state, "done");
    assert.equal(planner.info.label, "1.2 s");
    const rag = layout.nodes.find((node) => node.id === "rag_tool");
    assert.equal(rag.info.calls, 1);
    assert.ok(layout.edges.some((edge) => edge.active), `${pattern}: no edge became active`);
    assert.ok(layout.edges.some((edge) => edge.bytes && edge.labelBox && edge.labelLines.length === 2),
      `${pattern}: byte labels did not render`);
  }
});

test("layouts are deterministic and change when live state changes", () => {
  const first = layoutFor("parallel");
  const second = layoutFor("parallel");
  assert.deepEqual(first, second);
  const state = logic.diagramState([
    { kind: "a2a_call", agent: "writer", extra: { request_bytes: 10, response_bytes: 5 } },
  ], "parallel");
  const live = layoutFor("parallel", state);
  assert.notDeepEqual(first.edges, live.edges);
  assert.notEqual(
    diagram.layoutSignature(logic.diagramFor("parallel"), emptyState()),
    diagram.layoutSignature(logic.diagramFor("parallel"), state),
  );
});

test("all recorded replay bundles render without collisions", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const replayDir = path.join(__dirname, "../../../data/demo/replay");
  const names = ["sequential", "parallel", "hierarchical", "p2p"];
  let checked = 0;
  names.forEach((name) => {
    const file = path.join(replayDir, name + ".json");
    if (!fs.existsSync(file)) return;
    const bundle = JSON.parse(fs.readFileSync(file, "utf8"));
    const pattern = (bundle.summary && bundle.summary.pattern) || name;
    const state = logic.diagramState(bundle.events || [], pattern);
    assertLayout(pattern, state);
    checked += 1;
  });
  assert.ok(checked >= 1, "no recorded replay bundles were available to check");
});

test("the renderer keeps one SVG per host and exposes its viewBox", () => {
  const source = require("node:fs").readFileSync(require("node:path").join(__dirname, "diagram.js"), "utf8");
  assert.match(source, /viewBox: "0 0 " \+ layout\.width/);
  assert.match(source, /target\.__athenaDiagram/);
  assert.match(source, /marker-end/);
  assert.match(source, /data-pattern/);
});
