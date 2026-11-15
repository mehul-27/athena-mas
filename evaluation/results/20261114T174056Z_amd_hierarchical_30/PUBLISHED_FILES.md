# Publication notes

This publication adds the completed AMD evaluation, all 30 prompt/answer outputs,
CSV metrics, charts, runtime configuration, per-run summary JSON, audit metadata
and 30 portable UI recordings. It does not rerun research or change benchmark
answers, scores, verdicts or raw attempt records.

The benchmark was recorded at commit 3584d5b with its documented local launcher
edit. Later source fixes persist terminal summaries for UI history and select
the most recent 50 history entries. The local event database was backfilled with
the already saved CLI summaries; each appended summary records its import
source and time. UI_RECORDINGS.json maps the original runs to the shared files.

The original MANIFEST.sha256 is retained unchanged. It includes hashes of local
execution logs and the transient .evaluation.lock, which are deliberately NOT
published. They are not required to read the reports or replay the shared runs.
The original SQLite databases, corpus/index/cache, provider settings/vault and
credentials also remain local. The pre-existing scripts/dev_up.py edit and
evaluation/quarantine directory are not part of this publication commit.

PUBLICATION.sha256 lists the published evaluation files and UI recordings. It
excludes itself and does not replace the benchmark's original manifest. Hashes
are over the committed bytes; checkout newline conversion can change text-file
bytes locally without changing their Git content.

Verification: 45 focused lifecycle/history/streaming tests and 3 recording/schema
tests passed offline, with external network connections denied. Direct checks
confirmed 30/30 local history entries, 30/30 recorded final snapshots, 30 valid
portable recording schemas and PNG heatmaps, and 17 approvals / 13 rejections.
No API keys were printed or published and no model calls were made for export.
