#!/usr/bin/env python3
"""Create a non-mutating candidate for the next Freqtrade Hedge rebase."""
from __future__ import annotations
import argparse, hashlib, json, shutil
from pathlib import Path

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def locate_state(script: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    project = script.resolve().parents[2]
    for candidate in (project / "hedge_port", project):
        if (candidate / "recipes/PORT_RECIPES.json").is_file() and (candidate / "manifests/PORT_FILE_CLASSES.json").is_file():
            return candidate
    raise FileNotFoundError("Cannot locate Hedge port recipes/manifests")

def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--new-upstream",type=Path,required=True)
    parser.add_argument("--current-ported",type=Path,required=True)
    parser.add_argument("--state-root",type=Path)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    state=locate_state(Path(__file__),args.state_root)
    recipes=json.loads((state/"recipes/PORT_RECIPES.json").read_text(encoding="utf-8"))
    classes=json.loads((state/"manifests/PORT_FILE_CLASSES.json").read_text(encoding="utf-8"))
    shared={item["path"]:item for item in recipes["shared_files"]}
    if args.output.exists():shutil.rmtree(args.output)
    shutil.copytree(args.new_upstream,args.output,ignore=shutil.ignore_patterns(".git",".venv","venv","__pycache__",".pytest_cache",".ruff_cache"))
    rows=[]
    for relative in classes["modular_files"]:
        source=args.current_ported/relative; destination=args.output/relative
        if not source.is_file():
            rows.append({"path":relative,"status":"MISSING_FROM_CURRENT_PORT"});continue
        destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,destination)
        rows.append({"path":relative,"status":"MODULAR_COPIED"})
    for relative,recipe in shared.items():
        upstream=args.new_upstream/relative
        text=upstream.read_text(encoding="utf-8",errors="ignore") if upstream.is_file() else ""
        rows.append({"path":relative,"status":"SHARED_REBASE_REQUIRED","mode":recipe["mode"],
                     "upstream_sha256":sha(upstream) if upstream.is_file() else None,
                     "anchors_present":{anchor:anchor in text for anchor in recipe.get("anchors",[])}})
    report={"schema_version":"hedge-rebase-assistant-v1","new_upstream":str(args.new_upstream),
            "current_ported":str(args.current_ported),"candidate":str(args.output),"state_root":str(state),"items":rows}
    path=args.output/"HEDGE_REBASE_REPORT.json"
    path.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(path);return 0
if __name__=="__main__":raise SystemExit(main())
