from semantic_terraform_agent import __version__


def test_package_version_is_v0_9_0() -> None:
    assert __version__ == "0.9.0"
