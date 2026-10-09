from __future__ import annotations

import argparse
import os
import shutil
import sys
import urllib.request
from pathlib import Path

from .catalog import CatalogItem, load_catalog
from .storage import find_gdb, safe_extract, write_state


def remote_size(item: CatalogItem) -> int:
    request = urllib.request.Request(item.download_url, method="HEAD")
    with urllib.request.urlopen(request, timeout=60) as response:
        return int(response.headers.get("Content-Length", "0"))


def download(item: CatalogItem, base_dir: Path) -> Path:
    folder = base_dir / item.friendly_name
    folder.mkdir(parents=True, exist_ok=True)
    final_path = folder / f"{item.friendly_name}.gdb.zip"
    part_path = final_path.with_suffix(final_path.suffix + ".part")
    expected = remote_size(item)
    if final_path.exists() and (not expected or final_path.stat().st_size == expected):
        print(f"[{item.year}] ZIP já completo ({final_path.stat().st_size / 1048576:.1f} MiB)", flush=True)
        return final_path

    existing = part_path.stat().st_size if part_path.exists() else 0
    headers = {"Range": f"bytes={existing}-"} if existing else {}
    request = urllib.request.Request(item.download_url, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response:
        append = existing > 0 and response.status == 206
        if not append:
            existing = 0
        mode = "ab" if append else "wb"
        received = existing
        next_report = received + 10 * 1048576
        with part_path.open(mode) as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                received += len(chunk)
                if received >= next_report:
                    pct = (received * 100 / expected) if expected else 0
                    print(f"[{item.year}] {received / 1048576:.1f} MiB ({pct:.0f}%)", flush=True)
                    next_report = received + 10 * 1048576
    if expected and part_path.stat().st_size != expected:
        raise IOError(f"Download incompleto de {item.year}: {part_path.stat().st_size}/{expected} bytes")
    os.replace(part_path, final_path)
    return final_path


def sync_item(item: CatalogItem, base_dir: Path) -> None:
    if find_gdb(base_dir, item.friendly_name):
        print(f"[{item.year}] geodatabase já sincronizada", flush=True)
        return
    archive = download(item, base_dir)
    free = shutil.disk_usage(base_dir if base_dir.exists() else base_dir.parent).free
    if free < archive.stat().st_size * 2:
        raise OSError(f"Espaço livre insuficiente para extrair {item.friendly_name}")
    print(f"[{item.year}] extraindo…", flush=True)
    gdb = safe_extract(archive, base_dir / item.friendly_name / "current")
    write_state(base_dir, item.friendly_name, {
        "item_id": item.item_id,
        "original_title": item.original_title,
        "reference_date": item.reference_date,
        "modified": item.modified_raw,
        "friendly_name": item.friendly_name,
        "gdb_path": str(gdb),
    })
    print(f"[{item.year}] concluído: {gdb}", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sincroniza série anual BDGD da ANEEL")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--distributor", required=True)
    parser.add_argument("--start-year", type=int, required=True)
    parser.add_argument("--end-year", type=int, required=True)
    parser.add_argument("--base-dir", required=True)
    args = parser.parse_args(argv)

    wanted = [
        item for item in load_catalog(args.catalog)
        if item.distributor == args.distributor.upper()
        and args.start_year <= item.year <= args.end_year
    ]
    by_year = {item.year: item for item in wanted}
    missing = [year for year in range(args.start_year, args.end_year + 1) if year not in by_year]
    if missing:
        print(f"Anos ausentes no catálogo: {missing}", file=sys.stderr)
        return 2
    base_dir = Path(args.base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    for year in range(args.start_year, args.end_year + 1):
        sync_item(by_year[year], base_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
