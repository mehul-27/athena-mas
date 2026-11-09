# Provider retry boundaries

These notes describe the installed SDK behavior inspected on 2026-11-09:
LiteLLM 1.105.3, OpenAI 2.54.0, CrewAI 1.15.28, and Instructor 1.15.4.

## SDK behavior

- In this LiteLLM version, direct `acompletion(num_retries=3)` uses
  `stop_after_attempt(3)`: three total attempts, including the first call.
  LiteLLM's wrapper retries API errors broadly; do not assume that `num_retries`
  alone excludes invalid requests or authentication failures. `RetryPolicy`
  selects counts for known exception classes, but it does not replace the
  underlying transport's retry rules.
- For adapters that use the installed OpenAI client, `max_retries=2` allows
  three total attempts. The OpenAI client retries connection/timeouts and
  408, 409, 429, and 5xx responses; ordinary 400, 401, and 403 responses are
  not retried unless the server explicitly requests it. Other LiteLLM provider
  adapters may behave differently.
- CrewAI's structured `output_pydantic` path uses Instructor's direct
  `litellm.completion` client. CrewAI's `additional_params` are not forwarded
  to that call, so retry settings attached only to `additional_params` do not
  configure structured-output parsing retries.

## Athena boundaries

- Chat may retry once without `stream_options` only after an explicit
  pre-stream 400/422 rejection of that field. Other provider errors propagate;
  errors after stream iteration begins do not trigger this fallback.
- The writer's outer correction retries once for invalid `FinalReport`
  structure. Provider failures remain provider failures and are not converted
  into validation errors by the correction path. A truncated-report retry keeps
  the original if replacement output is invalid and merges usage from successful
  tuple-shaped results.
- Evaluation retries only explicitly classified transient infrastructure
  failures when `--retry-failed` is enabled. Authentication, quota/rate-limit,
  local limiter, validation, and completed critic results are not resampled.
  `--max-attempts-per-run` and the batch's `max_runs` cap bound attempts.
- The evaluation runner binds an executor's signature before invoking it once;
  it does not use a runtime `TypeError` to probe alternate call shapes.
- Each model retry reserves its full request estimate and output cap before
  dispatch. If output acquisition fails before dispatch, the unsent input
  reservation is released; failures after dispatch retain conservative usage
  estimates.

A previously recorded 503 between successful responses is a historical
observation, not a current provider health result. The fake completion test in
`tests/test_chat_provider_retry.py` exercises the chat fallback, not provider
health. Provider-specific behavior must be revalidated before a fresh
benchmark. No provider or model choice is implied here.
