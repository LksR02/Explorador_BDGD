from __future__ import annotations

import argparse
import json
from pathlib import Path

from osgeo import gdal, ogr


MONTH_FIELDS = [f"ENE_{month:02d}" for month in range(1, 13)]


def profile_year(gdb: Path) -> tuple[dict, set[str]]:
    dataset = gdal.OpenEx(str(gdb), gdal.OF_VECTOR | gdal.OF_READONLY)
    if dataset is None:
        raise RuntimeError(f"Não foi possível abrir {gdb}")
    layer = dataset.GetLayerByName("UCBT_tab") or dataset.GetLayerByName("UCBT")
    if layer is None:
        raise RuntimeError(f"UCBT não encontrada em {gdb}")
    available = {field.name for field in layer.schema}
    if "COD_ID" not in available:
        raise RuntimeError(f"COD_ID ausente em {gdb}")
    months = [field for field in MONTH_FIELDS if field in available]
    identifiers: set[str] = set()
    rows = 0
    duplicates = 0
    active = 0
    null_months = 0
    zero_months = 0
    annual_energy = 0.0
    for feature in layer:
        rows += 1
        identifier = str(feature.GetField("COD_ID") or "").strip()
        if identifier:
            if identifier in identifiers:
                duplicates += 1
            identifiers.add(identifier)
        if "SIT_ATIV" in available and feature.GetField("SIT_ATIV") == "AT":
            active += 1
        for field in months:
            value = feature.GetField(field)
            if value is None:
                null_months += 1
            else:
                numeric = float(value)
                annual_energy += numeric
                if numeric == 0:
                    zero_months += 1
    return ({
        "rows": rows,
        "unique_ids": len(identifiers),
        "duplicate_ids": duplicates,
        "active_rows": active,
        "month_fields": months,
        "null_customer_months": null_months,
        "zero_customer_months": zero_months,
        "annual_energy": annual_energy,
    }, identifiers)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", required=True)
    parser.add_argument("--distributor", required=True)
    parser.add_argument("--start-year", type=int, required=True)
    parser.add_argument("--end-year", type=int, required=True)
    args = parser.parse_args(argv)
    base = Path(args.base_dir)
    previous: set[str] | None = None
    previous_year = None
    result = {"years": {}, "transitions": []}
    for year in range(args.start_year, args.end_year + 1):
        roots = list((base / f"{args.distributor.upper()}_{year}" / "current").glob("*.gdb"))
        if len(roots) != 1:
            raise RuntimeError(f"Esperada uma GDB para {year}; encontradas {len(roots)}")
        summary, identifiers = profile_year(roots[0])
        result["years"][str(year)] = summary
        if previous is not None:
            overlap = previous & identifiers
            union = previous | identifiers
            result["transitions"].append({
                "from": previous_year,
                "to": year,
                "common_ids": len(overlap),
                "retention_from_previous": len(overlap) / len(previous) if previous else 0,
                "coverage_of_current": len(overlap) / len(identifiers) if identifiers else 0,
                "jaccard": len(overlap) / len(union) if union else 0,
            })
        previous, previous_year = identifiers, year
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
