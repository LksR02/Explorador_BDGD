from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path


def state_path(base_dir: str | Path, name: str) -> Path:
    return Path(base_dir) / name / "sync.json"


def read_state(base_dir: str | Path, name: str) -> dict:
    path = state_path(base_dir, name)
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def write_state(base_dir: str | Path, name: str, values: dict) -> None:
    path = state_path(base_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def find_gdb(base_dir: str | Path, name: str) -> Path | None:
    root = Path(base_dir) / name / "current"
    return next((p for p in root.rglob("*.gdb") if p.is_dir()), None) if root.exists() else None


def safe_extract(zip_path: str | Path, target_dir: str | Path) -> Path:
    archive, target = Path(zip_path), Path(target_dir).resolve()
    staging = target.with_name(target.name + ".extracting")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                destination = (staging / member.filename).resolve()
                if destination != staging and staging not in destination.parents:
                    raise ValueError(f"Entrada insegura no ZIP: {member.filename}")
            bundle.extractall(staging)
        if target.exists():
            backup = target.with_name(target.name + ".previous")
            if backup.exists():
                shutil.rmtree(backup)
            os.replace(target, backup)
            try:
                os.replace(staging, target)
            except Exception:
                os.replace(backup, target)
                raise
            shutil.rmtree(backup)
        else:
            os.replace(staging, target)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    gdb = next((p for p in target.rglob("*.gdb") if p.is_dir()), None)
    if gdb is None:
        raise ValueError("O ZIP não contém uma File Geodatabase (.gdb).")
    return gdb
