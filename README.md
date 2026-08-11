# Semantic Terraform Failure Agent

Semantic Terraform Failure Agent is a local CLI that diagnoses a Terraform failure in
an arbitrary checked-out repository. It combines the Terraform diagnostic, relevant
source and Git changes, and—when useful—only the affected provider resource schemas.
Gemini produces a validated diagnosis and candidate patch; the agent checks that patch
inside an isolated copy and writes a stable JSON result for downstream tooling.

This repository is the reusable product. It has no dependency on a benchmark case ID,
known resource name, cloud provider, or the separate `terraform-failure-benchmarks`
implementation.

## Status and scope

Version `0.3.0` implements bounded diagnosis, repair, and plan verification:

```text
repository + failure log
  -> safe input discovery
  -> changed Terraform files
  -> candidate resource blocks
  -> deterministic context selection
  -> selective schema inspection (when selected)
  -> Gemini structured diagnosis
  -> isolated patch application + fmt/init/validate/plan
  -> at most one evidence-driven repair + fresh verification
  -> result JSON + concise terminal summary
```

It intentionally does **not** apply a patch to the source checkout, run Terraform apply or
destroy, make more than two model calls, call GitHub, comment on pull requests, host a
service, persist results, or implement an MCP server.

## Requirements and installation

- Python 3.11 or newer
- Git and Terraform on `PATH` for patch verification
- Terraform on `PATH` for schema-aware diagnosis
- `GEMINI_API_KEY` in the environment

From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
export GEMINI_API_KEY='your-key'
```

The key is read at request time. The CLI never prints or stores it.

## CLI usage

```bash
semantic-terraform-agent diagnose \
  --repo-path /path/to/repository \
  --terraform-dir infrastructure \
  --log-file /path/to/plan.stderr.log \
  --diff-file /path/to/change.patch \
  --provider gemini \
  --model gemini-3.6-flash \
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
- `schema-aware`: adds matched resource schemas and provider metadata.
- `auto`: applies the deterministic policy described below.

The default model is `gemini-2.5-flash`; pass another Gemini model ID explicitly when
needed. Patch verification is enabled by default. Use `--no-verify-patch` to record a
deliberately skipped verification in environments where local commands must not run.
`--max-repair-attempts` accepts only `0` or `1` in version 0.3.0 and defaults to `1`.
Use `0` to verify the initial patch without asking the model for a repair.

## Repository discovery

`--repo-path` must name an existing directory. `--terraform-dir` must be relative to
that repository and resolve inside it. The collector identifies `.tf` and `.tf.json`
files loaded directly by that Terraform working directory, retains paths relative to
the repository root, and reads only bounded Terraform source inputs. Symlinks or diff
paths that escape the root are rejected. It does not scan unrelated file contents.

Diff `+++` paths are normalized and intersected with discovered Terraform files.
Changed line numbers from unified-diff hunks are retained so a change inside a resource
block is stronger evidence than a change elsewhere in its file.

## Failure parsing

The parser supports normal Terraform terminal diagnostics (including ANSI/box drawing
format) and newline-delimited JSON diagnostics. It extracts:

- primary error summary and detail;
- referenced `.tf`/`.tf.json` file and line, when present;
- resource address from Terraform's `with ...` diagnostic;
- inferred stage: `init`, `fmt`, `validate`, `plan`, `apply`, or `unknown`.

Unstructured or malformed logs produce a conservative fallback rather than an invented
resource. The entire caller-supplied log is preserved in `failure.original_log`; only a
bounded, redacted excerpt is sent to Gemini.

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

The full schema JSON exists only in process memory long enough to locate candidate
resource types. The prompt receives only matching `resource_schemas` entries plus their
provider source and lock-file version. If there are no candidate types, the agent does
not fall back to sending the entire provider schema. Missing Terraform, failed init,
unknown resource types, invalid JSON, and partial extraction are recorded explicitly.

Registry/provider download failures can therefore reduce a schema-aware run to the
available lightweight evidence; they do not modify the source repository.

## Isolated patch verification

After Gemini returns a candidate, the verifier validates the unified-diff structure and
every path before executing a command. A patch is rejected if it is oversized, malformed,
binary, a rename/copy, creates a symlink/submodule, touches a non-Terraform file, escapes
the repository, or targets a file outside the selected Terraform working directory.

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

## Bounded repair policy

An initial failure at `fmt`, `validate`, or `plan` contains actionable evidence and may
trigger one dedicated repair call when `--max-repair-attempts 1` is active. The repair
prompt contains the original Terraform error, relevant source and Git diff, original root
cause and patch, failed stage, only that command's bounded/redacted output, and relevant
schemas in schema-aware mode. It tells the model to preserve the diagnosis unless the new
evidence contradicts it.

No repair runs for rejected/unsafe patches, path or format violations, patch check/apply
failures, missing Terraform, unavailable environment/provider initialization, explicit
verification skip, init failure, malformed model output, or when retries are disabled.
There is no loop or recursion: maximum model invocations are exactly two—one diagnosis and
at most one repair. The repaired patch is subjected to the full safety checks in a new
temporary copy.

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
  }
}
```

Final statuses are `verified_first_attempt`, `verified_after_retry`,
`verification_failed`, `patch_rejected`, `verification_unavailable`, and
`verification_skipped`. `failed_stage` is one of `patch_check`, `patch_apply`, `fmt`,
`init`, `validate`, or `plan`.

## Automatic context policy

`auto` selects lightweight context only when exactly one resource has high-confidence
evidence and the diagnostic clearly names the invalid/conflicting argument or constraint.
It selects schema-aware context when resource detection is absent or plural, evidence is
weaker, or a provider validation diagnostic is ambiguous. The reason is always written to
`context.selection_reason`. No LLM call is spent choosing the mode.

## Gemini and structured output

`GeminiProvider` implements the provider-neutral `LLMProvider` protocol. Future OpenAI,
Claude, or local integrations can implement its `diagnose(DiagnosisRequest)` and
`repair(RepairRequest)` methods.
Gemini is asked for JSON using an SDK response schema, and the returned value is validated
again with a strict Pydantic model. Missing fields, out-of-range confidence, invalid
evidence sources, or arbitrary extra fields reject the response.

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

The suite uses temporary arbitrary repositories and mocked Gemini/Terraform boundaries;
normal tests require no API key, network, cloud credentials, or installed Terraform CLI.
Coverage includes discovery, traversal rejection, diff lines, human/JSON/malformed logs,
unknown and multiple resources, schema selection/missing CLI, all context modes, strict
Gemini JSON, missing API key, isolated patch application, plan flags/gating, unsafe first
and second patches, state and `.terraform` exclusion, output redaction, missing Terraform,
real Git application, complete attempt history, final status mapping, malformed repair,
and the exact one-retry/model-call bound.

## Running against the existing benchmark repository

From this repository's directory, with Terraform installed and `GEMINI_API_KEY` exported:

```bash
semantic-terraform-agent diagnose \
  --repo-path ../terraform-failure-benchmarks \
  --terraform-dir cases/dynamodb-key-schema-failure \
  --log-file ../terraform-failure-benchmarks/collected-runs/terraform-logs-dynamodb-key-schema-failure/plan.stderr.log \
  --provider gemini \
  --model gemini-3.6-flash \
  --context-mode auto \
  --max-repair-attempts 1 \
  --output benchmark-result.json
```

This uses only generic repository and diagnostic inputs. It deliberately does not import
benchmark scripts, diagnostic packages, case metadata, or ground truth. To test a supplied
diff, use one whose paths are relative to the selected repository checkout; package-local
diffs whose paths start with `terraform/` describe the package layout rather than the live
`cases/...` checkout and should not be hardcoded into the product.

## Recommended next phase

Add provider-neutral verification evaluation and environment-failure classification: measure
first-patch versus repaired-patch success across external repositories, refine the boundary
between candidate failures and unavailable credentials/network, and optionally allow an
explicit safe allowlist for non-secret local files needed by Terraform functions. Keep this
local and auditable before adding any GitHub integration.
