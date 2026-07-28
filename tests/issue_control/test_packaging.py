from pathlib import Path
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_issue_control_code_and_migrations_ship_in_wheel_and_sdist() -> None:
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    include = pyproject["tool"]["setuptools"]["packages"]["find"]["include"]
    package_data = pyproject["tool"]["setuptools"]["package-data"]
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "issue_control" in include
    assert "issue_control.*" in include
    assert "migrations/*.sql" in package_data["issue_control"]
    assert "recursive-include issue_control/migrations *.sql" in manifest
