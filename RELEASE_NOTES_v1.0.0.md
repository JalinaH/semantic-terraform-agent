# Semantic Terraform Agent 1.0.0

Version 1.0.0 introduces Verified Failure Memory. An exact repository-scoped failure can
reuse a previously verified patch only after fresh isolated Terraform verification on
the current checkout. Successful warm reuse performs zero model calls; stale, corrupt,
ambiguous, or ineligible entries fall back to the complete v0.9 diagnosis flow.

Provider-schema and schema-slice caches remain separate deterministic artifacts. This
release adds no semantic similarity matching, billing, arbitrary file inclusion,
Terraform apply, or extra model attempt.

MVP evolution: v0.5 added provider neutrality, OpenRouter, and usage/cost telemetry;
v0.6 deterministic minimal context; v0.7 schema slicing; v0.8 progressive context;
v0.9 cost-aware routing; and v1.0 exact verified memory, deterministic caching, and
release stabilization. No unmeasured performance claim is made.
