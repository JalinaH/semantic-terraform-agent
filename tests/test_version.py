from semantic_terraform_agent import __version__


def test_package_version_is_v1_2_0() -> None:
    assert __version__ == "1.2.0"
