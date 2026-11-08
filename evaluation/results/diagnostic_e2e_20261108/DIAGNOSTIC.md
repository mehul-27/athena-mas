# Diagnostic run — EXCLUDED from the 30-run benchmark

One end-to-end validation run, executed 2026-11-08 to verify the pipeline before
the benchmark. **It is not one of the 30 planned runs and must never be counted
as such.**

- run key: `gf-01__hierarchical__r0`
- pattern: `hierarchical`, context sharing: `blackboard`
- account: `Groq1` (`cbd1a14650`), the ADK half and the CrewAI half both billed
  to ORG1 because the server key is fixed there
- outcome: `terminal_state=completed`, `outcome=approved`, critic approved
- evidence: 5 items · citations: 3, all resolving · rubric 1.0
- tokens: 28,002 in + 2,093 out = 30,095 across 9 model calls

## Why it is kept rather than deleted

It is the only record that proves the whole chain — MCP tool startup →
researcher tools → Blackboard evidence → citations → analyst → writer → critic
approval — worked on the final configuration. Deleting it would remove that
evidence; keeping it unlabelled would let it be mistaken for benchmark data.
Hence this marker.

## Effect on reporting

Any analysis that globs `evaluation/results/*` will include this batch in pooled
totals. Per-batch tables show it separately. When reporting the benchmark, filter
to `evaluation/results/20261108_hierarchical_30` — the benchmark batch — and do
not pool this directory into it.

`evaluation.analyze --results evaluation/results/20261108_hierarchical_30` and
`evaluation.report --results <benchmark dir>` both operate per directory, so this
is a matter of pointing them at the right one.
