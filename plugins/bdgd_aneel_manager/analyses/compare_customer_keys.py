from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from osgeo import gdal


KEY_FIELDS = ["COD_ID", "PAC", "PN_CON", "UNI_TR_MT", "CTMT"]
FINGERPRINT_DEFINITIONS = {
    "physical_location": ["PN_CON", "UNI_TR_MT", "CTMT"],
    "connection_customer": ["PN_CON", "DAT_CON", "CLAS_SUB", "GRU_TAR", "TEN_FORN", "FAS_CON"],
    "connection_profile": [
        "PN_CON",
        "MUN",
        "CLAS_SUB",
        "GRU_TAR",
        "TIP_CC",
        "FAS_CON",
        "GRU_TEN",
        "TEN_FORN",
    ],
}


def find_gdb(base: Path, distributor: str, year: int) -> Path:
    roots = list((base / f"{distributor.upper()}_{year}" / "current").glob("*.gdb"))
    if len(roots) != 1:
        raise RuntimeError(f"Esperada uma GDB para {year}; encontradas {len(roots)}")
    return roots[0]


def normalized(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return format(value, ".12g")
    return str(value).strip()


def read_table(gdb: Path, include_complete_rows: bool = False) -> dict:
    dataset = gdal.OpenEx(str(gdb), gdal.OF_VECTOR | gdal.OF_READONLY)
    if dataset is None:
        raise RuntimeError(f"Não foi possível abrir {gdb}")
    layer = dataset.GetLayerByName("UCBT_tab") or dataset.GetLayerByName("UCBT")
    if layer is None:
        raise RuntimeError(f"UCBT não encontrada em {gdb}")
    available = [field.name for field in layer.schema]
    keys = {field: Counter() for field in KEY_FIELDS if field in available}
    fingerprint_fields = {
        name: [field for field in fields if field in available]
        for name, fields in FINGERPRINT_DEFINITIONS.items()
    }
    fingerprints = {name: Counter() for name in fingerprint_fields}
    complete_rows = Counter()
    for feature in layer:
        for field, counts in keys.items():
            value = normalized(feature.GetField(field))
            if value:
                counts[value] += 1
        for name, fields in fingerprint_fields.items():
            fingerprint = tuple(normalized(feature.GetField(field)) for field in fields)
            if fingerprint and fingerprint[0]:
                fingerprints[name][fingerprint] += 1
        if include_complete_rows:
            complete_rows[tuple(normalized(feature.GetField(field)) for field in available)] += 1
    return {
        "available": available,
        "keys": keys,
        "fingerprint_fields": fingerprint_fields,
        "fingerprints": fingerprints,
        "complete_rows": complete_rows,
    }


def counter_overlap(left: Counter, right: Counter) -> dict:
    left_values = set(left)
    right_values = set(right)
    common = left_values & right_values
    return {
        "left_unique": len(left_values),
        "right_unique": len(right_values),
        "common_unique": len(common),
        "left_unique_once": sum(count == 1 for count in left.values()),
        "right_unique_once": sum(count == 1 for count in right.values()),
        "common_unique_once_both": sum(left[value] == 1 and right[value] == 1 for value in common),
        "retention_from_left": len(common) / len(left_values) if left_values else 0,
        "coverage_of_right": len(common) / len(right_values) if right_values else 0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", required=True)
    parser.add_argument("--distributor", required=True)
    parser.add_argument("--left-year", type=int, required=True)
    parser.add_argument("--right-year", type=int, required=True)
    args = parser.parse_args(argv)
    base = Path(args.base_dir)
    left = read_table(find_gdb(base, args.distributor, args.left_year))
    right = read_table(find_gdb(base, args.distributor, args.right_year))
    shared_fields = [field for field in left["available"] if field in right["available"]]
    exact_schema = left["available"] == right["available"]
    result = {
        "left_year": args.left_year,
        "right_year": args.right_year,
        "same_schema_and_field_order": exact_schema,
        "shared_field_count": len(shared_fields),
        "key_overlap": {},
    }
    for field in KEY_FIELDS:
        if field in left["keys"] and field in right["keys"]:
            result["key_overlap"][field] = counter_overlap(left["keys"][field], right["keys"][field])
    if left["fingerprint_fields"] == right["fingerprint_fields"]:
        result["fingerprint_overlap"] = {
            name: {
                "fields": left["fingerprint_fields"][name],
                **counter_overlap(left["fingerprints"][name], right["fingerprints"][name]),
            }
            for name in left["fingerprint_fields"]
        }
    if exact_schema and left["complete_rows"] and right["complete_rows"]:
        common_rows = left["complete_rows"] & right["complete_rows"]
        result["exact_complete_rows"] = {
            "left_rows": sum(left["complete_rows"].values()),
            "right_rows": sum(right["complete_rows"].values()),
            "common_rows_with_multiplicity": sum(common_rows.values()),
            "identical_multiset": left["complete_rows"] == right["complete_rows"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
