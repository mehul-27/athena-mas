from athena.agents.routing import contrasting_accounts
from athena.agents.sufficiency import assess
from athena.blackboard import EvidenceItem


def E(i, t, r, c):
    return EvidenceItem(id=i, source_type=t, source_ref=r, content=c, retrieved_by="web_researcher")


Q = (
    "What propellant does SpaceX's Draco thruster use? Some sources describe it as a "
    "nitrogen cold-gas thruster, while others describe a hypergolic engine."
)
print("accounts:", contrasting_accounts(Q))
cold = E(
    "fetch-cold", "web", "https://example.com/cold",
    "The nitrogen cold gas thruster stores pressurized nitrogen and expands it through "
    "a nozzle to produce thrust without combustion. It is used for attitude control. " * 2,
)
a = assess(Q, [cold], enabled_tools=["web_search", "fetch_page"])
print("subject:", a.subject, "| subs:", a.requested_subjects, "| subtopics:", a.requested_subtopics)
print("quality", a.evidence_quality, "ceiling", a.confidence_ceiling, "as_asked", a.answer_as_asked)
print("subject refs", a.subject_source_refs, "bg refs", a.background_source_refs)
print("missing aspects", a.missing_aspects, "missing req", a.missing_requirements)
for c in a.conflicts:
    print("conflict", c)
print("limits", a.limitations)
print("issues", a.issues)

hot = E(
    "fetch-hot", "web", "https://example.com/hot",
    "The Draco engine is a hypergolic engine that burns monomethylhydrazine and nitrogen "
    "tetroxide. The SpaceX Draco thruster uses hydrazine based propellant for spacecraft control. " * 2,
)
b = assess(Q, [cold, hot], enabled_tools=["web_search", "fetch_page"])
print("=== both sides")
print("quality", b.evidence_quality, "ceiling", b.confidence_ceiling, "as_asked", b.answer_as_asked)
print("missing aspects", b.missing_aspects)
for c in b.conflicts:
    print("conflict", c)
print("limits", b.limitations)

print("=== latest python")
Q2 = "What is the latest stable Python release version?"
shallow = E("fetch-p", "web", "https://docs.python.org/3/", "Python 3.13 is the latest stable release of the Python language. " * 4)
c1 = assess(Q2, [shallow], enabled_tools=["web_search", "fetch_page"])
print("missing req", c1.missing_requirements, "ceiling", c1.confidence_ceiling, "as_asked", c1.answer_as_asked)
print("issues", c1.issues)
deep = E("fetch-p2", "web", "https://docs.python.org/3/", "Python 3.13.7 is the latest stable release of the Python language, released in 2025. " * 4)
c2 = assess(Q2, [deep], enabled_tools=["web_search", "fetch_page"])
print("missing req", c2.missing_requirements, "ceiling", c2.confidence_ceiling, "as_asked", c2.answer_as_asked)

print("=== hall effect")
Q3 = "How does a Hall-effect thruster work, and why is it efficient?"
overview = E(
    "fetch-o", "web", "https://www.nasa.gov/glenn/x/",
    "NASA explores space and develops electric propulsion for missions. Its most capable "
    "in-space system produces about one pound of thrust. The agency studies many spacecraft technologies. " * 2,
)
d = assess(Q3, [overview], enabled_tools=["web_search", "fetch_page"])
print("subject", d.subject, "| subs", d.requested_subjects, "| quality", d.evidence_quality,
      "| specific", d.subject_specific, "| bg", d.background, "| ceiling", d.confidence_ceiling)
print("aspects", d.requested_subtopics, d.supported_aspects, d.missing_aspects)
print("refs", d.subject_source_refs, d.background_source_refs)
