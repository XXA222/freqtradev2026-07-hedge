from pathlib import Path

from freqtrade.hedge.r561.environment import audit_powershell_source


def test_project_docker_sources_are_python312_and_cross_shell_safe() -> None:
    root = Path(__file__).resolve().parents[3]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "python:3.12" in dockerfile
    assert "--no-deps" not in dockerfile
    assert "pip check" in dockerfile
    assert "sys.version_info[:2] == (3, 12)" in dockerfile
    assert "--pause=false" in (
        root / "scripts/Run-Freqtrade-Hedge-R5.6.1-Docker-Commit.ps1"
    ).read_text(encoding="utf-8")
    for script in (root / "scripts").glob("*R5.6.1*Docker*.ps1"):
        report = audit_powershell_source(script.read_text(encoding="utf-8"))
        assert report.ready, (script, report.errors)


def test_compose_publishes_dashboard_only_on_host_loopback_and_bootstraps_config():
    from pathlib import Path

    project = Path(__file__).resolve().parents[3]
    compose = (project / "docker/docker-compose-hedge-r561.yml").read_text(encoding="utf-8")
    entrypoint = (project / "docker/entrypoint-hedge-r561.sh").read_text(encoding="utf-8")
    assert '"127.0.0.1:8080:8080"' in compose
    assert "config_hedge_r561_docker_dryrun.template.json" in compose
    assert "bootstrap-config" in entrypoint
    assert "--upstream-schema" in entrypoint
