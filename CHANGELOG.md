# Changelog

## 1.2.0

- Add explicit `local` and `full` verification modes while retaining `full` as the
  backward-compatible default.
- Treat patch check/application plus Terraform fmt, init, and validate as the distinct
  successful `locally_validated` outcome when cloud verification is not configured.
- Make locally validated patches conditionally mutation-eligible without weakening
  provenance checks or admitting them to Verified Failure Memory.

## 1.1.6

- Route Terraform precondition, postcondition, module-output precondition, and
  check-block assertion failures directly to source-backed semantic repair.
- Select bounded Terraform source by plan diagnostic file and line, and fall back
  to it when provider-schema escalation produces no usable schema.

## 1.1.5

- Classify Terraform resource precondition, postcondition, module-output
  precondition, and check-block assertion failures as semantic plan failures.
- Include the bounded structured plan diagnostic in the existing second-call repair
  evidence so semantic candidates can be revised without changing diagnosis fields.

## 1.1.4

- Make Terraform plan the authoritative final verification gate and expose a bounded,
  redacted deterministic plan-failure diagnostic.
- Classify plan failures locally as Terraform semantic, credentials, permissions,
  network, provider unavailable, external service, runtime environment, or unknown.
- Add an additive verification assessment with explicit full, environment-blocked,
  semantic, patch-invalid, and fail-closed unknown outcomes.
- Allow conditional mutation eligibility only for confidently environmental plan
  failures after every existing provenance, scope, and pre-plan verification gate passes.
- Keep Verified Failure Memory restricted to complete plan success and avoid model repair
  calls for environmental or unknown plan failures.

## 1.1.3

- Recognize OpenRouter's current "requested parameters" incompatibility response and
  fall back to schema-guided JSON without API-enforced structured output.
- Retry one schema-invalid completion with the bounded JSON fallback prompt before
  returning `response_invalid`.

## 1.1.2

- Preserve the first-pass semantic diagnosis as immutable throughout repair,
  escalation, result serialization, and verified-memory persistence.
- Add a bounded structured semantic edit contract for existing Terraform files.
- Construct unified diffs deterministically with Git from exact source replacements.
- Convert malformed legacy diffs through an edit-only repair response instead of
  asking the model to serialize another patch.
- Reduce repair output surface and retain the exact verified patch as the reusable
  provenance and cache artifact.

## 1.1.1

- Classify patch failures deterministically as repairable malformed, unsafe,
  semantic verification, environment, or unknown failures.
- Allow malformed unified-diff serialization to consume the existing bounded
  same-model repair call without schema escalation.
- Preserve terminal behavior for unsafe paths, files, operations, binary patches,
  and symlink targets.
- Report schema avoidance only after successful minimal-context verification.

## 1.1.0

- Added a stable verified-patch artifact with exact UTF-8 SHA-256 provenance.
- Added deterministic repository-relative affected-file and Terraform-only manifests.
- Added detected Git commit/tree provenance and optional strict `--source-revision`
  validation before model inference.
- Added current-source fingerprints and concise isolated-verification provenance.
- Added conservative mutation eligibility for future human-approved platform flows;
  only verified modifications to existing Terraform files from a clean Git revision
  qualify.
- Recompute provenance and eligibility after fresh verification of a Verified Failure
  Memory candidate.
- Added no GitHub, branch, repository, or Terraform mutation behavior.

## 1.0.1

- Made OpenRouter and `openrouter/free` the CLI and reusable-workflow defaults.
- Removed the standalone benchmark workflow, comparison launchers, generated artifacts,
  and duplicate release notes from the product repository.
- Replaced historical benchmark documentation with a focused operator README.
- Excluded retained regression-evaluation helpers from the production wheel.

## 1.0.0

- Added exact repository-scoped Verified Failure Memory with mandatory fresh isolated verification.
- Added versioned bounded SQLite caches for provider schemas and deterministic schema slices.
- Added hit/miss/stale/error telemetry, zero-call provenance, and historical avoided-usage fields.
- Added safe cache stats/clear commands and opt-in reusable-workflow hooks.
- Added cold/warm comparison reports without fabricated provider metrics.
- Preserved v0.9 routing behavior and the two-semantic-call ceiling on fallback.
