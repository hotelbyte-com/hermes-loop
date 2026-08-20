"""Static contract checks for the two public installers."""

from pathlib import Path
import json
import os
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_unix_installer_has_typed_readiness_and_direct_launch():
    source = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
    assert "doctor --ready --json" in source
    assert '"$INSTALL_DIR/venv/bin/hermes"' in source
    assert 'exec "$(readiness_command)"' in source
    assert '"name":"readiness"' in source
    assert 'rm -rf venv' not in source
    assert "venv.broken." in source
    assert "refusing unverified dependency resolution" in source
    assert "Skipping gateway startup (install-only mode)" in source
    assert "invalid or contradictory receipt" in source
    assert "Next command: hermes setup" in source


def test_windows_installer_has_equivalent_readiness_and_direct_launch():
    source = (ROOT / "scripts/install.ps1").read_text(encoding="utf-8")
    assert "doctor --ready --json" in source
    assert "$InstallDir\\venv\\Scripts\\hermes.exe" in source
    assert "& $hermesCmd" in source
    assert 'Name = "readiness"' in source
    assert "Virtual environment already usable, keeping it" in source
    assert "venv.broken." in source
    assert "refusing unverified dependency resolution" in source
    assert "Skipping gateway startup (install-only mode)" in source
    assert "invalid or contradictory receipt" in source
    assert "Next command: hermes setup" in source


def test_installers_reject_venv_interpreters_outside_project_constraint():
    unix_source = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
    windows_source = (ROOT / "scripts/install.ps1").read_text(encoding="utf-8")

    assert "python_version_supported" in unix_source
    assert "(3, 14)" in unix_source
    assert "$PythonFallbackVersions = @(\"3.12\", \"3.13\")" in windows_source
    assert "function Test-SupportedPythonExecutable" in windows_source
    assert "(3, 14)" in windows_source


def _fake_install_tree(tmp_path, receipt, exit_code=0, expected_home=None):
    install_dir = tmp_path / "install"
    hermes_bin = install_dir / "venv" / "bin" / "hermes"
    python_bin = install_dir / "venv" / "bin" / "python"
    hermes_bin.parent.mkdir(parents=True)
    # A symlink to sys.executable loses the venv context when resolved from a
    # different directory.  Use a thin wrapper script so the real interpreter
    # keeps its own site-packages (yaml, hermes_cli, etc.).
    python_bin.write_text(
        f"#!/bin/sh\nexec '{sys.executable}' \"$@\"\n", encoding="utf-8"
    )
    home_guard = ""
    if expected_home is not None:
        home_guard = f"[ \"${{HERMES_HOME:-}}\" = '{expected_home}' ] || exit 88\n"
    hermes_bin.write_text(
        "#!/bin/sh\n"
        + home_guard
        + f"printf '%s\\n' '{json.dumps(receipt, separators=(',', ':'))}'\n"
        + f"exit {exit_code}\n",
        encoding="utf-8",
    )
    python_bin.chmod(0o755)
    hermes_bin.chmod(0o755)
    return install_dir


def _run_readiness_stage(tmp_path, receipt, exit_code=0, extra_args=()):
    hermes_home = tmp_path / "home"
    install_dir = _fake_install_tree(tmp_path, receipt, exit_code, hermes_home)
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/install.sh"),
            "--dir",
            str(install_dir),
            "--hermes-home",
            str(hermes_home),
            "--stage",
            "readiness",
            "--json",
            *extra_args,
        ],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
    )
    return result


def _ready_receipt():
    return {
        "schema_version": 1,
        "status": "ready",
        "ready": True,
        "checks": [
            {"name": "config", "status": "ok", "detail": "configuration is readable"},
            {"name": "runtime", "status": "ok", "detail": "runtime is importable"},
            {"name": "provider", "status": "ok", "detail": "provider is reachable"},
            {"name": "gateway", "status": "optional_absent", "detail": "optional"},
        ],
        "gateway": {"status": "optional_absent", "optional": True},
    }


def test_unix_readiness_stage_executes_embedded_receipt_validator(tmp_path):
    result = _run_readiness_stage(tmp_path, _ready_receipt())
    assert result.returncode == 0, result.stderr + result.stdout
    frame = json.loads(result.stdout.strip().splitlines()[-1])
    assert frame == {"ok": True, "stage": "readiness", "skipped": False}


def test_unix_readiness_stage_rejects_malformed_receipt_before_launch(tmp_path):
    malformed = _ready_receipt()
    del malformed["gateway"]
    result = _run_readiness_stage(tmp_path, malformed)
    assert result.returncode == 1
    frame = json.loads(result.stdout.strip().splitlines()[-1])
    assert frame["ok"] is False
    assert "invalid or contradictory receipt" in result.stdout


def test_unix_readiness_stage_rejects_contradictory_ready_exit(tmp_path):
    result = _run_readiness_stage(tmp_path, _ready_receipt(), exit_code=1)
    assert result.returncode == 1
    frame = json.loads(result.stdout.strip().splitlines()[-1])
    assert frame["ok"] is False


def test_unix_noninteractive_gateway_stage_is_install_only(tmp_path):
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / ".env").write_text("TELEGRAM_BOT_TOKEN=configured\n", encoding="utf-8")
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/install.sh"),
            "--dir",
            str(install_dir),
            "--hermes-home",
            str(tmp_path / "home"),
            "--stage",
            "gateway",
            "--non-interactive",
            "--json",
        ],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "install-only mode" in result.stdout


def test_unix_noninteractive_setup_stage_names_exact_recovery(tmp_path):
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/install.sh"),
            "--dir",
            str(install_dir),
            "--hermes-home",
            str(tmp_path / "home"),
            "--stage",
            "setup",
            "--non-interactive",
            "--json",
        ],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "install-only mode" in result.stdout
    assert "Next command: hermes setup" in result.stdout
