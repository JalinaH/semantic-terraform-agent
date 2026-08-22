# Semantic Terraform Failure Agent

Semantic Terraform Failure Agent is a local CLI that diagnoses a Terraform failure in
an arbitrary checked-out repository. It combines the Terraform diagnostic, relevant
source and Git changes, and—when useful—only the affected provider resource schemas.
The selected LLM provider produces a validated diagnosis and candidate patch; the agent checks that patch
inside an isolated copy and writes a stable JSON result for downstream tooling.

This repository is the reusable product. It has no dependency on a benchmark case ID,
known resource name, cloud provider, or the separate `terraform-failure-benchmarks`
implementation.

## Status and scope

Version `0.8.0` adds progressive deterministic context escalation on top of minimal
Terraform context and provider-schema slicing. Production `auto` mode begins without a
provider schema, verifies the first candidate, and retrieves a sliced schema only when a
cheap deterministic classifier identifies missing provider-semantic evidence. Formatting
failures use the existing minimal-context repair; environment and unsafe-patch failures
stop. Both attempts use the same provider and requested model, and the hard limit remains
two model calls. Critical HCL and schema definitions remain exact; there is no LLM-based
selection, lossy compression, model routing, or caching.

```text
repository + failure log
  -> safe input discovery
  -> exact normalized diagnostic
  -> relevant changed Terraform lines
  -> affected resource block(s)
  -> direct var/local/data/resource definitions (depth 1)
  -> deterministic context selection
  -> provider-neutral Gemini or OpenRouter structured diagnosis with minimal context
  -> isolated patch application + fmt/init/validate/plan
  -> deterministic stop / minimal repair / sliced-schema escalation decision
  -> at most one same-model second attempt + fresh verification
  -> result JSON + concise terminal and LLM usage summary
  -> optional GitHub Step Summary and idempotent pull-request comment
```

It intentionally does **not** apply a patch to the source checkout, run Terraform apply or
destroy, make more than two model calls, commit or push code, auto-merge, host a service,
persist results outside caller-controlled Actions artifacts, or implement an MCP server.

## Requirements and installation

- Python 3.11 or newer
- Git and Terraform on `PATH` for patch verification
- Terraform on `PATH` for schema-aware diagnosis
- `OPENROUTER_API_KEY` for OpenRouter, or `GEMINI_API_KEY` for direct Gemini

From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
export OPENROUTER_API_KEY='your-key'
```

Keys are read at request time. The CLI never prints or stores them.

## CLI usage

```bash
semantic-terraform-agent diagnose \
  --repo-path /path/to/repository \
  --terraform-dir infrastructure \
  --log-file /path/to/plan.stderr.log \
  --diff-file /path/to/change.patch \
  --failed-stage plan \
  --provider openrouter \
  --model '<provider>/<model>:free' \
  --context-mode auto \
  --verify-patch \
  --max-repair-attempts 1 \
  --output result.json
```

`--diff-file` is optional. Without it, the collector tries these comparisons in order
and records the first successful one in `repository.diff_comparison`:

1. `git diff HEAD~1 HEAD --`
2. `git diff HEAD --` (working tree versus `HEAD`)
3. `git diff --cached --` (index versus `HEAD`)

An empty diff is allowed and reported as a warning. The failure reference can still
identify a resource.

`--context-mode` accepts:

- `lightweight`: error, relevant Terraform source, and Git diff only.
- `schema-aware`: adds deterministic slices of matched resource schemas and provider metadata.
- `auto`: starts minimal and applies progressive deterministic escalation after verification.

The default Gemini model is `gemini-2.5-flash`. OpenRouter requires an explicit dynamic
model ID in `provider/model` form; the agent does not maintain a fixed model allowlist.
Patch verification is enabled by default. Use `--no-verify-patch` to record a
deliberately skipped verification in environments where local commands must not run.
`--max-repair-attempts` accepts only `0` or `1` in version 0.8.0 and defaults to `1`.
The value bounds the unified second opportunity: either minimal-context repair or schema
context escalation, never both. Use `0` to verify the initial patch without a second call.

`--failed-stage` optionally records the caller-known stage as `init`, `fmt`, `validate`,
`plan`, `apply`, or `unknown`. It overrides log inference and is included in the model
context and result document. This is metadata only; it never causes the agent to run
`terraform apply`.

## OpenRouter and provider architecture

The orchestration layer depends only on the `LLMProvider` protocol. Provider creation is
centralized, and OpenRouter-specific HTTP, retry, structured-output, error, and usage
handling lives in `reasoning/openrouter.py`; direct Gemini remains in
`reasoning/gemini.py`. Adding a future provider does not require putting provider HTTP
logic into diagnosis orchestration.

OpenRouter uses the official non-streaming `POST /api/v1/chat/completions` endpoint and
reads `OPENROUTER_API_KEY`. Optional app attribution is read from:

- `OPENROUTER_APP_URL`, sent as `HTTP-Referer`;
- `OPENROUTER_APP_NAME`, sent as `X-OpenRouter-Title`.

Neither attribution value is required. The API endpoint has an injectable base URL for
offline tests; normal CLI operation always uses the official default. API keys,
authorization headers, raw request bodies, and complete provider responses are never
placed in result telemetry.

OpenRouter model IDs remain dynamic because catalog availability changes. The CLI accepts
conservatively validated IDs such as `<provider>/<model>` and
`<provider>/<model>:free`, as well as `openrouter/free`. Control characters, whitespace,
malformed unqualified IDs, and IDs longer than 200 characters are rejected locally.
Tier policy and model allowlisting belong to the future dashboard, not the agent core.

For zero-cost development, either select a currently available fixed `:free` model or use
`openrouter/free`:

```bash
export OPENROUTER_API_KEY='your-key'
semantic-terraform-agent diagnose \
  --repo-path /path/to/repository \
  --terraform-dir infrastructure \
  --log-file /path/to/plan.stderr.log \
  --provider openrouter \
  --model 'openrouter/free' \
  --context-mode auto \
  --verify-patch \
  --max-repair-attempts 1 \
  --output result.json
```

`openrouter/free` chooses an available free model and may report a different underlying
model on each run. A fixed free model ID is preferable for reproducible benchmarking.
Free availability and rate limits can change, and not every free model follows structured
output equally well.

The OpenRouter adapter first requests strict `json_schema` structured output and requires
a route that supports the parameter. When OpenRouter reports that the selected model
cannot enforce structured output, the adapter makes one capability fallback request
without `response_format`, includes the complete diagnosis schema in a JSON-only prompt,
parses the returned JSON, and validates it with the same strict Pydantic model. Malformed,
missing-field, or extra-field responses are rejected; schema validation is never weakened.

OpenRouter transport retry is separate from Terraform patch repair. A transport request
may retry at most twice after `408`, `429`, `500`, `502`, `503`, `504`, timeout, or network
failure, with capped exponential backoff and bounded `Retry-After` handling. The agent
still permits at most one evidence-driven repair model invocation after Terraform
verification failure. Neither mechanism is an unbounded loop.

Provider failures are normalized to safe categories: `model_not_found`,
`model_unavailable`, `structured_output_unsupported`, `rate_limited`, `quota_exceeded`,
`authentication_failed`, `provider_unavailable`, `response_invalid`, `timeout`, and
`network_error`. CLI error documents expose the category as `error_code` without a raw
provider stack trace. OpenRouter is never silently replaced by Gemini after a failure.

## LLM usage and context telemetry

Every successful diagnosis and repair invocation is recorded in `llm_calls` with:

- provider, requested model, provider-reported model, and upstream provider when reported;
- input, cached-input, output, reasoning, and total tokens when reported;
- provider-reported cost, latency, cache status, finish reason, and `call_type`;
- deterministic total, system, and user prompt character counts measured before the call.

Run-level `llm_usage` sums known numeric values across diagnosis and repair. `call_count`
and aggregate latency cover all recorded calls. `token_counts_complete` is false when a
call omitted a core input/output/total token count. `cost_complete` is false when any call
omitted cost; in that case `cost_usd` is either the explicitly incomplete sum of reported
costs or `null` when no cost was reported. An explicit free-model cost of zero remains
`0.0` and renders as `$0.000000`; unknown cost remains `null` and renders as
`not reported`. The agent uses provider-reported cost and does not embed a pricing table.

`token_usage` remains for backward compatibility and is derived from the aggregate
input/output/total counts. `context_telemetry` retains the original fields and adds exact
pre-call section character counts, selected-context and rendered-user-prompt character
counts, block/change/reference counts, schema inclusion, and separate diagnosis/repair
call records. It does not estimate per-section tokens.

`context_manifest` records only identities and counts: included files/resources/symbols,
referenced/resolved/unresolved symbols, changed-line count, truncation reasons, and
whether candidate selection was ambiguous. It does not duplicate source. The separate
`context_optimization` object defines available source as all Terraform source characters
discoverable in the selected working directory and compares that with exact selected HCL.
Its token-reduction ratio stays `null` unless a comparable provider-reported baseline is
available; character reduction is never presented as token reduction.
For schema-aware calls, the `provider_schema` section also records raw full-available and
selected-schema character counts separately from its rendered section characters.
`schema_slice_manifest` contains selected paths, per-path reasons, unmatched diagnostic
terms, description truncations, and dropped paths without schema definitions.
`schema_optimization` aggregates full/selected schema characters, paths, fallback state,
and a null token-reduction ratio. `timing.context_build_seconds` and
`timing.schema_slice_seconds` measure the two local deterministic stages. The CLI prints
Terraform-source and provider-schema character reductions, but makes no token-saving claim
without a comparison run.

An abbreviated result is:

```json
{
  "token_usage": {"input_tokens": 1842, "output_tokens": 218, "total_tokens": 2060},
  "llm_usage": {
    "call_count": 1,
    "input_tokens": 1842,
    "cached_input_tokens": 0,
    "output_tokens": 218,
    "reasoning_tokens": 0,
    "total_tokens": 2060,
    "cost_usd": 0.0,
    "latency_ms": 1821,
    "token_counts_complete": true,
    "cost_complete": true
  },
  "llm_calls": [
    {
      "provider": "openrouter",
      "requested_model": "openrouter/free",
      "reported_model": "<provider-reported-model>",
      "call_type": "diagnosis"
    }
  ],
  "context_telemetry": {
    "mode": "lightweight",
    "prompt_characters": 3100,
    "resource_schema_included": false,
    "git_diff_included": true,
    "source_file_count": 2,
    "source_block_count": 2,
    "changed_line_count": 2,
    "referenced_symbol_count": 1,
    "sections": {
      "terraform_error": {"characters": 280},
      "git_diff": {"characters": 310},
      "terraform_source": {"characters": 520},
      "supporting_context": {"characters": 120},
      "metadata": {"characters": 95},
      "provider_schema": {"characters": 0}
    }
  },
  "context_manifest": {
    "included_files": ["infrastructure/main.tf", "infrastructure/variables.tf"],
    "included_resources": ["aws_ebs_volume.example"],
    "included_symbols": ["var.volume_type"],
    "unresolved_symbols": [],
    "changed_lines": 2,
    "truncated_sections": []
  },
  "context_optimization": {
    "strategy": "deterministic_minimal_v1",
    "available_source_characters": 18200,
    "selected_source_characters": 3100,
    "characters_avoided": 15100,
    "reduction_ratio": 0.82967,
    "character_reduction_ratio": 0.82967,
    "input_token_reduction_ratio": null
  },
  "schema_slice_manifest": [],
  "schema_optimization": null
}
```

For the hosted product, the dashboard/worker owns `OPENROUTER_API_KEY`, applies the future
model policy, and invokes this non-interactive CLI with the selected model. Customer
repositories do not need OpenRouter or Gemini keys. CLI/self-hosted and reusable-workflow
users provide the selected provider key in their own execution environment. No dashboard
code is changed by this agent release.

## Repository discovery

`--repo-path` must name an existing directory. `--terraform-dir` must be relative to
that repository and resolve inside it. The collector identifies `.tf` and `.tf.json`
files loaded directly by that Terraform working directory, retains paths relative to
the repository root, and reads only bounded Terraform source inputs. Symlinks or diff
paths that escape the root are rejected. It does not scan unrelated file contents.

Diff `+++` paths are normalized and intersected with discovered Terraform files.
Changed line numbers from unified-diff hunks are retained so a change inside a resource
block is stronger evidence than a change elsewhere in its file. Prompt context excludes
non-Terraform changes and unrelated Terraform hunks. Oversized relevant hunks retain exact
changed lines plus bounded nearby context and are explicitly marked truncated.

## Failure parsing

The parser supports normal Terraform terminal diagnostics (including ANSI/box drawing
format) and newline-delimited JSON diagnostics. It extracts:

- primary error summary and detail;
- referenced `.tf`/`.tf.json` file and line, when present;
- resource address from Terraform's `with ...` diagnostic;
- inferred stage: `init`, `fmt`, `validate`, `plan`, `apply`, or `unknown`.

When the caller supplies `--failed-stage`, that explicit value replaces the inferred stage.

Unstructured or malformed logs produce a conservative fallback rather than an invented
resource. The entire caller-supplied log is preserved in `failure.original_log`, but the
normal v0.7 prompt sends the bounded normalized diagnostic rather than duplicating the raw
log. The original log remains in `failure.original_log` for backward compatibility and is
not copied into context telemetry or the context manifest.

## Resource detection

The resource parser finds generic `resource "TYPE" "NAME" { ... }` declarations and
matches their braces while ignoring quoted strings and comments. Candidate ranking uses:

1. a resource address named by the Terraform diagnostic;
2. a referenced line inside a resource block;
3. changed lines inside a resource block;
4. the referenced or changed Terraform file.

The result may contain zero, one, or several candidates. No provider or resource type is
hardcoded. Terraform addresses with module prefixes or instance keys retain the address
from the error while mapping to the underlying resource type for schema lookup.

## Deterministic minimal context

`ContextBuilder` separates source selection from prompt formatting and returns a
structured `DiagnosisContext`. Production prompts consume that structure; the legacy
v0.5 formatter is retained only for evaluation comparisons.

Lightweight context is packed in this deterministic priority order:

1. exact Terraform summary, detail, stage, address, and referenced file/line;
2. the affected Terraform resource block;
3. relevant changed Terraform lines;
4. directly referenced variable/local definitions;
5. direct data/resource declarations when available;
6. small metadata.

An explicit diagnostic address wins. Indexed instances such as
`aws_instance.web[0]` and `aws_instance.web["blue"]`, plus module-prefixed addresses such
as `module.network.aws_subnet.private[0]`, map back to the source declaration without
discarding the original identity. Without an address, changed blocks and referenced
file/line evidence rank candidates. Genuine ambiguity includes at most three blocks and
is recorded in the manifest; no-diff runs fall back to diagnostic evidence.

References beginning with `var.` and `local.` are resolved selectively. Variables include
their exact declaration/default. Simple locals include the exact assignment; complex
locals use the smallest bounded containing block. Direct `data.TYPE.NAME` and resource
references may add one declaration. Expansion depth is one: references discovered inside
a supporting block are not recursively followed. `module.*`, `file()`, and
`templatefile()` are recorded unresolved and never trigger child-module or arbitrary-file
traversal.

Soft limits independently bound the diagnostic, diff, affected block, supporting context,
and total selected context. Packing drops lower-priority material first. Oversized HCL is
reduced only at line boundaries around the diagnostic/changed location and marked with a
stable truncation reason; it is never summarized or rewritten. Schema-aware and auto modes
use this same minimal Terraform context. Explicit schema-aware mode pairs it with a
deterministic schema slice on the first call. Auto mode adds that slice only after an
eligible failed verification.

## Selective schema inspection

Schema-aware mode never initializes the original checkout. The agent creates a temporary
repository-shaped copy containing only Terraform configuration and lock files, excludes
`.git`, `.terraform`, state, environment files, and unrelated content, and gives Terraform
a temporary `HOME` with a reduced environment. It runs:

```text
terraform version -json
terraform init -backend=false -input=false -no-color
terraform providers schema -json
```

The full schema JSON stays local to the agent while `SchemaSlicer` indexes each matched
resource schema once. It maps exact keys and nested HCL paths to stable paths such as
`block.attributes.type` and
`block.block_types.rule.block.attributes.name`. Selection uses, in order, exact diagnostic
fields, changed attributes, affected expressions, explicit nested blocks, required
siblings, and bounded source-attribute fallback. Dynamic values are not fuzzy-matched to
schema paths. Parent `nesting_mode`, `min_items`, `max_items`, and complete selected field
metadata are preserved.

The defaults allow at most 32 selected paths, nested depth four, 8,000 compact schema JSON
characters, and 400 description characters per selected field. Descriptions stop at a
deterministic sentence/word boundary; JSON is never cut mid-structure. Exact diagnostic
fields are soft-protected and lower-priority paths are dropped first. Unsupported schema
shapes or cases with no useful source-attribute fallback use the full matched resource
schema and record `full_schema_fallback` plus a reason.

The model prompt receives only the slice, provider source, and version. For result
compatibility, the existing `terraform.schemas` field still contains its single full
matched schema copy; v0.7 does not add a second sliced-schema copy to the persisted result.
Instead, `schema_slice_manifest` stores paths/reasons only and `schema_optimization`
stores character counts, reduction, fallback state, and path count. Missing Terraform,
failed init, unknown resource types, invalid JSON, and partial extraction remain explicit.

Registry/provider download failures can therefore reduce a schema-aware run to the
available lightweight evidence; they do not modify the source repository.

## Isolated patch verification

After the selected provider returns a candidate, the verifier validates the unified-diff structure and
every path before executing a command. A patch is rejected if it is oversized, malformed,
binary, a rename/copy, creates a symlink/submodule, touches a non-Terraform file, escapes
the repository, or targets a file outside the selected Terraform working directory.
Exact known files expressed relative to that working directory are resolved within it,
then all accepted headers are canonicalized to standard `a/<repository-path>` and
`b/<repository-path>` form before Git runs. The canonical patch is recorded as
`diagnosis.final_patch`; the model's original text remains in its diagnosis candidate.

Accepted patches are written only into a new temporary directory. That directory receives
the same filtered Terraform configuration/lock-file copy used by inspection; it receives
no `.git`, `.terraform`, state, `.env`, credential, or unrelated repository files. Commands
run without a shell, with a temporary `HOME` and reduced environment, in this order:

```text
git apply --check candidate.patch
git apply candidate.patch
terraform fmt -check
terraform init -backend=false -input=false -no-color
terraform validate -no-color
terraform plan -input=false -lock=false -refresh=false -no-color
```

Commands are sequentially gated: plan runs only after patch checking/application, format,
backend-disabled initialization, and validation all pass. All Terraform commands are
skipped when the patch cannot be checked/applied, and missing Git or Terraform is reported
as `unavailable` rather than silently treated as success. Plan does not lock or refresh
state and writes no plan file. Command output is bounded and redacted before it is attached
to the result. The temporary directory is cleaned when the stage exits.

Every attempt is preserved under `diagnosis.attempts`:

```json
{
  "attempt": 1,
  "patch": "...",
  "status": "failed",
  "failed_stage": "plan",
  "isolation": "temporary-copy",
  "changed_files": ["infrastructure/main.tf"],
  "commands": {
    "patch_check": {"command": ["git", "apply", "--check", "candidate.patch"], "status": "passed"},
    "patch_apply": {"command": ["git", "apply", "candidate.patch"], "status": "passed"},
    "fmt": {"command": ["terraform", "fmt", "-check"], "status": "passed"},
    "init": {"command": ["terraform", "init", "-backend=false", "-input=false", "-no-color"], "status": "passed"},
    "validate": {"command": ["terraform", "validate", "-no-color"], "status": "passed"},
    "plan": {"command": ["terraform", "plan", "-input=false", "-lock=false", "-refresh=false", "-no-color"], "status": "failed"}
  },
  "temporary_copy_cleaned": true,
  "warnings": ["terraform plan did not pass."]
}
```

Attempt statuses are `verified`, `failed`, `rejected`, `unavailable`, and `skipped`.
Each command separately records `passed`, `failed`, `error`, or `skipped`, its exit code,
bounded stdout/stderr, and duration. Verification failure does not discard the diagnosis.

## Progressive context and bounded second attempt

`auto` uses `MINIMAL → SCHEMA` progression. The first request contains only the normalized
diagnostic, selected Terraform blocks, relevant diff, and bounded one-hop definitions; it
does not inspect or include provider schema. After isolated verification,
`ContextEscalationPolicy` returns one structured `stop`, `repair`, or `escalate` decision.
The `EXPANDED` level exists in the data model for future work but is never selected
automatically in v0.8.

Decision precedence is deterministic:

1. verified, rejected/unsafe, skipped, environment, credentials, network, patch-apply, and
   initialization outcomes stop;
2. patch-check, formatting, or newly introduced syntax failures use the existing repair
   with the original context;
3. a same or new provider-semantic `validate`/`plan` diagnostic, bounded resource
   ambiguity, or a failure-relevant unresolved symbol may escalate from minimal to sliced
   schema;
4. otherwise an actionable failure gets the conservative bounded repair, or stops.

Similarity uses the failed stage, parsed resource address when both diagnostics provide
one, and normalized non-generic term overlap. Verification relationships are recorded as
`same_failure`, `new_semantic_failure`, `new_syntactic_failure`,
`environment_failure`, or `unknown`. Phrase sets for semantic, syntactic, credential, and
network failures are provider-neutral module constants and covered by tests. The policy
performs no Terraform command, network request, schema lookup, or model call.

When the action is schema escalation, the existing safe inspector runs once, the v0.7
slicer selects the relevant paths, and the second prompt combines the original normalized
diagnostic/minimal source, first diagnosis and patch, failed stage, bounded redacted failed
command output, escalation reason/signals, and sliced schema. If no usable schema is
available, the agent stops instead of rerunning an unchanged prompt. Schema retrieval is
never repeated within a run and has no cross-run cache.

Explicit `lightweight` never escalates and retains one minimal-context repair. Explicit
`schema-aware` retrieves sliced schema before its first call and retains one schema-aware
repair. There is no loop: one initial call plus at most one repair or escalation call, with
the same provider and requested model. A second failed verification always stops.

The diagnosis contract is:

```json
{
  "initial": {"root_cause": "...", "suggested_patch": "...", "model_confidence": 0.91},
  "repair": null,
  "attempts": [{"attempt": 1, "status": "verified", "failed_stage": null}],
  "final_patch": "...",
  "verification_status": "verified_first_attempt",
  "model_confidence": 0.91,
  "evidence_score": 0.8,
  "verification": {
    "passed": true,
    "status": "verified_first_attempt"
  },
  "second_attempt_reason": "none"
}
```

Final statuses are `verified_first_attempt`, `verified_after_retry`,
`verification_failed`, `patch_rejected`, `verification_unavailable`, and
`verification_skipped`. `failed_stage` is one of `patch_check`, `patch_apply`, `fmt`,
`init`, `validate`, or `plan`.

## Progressive telemetry

For `auto`, `context.selected_mode` is `progressive`. `context_progression` records the
strategy, initial/final levels, levels used, whether escalation occurred, a stable reason
code, bounded deterministic signals, error relationship, second-attempt reason, schema
retrieval/avoidance, and same-model status. Explicit modes produce the same structure with
`progressive_enabled: false` so hosted consumers do not need separate result shapes.

Each `llm_calls` and `context_telemetry.calls` entry records its `context_level`. Per-call
context telemetry also records prompt/source/schema characters, source files, resource
blocks, and selected schema paths. `llm_usage.initial_input_tokens` is the complete first
request input count; `escalation_input_tokens` is the complete second request count only
for minimal-to-schema progression, not incremental schema tokens. Provider-reported token
counts remain authoritative.

Timing includes `initial_context_build_seconds`, `initial_llm_seconds`,
`initial_verification_seconds`, `escalation_decision_seconds`,
`schema_retrieval_seconds`, `schema_slice_seconds`, `second_llm_seconds`, and
`second_verification_seconds`, while existing aggregate timing keys remain available.
Auto runs also expose `schema_avoided`; explicit modes leave it null.

## Direct Gemini support

`GeminiProvider` remains a first-class implementation of the same provider-neutral
`LLMProvider` protocol. It reads `GEMINI_API_KEY`, uses the Gemini SDK response schema,
validates the returned value again with the strict Pydantic contract, and emits the same
per-call telemetry shape. Gemini does not report cost through this integration, so its
`cost_usd` is `null` and `cost_complete` is false for a run containing Gemini calls.
Missing fields, out-of-range confidence, invalid evidence sources, or arbitrary extra
fields reject either provider's response.

The output document includes repository/diff metadata, Terraform/schema metadata, parsed
failure, selected context and reason, diagnosis, nested patch verification, timings, token
usage, and warnings. Initial and repair model responses keep their own confidence; the
top-level `model_confidence` is the final model estimate. A separate `evidence_score`
checks identified resource evidence, error evidence, diff evidence, a non-empty patch,
and schema evidence when schema-aware mode is used. Verification contributes a separate
deterministic `passed/status` signal and never changes model confidence to `1.0`.

## Safety boundaries

The implementation never runs `terraform apply`, `destroy`, `import`, `state rm`,
`state mv`, or `taint`. It does not read Terraform state, `.env` files, Git credential
data, or cloud credential files. It does not modify or delete source-repository files.

The candidate remains untrusted model output and must be reviewed manually. A verified
status means only that the patch applied to the filtered temporary copy and passed format,
initialization, validation, and a refresh-disabled plan there. It does not prove developer
intent, permit an apply, access source state, or verify infrastructure outcomes. Provider
configuration or data sources can still require credentials/network access; those failures
are reported rather than converted into success. Configurations that depend on omitted
non-Terraform local files may fail and will be reported as such.

## Tests

```bash
python -m pytest
```

The suite uses temporary arbitrary repositories and mocked OpenRouter/Gemini/Terraform boundaries;
normal tests require no API key, network, cloud credentials, or installed Terraform CLI.
Coverage includes discovery, traversal rejection, diff lines, human/JSON/malformed logs,
unknown and multiple resources, indexed/module addresses, exact block and symbol selection,
multi-file/no-diff/ambiguous fallbacks, deterministic budgets and de-duplication, section
telemetry, minimal repair prompts, context manifests/optimization math, schema
compatibility, exact and changed schema-field selection, nested structural pruning, required
siblings, description/path/depth budgets, malformed-schema fallback, schema manifests and
telemetry, arbitrary-file deferral, schema selection/missing CLI, all context modes,
minimal-first auto behavior, semantic escalation, formatting repair, environment stopping,
verification-error relationships, call bounds, schema avoidance, progression telemetry,
three-strategy evaluation aggregates,
strict provider JSON, OpenRouter request construction, structured-output fallback,
categorized errors, bounded transport retries, free/unknown cost handling, secret safety,
usage aggregation, missing API keys, isolated patch application, plan flags/gating, unsafe
first and second patches, state and `.terraform` exclusion, output redaction, missing
Terraform, real Git application, complete attempt history, final status mapping, malformed
repair, and the exact one-second-attempt bound.

## v0.5 versus v0.6 context benchmark

The comparison harness uses the three diagnostic packages and always emits JSON, JSONL,
and Markdown under the ignored `evaluation-results/v0.6-context-comparison/` directory:

```bash
python3 scripts/compare_context.py \
  --benchmark-root ../terraform-failure-benchmarks/diagnostic-packages
```

Offline mode deterministically compares prompt characters and leaves input/output/total
tokens, cost, latency, diagnosis, patch, repair, and verification fields `null`. It never
derives token claims from characters. Existing result directories can be merged with
`--v0-5-results DIR --v0-6-results DIR`; each directory should contain `<case-id>.json`.

When Terraform, AWS access, and a valid OpenRouter key are available, both strategies can
be executed with the same fixed free model:

```bash
OPENROUTER_API_KEY='...' python3 scripts/compare_context.py \
  --benchmark-root ../terraform-failure-benchmarks/diagnostic-packages \
  --live-repository-root ../terraform-failure-benchmarks \
  --run-live \
  --model '<provider>/<fixed-model>:free'
```

Live mode refuses non-`:free` models. Provider-reported input tokens and cost remain the
authoritative comparison. Free-model output can still be nondeterministic, so verified
patch status is the strongest regression gate and exact parity may require repeated runs.
The live repository must contain the complete `cases/<case-id>` configurations and
collected plan logs; the diagnostic packages remain the deterministic context fixtures.
When AWS credentials are environment variables, allowlist their names with
`SEMANTIC_TERRAFORM_AGENT_PASSTHROUGH_ENV` as documented by the isolated verifier.

## v0.6 versus v0.7 schema benchmark

The schema comparison forces all three benchmark cases into schema-aware mode, then uses
the same v0.6 minimal Terraform context with either the complete matched resource schema
or the v0.7 deterministic slice:

```bash
python3 scripts/compare_schema_context.py \
  --benchmark-root ../terraform-failure-benchmarks/diagnostic-packages
```

It writes JSON, JSONL, and Markdown to the ignored
`evaluation-results/v0.7-schema-comparison/` directory. The deterministic offline fixture
currently measures DynamoDB at 8,303→392 schema characters (95.3%), forced-schema EBS at
2,170→239 (89.0%), and S3 at 11,744→244 (97.9%). Corresponding total prompt-character
reductions are 73.0%, 40.3%, and 82.1%. These are character measurements, not token or
correctness claims; provider token, cost, diagnosis, repair, and verification fields stay
`null` without comparable live results.

Existing result directories can be merged with
`--v0-6-results DIR --v0-7-results DIR`. A live comparison requires a fixed free model,
the full benchmark checkout, Terraform/AWS access, and `OPENROUTER_API_KEY`:

```bash
OPENROUTER_API_KEY='...' python3 scripts/compare_schema_context.py \
  --benchmark-root ../terraform-failure-benchmarks/diagnostic-packages \
  --live-repository-root ../terraform-failure-benchmarks \
  --run-live \
  --model '<provider>/<fixed-model>:free'
```

The live path rejects non-`:free` models and runs the same model once with `full` and once
with `sliced`; provider-reported tokens/cost and verified patch status remain authoritative.

## v0.8 progressive-context benchmark

The v0.8 harness compares `always_lightweight`, `always_schema`, and `progressive` for the
DynamoDB, EBS, and S3 diagnostic packages using the same model setting:

```bash
python3 scripts/compare_progressive_context.py \
  --benchmark-root ../terraform-failure-benchmarks/diagnostic-packages
```

It writes `results.json`, `results.jsonl`, and `comparison.md` under the ignored
`evaluation-results/v0.8-progressive-context/` directory. Offline mode records
deterministic initial prompt characters, a synthetic schema-escalation prompt character
measurement, and the always-schema prompt characters. It leaves model calls, tokens,
cost, latency, escalation, and verification values uncollected rather than estimating
them. The synthetic prompt is a rendering measurement, not a correctness result.

Existing result directories can be merged with `--always-lightweight-results DIR`,
`--always-schema-results DIR`, and `--progressive-results DIR`. Aggregates cover minimal
first-pass verification, schema escalation/avoidance, overall verified fixes, and mean
tokens/cost/latency from actual live rows only. Per-case token and cost reductions versus
always-schema are computed only when both results report the same provider and model.

To execute all nine live runs with a fixed free OpenRouter model:

```bash
OPENROUTER_API_KEY='...' python3 scripts/compare_progressive_context.py \
  --benchmark-root ../terraform-failure-benchmarks/diagnostic-packages \
  --live-repository-root ../terraform-failure-benchmarks \
  --run-live \
  --model '<provider>/<fixed-model>:free'
```

Live mode refuses non-`:free` models. Terraform, provider access, benchmark case sources,
and any required cloud credentials must be available. Provider-reported usage and verified
patch status are authoritative; free-model nondeterminism remains an evaluation limitation.

## Running against the existing benchmark repository

From this repository's directory, with Terraform installed and `OPENROUTER_API_KEY`
exported, this exact command tests the current free router without hardcoding a transient
catalog entry:

```bash
semantic-terraform-agent diagnose \
  --repo-path ../terraform-failure-benchmarks \
  --terraform-dir cases/dynamodb-key-schema-failure \
  --log-file ../terraform-failure-benchmarks/collected-runs/terraform-logs-dynamodb-key-schema-failure/plan.stderr.log \
  --provider openrouter \
  --model openrouter/free \
  --context-mode auto \
  --max-repair-attempts 1 \
  --output benchmark-result.json
```

This uses only generic repository and diagnostic inputs. It deliberately does not import
benchmark scripts, diagnostic packages, case metadata, or ground truth. To test a supplied
diff, use one whose paths are relative to the selected repository checkout; package-local
diffs whose paths start with `terraform/` describe the package layout rather than the live
`cases/...` checkout and should not be hardcoded into the product.

## GitHub Actions E2E benchmark

The agent repository owns
[`.github/workflows/e2e-benchmark-test.yml`](.github/workflows/e2e-benchmark-test.yml).
It is manual-only (`workflow_dispatch`) and checks out
`JalinaH/terraform-failure-benchmarks` into a subfolder without persisted Git credentials.
It installs Python, Terraform `1.15.7`, and this package; authenticates to AWS through OIDC;
runs one selected benchmark diagnosis with one permitted repair; requires a verified final
status and successful final plan; checks that the benchmark checkout remains clean; and
uploads `result.json` even when the verification gate fails.

Use **Actions → E2E benchmark diagnosis → Run workflow** and select one of these fixed
cases:

| Selection | Terraform directory | Historical plan log | Expected resource type |
| --- | --- | --- | --- |
| `dynamodb-key-schema-failure` | `cases/dynamodb-key-schema-failure` | `collected-runs/terraform-logs-dynamodb-key-schema-failure/plan.stderr.log` | `aws_dynamodb_table` |
| `ebs-throughput-volume-type-failure` | `cases/ebs-throughput-volume-type-failure` | `collected-runs/terraform-logs-ebs-throughput-volume-type-failure/plan.stderr.log` | `aws_ebs_volume` |
| `s3-bucket-naming-conflict-failure` | `cases/s3-bucket-naming-conflict-failure` | `collected-runs/terraform-logs-s3-bucket-naming-conflict-failure/plan.stderr.log` | `aws_s3_bucket` |

The workflow maps the selected identifier to these allowlisted paths rather than accepting
arbitrary path input. It verifies the directory, Terraform files, and log before invoking
the agent. Result assertions cover the document status, affected resource/schema type,
non-empty final patch, deterministic verification signal, final attempt, successful plan,
and zero plan exit code. The GitHub Step Summary contains only the case, resource, context,
verification status, repair use, runtime, and token counts. The artifact is named
`semantic-terraform-agent-e2e-<case>-<run-number>`.

Configure the following in the **semantic-terraform-agent** repository—not the benchmark
repository:

- Action variable `AWS_ROLE_ARN`
- Action variable `AWS_REGION`
- Action secret `GEMINI_API_KEY`
- Action secret `BENCHMARK_REPO_TOKEN`, containing a fine-grained PAT with read-only
  `Contents` access to `JalinaH/terraform-failure-benchmarks`

The IAM role trust policy must permit the agent repository's GitHub OIDC subject. For this
repository on `main`, the current immutable subject is
`repo:JalinaH@139668262/semantic-terraform-agent@1327763019:ref:refs/heads/main`. Use a
plan-only least-privilege policy. The workflow passes only explicit ephemeral AWS
environment variables plus `TF_VAR_*` into the isolated Terraform subprocesses; other
caller environment values remain excluded, and passed values are redacted if command
output repeats them.

## GitHub Actions Integration

The reusable workflow
[`terraform-agent.yml`](.github/workflows/terraform-agent.yml) runs after a consuming
repository has already detected a Terraform failure. It downloads that job's bounded
failure-log artifact, checks out the exact failed commit, constructs the best event-specific
diff, runs the exact called agent revision, uploads the result, writes a Step Summary, and—only
for trusted same-repository pull requests—creates or updates one bot comment.

### Reusable workflow API

| Input | Required | Default | Purpose |
| --- | --- | --- | --- |
| `terraform_dir` | yes | — | Repository-relative Terraform working directory |
| `failure_log_artifact` | yes | — | Artifact uploaded by the failed CI job |
| `failure_log_path` | no | `terraform-plan.log` | File inside the downloaded artifact |
| `failed_stage` | no | `plan` | `init`, `fmt`, `validate`, `plan`, `apply`, or `unknown` |
| `terraform_version` | no | `1.15.7` | Terraform used for isolated verification |
| `provider` | no | `gemini` | Provider-neutral selector: `gemini` or `openrouter` |
| `model` | no | `gemini-3.6-flash` | Dynamic provider model ID; override for OpenRouter |
| `context_mode` | no | `auto` | `lightweight`, `schema-aware`, or `auto` |
| `max_repair_attempts` | no | `1` | Bounded value `0` or `1` |
| `aws_region` | yes | — | Region for OIDC-authenticated Terraform verification |

Required workflow secrets are `AWS_ROLE_ARN` plus the selected provider key:
`GEMINI_API_KEY` or `OPENROUTER_API_KEY`. The reusable workflow
also exposes `result_status`, `verification_status`, and `artifact_name` outputs. The
caller must grant `contents: read` and `id-token: write`; grant `pull-requests: write` only
to the reusable-workflow job when PR comments are desired. GitHub's built-in token is used
for same-repository comments—no PAT is required.

A minimal caller job, after a job named `terraform` uploads `terraform-failure-log`, is:

```yaml
semantic-terraform-agent:
  needs: terraform
  if: >-
    ${{ always() && needs.terraform.result == 'failure' &&
    needs.terraform.outputs.failure_detected == 'true' &&
    (github.event_name != 'pull_request' ||
    github.event.pull_request.head.repo.full_name == github.repository) }}
  permissions:
    contents: read
    id-token: write
    pull-requests: write
  uses: JalinaH/semantic-terraform-agent/.github/workflows/terraform-agent.yml@main
  with:
    terraform_dir: infrastructure
    failure_log_artifact: terraform-failure-log
    failure_log_path: terraform-failure.log
    failed_stage: ${{ needs.terraform.outputs.failed_stage }}
    aws_region: ${{ vars.AWS_REGION }}
  secrets:
    GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
    AWS_ROLE_ARN: ${{ secrets.AWS_ROLE_ARN }}
```

For production use, pin `uses:` to a reviewed release tag or commit SHA instead of a
mutable branch. A complete validate/plan/log-upload example is available at
[`examples/github-actions/consumer.yml`](examples/github-actions/consumer.yml).
To use OpenRouter, set `provider: openrouter`, set an explicit `model`, and pass
`OPENROUTER_API_KEY` instead of `GEMINI_API_KEY` in the caller's `secrets` mapping.

### Failure evidence and commit/diff selection

The normal Terraform job combines the failed command's stdout/stderr into one file and
uploads it with `actions/upload-artifact`. The called workflow downloads that named
artifact during the same run and rejects absolute paths, traversal, symlink escape, missing
logs, control-character paths, invalid Terraform directories, and unsupported option
values. Git pathspecs are treated literally. It never scrapes the Actions web interface.

The called workflow analyzes the exact PR head SHA for pull requests and `github.sha` for
pushes, using full history. For pull requests it supplies `git diff <base SHA> <head SHA>`;
for pushes it supplies `git diff <event.before> <github.sha>`. Both are limited to the
validated Terraform directory and passed with `--diff-file`. New-branch pushes, missing
commits, and other unusable event comparisons produce an explicit warning and use the
agent's documented local Git fallback instead.

### Authentication and permissions

The consuming repository configures:

- repository variable `AWS_REGION`;
- repository secret `AWS_ROLE_ARN`;
- repository secret `GEMINI_API_KEY` or `OPENROUTER_API_KEY`, matching `provider`.

AWS credentials are obtained only through `aws-actions/configure-aws-credentials` and
GitHub OIDC. The IAM trust policy must authorize the **consuming repository's** applicable
branch or environment subject, because its workflow is the caller. The role should have
only the read/plan permissions needed by that Terraform configuration. The agent sandbox
receives only temporary AWS credential/region variables and explicitly configured
`TF_VAR_*` values. Permanent AWS keys are neither accepted nor documented.

The selected LLM key is scoped to the agent command step and is never printed or intentionally
written to result JSON, summaries, comments, or artifacts. Rendered text and patches are
bounded and passed through deterministic secret-pattern redaction before publication.

### Pull requests, pushes, and exit policy

For a trusted same-repository pull request, a separate job with `pull-requests: write`
posts a concise comment containing root cause, affected resource, constraint, verification
commands/status, confidence, evidence score, human-review warning, and a bounded collapsible
patch. The hidden `<!-- semantic-terraform-agent -->` marker identifies the bot's prior
comment; reruns update that comment instead of creating another.

For a direct push, no PR operation is attempted. The workflow writes repository, commit,
Terraform directory, failed stage, affected resource, context, verification, repair,
runtime, token, and diff-comparison metadata to `$GITHUB_STEP_SUMMARY`. Suggested patches
remain only in `result.json`; no commit or push operation exists.

The analysis job fails for integration/infrastructure problems such as invalid paths or
options, a missing artifact, Terraform/setup/OIDC failure, LLM provider failure, missing result,
or an `error` result document. A completed diagnosis with `verification_failed`,
`patch_rejected`, `verification_unavailable`, or `verification_skipped` remains a successful
workflow execution so humans can inspect the reported outcome. The exact status is exposed
as a workflow output, summary/comment field, and artifact content; it is not represented as
an agent crash.

The deterministic artifact name is
`semantic-terraform-agent-<run-id>-<run-attempt>`. It contains `result.json` and, for PRs,
the bounded rendered comment. It never includes Terraform state, `.terraform`, provider
caches, environment files, or credentials. A final cleanliness gate fails if the original
caller checkout has any tracked or untracked modification.

### Fork pull-request safety

Repository secrets and write-capable tokens are intentionally unavailable to untrusted
fork pull requests. Both the reusable workflow and example wrapper skip analysis when the
PR head repository differs from the base repository. Do not replace `pull_request` with
`pull_request_target` to run untrusted Terraform with LLM/AWS secrets. A repository owner
who wants to analyze a fork contribution must first adopt an explicit reviewed workflow,
such as checking the commit onto a trusted branch after inspection.

Regardless of verification status, every generated patch is a suggestion. Terraform
verification proves only that it applied and passed the configured isolated commands; it
does not prove developer intent. Human review and an explicit, separate application are
always required.

## Recommended next phase

Start v0.9 with a cost-aware model-routing policy layered outside the now-observable
context progression. Define deterministic model tiers and eligibility, keep the v0.8
context decision independent, compare models on the same benchmark inputs, and preserve
the two-call ceiling before considering any cheap-to-strong route. LLMLingua, semantic
caching, verified-failure memory, billing, dashboard charts, and hard budgets remain
explicitly deferred.
