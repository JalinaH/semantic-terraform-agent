from semantic_terraform_agent import __version__


def test_package_version_is_v1_0_1() -> None:
    assert __version__ == "1.0.1"
