# Changelog

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
