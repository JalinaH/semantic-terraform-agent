from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from semantic_terraform_agent.cache.fingerprint import (
    build_failure_fingerprint,
    canonical_hash,
    derive_repository_scope,
    schema_cache_key,
)
from semantic_terraform_agent.cache.models import VerifiedFailureEntry
from semantic_terraform_agent.context import ContextBuilder
from semantic_terraform_agent.collectors.failure_log import collect_failure_log
from semantic_terraform_agent.collectors.git_diff import collect_diff
from semantic_terraform_agent.collectors.repository import discover_repository, read_source_files
from semantic_terraform_agent.terraform.resources import detect_resources
from semantic_terraform_agent.cache.store import CacheStoreError, LocalCacheStore
from semantic_terraform_agent.models import (
    ModelDiagnosis,
    ProviderResponse,
    SchemaRecord,
    TerraformInfo,
    TokenUsage,
    VerificationAttempt,
    VerificationCommands,
)
from semantic_terraform_agent.orchestration.diagnose import diagnose_repository


PATCH = (
    "--- a/infrastructure/main.tf\n+++ b/infrastructure/main.tf\n"
    "@@ -2 +2 @@\n-  mode = \"fast\"\n+  mode = \"safe\""
)


class CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def diagnose(self, request):
        self.calls += 1
        return ProviderResponse(
            diagnosis=ModelDiagnosis(
                root_cause="The exact mode is invalid.",
                affected_resources=["example_widget.primary"],
                violated_constraint="mode must be safe",
                suggested_patch=PATCH,
                confidence=0.9,
                evidence=[
                    {"source": "terraform_error", "detail": "mode invalid"},
                    {"source": "terraform_source", "detail": "exact resource"},
                    {"source": "git_diff", "detail": "mode changed"},
                ],
            ),
            token_usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )

    def repair(self, request):  # pragma: no cover - the fixtures never repair
        raise AssertionError("unexpected repair")


def _attempt(patch: str, attempt: int, status: str = "verified") -> VerificationAttempt:
    return VerificationAttempt(
        attempt=attempt,
        patch=patch,
        status=status,
        failed_stage="patch_check" if status != "verified" else None,
        commands=VerificationCommands(),
        temporary_copy_cleaned=True,
    )


def _run(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    provider: CountingProvider,
    store: LocalCacheStore,
    verifier,
):
    lock_file = terraform_repo / "infrastructure/.terraform.lock.hcl"
    if not lock_file.exists():
        lock_file.write_text(
            'provider "registry.terraform.io/example/example" {\n  version = "1.2.3"\n}\n',
            encoding="utf-8",
        )
    return diagnose_repository(
        repo_path=terraform_repo,
        terraform_dir=Path("infrastructure"),
        log_file=failure_log,
        diff_file=diff_file,
        provider_name="openrouter",
        model="test/model:free",
        context_mode="lightweight",
        llm_provider=provider,
        patch_verifier=verifier,
        max_repair_attempts=0,
        cache_store=store,
        failure_memory_enabled=True,
        repository_id="owner/repository",
    )


def test_warm_verified_memory_uses_zero_llm_calls_and_fresh_verification(
    terraform_repo: Path, failure_log: Path, diff_file: Path, tmp_path: Path
) -> None:
    store = LocalCacheStore(tmp_path / "cache")
    provider = CountingProvider()
    verification_calls = 0

    def verifier(patch, layout, *, attempt):
        nonlocal verification_calls
        verification_calls += 1
        return _attempt(patch, attempt)

    cold = _run(terraform_repo, failure_log, diff_file, provider, store, verifier)
    warm = _run(terraform_repo, failure_log, diff_file, provider, store, verifier)

    assert cold.resolution_source == "llm"
    assert cold.cache.failure_memory.status == "miss"
    assert cold.cache.failure_memory.write_status == "stored"
    assert warm.resolution_source == "verified_failure_memory"
    assert warm.cache.failure_memory.status == "hit_verified"
    assert warm.cache.failure_memory.reuse_attempt.candidate_source == "verified_failure_memory"
    assert warm.verified_patch.candidate_source == "verified_failure_memory"
    assert warm.verified_patch.patch_sha256 is not None
    assert warm.mutation_eligibility.eligible is False
    assert warm.mutation_eligibility.reason_code == "not_verified"
    assert warm.llm_calls == []
    assert warm.llm_usage.call_count == 0
    assert warm.llm_usage.input_tokens == 0
    assert warm.llm_usage.cost_usd == 0.0
    assert warm.model_progression is None
    assert provider.calls == 1
    assert verification_calls == 2


def test_warm_hit_bypasses_registry_and_provider_construction(
    terraform_repo: Path, failure_log: Path, diff_file: Path, tmp_path: Path
) -> None:
    store = LocalCacheStore(tmp_path / "cache")
    provider = CountingProvider()
    verifier = lambda patch, layout, *, attempt: _attempt(patch, attempt)  # noqa: E731
    _run(terraform_repo, failure_log, diff_file, provider, store, verifier)

    def forbidden_factory(provider_name, model):
        raise AssertionError("provider construction must not occur on a warm hit")

    result = diagnose_repository(
        repo_path=terraform_repo,
        terraform_dir=Path("infrastructure"),
        log_file=failure_log,
        diff_file=diff_file,
        provider_name="openrouter",
        model="test/model:free",
        context_mode="lightweight",
        provider_factory=forbidden_factory,
        patch_verifier=verifier,
        cache_store=store,
        failure_memory_enabled=True,
        repository_id="owner/repository",
    )
    assert result.resolution_source == "verified_failure_memory"


def test_changed_relevant_source_invalidates_memory(
    terraform_repo: Path, failure_log: Path, diff_file: Path, tmp_path: Path
) -> None:
    store = LocalCacheStore(tmp_path / "cache")
    provider = CountingProvider()
    def verifier(patch, layout, *, attempt):
        return _attempt(patch, attempt)
    _run(terraform_repo, failure_log, diff_file, provider, store, verifier)
    main = terraform_repo / "infrastructure/main.tf"
    main.write_text(main.read_text().replace('mode = "fast"', 'mode = "slow"'))

    changed = _run(terraform_repo, failure_log, diff_file, provider, store, verifier)

    assert changed.resolution_source == "llm"
    assert provider.calls == 2


def test_provider_lock_change_invalidates_memory(
    terraform_repo: Path, failure_log: Path, diff_file: Path, tmp_path: Path
) -> None:
    store = LocalCacheStore(tmp_path / "cache")
    provider = CountingProvider()

    def verifier(patch, layout, *, attempt):
        return _attempt(patch, attempt)

    _run(terraform_repo, failure_log, diff_file, provider, store, verifier)
    lock_file = terraform_repo / "infrastructure/.terraform.lock.hcl"
    lock_file.write_text(lock_file.read_text().replace("1.2.3", "1.2.4"))
    result = _run(terraform_repo, failure_log, diff_file, provider, store, verifier)

    assert result.cache.failure_memory.status == "miss"
    assert result.resolution_source == "llm"
    assert provider.calls == 2


def test_stale_memory_falls_back_with_full_model_budget(
    terraform_repo: Path, failure_log: Path, diff_file: Path, tmp_path: Path
) -> None:
    store = LocalCacheStore(tmp_path / "cache")
    provider = CountingProvider()
    _run(
        terraform_repo,
        failure_log,
        diff_file,
        provider,
        store,
        lambda patch, layout, *, attempt: _attempt(patch, attempt),
    )
    calls = 0

    def verifier(patch, layout, *, attempt):
        nonlocal calls
        calls += 1
        return _attempt(patch, attempt, "rejected" if calls == 1 else "verified")

    result = _run(terraform_repo, failure_log, diff_file, provider, store, verifier)

    assert result.resolution_source == "llm"
    assert result.cache.failure_memory.status == "hit_stale"
    assert result.cache.failure_memory.reuse_attempt.status == "rejected"
    assert result.verified_patch.candidate_source == "llm"
    assert result.llm_usage.call_count == 1
    assert provider.calls == 2
    assert calls == 2


def test_failed_or_unverified_candidate_is_never_persisted(
    terraform_repo: Path, failure_log: Path, diff_file: Path, tmp_path: Path
) -> None:
    store = LocalCacheStore(tmp_path / "cache")
    provider = CountingProvider()
    result = _run(
        terraform_repo,
        failure_log,
        diff_file,
        provider,
        store,
        lambda patch, layout, *, attempt: _attempt(patch, attempt, "rejected"),  # noqa: E731
    )
    assert result.diagnosis.verification_status == "patch_rejected"
    assert result.cache.failure_memory.write_status == "not_attempted"
    assert store.stats()["failure_memory_entries"] == 0


def test_secret_shaped_patch_is_not_persisted(
    terraform_repo: Path, failure_log: Path, diff_file: Path, tmp_path: Path
) -> None:
    class SecretProvider(CountingProvider):
        def diagnose(self, request):
            response = super().diagnose(request)
            secret_patch = response.diagnosis.model_copy(
                update={
                    "suggested_patch": PATCH.replace(
                        'mode = "safe"', 'mode = "AKIA1234567890ABCDEF"'
                    )
                }
            )
            return response.model_copy(update={"diagnosis": secret_patch})

    store = LocalCacheStore(tmp_path / "cache")
    result = _run(
        terraform_repo,
        failure_log,
        diff_file,
        SecretProvider(),
        store,
        lambda patch, layout, *, attempt: _attempt(patch, attempt),  # noqa: E731
    )
    assert result.diagnosis.verification_status == "verified_first_attempt"
    assert result.cache.failure_memory.write_status == "not_attempted"
    assert store.stats()["failure_memory_entries"] == 0


def test_schema_cache_keys_are_version_and_lock_sensitive() -> None:
    base = dict(
        terraform_version="1.9.0",
        provider_lock_hash="a" * 64,
        source_fingerprint=canonical_hash({"main.tf": "resource exact"}),
        resource_types=["example_widget"],
    )
    key = schema_cache_key(**base)
    assert key == schema_cache_key(**base)
    assert key != schema_cache_key(**{**base, "terraform_version": "1.10.0"})
    assert key != schema_cache_key(**{**base, "provider_lock_hash": "b" * 64})


def test_provider_schema_and_slice_cache_hit_without_failure_memory(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = LocalCacheStore(tmp_path / "cache")
    provider = CountingProvider()
    inspections = 0

    def inspect(layout, resource_types, *, enabled):
        nonlocal inspections
        inspections += 1
        return (
            TerraformInfo(
                version="1.9.0",
                schema_extraction_status="ok",
                schemas=[
                    SchemaRecord(
                        resource_type="example_widget",
                        provider_source="registry.terraform.io/example/example",
                        provider_version="1.2.3",
                        extraction_status="ok",
                        schema={
                            "version": 1,
                            "block": {
                                "attributes": {
                                    "mode": {"type": "string", "required": True}
                                }
                            },
                        },
                    )
                ],
            ),
            [],
        )

    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas", inspect
    )
    settings = dict(
        repo_path=terraform_repo,
        terraform_dir=Path("infrastructure"),
        log_file=failure_log,
        diff_file=diff_file,
        provider_name="openrouter",
        model="test/model:free",
        context_mode="schema-aware",
        llm_provider=provider,
        patch_verifier=lambda patch, layout, *, attempt: _attempt(patch, attempt),  # noqa: E731
        max_repair_attempts=0,
        cache_store=store,
        failure_memory_enabled=False,
    )
    first = diagnose_repository(**settings)
    second = diagnose_repository(**settings)

    assert inspections == 1
    assert first.cache.provider_schema.status == "miss"
    assert first.cache.provider_schema.write_status == "stored"
    assert second.cache.provider_schema.status == "hit"
    assert first.cache.schema_slice.write_status == "stored"
    assert second.cache.schema_slice.status == "hit"


def test_failure_fingerprint_is_deterministic_and_policy_scoped(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    failure = collect_failure_log(failure_log)
    diff = collect_diff(layout, diff_file)
    sources = read_source_files(layout, layout.terraform_files)
    resources = detect_resources(failure, sources, diff.changed_files, diff.changed_lines)
    context = ContextBuilder().build(
        repository=layout,
        failure=failure,
        diff=diff,
        all_sources=sources,
        detected_resources=resources,
        mode="lightweight",
    )
    scope = derive_repository_scope(terraform_repo, "owner/repository")
    values = dict(
        failure=failure,
        context=context,
        repository_scope=scope,
        terraform_version="1.9.0",
        provider_lock_fingerprint="a" * 64,
        terraform_source_fingerprint=canonical_hash(sources),
    )
    first = build_failure_fingerprint(**values)
    assert first.value == build_failure_fingerprint(**values).value
    assert first.value != build_failure_fingerprint(
        **{**values, "terraform_version": "1.10.0"}
    ).value
    assert first.value != build_failure_fingerprint(
        **{**values, "context_policy_version": "future_policy"}
    ).value
    assert scope != derive_repository_scope(terraform_repo, "other/repository")


def test_corrupt_cache_entry_fails_closed_without_exposing_contents(tmp_path: Path) -> None:
    store = LocalCacheStore(tmp_path / "cache")
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "INSERT INTO failure_memory "
            "(fingerprint, repository_scope, entry_json, created_at) VALUES (?, ?, ?, ?)",
            ("a" * 64, "b" * 64, '{"secret":"do-not-expose"}', "now"),
        )
    with pytest.raises(CacheStoreError, match="could not be read") as error:
        store.get_failure("a" * 64)
    assert "do-not-expose" not in str(error.value)


def test_cache_clear_is_bounded_and_rejects_dangerous_roots(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        LocalCacheStore(Path.home())
    repository_root = tmp_path / "repository"
    (repository_root / ".git").mkdir(parents=True)
    with pytest.raises(ValueError):
        LocalCacheStore(repository_root)
    with pytest.raises(ValueError):
        LocalCacheStore(tmp_path / "child" / ".." / "cache")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    linked_store = LocalCacheStore(link / "cache")
    assert linked_store.cache_dir == (target / "cache").resolve()
    store = LocalCacheStore(tmp_path / "cache")
    marker = store.cache_dir / "unrelated.txt"
    marker.write_text("preserved")
    before = store.clear()
    assert before["failure_memory_entries"] == 0
    assert marker.read_text() == "preserved"


def test_duplicate_memory_preserves_first_verified_entry(tmp_path: Path) -> None:
    store = LocalCacheStore(tmp_path / "cache")
    diagnosis = CountingProvider().diagnose(None).diagnosis
    candidate = {
        "root_cause": diagnosis.root_cause,
        "affected_resources": diagnosis.affected_resources,
        "violated_constraint": diagnosis.violated_constraint,
        "suggested_patch": diagnosis.suggested_patch,
        "model_confidence": diagnosis.confidence,
        "evidence": diagnosis.evidence,
    }
    base = dict(
        fingerprint_version="verified_failure_v1",
        fingerprint="a" * 64,
        repository_scope="b" * 64,
        created_at=VerifiedFailureEntry.timestamp(),
        agent_version="1.0.0",
        failure_signature="exact failure",
        failed_stage="plan",
        candidate_patch=PATCH,
        diagnosis=candidate,
        evidence_score=1.0,
        verification_status="verified_first_attempt",
    )
    assert store.put_failure(VerifiedFailureEntry(**base)) is True
    assert store.put_failure(
        VerifiedFailureEntry(**{**base, "candidate_patch": "replacement"})
    ) is False
    assert store.get_failure("a" * 64).candidate_patch == PATCH


def test_oversized_memory_entry_is_rejected(tmp_path: Path) -> None:
    store = LocalCacheStore(tmp_path / "cache")
    diagnosis = CountingProvider().diagnose(None).diagnosis
    entry = VerifiedFailureEntry(
        fingerprint_version="verified_failure_v1",
        fingerprint="a" * 64,
        repository_scope="b" * 64,
        created_at=VerifiedFailureEntry.timestamp(),
        agent_version="1.0.0",
        failure_signature="exact failure",
        failed_stage="plan",
        candidate_patch="x" * (2 * 1024 * 1024),
        diagnosis={
            "root_cause": diagnosis.root_cause,
            "affected_resources": diagnosis.affected_resources,
            "violated_constraint": diagnosis.violated_constraint,
            "suggested_patch": diagnosis.suggested_patch,
            "model_confidence": diagnosis.confidence,
            "evidence": diagnosis.evidence,
        },
        evidence_score=1.0,
        verification_status="verified_first_attempt",
    )
    with pytest.raises(CacheStoreError, match="size limit"):
        store.put_failure(entry)
