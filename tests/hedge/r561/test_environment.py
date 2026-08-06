from pathlib import Path

from freqtrade.hedge.r561.environment import (
    audit_powershell_source,
    build_dependency_plan,
    proxy_environment,
    validate_install_arguments,
)


def test_dependency_plan_uses_pyproject_and_forbids_no_deps() -> None:
    root = Path(__file__).resolve().parents[3]
    plan = build_dependency_plan(root / "pyproject.toml", selected_groups=("develop",))
    assert plan.install_target == ".[develop]"
    assert "develop" in plan.optional_groups
    assert validate_install_arguments(("pip", "install", ".[develop]")) == ()
    assert "NO_DEPS_FORBIDDEN" in validate_install_arguments(
        ("pip", "install", "--no-deps", ".")
    )


def test_proxy_environment_is_structured_and_powershell_audit_detects_known_traps() -> None:
    assert proxy_environment({"http_proxy": "http://host:1"}) == {
        "HTTP_PROXY": "http://host:1"
    }
    bad = audit_powershell_source('Write-Host "$Name: failed"')
    assert not bad.ready and bad.errors
    good = audit_powershell_source('Write-Host ("{0}: failed" -f $Name)')
    assert good.ready
