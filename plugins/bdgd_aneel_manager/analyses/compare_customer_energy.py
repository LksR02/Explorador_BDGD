from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from osgeo import gdal


MONTH_FIELDS = [f"ENE_{month:02d}" for month in range(1, 13)]


def find_gdb(base: Path, distributor: str, year: int) -> Path:
    roots = list((base / f"{distributor.upper()}_{year}" / "current").glob("*.gdb"))
    if len(roots) != 1:
        raise RuntimeError(f"Esperada uma GDB para {year}; encontradas {len(roots)}")
    return roots[0]


def read_energy(gdb: Path) -> dict[str, tuple[float, ...]]:
    dataset = gdal.OpenEx(str(gdb), gdal.OF_VECTOR | gdal.OF_READONLY)
    layer = dataset.GetLayerByName("UCBT_tab") or dataset.GetLayerByName("UCBT")
    totals: dict[str, list[float]] = defaultdict(lambda: [0.0] * 12)
    for feature in layer:
        identifier = str(feature.GetField("COD_ID") or "").strip()
        if not identifier:
            continue
        for index, field in enumerate(MONTH_FIELDS):
            totals[identifier][index] += float(feature.GetField(field) or 0)
    return {identifier: tuple(values) for identifier, values in totals.items()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", required=True)
    parser.add_argument("--distributor", required=True)
    parser.add_argument("--left-year", type=int, required=True)
    parser.add_argument("--right-year", type=int, required=True)
    args = parser.parse_args(argv)
    base = Path(args.base_dir)
    left = read_energy(find_gdb(base, args.distributor, args.left_year))
    right = read_energy(find_gdb(base, args.distributor, args.right_year))
    common = set(left) & set(right)
    identical = sum(left[key] == right[key] for key in common)
    absolute_error = sum(
        abs(left[key][month] - right[key][month])
        for key in common
        for month in range(12)
    )
    print(json.dumps({
        "left_year": args.left_year,
        "right_year": args.right_year,
        "left_customers": len(left),
        "right_customers": len(right),
        "common_customers": len(common),
        "identical_monthly_profiles": identical,
        "changed_monthly_profiles": len(common) - identical,
        "sum_absolute_monthly_difference": absolute_error,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
