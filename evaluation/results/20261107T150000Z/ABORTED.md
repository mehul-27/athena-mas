# Aborted retry batch (2026-11-07T15:00:00Z)

This directory holds partial records from a retry batch that was **started and
then stopped by the operator**, not a completed experiment.

Sequence:

1. The 36-run batch in `../20261107T133000Z/` failed 35 runs on the provider's
   daily token quota.
2. A single provider probe returned HTTP 200, which suggested the quota might
   have reset, so a retry batch was launched against this directory.
3. The very next runs failed again with the same message
   (`tokens per day (TPD): Limit 200000, Used 199534`), so the batch was killed
   rather than left to burn attempts against a quota that was not available.
4. The runner had already flushed the records it wrote before being stopped.

**These records are not a result.** Every run here failed before any research
was attempted, because no model call could be made. They are kept rather than
deleted so the provenance of the day's work is complete, and any aggregation
over `evaluation/results/` will show them as `failed` rather than as evidence
about the pipeline.

No result was fabricated, no rate limit was disabled, and the model was not
changed.
