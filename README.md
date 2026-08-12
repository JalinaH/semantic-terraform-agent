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

Version `0.4.0` implements bounded diagnosis, repair, plan verification, and reusable
GitHub Actions integration:

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
  -> optional GitHub Step Summary and idempotent pull-request comment
```

It intentionally does **not** apply a patch to the source checkout, run Terraform apply or
destroy, make more than two model calls, commit or push code, auto-merge, host a service,
persist results outside caller-controlled Actions artifacts, or implement an MCP server.

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
  --failed-stage plan \
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
`--max-repair-attempts` accepts only `0` or `1` in version 0.4.0 and defaults to `1`.
Use `0` to verify the initial patch without asking the model for a repair.

`--failed-stage` optionally records the caller-known stage as `init`, `fmt`, `validate`,
`plan`, `apply`, or `unknown`. It overrides log inference and is included in the model
context and result document. This is metadata only; it never causes the agent to run
`terraform apply`.

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

When the caller supplies `--failed-stage`, that explicit value replaces the inferred stage.

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

## Bounded repair policy

An initial failure at `git apply --check`, `fmt`, `validate`, or `plan` contains actionable
evidence and may trigger one dedicated repair call when `--max-repair-attempts 1` is active.
Patch-check repair applies only after the patch has already passed structural, path, and
scope safety validation. The repair prompt contains the original Terraform error, relevant
source and Git diff, original root cause and patch, failed stage, only that command's
bounded/redacted output, and relevant schemas in schema-aware mode. It tells the model to
preserve the diagnosis unless the new evidence contradicts it.

No repair runs for rejected/unsafe patches, structural/path/scope validation failures,
patch application failures, missing Terraform, unavailable environment/provider
initialization, explicit verification skip, init failure, malformed model output, or when
retries are disabled.
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
| `provider` | no | `gemini` | Provider-neutral interface selector; `gemini` is currently implemented |
| `model` | no | `gemini-3.6-flash` | Provider model ID |
| `context_mode` | no | `auto` | `lightweight`, `schema-aware`, or `auto` |
| `max_repair_attempts` | no | `1` | Bounded value `0` or `1` |
| `aws_region` | yes | — | Region for OIDC-authenticated Terraform verification |

Required workflow secrets are `GEMINI_API_KEY` and `AWS_ROLE_ARN`. The reusable workflow
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
- repository secret `GEMINI_API_KEY`.

AWS credentials are obtained only through `aws-actions/configure-aws-credentials` and
GitHub OIDC. The IAM trust policy must authorize the **consuming repository's** applicable
branch or environment subject, because its workflow is the caller. The role should have
only the read/plan permissions needed by that Terraform configuration. The agent sandbox
receives only temporary AWS credential/region variables and explicitly configured
`TF_VAR_*` values. Permanent AWS keys are neither accepted nor documented.

`GEMINI_API_KEY` is scoped to the agent command step and is never printed or intentionally
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
options, a missing artifact, Terraform/setup/OIDC failure, Gemini failure, missing result,
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
`pull_request_target` to run untrusted Terraform with Gemini/AWS secrets. A repository owner
who wants to analyze a fork contribution must first adopt an explicit reviewed workflow,
such as checking the commit onto a trusted branch after inspection.

Regardless of verification status, every generated patch is a suggestion. Terraform
verification proves only that it applied and passed the configured isolated commands; it
does not prove developer intent. Human review and an explicit, separate application are
always required.

## Recommended next phase

Install this workflow in a separate Terraform repository, validate same-repository PR and
direct-push behavior, then cut and pin a reviewed `v0.4.0` release. After that, add
provider-neutral evaluation across real repositories and refine environment-failure
classification. Keep the integration human-reviewed and auditable before considering a
GitHub App or any broader automation.
