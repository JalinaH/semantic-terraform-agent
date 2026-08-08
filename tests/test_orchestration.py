from __future__ import annotations

from pathlib import Path

from semantic_terraform_agent.models import ModelDiagnosis, ProviderResponse
from semantic_terraform_agent.orchestration.diagnose import diagnose_repository


class FakeProvider:
    def __init__(self) -> None:
        self.request = None

    def diagnose(self, request):
        self.request = request
        return ProviderResponse(
            diagnosis=ModelDiagnosis(
                root_cause="The mode value violates the provider constraint.",
                affected_resources=["example_widget.primary"],
                violated_constraint="mode must be safe",
                suggested_patch=(
                    "--- a/infrastructure/main.tf\n+++ b/infrastructure/main.tf\n"
                    "@@ -2 +2 @@\n-  mode = \"fast\"\n+  mode = \"safe\""
                ),
                confidence=0.9,
                evidence=[
                    {"source": "terraform_error", "detail": "mode is invalid"},
                    {"source": "git_diff", "detail": "mode changed to fast"},
                    {"source": "terraform_source", "detail": "resource sets mode"},
                ],
            )
        )


def test_lightweight_end_to_end_without_live_api(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    provider = FakeProvider()
    result = diagnose_repository(
        repo_path=terraform_repo,
        terraform_dir=Path("infrastructure"),
        log_file=failure_log,
        diff_file=diff_file,
        provider_name="gemini",
        model="fake",
        context_mode="lightweight",
        llm_provider=provider,
    )
    assert result.status == "ok"
    assert result.context.selected_mode == "lightweight"
    assert result.terraform.schema_extraction_status == "not-requested"
    assert result.repository.changed_terraform_files == ["infrastructure/main.tf"]
    assert result.diagnosis.model_confidence == 0.9
    assert result.diagnosis.evidence_score == 1.0
    assert provider.request.schemas == []

