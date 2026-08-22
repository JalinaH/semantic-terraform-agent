# Semantic Terraform Agent

Semantic Terraform Agent diagnoses Terraform CI failures, generates a candidate patch,
and verifies that patch in an isolated temporary workspace. It supports arbitrary
Terraform repositories and does not contain benchmark-specific resource logic.

Version: `1.0.1`

## How it works

```text
Terraform failure and Git diff
  → normalized diagnostic
  → deterministic minimal Terraform context
  → exact Verified Failure Memory lookup
  → model selection and structured diagnosis on a miss
  → isolated patch application
  → terraform fmt/init/validate/plan
  → at most one repair or schema-context escalation
  → final result JSON
```

The normal semantic-call ceiling is two. An exact remembered candidate can complete with
zero model calls only after it passes fresh isolated verification for the current source.

## Requirements

- Python 3.11+
- Git
- Terraform on `PATH` for schema inspection and patch verification
- `OPENROUTER_API_KEY` for the default OpenRouter provider
- AWS or other provider credentials required by the repository's safe Terraform plan

Install locally:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
export OPENROUTER_API_KEY='...'
```

## CLI

OpenRouter and `openrouter/free` are the fixed-routing defaults:

```bash
semantic-terraform-agent diagnose \
  --repo-path /path/to/repository \
  --terraform-dir infrastructure \
  --log-file /tmp/terraform-plan.log \
  --failed-stage plan \
  --context-mode auto \
  --verify-patch \
  --max-repair-attempts 1 \
  --output /tmp/result.json
```

For reproducible runs, select a fixed OpenRouter model explicitly:

```bash
semantic-terraform-agent diagnose \
  --repo-path /path/to/repository \
  --terraform-dir infrastructure \
  --log-file /tmp/terraform-plan.log \
  --diff-file /tmp/change.patch \
  --failed-stage plan \
  --provider openrouter \
  --model 'provider/model:free' \
  --model-routing fixed \
  --context-mode auto \
  --output /tmp/result.json
```

`--diff-file` is optional. Without it, the agent tries repository-local Git comparisons
and records the selected comparison. Unified-diff paths must be relative to
`--repo-path`.

Gemini remains available as an explicit compatibility provider. It requires
`GEMINI_API_KEY`; neither the CLI nor workflow selects it by default.

## Context and model policy

Context selection and model routing are independent:

- `--context-mode lightweight` uses exact minimal Terraform evidence.
- `--context-mode schema-aware` adds deterministic provider-schema slices.
- `--context-mode auto` starts minimal and escalates only when policy requires it.
- `--model-routing fixed` uses the requested model for both possible calls.
- `--model-routing auto` selects from a local model registry within
  `--max-model-tier free|economy|balanced|premium`.

An example registry is available at [`examples/model-registry.json`](examples/model-registry.json).
Configure it with `--model-registry` or `SEMANTIC_TERRAFORM_MODEL_REGISTRY_PATH`.
Model tiers are local policy metadata; actual cost remains provider-reported.

## Verified Failure Memory

Enable repository-scoped exact reuse with a local cache directory:

```bash
semantic-terraform-agent diagnose ... \
  --cache-dir /safe/cache/directory \
  --failure-memory \
  --repository-id owner/repository
```

The versioned SHA-256 fingerprint conservatively covers repository scope, failure and
stage, resource identity, exact selected evidence, Terraform source, Terraform/provider
version evidence, policy versions, and context budgets.

Only patches that previously reached `verified_first_attempt` or
`verified_after_retry` can be stored. A hit means a candidate exists—not that it is
trusted. Every remembered patch is freshly applied and verified in a new temporary copy.
Stale, corrupt, ambiguous, or unsafe entries fall back to the complete normal pipeline.

Inspect or clear only the configured SQLite store:

```bash
semantic-terraform-agent cache stats --cache-dir /safe/cache/directory
semantic-terraform-agent cache clear --cache-dir /safe/cache/directory
```

Provider schemas and deterministic schema slices use separate versioned cache entries.
Minimal source context is rebuilt rather than persisted.

## Result contract

The JSON result includes repository/failure metadata, structured diagnosis, the candidate
patch, isolated verification attempts, actual model usage/cost, separate context and model
progression, optimization telemetry, cache status, and `resolution_source`.

On successful warm reuse, `resolution_source` is `verified_failure_memory`, `llm_calls`
is empty, and current-run model usage is zero. Historical avoided usage is reported only
when authoritative prior telemetry exists.

## Safety guarantees

The agent never runs `terraform apply`, `terraform destroy`, `terraform import`,
`terraform taint`, or Terraform state mutation commands. It never auto-commits, pushes,
merges, or edits the original checkout.

Candidate patches are advisory and are applied only to filtered temporary verification
workspaces. State, `.terraform`, `.env`, credentials, private keys, arbitrary `file()`
targets, and unrelated files are excluded. Commands run without a shell and with a
reduced environment.

## Reusable GitHub Actions workflow

The production workflow is:

```yaml
jobs:
  diagnose:
    uses: JalinaH/semantic-terraform-agent/.github/workflows/terraform-agent.yml@v1.0.1
    permissions:
      contents: read
      id-token: write
      pull-requests: write
    with:
      terraform_dir: infrastructure
      failure_log_artifact: terraform-failure-log
      failure_log_path: terraform-plan.log
      failed_stage: plan
      provider: openrouter
      model: openrouter/free
      context_mode: auto
      max_repair_attempts: 1
      failure_memory: false
      aws_region: ${{ vars.AWS_REGION }}
    secrets:
      OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
      AWS_ROLE_ARN: ${{ secrets.AWS_ROLE_ARN }}
```

The caller must run Terraform first and upload its combined failure log. A complete
consumer example is provided at
[`examples/github-actions/consumer.yml`](examples/github-actions/consumer.yml).

Important workflow inputs:

| Input | Default | Purpose |
|---|---|---|
| `provider` | `openrouter` | OpenRouter or explicit Gemini |
| `model` | `openrouter/free` | Provider model ID |
| `model_routing` | `fixed` | Fixed or registry-driven automatic routing |
| `max_model_tier` | `premium` | Automatic-routing ceiling |
| `context_mode` | `auto` | Lightweight, schema-aware, or progressive context |
| `max_repair_attempts` | `1` | Unified second-opportunity limit (`0` or `1`) |
| `failure_memory` | `false` | Opt-in repository-scoped Actions cache |

The workflow authenticates to AWS with OIDC, verifies the original checkout remains
unchanged, uploads bounded result artifacts, and can publish an idempotent PR comment.
Memory is off by default. When enabled, GitHub Actions cache persistence is best-effort
and repository scoped; it is not a permanent hosted database.

## Development checks

The full Python test suite remains part of the repository:

```bash
pytest
ruff check .
python3 -m compileall src tests
git diff --check
```

Tests use temporary repositories and mocked provider/Terraform boundaries, so the normal
suite requires no API key, cloud credentials, network, or installed Terraform CLI.

## Release

Package metadata is defined in [`pyproject.toml`](pyproject.toml). Build with
`python3 -m build`. Production consumers should pin a reviewed tag or commit rather than
`@main`. Release history is maintained in [`CHANGELOG.md`](CHANGELOG.md).
