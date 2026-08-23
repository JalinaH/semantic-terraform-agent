from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from semantic_terraform_agent.cache import LocalCacheStore
from semantic_terraform_agent.models import (
    ModelDiagnosis,
    PatchFailureCategory,
    ProviderResponse,
    SchemaRecord,
    TerraformInfo,
    TokenUsage,
    VerificationCommand,
)
from semantic_terraform_agent.orchestration.diagnose import diagnose_repository
from semantic_terraform_agent.reasoning.prompts import build_repair_prompt_parts
from semantic_terraform_agent.terraform import verification as verification_module


CONCATENATED_PATCH = (
    "--- a/infrastructure/main.tf+++ b/infrastructure/main.tf"
    '@@ -2 +2 @@-  mode = "fast"+  mode = "safe"'
)
VALID_PATCH = (
    "--- a/infrastructure/main.tf\n"
    "+++ b/infrastructure/main.tf\n"
    "@@ -2 +2 @@\n"
    '-  mode = "fast"\n'
    '+  mode = "safe"\n'
)


def _diagnosis(patch: str) -> ModelDiagnosis:
    return ModelDiagnosis(
        root_cause="The mode violates the provider constraint.",
        affected_resources=["example_widget.primary"],
        violated_constraint="mode must be safe",
        suggested_patch=patch,
        confidence=0.9,
        evidence=[
            {"source": "terraform_error", "detail": "mode is invalid"},
            {"source": "terraform_source", "detail": "resource sets mode"},
            {"source": "git_diff", "detail": "mode changed"},
        ],
    )


class MalformedThenRepairedProvider:
    def __init__(self) -> None:
        self.diagnose_calls = 0
        self.repair_calls = 0
        self.repair_request = None

    def diagnose(self, request):
        self.diagnose_calls += 1
        return ProviderResponse(
            diagnosis=_diagnosis(CONCATENATED_PATCH),
            token_usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )

    def repair(self, request):
        self.repair_calls += 1
        self.repair_request = request
        return ProviderResponse(
            diagnosis=_diagnosis(VALID_PATCH),
            token_usage=TokenUsage(input_tokens=8, output_tokens=4, total_tokens=12),
        )


def _commit(root: Path) -> str:
    for command in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "tests@example.invalid"],
        ["git", "config", "user.name", "Tests"],
        ["git", "add", "."],
        ["git", "commit", "-m", "fixture"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_live_concatenated_patch_repairs_same_model_without_schema(
    monkeypatch,
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    tmp_path: Path,
) -> None:
    commit = _commit(terraform_repo)
    provider = MalformedThenRepairedProvider()
    cache = LocalCacheStore(tmp_path / "cache")
    commands: list[list[str]] = []

    def passed(actual, recorded, **kwargs):
        commands.append(recorded)
        return VerificationCommand(command=recorded, status="passed", exit_code=0)

    monkeypatch.setattr(verification_module, "find_git", lambda: "/usr/bin/git")
    monkeypatch.setattr(verification_module, "find_terraform", lambda: "/usr/bin/terraform")
    monkeypatch.setattr(verification_module, "_run_command", passed)
    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("format repair must not retrieve schema")
        ),
    )

    result = diagnose_repository(
        repo_path=terraform_repo,
        terraform_dir=Path("infrastructure"),
        log_file=failure_log,
        diff_file=diff_file,
        provider_name="openrouter",
        model="test/free-model",
        context_mode="auto",
        llm_provider=provider,
        max_repair_attempts=1,
        source_revision=commit,
        cache_store=cache,
        failure_memory_enabled=True,
        repository_id="owner/repository",
    )

    assert provider.diagnose_calls == 1
    assert provider.repair_calls == 1
    assert result.llm_usage.call_count == 2
    assert len(result.llm_calls) == 2
    assert result.llm_calls[1].call_type.value == "repair"
    assert result.llm_calls[1].repair_reason == "malformed_patch"
    assert result.diagnosis.attempts[0].failure_category is (
        PatchFailureCategory.MALFORMED_REPAIRABLE
    )
    assert result.diagnosis.attempts[0].failure_reason_code == "concatenated_diff"
    assert result.diagnosis.verification_status == "verified_after_retry"
    assert result.diagnosis.final_patch == VALID_PATCH
    assert result.diagnosis.repair_reason == "malformed_patch"
    assert provider.repair_request.repair_reason == "malformed_patch"
    repair_prompt = build_repair_prompt_parts(provider.repair_request)
    assert "Do not rediagnose" in repair_prompt.system
    assert "concatenated_diff" in repair_prompt.user
    assert repair_prompt.user.count(CONCATENATED_PATCH) == 1

    assert result.context_progression.initial_level.value == "minimal"
    assert result.context_progression.final_level.value == "minimal"
    assert result.context_progression.escalated is False
    assert result.context_progression.repair_reason == "malformed_patch"
    assert result.context_progression.schema_retrieval_attempted is False
    assert result.context_progression.schema_retrieved is False
    assert result.context_progression.schema_avoided is True
    assert result.context_progression.schema_avoidance_reason == (
        "successful_minimal_verification"
    )
    assert result.model_progression.model_escalated is False
    assert result.model_progression.initial_model == result.model_progression.final_model

    assert [command[:2] for command in commands] == [
        ["git", "apply"],
        ["git", "apply"],
        ["terraform", "fmt"],
        ["terraform", "init"],
        ["terraform", "validate"],
        ["terraform", "plan"],
    ]
    assert result.verified_patch.patch_sha256 == hashlib.sha256(
        VALID_PATCH.encode("utf-8")
    ).hexdigest()
    assert result.verified_patch.affected_files == ["infrastructure/main.tf"]
    assert result.verified_patch.verified_against_commit_sha == commit
    assert result.mutation_eligibility.eligible is True
    assert result.cache.failure_memory.write_status == "stored"
    fingerprint = result.cache.failure_memory.fingerprint
    assert fingerprint is not None
    stored = cache.get_failure(fingerprint)
    assert stored is not None
    assert stored.candidate_patch == VALID_PATCH
    assert CONCATENATED_PATCH not in stored.candidate_patch


def test_second_malformed_patch_stops_without_third_call(
    monkeypatch,
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    tmp_path: Path,
) -> None:
    provider = MalformedThenRepairedProvider()
    provider.repair = lambda request: ProviderResponse(  # type: ignore[method-assign]
        diagnosis=_diagnosis("```diff\nmalformed\n```"),
        token_usage=TokenUsage(input_tokens=8, output_tokens=4, total_tokens=12),
    )
    result = diagnose_repository(
        repo_path=terraform_repo,
        terraform_dir=Path("infrastructure"),
        log_file=failure_log,
        diff_file=diff_file,
        provider_name="openrouter",
        model="test/free-model",
        context_mode="lightweight",
        llm_provider=provider,
        max_repair_attempts=1,
        cache_store=LocalCacheStore(tmp_path / "cache"),
        failure_memory_enabled=True,
        repository_id="owner/repository",
    )
    assert result.llm_usage.call_count == 2
    assert result.diagnosis.verification_status == "patch_rejected"
    assert len(result.diagnosis.attempts) == 2
    assert result.diagnosis.attempts[-1].failure_reason_code == "markdown_fence_leak"
    assert result.mutation_eligibility.eligible is False
    assert result.cache.failure_memory.write_status == "not_attempted"


def test_explicit_schema_aware_malformed_repair_reuses_initial_schema(
    monkeypatch,
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
) -> None:
    provider = MalformedThenRepairedProvider()
    schema_calls = 0

    def inspect(*args, **kwargs):
        nonlocal schema_calls
        schema_calls += 1
        return TerraformInfo(
            version="1.9.0",
            schema_extraction_status="ok",
            schemas=[
                SchemaRecord(
                    resource_type="example_widget",
                    extraction_status="ok",
                    schema={
                        "version": 0,
                        "block": {
                            "attributes": {
                                "mode": {"type": "string", "required": True}
                            }
                        },
                    },
                )
            ],
        ), []

    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas", inspect
    )
    monkeypatch.setattr(verification_module, "find_git", lambda: "/usr/bin/git")
    monkeypatch.setattr(verification_module, "find_terraform", lambda: "/usr/bin/terraform")
    monkeypatch.setattr(
        verification_module,
        "_run_command",
        lambda actual, recorded, **kwargs: VerificationCommand(
            command=recorded, status="passed", exit_code=0
        ),
    )

    result = diagnose_repository(
        repo_path=terraform_repo,
        terraform_dir=Path("infrastructure"),
        log_file=failure_log,
        diff_file=diff_file,
        provider_name="openrouter",
        model="test/free-model",
        context_mode="schema-aware",
        llm_provider=provider,
        max_repair_attempts=1,
    )
    assert schema_calls == 1
    assert provider.repair_request.original.context_level.value == "schema"
    assert provider.repair_request.original.schema_slices
    assert result.context_progression.escalated is False
    assert result.context_progression.initial_level.value == "schema"
    assert result.context_progression.final_level.value == "schema"
    assert result.llm_calls[1].context_level.value == "schema"


def test_auto_unsafe_patch_stops_and_does_not_claim_schema_avoidance(
    monkeypatch,
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
) -> None:
    provider = MalformedThenRepairedProvider()
    unsafe_patch = "--- a/../../secret.tf\n+++ b/../../secret.tf\n@@ -1 +1 @@\n-a\n+b\n"
    provider.diagnose = lambda request: ProviderResponse(  # type: ignore[method-assign]
        diagnosis=_diagnosis(unsafe_patch),
        token_usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
    )
    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe patch must not retrieve schema")
        ),
    )
    result = diagnose_repository(
        repo_path=terraform_repo,
        terraform_dir=Path("infrastructure"),
        log_file=failure_log,
        diff_file=diff_file,
        provider_name="openrouter",
        model="test/free-model",
        context_mode="auto",
        llm_provider=provider,
        max_repair_attempts=1,
    )
    assert result.llm_usage.call_count == 1
    assert provider.repair_calls == 0
    assert result.diagnosis.verification_status == "patch_rejected"
    assert result.diagnosis.attempts[0].failure_category is PatchFailureCategory.UNSAFE
    assert result.context_progression.stop_reason == "unsafe_patch"
    assert result.context_progression.schema_avoided is None
    assert result.context_progression.schema_avoidance_reason == (
        "verification_stopped_before_schema_decision"
    )
