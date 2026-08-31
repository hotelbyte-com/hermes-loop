"""README contract tests for the supported one-click collaboration lanes."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_engineer_commands_and_readiness_contract_are_identical():
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    required = (
        "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash",
        "iex (irm https://hermes-agent.nousresearch.com/install.ps1)",
        "hermes doctor --ready --json",
        "incomplete_setup",
        "optional_absent",
        "HERMES_HOME",
        "--skip-setup",
        "--non-interactive",
    )
    for marker in required:
        assert marker in english, marker
        assert marker in chinese, marker


def test_readme_separates_desktop_and_docker_lanes():
    for name in ("README.md", "README.zh-CN.md"):
        content = (ROOT / name).read_text(encoding="utf-8")
        assert "Desktop" in content
        assert "Docker" in content or "docker" in content
        assert "--insecure" in content
        assert "gateway" in content.lower()


def test_readme_does_not_require_shell_reload_for_first_launch():
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    assert "source ~/.bashrc" not in english
    assert "source ~/.bashrc" not in chinese
