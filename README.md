# Semantic Terraform Failure Agent

Semantic Terraform Failure Agent is a local CLI that diagnoses a Terraform failure in
an arbitrary checked-out repository. It combines the Terraform diagnostic, relevant
source and Git changes, and—when useful—only the affected provider resource schemas.
Gemini produces a validated diagnosis and candidate patch; the agent writes a stable
JSON result for downstream tooling.

This repository is the reusable product. It has no dependency on a benchmark case ID,
known resource name, cloud provider, or the separate `terraform-failure-benchmarks`
implementation.

## Status and scope

Version `0.1.0` implements the first local diagnosis loop:

```text
repository + failure log
  -> safe input discovery
  -> changed Terraform files
  -> candidate resource blocks
  -> deterministic context selection
  -> selective schema inspection (when selected)
  -> Gemini structured diagnosis
  -> result JSON + concise terminal summary
```

It intentionally does **not** verify or apply the candidate patch, retry a diagnosis,
call GitHub, comment on pull requests, host a service, persist results, or implement an
MCP server.

## Requirements and installation

- Python 3.11 or newer
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
needed.

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

## Automatic context policy

`auto` selects lightweight context only when exactly one resource has high-confidence
evidence and the diagnostic clearly names the invalid/conflicting argument or constraint.
It selects schema-aware context when resource detection is absent or plural, evidence is
weaker, or a provider validation diagnostic is ambiguous. The reason is always written to
`context.selection_reason`. No LLM call is spent choosing the mode.

## Gemini and structured output

`GeminiProvider` implements the provider-neutral `LLMProvider` protocol. Future OpenAI,
Claude, or local integrations can implement the same `diagnose(DiagnosisRequest)` method.
Gemini is asked for JSON using an SDK response schema, and the returned value is validated
again with a strict Pydantic model. Missing fields, out-of-range confidence, invalid
evidence sources, or arbitrary extra fields reject the response.

The output document includes repository/diff metadata, Terraform/schema metadata, parsed
failure, selected context and reason, diagnosis, timings, token usage, and warnings. The
model's score is retained as `model_confidence`. A separate `evidence_score` deterministically
checks identified resource evidence, error evidence, diff evidence, a non-empty patch, and
schema evidence when schema-aware mode is used. Neither score is claimed as verified.

## Safety boundaries

The implementation never runs `terraform apply`, `destroy`, `import`, `state rm`,
`state mv`, or `taint`. It does not read Terraform state, `.env` files, Git credential
data, or cloud credential files. It does not modify or delete source-repository files.

The candidate patch is unverified model output. Review it manually. Patch verification,
`terraform validate` of the candidate, and bounded retry behavior belong in a later phase.

## Tests

```bash
python -m pytest
```

The suite uses temporary arbitrary repositories and mocked Gemini/Terraform boundaries;
normal tests require no API key, network, cloud credentials, or installed Terraform CLI.
Coverage includes discovery, traversal rejection, diff lines, human/JSON/malformed logs,
unknown and multiple resources, schema selection/missing CLI, all context modes, strict
Gemini JSON, missing API key, and end-to-end lightweight orchestration.

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
  --output benchmark-result.json
```

This uses only generic repository and diagnostic inputs. It deliberately does not import
benchmark scripts, diagnostic packages, case metadata, or ground truth. To test a supplied
diff, use one whose paths are relative to the selected repository checkout; package-local
diffs whose paths start with `terraform/` describe the package layout rather than the live
`cases/...` checkout and should not be hardcoded into the product.

## Recommended next phase

Build isolated patch verification: apply the candidate diff only to another temporary copy,
run `terraform fmt -check` and `terraform validate` under the existing command allowlist,
and report verification evidence without applying infrastructure. After that foundation is
reliable, add a bounded repair/retry loop before any GitHub integration.

