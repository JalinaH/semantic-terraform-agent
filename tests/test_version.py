from semantic_terraform_agent import __version__


def test_package_version_is_v1_1_2() -> None:
    assert __version__ == "1.1.3"
