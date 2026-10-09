"""Painel mensal de demanda para uma distribuidora."""
from __future__ import annotations

import argparse
import csv
import gzip
import heapq
import json
import math
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from osgeo import gdal

gdal.UseExceptions()


MONTH_FIELDS = [f"ENE_{month:02d}" for month in range(1, 13)]
from .variable_catalog import EXTRA_FIELDS

CUSTOMER_FIELDS = ["PN_CON", "PAC", "UNI_TR_MT", "CTMT", "MUN", "CLAS_SUB", "GRU_TAR", "SIT_ATIV", *EXTRA_FIELDS]
MIN_ANALYSIS_YEAR = 2017
MAX_ANALYSIS_YEAR = 2025
DEFAULT_CONFIDENCE = 0.99
DEFAULT_MARGIN_ERROR = 0.01
AUTO_SAMPLE_THRESHOLD = 250_000
MIN_PER_STRATUM = 10
Z_SCORES = {0.90: 1.6448536269514722, 0.95: 1.959963984540054, 0.99: 2.5758293035489004}
CONSUMPTION_BANDS = (
    "SEM_CONSUMO",
    "ATE_100_KWH_MES",
    "101_500_KWH_MES",
    "501_2000_KWH_MES",
    "2001_10000_KWH_MES",
    "ACIMA_10000_KWH_MES",
)
CONSUMPTION_LIMITS = np.array([0.0, 100.0, 500.0, 2_000.0, 10_000.0], dtype=np.float64)
DEFAULT_STRATA_FIELDS = ("patamar_consumo", "GRU_TAR", "MUNICIPIO")
STRATUM_FIELDS = {
    "patamar_consumo": ("Patamar de consumo", None),
    "GRU_TAR": ("Grupo tarifário", "GRU_TAR"),
    "MUNICIPIO": ("Município", "MUN"),
    "CLAS_SUB": ("Classe e subclasse", "CLAS_SUB"),
    "TIP_CC": ("Tipologia de curva de carga", "TIP_CC"),
    "ARE_LOC": ("Área de localização", "ARE_LOC"),
    "FAS_CON": ("Fases de conexão", "FAS_CON"),
    "SIT_ATIV": ("Situação de ativação", "SIT_ATIV"),
    "SUB": ("Subestação", "SUB"),
    "CONJ": ("Conjunto elétrico", "CONJ"),
    "CTMT": ("Alimentador", "CTMT"),
}
OUTPUT_LEVELS = {
    "distributor": ("Distribuidora", None, "distributor"),
    "municipality": ("Município", "MUNICIPIO", "municipio"),
    "substation": ("Subestação", "SUB", "sub"),
    "set": ("Conjunto elétrico", "CONJ", "conj"),
    "feeder": ("Alimentador", "CTMT", "ctmt"),
    "transformer": ("Transformador", "UNI_TR_MT", "untrmt"),
    "connection": ("Ponto de conexão", "PN_CON", "pncon"),
    "customer": ("Consumidor", "COD_ID", "customer_panel"),
}
DEFAULT_OUTPUT_LEVELS = ("distributor", "feeder", "customer")
IBGE_MUNICIPALITIES_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios?orderBy=nome"
IBGE_CACHE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
GD_WITH_REGISTRY = "Com indicação em CEG_GD"
GD_WITHOUT_REGISTRY = "Sem indicação em CEG_GD"


class AnalysisCancelled(RuntimeError):
    pass


def gd_registry_status(value) -> str:
    """Classify the registry field without claiming physical absence of generation."""
    text = str(value or "").strip()
    if not text:
        return GD_WITHOUT_REGISTRY
    digits = "".join(character for character in text if character.isdigit())
    if digits and not any(character != "0" for character in digits):
        return GD_WITHOUT_REGISTRY
    return GD_WITH_REGISTRY


def _check_cancel(cancel_callback) -> None:
    if cancel_callback and cancel_callback():
        raise AnalysisCancelled("Análise cancelada pelo usuário.")


def _emit_progress(progress_callback, **payload) -> None:
    if progress_callback:
        progress_callback(payload)


def _gdb(base: Path, distributor: str, year: int) -> Path:
    roots = list((base / f"{distributor.upper()}_{year}" / "current").glob("*.gdb"))
    if len(roots) != 1:
        raise RuntimeError(f"Esperada uma GDB para {year}; encontradas {len(roots)}")
    return roots[0]


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _number(value) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_municipality_code(value) -> str:
    """Normalize BDGD municipality values to the seven-digit IBGE code."""
    code = _text(value)
    if not code:
        return ""
    if code.endswith(".0"):
        code = code[:-2]
    digits = "".join(character for character in code if character.isdigit())
    if not digits:
        return ""
    return digits.zfill(7) if len(digits) < 7 else digits


def _municipality_uf(item: dict) -> str:
    micro = item.get("microrregiao") or {}
    meso = micro.get("mesorregiao") or {}
    uf = meso.get("UF") or {}
    if uf.get("sigla"):
        return str(uf["sigla"])
    immediate = item.get("regiao-imediata") or {}
    intermediate = immediate.get("regiao-intermediaria") or {}
    return str((intermediate.get("UF") or {}).get("sigla") or "")


def _municipality_map_from_payload(payload) -> dict[str, str]:
    if not isinstance(payload, list):
        raise ValueError("Resposta da API do IBGE não contém uma lista de municípios.")
    municipalities = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        code = normalize_municipality_code(item.get("id"))
        name = _text(item.get("nome"))
        if not code or not name:
            continue
        uf = _municipality_uf(item)
        municipalities[code] = f"{name} ({uf})" if uf else name
    if len(municipalities) < 5_000:
        raise ValueError(f"Resposta incompleta da API do IBGE: {len(municipalities)} municípios.")
    return municipalities


def load_ibge_municipalities(cache_path: Path, max_age_seconds: int = IBGE_CACHE_MAX_AGE_SECONDS):
    """Load official municipality names from IBGE, with a local fallback cache."""
    cache_path = Path(cache_path)
    cached = None
    if cache_path.exists():
        try:
            cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
            cached = {
                normalize_municipality_code(code): _text(name)
                for code, name in (cached_payload.get("municipalities") or {}).items()
                if normalize_municipality_code(code) and _text(name)
            }
            cache_age = max(0.0, time.time() - cache_path.stat().st_mtime)
            if len(cached) >= 5_000 and cache_age <= max_age_seconds:
                return cached, {"source": "cache", "count": len(cached), "cache": str(cache_path)}
        except (OSError, ValueError, TypeError):
            cached = None

    try:
        request = urllib.request.Request(
            IBGE_MUNICIPALITIES_URL,
            headers={"User-Agent": "bdgd-aneel-manager/0.4"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            raw_payload = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip" or raw_payload.startswith(b"\x1f\x8b"):
                raw_payload = gzip.decompress(raw_payload)
            municipalities = _municipality_map_from_payload(json.loads(raw_payload.decode("utf-8")))
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps({
                "source": IBGE_MUNICIPALITIES_URL,
                "fetched_at": datetime.now().astimezone().isoformat(),
                "municipalities": municipalities,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _publish_temp_file(temp_path, cache_path)
        return municipalities, {"source": "api", "count": len(municipalities), "cache": str(cache_path)}
    except (OSError, ValueError, TypeError, urllib.error.URLError) as exc:
        if cached and len(cached) >= 5_000:
            return cached, {
                "source": "stale_cache",
                "count": len(cached),
                "cache": str(cache_path),
                "warning": f"API do IBGE indisponível; cache local utilizado: {exc}",
            }
        raise RuntimeError(
            "Não foi possível consultar a API de Localidades do IBGE e não há cache válido disponível."
        ) from exc


def municipality_name(value, municipalities: dict[str, str]) -> str:
    code = normalize_municipality_code(value)
    if not code:
        return "SEM_MUNICIPIO"
    return municipalities.get(code, f"Código IBGE {code}")


def sample_size(population: int, confidence: float = DEFAULT_CONFIDENCE, margin_error: float = DEFAULT_MARGIN_ERROR) -> int:
    """Conservative finite-population sample size for a proportion (p=0.5)."""
    if population <= 0:
        return 0
    if not 0 < margin_error < 1:
        raise ValueError("A margem de erro deve estar entre 0 e 1.")
    try:
        z = Z_SCORES[round(confidence, 2)]
    except KeyError as exc:
        raise ValueError("Confiança suportada: 90%, 95% ou 99%.") from exc
    initial = (z * z * 0.25) / (margin_error * margin_error)
    corrected = initial / (1 + (initial - 1) / population)
    return min(population, math.ceil(corrected))


def consumption_band(monthly_average: np.ndarray) -> np.ndarray:
    values = np.nan_to_num(monthly_average, nan=0.0, posinf=np.inf, neginf=0.0)
    values = np.maximum(values, 0.0)
    return np.searchsorted(CONSUMPTION_LIMITS, values, side="left").astype(np.uint8)


def allocate_strata(populations: dict[tuple[int, str, str], int], target: int) -> dict[tuple[int, str, str], int]:
    """Proportional allocation with a small floor so rare combinations appear."""
    total = sum(populations.values())
    if total <= 0 or target <= 0:
        return {key: 0 for key in populations}
    quotas = {}
    for key, size in populations.items():
        proportional = int(round(target * size / total))
        quotas[key] = min(size, max(min(MIN_PER_STRATUM, size), proportional))
    return quotas


def _decode(value) -> str:
    if value is None or np.ma.is_masked(value):
        return ""
    if isinstance(value, (bytes, np.bytes_)):
        return bytes(value).decode("utf-8", errors="replace").strip()
    return str(value).strip()


def _array_values(batch: dict, field: str, length: int, numeric: bool = False) -> np.ndarray:
    value = batch.get(field)
    if value is None:
        return np.zeros(length, dtype=np.float64) if numeric else np.full(length, b"", dtype="S1")
    if np.ma.isMaskedArray(value):
        value = value.filled(0.0 if numeric else b"")
    array = np.asarray(value)
    if numeric:
        return np.nan_to_num(array.astype(np.float64, copy=False), nan=0.0, posinf=0.0, neginf=0.0)
    if array.dtype.kind == "U":
        return np.char.encode(array, "utf-8")
    if array.dtype.kind == "O":
        return np.asarray([_decode(item).encode("utf-8") for item in array])
    return array


def _validate_strata_fields(strata_fields) -> tuple[str, ...]:
    fields = tuple(dict.fromkeys(strata_fields or DEFAULT_STRATA_FIELDS))
    unknown = [field for field in fields if field not in STRATUM_FIELDS]
    if unknown:
        raise ValueError("Estratos desconhecidos: " + ", ".join(unknown))
    if not fields:
        raise ValueError("Selecione pelo menos uma variável de estratificação.")
    return fields


def _strata_for_batch(batch: dict, strata_fields=DEFAULT_STRATA_FIELDS) -> tuple[np.ndarray, np.ndarray]:
    strata_fields = _validate_strata_fields(strata_fields)
    length = len(next(iter(batch.values())))
    values = {}
    if "patamar_consumo" in strata_fields:
        annual = np.zeros(length, dtype=np.float64)
        for field in MONTH_FIELDS:
            annual += _array_values(batch, field, length, numeric=True)
        values["patamar_consumo"] = consumption_band(annual / 12.0)
    for field in strata_fields:
        source_field = STRATUM_FIELDS[field][1]
        if source_field:
            values[field] = _array_values(batch, source_field, length)
    dtype = np.dtype([(field, values[field].dtype) for field in strata_fields])
    strata = np.empty(length, dtype=dtype)
    for field in strata_fields:
        strata[field] = values[field]
    cod = _array_values(batch, "COD_ID", length)
    valid = np.char.str_len(cod) > 0
    return strata, valid


def _stratum_key(value, municipalities: dict[str, str], strata_fields=DEFAULT_STRATA_FIELDS) -> tuple[str, ...]:
    parts = []
    for field in _validate_strata_fields(strata_fields):
        if field == "patamar_consumo":
            parts.append(CONSUMPTION_BANDS[int(value[field])])
        elif field == "MUNICIPIO":
            parts.append(municipality_name(_decode(value[field]), municipalities))
        else:
            parts.append(_decode(value[field]) or f"SEM_{field}")
    return tuple(parts)


def _open_ucbt(gdb):
    if hasattr(gdb, 'open_layer'):
        return gdb.open_layer()
    dataset = gdal.OpenEx(str(gdb), gdal.OF_VECTOR | gdal.OF_READONLY)
    if dataset is None:
        raise RuntimeError(f"Não foi possível abrir {gdb}")
    layer = dataset.GetLayerByName("UCBT_tab") or dataset.GetLayerByName("UCBT")
    if layer is None:
        raise RuntimeError(f"UCBT não encontrada em {gdb}")
    return dataset, layer


def _open_arrow_layer(gdb: Path, fields: set[str]):
    dataset, layer = _open_ucbt(gdb)
    available = {field.name for field in layer.schema}
    required = {"COD_ID", "GRU_TAR", "MUN", *MONTH_FIELDS}
    missing = required - available
    if missing:
        raise RuntimeError(f"Campos ausentes para amostragem: {', '.join(sorted(missing))}")
    ignored = [field for field in available if field not in fields]
    if ignored:
        layer.SetIgnoredFields(ignored)
    return dataset, layer, available


def _population_by_stratum(gdb: Path, municipalities: dict[str, str], strata_fields=DEFAULT_STRATA_FIELDS) -> tuple[dict[tuple[str, ...], int], int]:
    strata_fields = _validate_strata_fields(strata_fields)
    fields = {"COD_ID", *MONTH_FIELDS, *(STRATUM_FIELDS[field][1] for field in strata_fields if STRATUM_FIELDS[field][1])}
    dataset, layer, _ = _open_arrow_layer(gdb, fields)
    populations = defaultdict(int)
    total = 0
    for batch in layer.GetArrowStreamAsNumPy(options=["MAX_FEATURES_IN_BATCH=131072"]):
        strata, valid = _strata_for_batch(batch, strata_fields)
        if not valid.any():
            continue
        unique, counts = np.unique(strata[valid], return_counts=True)
        for value, count in zip(unique, counts):
            populations[_stratum_key(value, municipalities, strata_fields)] += int(count)
            total += int(count)
    del layer, dataset
    return dict(populations), total


def _random_scores(indices: np.ndarray, year: int) -> np.ndarray:
    """Deterministic SplitMix64 scores used by the per-stratum reservoirs."""
    with np.errstate(over="ignore"):
        values = indices.astype(np.uint64) + np.uint64(year * 0x9E3779B1)
        values = (values ^ (values >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        values = (values ^ (values >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        return values ^ (values >> np.uint64(31))


def _prepare_sample_batch(batch: dict) -> dict[str, np.ndarray]:
    length = len(next(iter(batch.values())))
    prepared = {
        field: _array_values(batch, field, length)
        for field in {"COD_ID", *CUSTOMER_FIELDS}
    }
    prepared.update({
        field: _array_values(batch, field, length, numeric=True)
        for field in MONTH_FIELDS
    })
    return prepared


def _sample_record(prepared: dict, index: int, stratum: tuple[int, str, str]) -> dict:
    row = {field: _decode(prepared[field][index]) for field in ["COD_ID", *CUSTOMER_FIELDS]}
    row["energy"] = [float(prepared[field][index]) for field in MONTH_FIELDS]
    row["stratum"] = stratum
    return row


def _grouped_indexes(strata, valid):
    """Group once per batch instead of scanning every row for every stratum."""
    indexes = np.flatnonzero(valid)
    unique, inverse, counts = np.unique(strata[valid], return_inverse=True, return_counts=True)
    grouped = indexes[np.argsort(inverse, kind="stable")]
    start = 0
    for value, count in zip(unique, counts):
        end = start + int(count)
        yield value, grouped[start:end]
        start = end


def read_year_stratified(
    gdb: Path,
    year: int,
    municipalities: dict[str, str],
    confidence: float = DEFAULT_CONFIDENCE,
    margin_error: float = DEFAULT_MARGIN_ERROR,
    strata_fields=DEFAULT_STRATA_FIELDS,
    progress_callback=None,
    cancel_callback=None,
):
    strata_fields = _validate_strata_fields(strata_fields)
    fields = {"COD_ID", *CUSTOMER_FIELDS, *MONTH_FIELDS,
              *(STRATUM_FIELDS[field][1] for field in strata_fields if STRATUM_FIELDS[field][1])}
    dataset, layer, _ = _open_arrow_layer(gdb, fields)
    population_hint = max(1, int(layer.GetFeatureCount()))
    target = sample_size(population_hint, confidence, margin_error)
    threshold = int((target / population_hint) * ((1 << 64) - 1))
    populations = defaultdict(int)
    selected: dict[tuple[str, ...], dict[int, dict]] = defaultdict(dict)
    rare_reservoirs: dict[tuple[str, ...], list] = defaultdict(list)
    offset = 0
    for batch in layer.GetArrowStreamAsNumPy(options=["MAX_FEATURES_IN_BATCH=131072"]):
        _check_cancel(cancel_callback)
        length = len(next(iter(batch.values())))
        strata, valid = _strata_for_batch(batch, strata_fields)
        scores = _random_scores(np.arange(offset, offset + length, dtype=np.uint64), year)
        for value, indexes in _grouped_indexes(strata, valid):
            _check_cancel(cancel_callback)
            key = _stratum_key(value, municipalities, strata_fields)
            populations[key] += len(indexes)
            chosen = indexes[scores[indexes] <= threshold]
            rare_count = min(MIN_PER_STRATUM, len(indexes))
            rare_local = np.argpartition(scores[indexes], rare_count - 1)[:rare_count]
            candidates = np.unique(np.concatenate((chosen, indexes[rare_local])))
            heap = rare_reservoirs[key]
            # Convert wide technical attributes only for reservoir candidates.
            prepared = _prepare_sample_batch({field: values[candidates] for field, values in batch.items()})
            for local_index, index in enumerate(candidates):
                score = int(scores[index])
                serial = offset + int(index)
                keep_rare = len(heap) < MIN_PER_STRATUM or score < -heap[0][0]
                if score > threshold and not keep_rare:
                    continue
                record = _sample_record(prepared, local_index, key)
                if score <= threshold:
                    selected[key][serial] = record
                if len(heap) < MIN_PER_STRATUM:
                    heapq.heappush(heap, (-score, serial, record))
                elif score < -heap[0][0]:
                    heapq.heapreplace(heap, (-score, serial, record))
        offset += length
        _emit_progress(
            progress_callback,
            stage="Amostragem estratificada",
            year=year,
            processed=min(offset, population_hint),
            total=population_hint,
            year_percent=min(100, int(offset * 100 / population_hint)),
            message=f"{year}: classificando e amostrando {min(offset, population_hint):,} de {population_hint:,} registros",
        )
    del layer, dataset

    for key, heap in rare_reservoirs.items():
        for _, serial, record in heap:
            selected[key].setdefault(serial, record)
    population = sum(populations.values())
    sampled_counts = {key: len(rows) for key, rows in selected.items()}
    weights = {key: populations[key] / sampled_counts[key] for key in sampled_counts if sampled_counts[key]}
    customers = {}
    ctmt = defaultdict(lambda: {"energy": [0.0] * 13, "customers": {}, "active": {}})
    for key, records in selected.items():
        weight = weights[key]
        for record in records.values():
            cod = record["COD_ID"]
            customer_key = (year, cod)
            row = customers.get(customer_key)
            if row is None:
                row = {
                    "cliente_ano_id": f"{year}:{cod}", "source_year": year, "COD_ID": cod,
                    **{field: record.get(field, "") for field in CUSTOMER_FIELDS},
                    "MUNICIPIO": municipality_name(record.get("MUN"), municipalities),
                    "patamar_consumo": CONSUMPTION_BANDS[int(consumption_band(
                        np.asarray([sum(record["energy"]) / 12.0]))[0])],
                    "estrato_amostral": " | ".join(key),
                    "peso_amostral": round(weight, 8),
                    **{field: 0.0 for field in MONTH_FIELDS},
                }
                customers[customer_key] = row
            feeder = record.get("CTMT", "")
            bucket = ctmt[feeder]
            bucket["customers"].setdefault(customer_key, weight)
            if record.get("SIT_ATIV", "").upper() == "AT":
                bucket["active"].setdefault(customer_key, weight)
            for month, value in enumerate(record["energy"], 1):
                row[MONTH_FIELDS[month - 1]] += value
                bucket["energy"][month] += value * weight
    for bucket in ctmt.values():
        bucket["customer_count"] = sum(bucket.pop("customers").values())
        bucket["active_count"] = sum(bucket.pop("active").values())
    summary = [
        {
            "source_year": year,
            **dict(zip(strata_fields, key)),
            "populacao": size,
            "amostra": sampled_counts.get(key, 0),
            "peso_amostral": round(weights.get(key, 0.0), 8),
        }
        for key, size in sorted(populations.items())
    ]
    diagnostics = {
        "year": year,
        "method": "stratified",
        "population": population,
        "target_sample": target,
        "actual_sample": sum(sampled_counts.values()),
        "strata": len(populations),
        "scanned_records": offset,
        "excluded_empty_id": offset - population,
        "sampling_fraction": sum(sampled_counts.values()) / population if population else 0.0,
        "covered_strata": len(sampled_counts),
        "census_strata": sum(sampled_counts.get(k, 0) == n for k, n in populations.items()),
        "weight_min": min(weights.values(), default=0.0),
        "weight_max": max(weights.values(), default=0.0),
        "kish_effective_n": (population ** 2 / sum(sampled_counts[k] * w * w for k, w in weights.items())) if population else 0.0,
        "algorithm": "splitmix64_threshold_with_stratum_floor_v1",
        "seed_year": year,
        "precision_note": "Dimensionamento de proporção (p=0,5); não garante margem para energia. Kish mede apenas desigualdade de pesos, sem corrigir dependência.",
        "stratification": list(strata_fields),
    }
    return list(customers.values()), ctmt, summary, diagnostics


def read_year(gdb: Path, year: int, municipalities: dict[str, str], progress_callback=None, cancel_callback=None):
    dataset, layer = _open_ucbt(gdb)
    available = {field.name for field in layer.schema}
    months = [field for field in MONTH_FIELDS if field in available]
    if "COD_ID" not in available or len(months) != 12:
        raise RuntimeError(f"UCBT de {year} não possui COD_ID e os 12 campos ENE")
    # Resolve field indexes once. GetField(name) performs a name lookup for
    # every feature; large UCBT layers make that surprisingly expensive.
    indexes = {field.name: index for index, field in enumerate(layer.schema)}
    month_specs = [(field, indexes[field]) for field in months]
    customer_specs = [(field, indexes[field]) for field in CUSTOMER_FIELDS if field in indexes]
    needed = {"COD_ID", *CUSTOMER_FIELDS, *MONTH_FIELDS}
    ignored = [field for field in available if field not in needed]
    if ignored:
        layer.SetIgnoredFields(ignored)
    customers = {}
    # Customer membership is identical for all 12 months. Keeping one set per
    # feeder instead of one set per feeder/month cuts both allocations and
    # memory substantially while preserving the previous unique-count logic.
    ctmt = defaultdict(lambda: {
        "energy": [0.0] * 13,
        "customers": set(),
        "active": set(),
    })
    total = max(1, int(layer.GetFeatureCount()))
    processed = 0
    for feature in layer:
        processed += 1
        if processed % 10_000 == 0:
            _check_cancel(cancel_callback)
            _emit_progress(
                progress_callback,
                stage="Leitura completa",
                year=year,
                processed=processed,
                total=total,
                year_percent=min(100, int(processed * 100 / total)),
                message=f"{year}: lendo {processed:,} de {total:,} registros",
            )
        cod = _text(feature.GetField(indexes["COD_ID"]))
        if not cod:
            continue
        key = (year, cod)
        row = customers.get(key)
        if row is None:
            row = {
                "cliente_ano_id": f"{year}:{cod}", "source_year": year, "COD_ID": cod,
                **{field: "" for field in CUSTOMER_FIELDS},
                **{field: 0.0 for field in MONTH_FIELDS},
            }
            for field, field_index in customer_specs:
                row[field] = _text(feature.GetField(field_index))
            customers[key] = row
        feeder = _text(feature.GetField(indexes["CTMT"])) if "CTMT" in indexes else ""
        active = _text(feature.GetField(indexes["SIT_ATIV"])).upper() == "AT" if "SIT_ATIV" in indexes else False
        feeder_bucket = ctmt[feeder]
        feeder_bucket["customers"].add(key)
        if active:
            feeder_bucket["active"].add(key)
        for index, (field, field_index) in enumerate(month_specs, 1):
            value = _number(feature.GetField(field_index))
            row[field] += value
            feeder_bucket["energy"][index] += value
    for bucket in ctmt.values():
        bucket["customer_count"] = len(bucket.pop("customers"))
        bucket["active_count"] = len(bucket.pop("active"))
    for row in customers.values():
        average = sum(float(row[field]) for field in MONTH_FIELDS) / 12.0
        band_index = int(consumption_band(np.asarray([average]))[0])
        group = row.get("GRU_TAR") or "SEM_GRU_TAR"
        municipality = municipality_name(row.get("MUN"), municipalities)
        row["MUNICIPIO"] = municipality
        row["patamar_consumo"] = CONSUMPTION_BANDS[band_index]
        row["estrato_amostral"] = " | ".join((CONSUMPTION_BANDS[band_index], group, municipality))
        row["peso_amostral"] = 1.0
    return list(customers.values()), ctmt


def _feature_count(gdb: Path) -> int:
    if hasattr(gdb, 'open_layer'):
        dataset, layer = _open_ucbt(gdb)
        return max(0, int(layer.GetFeatureCount()))
    dataset = gdal.OpenEx(str(gdb), gdal.OF_VECTOR | gdal.OF_READONLY)
    if dataset is None:
        return 0
    layer = dataset.GetLayerByName("UCBT_tab") or dataset.GetLayerByName("UCBT")
    return max(0, int(layer.GetFeatureCount())) if layer is not None else 0


def _publish_temp_file(temp_path: Path, target_path: Path, attempts: int = 6) -> Path:
    """Publish a completed file without discarding it when Windows locks the target."""
    last_error = None
    for attempt in range(attempts):
        try:
            os.replace(temp_path, target_path)
            return target_path
        except PermissionError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.25 * (attempt + 1))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fallback = target_path.with_name(f"{target_path.stem}_recuperado_{stamp}{target_path.suffix}")
    counter = 1
    while fallback.exists():
        fallback = target_path.with_name(
            f"{target_path.stem}_recuperado_{stamp}_{counter}{target_path.suffix}"
        )
        counter += 1
    try:
        os.replace(temp_path, fallback)
    except Exception:
        raise last_error
    return fallback


def _write_csv(path: Path, rows, fields) -> Path:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return _publish_temp_file(temp_path, path)


def _validate_output_levels(output_levels) -> tuple[str, ...]:
    levels = tuple(dict.fromkeys(output_levels or DEFAULT_OUTPUT_LEVELS))
    unknown = [level for level in levels if level not in OUTPUT_LEVELS]
    if unknown:
        raise ValueError("Níveis de saída desconhecidos: " + ", ".join(unknown))
    if not levels:
        raise ValueError("Selecione pelo menos um nível de saída.")
    return levels


def build_outputs(
    base_dir: str | Path,
    distributor: str,
    start_year: int,
    end_year: int,
    output_dir: str | Path | None = None,
    sampling_mode: str = "auto",
    confidence: float = DEFAULT_CONFIDENCE,
    margin_error: float = DEFAULT_MARGIN_ERROR,
    output_levels=DEFAULT_OUTPUT_LEVELS,
    strata_fields=DEFAULT_STRATA_FIELDS,
    progress_callback=None,
    cancel_callback=None,
    sources=None,
    source_metadata=None,
) -> dict:
    if start_year < MIN_ANALYSIS_YEAR or end_year > MAX_ANALYSIS_YEAR or start_year > end_year:
        raise ValueError(
            f"A análise deve usar exclusivamente o período {MIN_ANALYSIS_YEAR}-{MAX_ANALYSIS_YEAR}."
        )
    base = Path(base_dir)
    years = sorted(sources) if sources is not None else list(range(start_year, end_year + 1))
    if not years or years[0] != start_year or years[-1] != end_year:
        raise ValueError('Os anos da fonte não correspondem ao período informado.')
    out = Path(output_dir) if output_dir else base / f"{distributor.upper()}_ANALISES"
    out.mkdir(parents=True, exist_ok=True)
    if sampling_mode not in {"auto", "census", "stratified"}:
        raise ValueError("sampling_mode deve ser auto, census ou stratified.")
    output_levels = _validate_output_levels(output_levels)
    strata_fields = _validate_strata_fields(strata_fields)
    _emit_progress(
        progress_callback,
        stage="Municípios",
        overall_percent=1,
        message="Consultando nomes oficiais dos municípios no IBGE",
    )
    municipalities, municipality_lookup = load_ibge_municipalities(
        base / "_cache" / "municipios_ibge.json"
    )
    monthly_levels = {
        level: defaultdict(lambda: {"energy": 0.0, "customers": 0.0, "active": 0.0})
        for level in output_levels if level != "customer"
    }
    missing_level_rows = defaultdict(int)
    strata_rows = []
    sampling_diagnostics = []
    segment_monthly = defaultdict(lambda: {"energy": 0.0, "customers": 0.0})
    panel_path = out / f"{distributor.upper()}_customer_panel_{start_year}_{end_year}.csv"
    panel_fields = [
        "cliente_ano_id", "source_year", "COD_ID", *CUSTOMER_FIELDS,
        "MUNICIPIO", "patamar_consumo", "estrato_amostral", "peso_amostral", *MONTH_FIELDS,
    ]
    panel_temp = panel_path.with_suffix(panel_path.suffix + ".tmp")
    try:
        with panel_temp.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=panel_fields, extrasaction="ignore")
            writer.writeheader()
            for year_index, year in enumerate(years):
                _check_cancel(cancel_callback)
                year_count = len(years)
                _emit_progress(
                    progress_callback,
                    stage="Preparação",
                    year=year,
                    year_index=year_index,
                    year_count=year_count,
                    year_percent=0,
                    overall_percent=int(year_index * 85 / year_count),
                    message=f"Preparando {distributor.upper()} {year} ({year_index + 1}/{year_count})",
                )
                gdb = sources[year] if sources is not None else _gdb(base, distributor, year)
                feature_count = _feature_count(gdb)
                use_sample = sampling_mode == "stratified" or (
                    sampling_mode == "auto" and feature_count >= AUTO_SAMPLE_THRESHOLD
                )
                if use_sample:
                    rows, ctmt_data, year_strata, diagnostics = read_year_stratified(
                        gdb, year, municipalities, confidence, margin_error,
                        strata_fields=strata_fields,
                        progress_callback=progress_callback,
                        cancel_callback=cancel_callback,
                    )
                    strata_rows.extend(year_strata)
                    sampling_diagnostics.append(diagnostics)
                else:
                    rows, ctmt_data = read_year(
                        gdb,
                        year,
                        municipalities,
                        progress_callback=progress_callback,
                        cancel_callback=cancel_callback,
                    )
                    sampling_diagnostics.append({
                        "year": year,
                        "method": "census",
                        "population": len(rows),
                        "target_sample": len(rows),
                        "actual_sample": len(rows),
                        "strata": 0,
                    })
                _check_cancel(cancel_callback)
                for row in rows:
                    weight = float(row.get("peso_amostral") or 1.0)
                    group = row.get("GRU_TAR") or "SEM_GRU_TAR"
                    municipality = row.get("MUNICIPIO") or "SEM_MUNICIPIO"
                    band = row.get("patamar_consumo") or "SEM_PATAMAR"
                    load_curve_type = row.get("TIP_CC") or "SEM_TIP_CC"
                    gd_status = gd_registry_status(row.get("CEG_GD"))
                    for month, field in enumerate(MONTH_FIELDS, 1):
                        bucket = segment_monthly[(
                            f"{year}-{month:02d}", band, group, municipality,
                            load_curve_type, gd_status,
                        )]
                        bucket["energy"] += float(row.get(field) or 0.0) * weight
                        bucket["customers"] += weight
                    for level, buckets in monthly_levels.items():
                        if level == "feeder":
                            continue
                        _, source_field, _ = OUTPUT_LEVELS[level]
                        value = distributor.upper() if source_field is None else str(row.get(source_field) or "").strip()
                        if not value:
                            missing_level_rows[level] += 1
                            continue
                        active = str(row.get("SIT_ATIV") or "").upper() == "AT"
                        for month, field in enumerate(MONTH_FIELDS, 1):
                            bucket = buckets[(value, f"{year}-{month:02d}")]
                            bucket["energy"] += float(row.get(field) or 0.0) * weight
                            bucket["customers"] += weight
                            if active:
                                bucket["active"] += weight
                writer.writerows(rows)
                if "feeder" in monthly_levels:
                    for feeder, values in ctmt_data.items():
                        value = feeder or "SEM_CTMT"
                        for month in range(1, 13):
                            bucket = monthly_levels["feeder"][(value, f"{year}-{month:02d}")]
                            bucket["energy"] += values["energy"][month]
                            bucket["customers"] += values["customer_count"]
                            bucket["active"] += values["active_count"]
                del rows, ctmt_data
                _emit_progress(
                    progress_callback,
                    stage="Ano concluído",
                    year=year,
                    year_index=year_index,
                    year_count=year_count,
                    year_percent=100,
                    overall_percent=int((year_index + 1) * 85 / year_count),
                    message=f"{distributor.upper()} {year} concluído",
                )
        _check_cancel(cancel_callback)
        # Publication is the commit boundary: finish exporting once it begins.
        if "customer" in output_levels:
            panel_path = _publish_temp_file(panel_temp, panel_path)
        else:
            panel_temp.unlink(missing_ok=True)
    except Exception:
        panel_temp.unlink(missing_ok=True)
        raise
    strata_path = out / f"{distributor.upper()}_sampling_strata_{start_year}_{end_year}.csv"
    if strata_rows:
        strata_path = _write_csv(
            strata_path,
            strata_rows,
            ["source_year", *strata_fields, "populacao", "amostra", "peso_amostral"],
        )
    segment_path = out / f"{distributor.upper()}_segments_monthly_{start_year}_{end_year}.csv"
    segment_rows = [
        {
            "ano_mes": key[0],
            "patamar_consumo": key[1],
            "GRU_TAR": key[2],
            "MUNICIPIO": key[3],
            "TIP_CC": key[4],
            "GD_CADASTRO": key[5],
            "energia": round(values["energy"], 6),
            "clientes": round(values["customers"], 3),
            "consumo_medio": round(values["energy"] / values["customers"], 6) if values["customers"] else None,
        }
        for key, values in sorted(segment_monthly.items())
    ]
    unresolved_codes = sorted({
        row["MUNICIPIO"].replace("Código IBGE ", "", 1)
        for row in segment_rows
        if row["MUNICIPIO"].startswith("Código IBGE ")
    })
    segment_path = _write_csv(
        segment_path,
        segment_rows,
        ["ano_mes", "patamar_consumo", "GRU_TAR", "MUNICIPIO", "TIP_CC", "GD_CADASTRO",
         "energia", "clientes", "consumo_medio"],
    )
    _emit_progress(
        progress_callback,
        stage="Exportação",
        year=end_year,
        year_percent=100,
        overall_percent=90,
        message="Consolidando arquivos finais; previsão desativada nesta versão",
    )
    level_paths = {}
    for level, buckets in monthly_levels.items():
        _, source_field, suffix = OUTPUT_LEVELS[level]
        output_field = source_field or "DISTRIBUIDORA"
        rows = [
            {
                output_field: key[0], "ano_mes": key[1],
                "energia": round(values["energy"], 6),
                "clientes": round(values["customers"], 3),
                "clientes_ativos": round(values["active"], 3),
                "consumo_medio": round(values["energy"] / values["customers"], 6)
                if values["customers"] else None,
            }
            for key, values in sorted(buckets.items())
        ]
        path = out / f"{distributor.upper()}_{suffix}_monthly_{start_year}_{end_year}.csv"
        level_paths[level] = _write_csv(
            path, rows, [output_field, "ano_mes", "energia", "clientes", "clientes_ativos", "consumo_medio"]
        )

    canonical_names = {
        f"{distributor.upper()}_sampling_strata_{start_year}_{end_year}.csv",
        f"{distributor.upper()}_segments_monthly_{start_year}_{end_year}.csv",
        *(f"{distributor.upper()}_{OUTPUT_LEVELS[level][2]}_monthly_{start_year}_{end_year}.csv"
          for level in level_paths),
    }
    published_paths = [segment_path, *level_paths.values()]
    if "customer" in output_levels:
        canonical_names.add(f"{distributor.upper()}_customer_panel_{start_year}_{end_year}.csv")
        published_paths.append(panel_path)
    if strata_rows:
        published_paths.append(strata_path)
    recovered_files = [path.name for path in published_paths if path.name not in canonical_names]
    warnings = []
    if municipality_lookup.get("warning"):
        warnings.append(municipality_lookup["warning"])
    if unresolved_codes:
        warnings.append(
            f"{len(unresolved_codes)} código(s) de município não foram encontrados no IBGE: "
            + ", ".join(unresolved_codes[:20])
            + ("…" if len(unresolved_codes) > 20 else "")
        )
    for level, count in sorted(missing_level_rows.items()):
        if count:
            warnings.append(
                f"{OUTPUT_LEVELS[level][0]}: {count:,} registro(s) sem chave foram excluídos desse nível."
            )
    metadata = {
        "distributor": distributor.upper(),
        "years": years,
        "source": source_metadata or {"type": "local"},
        "output_dir": str(out),
        "forecast": {
            "status": "disabled",
            "reason": "Método de previsão em revisão; nenhum valor projetado foi calculado nesta versão.",
        },
        "municipality_lookup": {
            **municipality_lookup,
            "endpoint": IBGE_MUNICIPALITIES_URL,
            "unresolved_codes": unresolved_codes,
        },
        "sampling": {
            "mode": sampling_mode,
            "confidence": confidence,
            "margin_error": margin_error,
            "auto_threshold": AUTO_SAMPLE_THRESHOLD,
            "stratification": list(strata_fields),
            "precision_scope": "Proporção sob amostragem aleatória simples, p=0,5 e correção finita; não é intervalo de confiança do consumo.",
            "selection": "Limiar pseudoaleatório por posição e ano, com mínimo de 10 registros por estrato (ou censo dos menores). Tamanho realizado variável; reprodução exige a mesma ordem de entrada.",
            "years": sampling_diagnostics,
        },
        "publication_warnings": (
            ["Arquivos bloqueados foram preservados com nome de recuperação: " + ", ".join(recovered_files)]
            if recovered_files else []
        ),
        "warnings": warnings,
        "outputs": {
            "levels": list(output_levels),
            "level_files": {level: path.name for level, path in level_paths.items()},
            "missing_key_rows": dict(missing_level_rows),
        },
        "files": [path.name for path in published_paths],
    }
    (out / "run_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    _emit_progress(
        progress_callback,
        stage="Concluído",
        year=end_year,
        year_percent=100,
        overall_percent=100,
        message="Análise concluída",
    )
    return metadata


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", required=True)
    parser.add_argument("--distributor", required=True)
    parser.add_argument("--start-year", type=int, default=MIN_ANALYSIS_YEAR)
    parser.add_argument("--end-year", type=int, default=MAX_ANALYSIS_YEAR)
    parser.add_argument("--output-dir")
    parser.add_argument("--sampling-mode", choices=["auto", "census", "stratified"], default="auto")
    parser.add_argument("--confidence", type=float, choices=[0.90, 0.95, 0.99], default=DEFAULT_CONFIDENCE)
    parser.add_argument("--margin-error", type=float, default=DEFAULT_MARGIN_ERROR)
    args = parser.parse_args(argv)
    print(json.dumps(build_outputs(
        args.base_dir, args.distributor, args.start_year, args.end_year, args.output_dir,
        args.sampling_mode, args.confidence, args.margin_error,
    ), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
