"""Python ABI, dependency-plan, proxy-environment and cross-shell source gates."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sys
import tomllib
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class PythonEnvironmentReport:
    ready: bool
    version: str
    executable: str
    import_root: str | None
    reasons: tuple[str, ...]


def validate_python_environment(
    *,
    required_major_minor: tuple[int, int] = (3, 12),
    executable: str | None = None,
    import_root: str | None = None,
    expected_import_root: str | Path | None = None,
) -> PythonEnvironmentReport:
    current = sys.version_info[:2]
    reasons: list[str] = []
    if tuple(current) != required_major_minor:
        reasons.append(
            f"PYTHON_ABI_MISMATCH:expected={required_major_minor[0]}.{required_major_minor[1]}:"
            f"actual={current[0]}.{current[1]}"
        )
    exe = str(Path(executable or sys.executable).resolve())
    root = None if import_root is None else str(Path(import_root).resolve())
    if expected_import_root is not None:
        expected = Path(expected_import_root).resolve()
        if root is None:
            reasons.append("IMPORT_ROOT_UNAVAILABLE")
        elif expected not in Path(root).parents and Path(root) != expected:
            reasons.append(f"IMPORT_ROOT_MISMATCH:expected={expected}:actual={root}")
    return PythonEnvironmentReport(
        ready=not reasons,
        version=f"{current[0]}.{current[1]}",
        executable=exe,
        import_root=root,
        reasons=tuple(reasons),
    )


@dataclass(frozen=True, slots=True)
class DependencyPlan:
    core_dependencies: tuple[str, ...]
    optional_groups: tuple[str, ...]
    selected_groups: tuple[str, ...]
    install_target: str


def build_dependency_plan(
    pyproject_path: str | Path,
    *,
    selected_groups: Sequence[str] = ("develop",),
) -> DependencyPlan:
    payload = tomllib.loads(Path(pyproject_path).read_text(encoding="utf-8"))
    project = payload.get("project", {})
    core = tuple(str(item) for item in project.get("dependencies", ()))
    optional = project.get("optional-dependencies", {})
    available = tuple(sorted(str(name) for name in optional))
    selected = tuple(dict.fromkeys(str(name) for name in selected_groups))
    unknown = sorted(set(selected) - set(available))
    if unknown:
        raise ValueError(f"unknown dependency extras: {', '.join(unknown)}")
    target = "." if not selected else ".[" + ",".join(selected) + "]"
    return DependencyPlan(core, available, selected, target)


def validate_install_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
    lowered = tuple(str(item).lower() for item in arguments)
    reasons: list[str] = []
    if "--no-deps" in lowered:
        reasons.append("NO_DEPS_FORBIDDEN")
    if not any(item in {".", "-e", "--editable"} or item.startswith(".[") for item in lowered):
        reasons.append("PROJECT_INSTALL_TARGET_MISSING")
    return tuple(reasons)


def proxy_environment(env: Mapping[str, str]) -> dict[str, str]:
    names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    result: dict[str, str] = {}
    for name in names:
        value = env.get(name) or env.get(name.lower())
        if value:
            result[name] = str(value)
    return result


@dataclass(frozen=True, slots=True)
class PowerShellSourceAudit:
    ready: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


_VARIABLE_COLON = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*:")
_COMPLEX_PYTHON_C = re.compile(r"(?i)python(?:\.exe)?\s+-c\s+[\"'].*[\"']")
_BASH_REGEX = re.compile(r"(?i)bash\s+-lc.*grep\s+-E?i?.*[()]" )


def audit_powershell_source(text: str) -> PowerShellSourceAudit:
    errors: list[str] = []
    warnings: list[str] = []
    valid_scopes = {"$env:", "$global:", "$script:", "$local:", "$private:"}
    for match in _VARIABLE_COLON.finditer(text):
        token = match.group(0)
        if token.lower() in valid_scopes:
            continue
        errors.append(f"POWERSHELL_AMBIGUOUS_VARIABLE_COLON:{token}")
    if _COMPLEX_PYTHON_C.search(text):
        warnings.append("MULTILAYER_PYTHON_C_QUOTING")
    if _BASH_REGEX.search(text):
        warnings.append("MULTILAYER_BASH_REGEX_QUOTING")
    return PowerShellSourceAudit(not errors, tuple(errors), tuple(warnings))
