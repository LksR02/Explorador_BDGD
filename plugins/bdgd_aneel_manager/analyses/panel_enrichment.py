"""Read-only UCBT enrichment; preserves the original sample and every energy cell."""
import csv
from collections import defaultdict, Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path

from .results_model import input_fingerprint
from .variable_catalog import EXTRA_FIELDS, profile_variables, number
from .postgres_source import quote_identifier
from .distributed_generation import GD_FIELDS, enrich_gd_year, canonical, key


def read_panel(path):
    """Read a generated customer panel, returning an empty list if absent."""
    panel_path = Path(path)
    if not panel_path.exists():
        return []
    with panel_path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def compatible_energy(panel_row, source_row):
    for month in range(1, 13):
        key = f'ENE_{month:02d}'
        a, b = number(panel_row.get(key)), number(source_row.get(key))
        if a is None or b is None or not math.isclose(a, b, rel_tol=1e-8, abs_tol=0.000005):
            return False
    return True


def merge_year(rows, records, fields):
    """Ambiguous keys and changed energy never produce synthetic matched attributes."""
    lookup, ambiguous = {}, set()
    for record in records:
        key = str(record.get('COD_ID', ''))
        if key in lookup:
            ambiguous.add(key)
        else:
            lookup[key] = record
    counts = dict(matched=0, unmatched=0, ambiguous=0, changed_energy=0)
    for row in rows:
        for field in EXTRA_FIELDS:
            row[field] = ''
        key = str(row.get('COD_ID', ''))
        if key in ambiguous:
            counts['ambiguous'] += 1
        elif key not in lookup:
            counts['unmatched'] += 1
        elif not compatible_energy(row, lookup[key]):
            counts['changed_energy'] += 1
        else:
            counts['matched'] += 1
            for field in fields:
                row[field] = lookup[key].get(field, '')
    return counts


def enrichment_paths(path):
    path = Path(path)
    return path.with_suffix('.variables.csv'), path.with_suffix('.variables.json')


def cached_enrichment(path, source):
    output, manifest = enrichment_paths(path)
    try:
        data = json.loads(manifest.read_text(encoding='utf-8'))
        if (data.get('schema_version') == 3 and data['original_input'] == input_fingerprint(path) and data['source'] == source
                and data['enriched_input'] == input_fingerprint(output)):
            return output, data
    except (OSError, ValueError, KeyError):
        pass
    return Path(path), {}


def enrich_panel(path, connection_info, source, progress=None):
    from osgeo import gdal
    original = input_fingerprint(path)
    rows = read_panel(path)
    by_year = defaultdict(list)
    for row in rows:
        by_year[str(row['source_year'])].append(row)
    entries = {str(item['year']): item for item in source['tables']}
    if not set(by_year) <= set(entries):
        raise ValueError('Os anos do painel não correspondem às tabelas registradas na análise.')
    audit = []
    gd_audit, networks_by_year = [], {}
    gdal.PushErrorHandler('CPLQuietErrorHandler')
    dataset = None
    try:
        dataset = gdal.OpenEx('PG:' + connection_info, gdal.OF_VECTOR | gdal.OF_READONLY,
                             allowed_drivers=['PostgreSQL'], open_options=['LIST_ALL_TABLES=YES'])
        if dataset is None:
            raise RuntimeError('connection')
        dataset.ExecuteSQL('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        for year, year_rows in sorted(by_year.items()):
            if progress:
                progress(f'{year}: consultando atributos de {len(year_rows):,} registros amostrais…')
            entry = entries[year]
            relation = '.'.join(quote_identifier(entry[k]) for k in ('schema', 'table'))
            probe = dataset.ExecuteSQL(f'SELECT * FROM {relation} LIMIT 0')
            if probe is None:
                raise RuntimeError('schema')
            columns = {field.name.upper(): field.name for field in probe.schema}
            dataset.ReleaseResultSet(probe)
            fields = sorted(set(EXTRA_FIELDS) & columns.keys())
            link_fields = [k for k in ('PN_CON','PAC','CTMT','UNI_TR_MT','UNI_TR_D') if k in columns]
            keys = list(dict.fromkeys(['COD_ID', *[f'ENE_{m:02d}' for m in range(1, 13)], *fields, *link_fields]))
            projection = ', '.join(f'{quote_identifier(columns[k])} AS {quote_identifier(k)}' for k in keys)
            # One bounded ID set per year, no joins multiplying panel observations.
            ids = ', '.join(sql_literal(key) for key in sorted({r['COD_ID'] for r in year_rows}))
            layer = dataset.ExecuteSQL(f'SELECT {projection} FROM {relation} WHERE {quote_identifier(columns["COD_ID"])} IN ({ids})')
            if layer is None:
                raise RuntimeError('query')
            records = []
            for feature in layer:
                record = {key: feature.GetField(index) for index, key in enumerate(keys)}
                records.append({key: '' if value is None else str(value) for key, value in record.items()})
            dataset.ReleaseResultSet(layer)
            counts = merge_year(year_rows, records, fields)
            audit.append(dict(year=year, fields=fields, **counts))
            if progress:
                progress(f'{year}: auditando vínculos UGBT e agregando geração por rede…')
            frequencies = Counter(r['COD_ID'] for r in records)
            record_lookup = {r['COD_ID']: r for r in records}
            verified = {r['COD_ID'] for r in year_rows if frequencies[r['COD_ID']] == 1
                        and compatible_energy(r, record_lookup[r['COD_ID']])}
            gd_counts, networks = enrich_gd_year(dataset, entry, columns, year_rows, records, verified)
            gd_audit.append(gd_counts)
            networks_by_year[int(year)] = networks
            previous = networks_by_year.get(int(year) - 1)
            if networks is not None and previous is not None:
                for row in year_rows:
                    if row['COD_ID'] not in verified:
                        continue
                    for level, field in [('TRAFO', 'TRAFO_LINK'), ('CTMT', 'CTMT')]:
                        code = key(canonical(row).get(field))
                        before = previous[level].get(code)
                        now = networks[level].get(code)
                        if before and now and isinstance(before['POT_KW'], (int, float)) and isinstance(now['POT_KW'], (int, float)):
                            row[f'GD_{level}_DELTA_POT_KW'] = now['POT_KW'] - before['POT_KW']
        dataset.ExecuteSQL('COMMIT')
    except Exception:
        raise RuntimeError('Não foi possível enriquecer o painel. Confira a conexão salva e a estrutura das tabelas UCBT de origem.') from None
    finally:
        dataset = None
        gdal.PopErrorHandler()
    if progress:
        progress('Calculando cobertura, variação e disponibilidade por ano…')
    profile = profile_variables(rows)
    if original != input_fingerprint(path):
        raise ValueError('O painel original mudou durante a consulta; execute novamente.')
    output, manifest = enrichment_paths(path)
    temporary = output.with_suffix('.csv.tmp')
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        original_fields = next(csv.reader(handle))
    fields = list(dict.fromkeys(original_fields + EXTRA_FIELDS + GD_FIELDS))
    try:
        with temporary.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()
    metadata = dict(schema_version=3, original_input=original, enriched_input=input_fingerprint(output),
                    source=source, refreshed_at=datetime.now(timezone.utc).isoformat(),
                    row_count=len(rows), years=audit, generation=gd_audit, profile=profile,
                    note='Atributos consultados no estado atual das BDGDs; mesmos COD_ID/ano e energias conferidas. Não certifica disponibilidade histórica dos atributos.')
    manifest_tmp = manifest.with_suffix('.json.tmp')
    manifest_tmp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    manifest_tmp.replace(manifest)
    return output, metadata
