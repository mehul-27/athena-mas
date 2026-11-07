import asyncio
import json
from types import SimpleNamespace

from athena.agents.confidence import compute_confidence, finalize_confidence
from athena.agents.sufficiency import assess
from athena.blackboard import Blackboard, Citation, Critique, EvidenceItem, FinalReport, SQLiteStore, Task

STORE = SQLiteStore(".test-tmp/probe-bb.db")


def E(i, t, r, c):
    return EvidenceItem(id=i, source_type=t, source_ref=r, content=c, retrieved_by="web_researcher")


board = Blackboard.create(Task(question="What is the latest stable Python release version?"), store=STORE)
shallow = E("fetch-p", "web", "https://docs.python.org/3/", "Python 3.13 is the latest stable release of the Python language. " * 4)
board.append("evidence", shallow, author="web_researcher")
report = FinalReport(
    answer="Python 3.13 is the latest stable release of the Python language.",
    key_points=["It is the first release with free threading."],
    citations=[Citation(source_type="web", source_ref=shallow.source_ref, quote_or_summary="q")],
    confidence=0.9,
    run_id=board.run_id,
)
board.write("final_report", report, author="writer")
a = assess("What is the latest stable Python release version?", [shallow], enabled_tools=["web_search", "fetch_page"])
d = compute_confidence(report, a, None, [shallow], "What is the latest stable Python release version?")
print("decision", d.confidence, d.ceiling, d.changed, d.notes)
dec = finalize_confidence(board, board.run_id)
print("finalize", dec.confidence, "report now", board.read("final_report")[0].confidence)
version_before = board.read("final_report")[1]
dec2 = finalize_confidence(board, board.run_id)
print("second", dec2.changed, "version stable", version_before == board.read("final_report")[1])

print("=== with rejected critique")
board.write("critique", Critique(issues=["unsupported"], severity="high", approved=False), author="critic")
dec3 = finalize_confidence(board, board.run_id)
print("finalize2", dec3.confidence, dec3.ceiling, dec3.notes)

print("=== recovery plan")
from athena.agents.sufficiency import assess as A2
from athena.patterns.recovery import plan_recovery

Q = "What propellant does SpaceX's Draco thruster use? Some sources describe it as a nitrogen cold-gas thruster, while others describe a hypergolic engine."
cold = E("fetch-cold", "web", "https://example.com/cold", "The nitrogen cold gas thruster stores pressurized nitrogen. " * 4)
a2 = A2(Q, [cold], enabled_tools=["web_search", "fetch_page"])
tool_report = {"calls": [
    {"tool": "web_search", "ok": False, "summary": "web_search: {'query': 'SpaceX Draco propellant fuel oxidizer', 'max_results': 3}"},
    {"tool": "fetch_page", "ok": False, "summary": "fetch_page: x", "requested_url": "https://example.com/bad"},
]}
p = plan_recovery(Q, a2, tool_report=tool_report, available_roles=("web_researcher",))
print("needed", p.needed, "role", p.role, "queries", p.queries, "skipped", p.skipped_queries)
print("reason", p.reason_text())
p2 = plan_recovery(Q, a2, tool_report=tool_report, available_roles=("web_researcher",), rounds_used=1)
print("rounds skip", p2.needed, p2.skip_reason)
p3 = plan_recovery(Q, a2, tool_report=None, available_roles=())
print("no roles", p3.needed, p3.skip_reason)
ok = A2("What changed in Python 3.13?", [
    E("fetch-py", "web", "https://docs.python.org/3.13/whatsnew/3.13.html", "Python 3.13 release notes explain the changed behavior. " * 4)
], enabled_tools=["web_search", "fetch_page"])
p4 = plan_recovery("What changed in Python 3.13?", ok, tool_report=None, available_roles=("web_researcher",))
print("sufficient", p4.needed, p4.skip_reason)

print("=== lifecycle")
from athena.agents.lifecycle import close_toolsets, iter_toolsets, run_bounded, StageTimeout


class FakeToolset:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


class Worker:
    def __init__(self, name, delay=0, boom=False, toolset=None):
        self.name = name
        self.delay = delay
        self.boom = boom
        self.tools = [toolset] if toolset else []

    async def run_async(self, ctx):
        await asyncio.sleep(self.delay)
        if self.boom:
            raise RuntimeError("worker failed")
        yield "event"


async def main():
    ts = FakeToolset()
    print("iter", len(iter_toolsets(Worker("w", toolset=ts))))
    print("closed", await close_toolsets([Worker("w", toolset=ts)]), ts.closed)

    pool = [Worker("a", 0.5), Worker("b", boom=True)]
    try:
        await run_bounded([w.run_async(None) for w in pool], timeout=5, label="test")
    except RuntimeError as exc:
        print("raised", exc)
    print("pending tasks", len([t for t in asyncio.all_tasks() if t is not asyncio.current_task()]))

    try:
        await run_bounded([Worker("slow", 5).run_async(None)], timeout=0.05, label="pool")
    except StageTimeout as exc:
        print("timeout ok", str(exc))

    from athena.patterns.parallel import ConcurrentWorkers

    ts2 = FakeToolset()
    pool_agent = ConcurrentWorkers(
        name="pool",
        workers=[Worker("x", toolset=ts2), Worker("y", boom=True)],
    )
    try:
        [event async for event in pool_agent._run_async_impl(None)]
    except RuntimeError as exc:
        print("pool raised", exc)
    print("toolset closed after failure", ts2.closed)


asyncio.run(main())
