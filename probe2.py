from athena.agents.routing import relevance, named_entities, subject_terms, classify
from athena.agents.sufficiency import assess, _substantive, MIN_FETCHED_CHARS
from athena.blackboard import EvidenceItem

Q2 = "What is the latest stable Python release version?"
content = "Python 3.13 is the latest stable release of the Python language. " * 4
print("route", classify(Q2))
print("names", named_entities(Q2), "terms", subject_terms(Q2))
r = relevance(content, Q2)
print("rel", r)
print("substantive", _substantive(content), len(content), MIN_FETCHED_CHARS)
item = EvidenceItem(id="fetch-p", source_type="web", source_ref="https://docs.python.org/3/", content=content, retrieved_by="web")
a = assess(Q2, [item], enabled_tools=["web_search", "fetch_page"])
print("a", a)
