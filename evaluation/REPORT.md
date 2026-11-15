# Athena evaluation: AMD hierarchical-30

The latest completed evaluation ran on 14–15 November 2026 using
`openai/DeepSeek-V4-Flash` through AMD for both ADK and CrewAI.

The measurements were captured at commit `3584d5b` with the documented local
launcher edit. The later terminal-summary and history-list fixes make the saved
runs visible; the research pipeline was not rerun for this publication update.

## Results

- 30 scheduled run keys, 24 distinct prompts, 12 categories and 6 repeats.
- 30/30 technically completed, with 0 retries, timeouts or infrastructure errors
  in the main batch.
- 23/30 deterministic-rubric successes (76.7%).
- 17/30 critic approvals and 13 rejections. Approval is distinct from rubric success.
- Approximately 7.04 hours total; median run latency 770.6 seconds.
- 1,469,026 input-plus-output tokens. Reasoning tokens are tracked separately.
- All cost estimates are unknown; no AMD price was invented.

The batch is hierarchical with Blackboard context sharing only. It does not
establish an architecture comparison or fulfill a 40-run design. Security
coverage includes only two evaluable injection prompts and two toxic-persona
prompts; it is too small for a general security guarantee. One aborted preflight
pilot is preserved separately and excluded from the 30 main-batch records.

## Files to present

1. [BATCH_REPORT.md](results/20261114T174056Z_amd_hierarchical_30/BATCH_REPORT.md)
   contains the methodology, full results, provenance and limitations.
2. [ALL_RUN_OUTPUTS.md](results/20261114T174056Z_amd_hierarchical_30/ALL_RUN_OUTPUTS.md)
   contains every prompt, recorded answer, key point, citation and evaluation label.
3. [runs.csv](results/20261114T174056Z_amd_hierarchical_30/runs.csv) contains per-run metrics.
4. [Charts](results/20261114T174056Z_amd_hierarchical_30/charts/) include latency and quality
   boxplots with their stated denominators.
5. [RUN_AUDIT.json](results/20261114T174056Z_amd_hierarchical_30/RUN_AUDIT.json) and
   [raw.jsonl](results/20261114T174056Z_amd_hierarchical_30/raw.jsonl) preserve the audit trail.

Use the batch-specific report above as authoritative. The generated reports
within the batch retain generic interpretation caveats documented there. Earlier
batches remain separate and have not been pooled into these results.

## View the recorded outputs in the UI

Start `python scripts/demo.py --replay-only`, open Research, then Recordings.
The `amd-eval-01-…` through `amd-eval-30-…` entries replay the saved evaluation
outputs without inference. Rejected answers remain labelled as rejected.
Local Research history also works when the original event and Blackboard
databases are available; a fresh clone uses the committed recordings instead.

API keys, local databases, transient locks and execution logs are not published.
The original batch manifest includes some execution-log hashes for local auditing;
those ignored logs remain local. The publication manifest lists the files added
for sharing after the benchmark, without rewriting the original manifest.
