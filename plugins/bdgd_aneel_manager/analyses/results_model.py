"""Validated descriptive views of monthly BDGD aggregates (no Qt dependency).

`clientes` is a weighted monthly count, not distinct longitudinal customers.
Summing it across months gives customer-month exposure. Missing periods remain
missing; this module does not impute, forecast or estimate confidence intervals.
"""
from collections import Counter, defaultdict
import hashlib
import math
import re

DIMENSIONS = ('patamar_consumo', 'GRU_TAR', 'MUNICIPIO', 'TIP_CC', 'GD_CADASTRO')
KEY_FIELDS = ('ano_mes', *DIMENSIONS)
METRICS = {
    'energy': ('Consumo total', 'kWh'),
    'average': ('Consumo médio mensal por cliente', 'kWh/cliente/mês'),
    'customers': ('Clientes mensais médios estimados', 'clientes/mês'),
}
MODEL_VERSION = '1.1'


def input_fingerprint(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return {'file': path.name, 'sha256': digest.hexdigest(), 'bytes': path.stat().st_size}


def br_number(value, decimals=2):
    if value is None or not math.isfinite(float(value)):
        return '—'
    return f'{float(value):,.{decimals}f}'.translate(str.maketrans({',': '.', '.': ','}))


def normalize_rows(rows):
    """Reject invalid or ambiguous cells, reporting each exclusion explicitly."""
    accepted, problems = [], Counter()
    for raw in rows:
        period = str(raw.get('ano_mes', ''))
        if not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', period):
            problems['periodo_invalido'] += 1
            continue
        try:
            energy, customers = float(raw['energia']), float(raw['clientes'])
            if not math.isfinite(energy) or not math.isfinite(customers) or customers < 0:
                raise ValueError()
        except (KeyError, ValueError, TypeError, OverflowError):
            problems['valor_invalido'] += 1
            continue
        row = {field: str(raw.get(field) or 'Não informado') for field in DIMENSIONS}
        row.update(ano_mes=period, energia=energy, clientes=customers,
                   consumo_medio=energy / customers if customers else None)
        accepted.append(row)
    counts = Counter(tuple(r[k] for k in KEY_FIELDS) for r in accepted)
    unique = [r for r in accepted if counts[tuple(r[k] for k in KEY_FIELDS)] == 1]
    problems['celulas_duplicadas_excluidas'] = len(accepted) - len(unique)
    return unique, {k: v for k, v in problems.items() if v}


def filter_rows(rows, selection):
    """Absent filter means all; an explicitly empty selection means no rows."""
    sets = {k: {str(v) for v in values} for k, values in selection.items() if values is not None}
    result = rows
    # Apply narrow categorical filters first, reducing work on large panels.
    for key, values in sorted(sets.items(), key=lambda item: len(item[1])):
        if not values:
            return []
        if key == 'year':
            result = [r for r in result if r['ano_mes'][:4] in values]
        elif key == 'month':
            result = [r for r in result if r['ano_mes'][5:] in values]
        else:
            result = [r for r in result if r.get(key) in values]
    return result


def summarize(rows):
    energy = math.fsum(r['energia'] for r in rows)
    exposure = math.fsum(r['clientes'] for r in rows)
    months = sorted({r['ano_mes'] for r in rows})
    return dict(energy=energy, exposure=exposure, months=months,
                average=energy / exposure if exposure else None,
                customers=exposure / len(months) if months else None,
                cells=len(rows), zero_exposure=sum(r['clientes'] == 0 and r['energia'] != 0 for r in rows),
                negative_energy=sum(r['energia'] < 0 for r in rows))


def period_key(period, frequency):
    if frequency == 'year':
        return period[:4]
    if frequency == 'quarter':
        return f'{period[:4]}-T{(int(period[5:]) - 1) // 3 + 1}'
    return period


def calendar_periods(months, frequency):
    if not months:
        return []
    first, last = min(months), max(months)
    start, stop = int(first[:4]) * 12 + int(first[5:]) - 1, int(last[:4]) * 12 + int(last[5:]) - 1
    return sorted({period_key(f'{n // 12:04d}-{n % 12 + 1:02d}', frequency) for n in range(start, stop + 1)})


def make_series(rows, metric='energy', frequency='month', group=None, top_n=5, compare_years=False):
    """Return common x labels and series, using None for absent observations.

    Group selection is ranked by total energy; remaining groups are pooled in
    an explicit residual series so chart totals are conserved. Averages always
    use sum(energy)/sum(customer-month exposure), never a mean of means.
    """
    if metric not in METRICS or frequency not in ('month', 'quarter', 'year'):
        raise ValueError('Indicador ou frequência inválidos.')
    if group is not None and group not in DIMENSIONS:
        raise ValueError('Agrupamento inválido.')
    if compare_years:
        labels = [f'{m:02d}' for m in range(1, 13)]
        names = sorted({r['ano_mes'][:4] for r in rows})
    else:
        labels = calendar_periods([r['ano_mes'] for r in rows], frequency)
        totals = defaultdict(float)
        for r in rows:
            totals[r[group] if group else 'Total'] += r['energia']
        names = sorted(totals, key=lambda k: (-totals[k], k))[:max(1, top_n)]
    residual = 'Demais grupos (agrupados)'
    while residual in names:
        residual += '*'
    buckets = defaultdict(list)
    for r in rows:
        if compare_years:
            name, period = r['ano_mes'][:4], r['ano_mes'][5:]
        else:
            name = r[group] if group else 'Total'
            name = name if name in names else residual
            period = period_key(r['ano_mes'], frequency)
        buckets[(name, period)].append(r)
    if any(k[0] == residual for k in buckets):
        names.append(residual)
    series, coverage = {}, {}
    for name in names:
        series[name], coverage[name] = [], []
        for period in labels:
            part = buckets.get((name, period), [])
            series[name].append(summarize(part)[metric] if part else None)
            coverage[name].append(len({r['ano_mes'] for r in part}))
    return labels, series, coverage


def selection_manifest(metadata, selection, controls, diagnostics):
    """Export provenance/definitions alongside the view, without a DB URI."""
    source = metadata.get('source', {})
    return {
        'view_schema_version': MODEL_VERSION,
        'distributor': metadata.get('distributor'),
        'source': {k: source[k] for k in ('type', 'connection', 'tables') if k in source},
        'analysis_years': metadata.get('years', []),
        'input': metadata.get('view_input', {}),
        'sampling': metadata.get('sampling', {}),
        'selection': selection, 'controls': controls, 'diagnostics': diagnostics,
        'definitions': {
            'energy': 'Soma de energia em kWh nas células selecionadas.',
            'exposure': 'Soma das contagens mensais estimadas: cliente-mês, não clientes únicos.',
            'average': 'Soma da energia / soma da exposição cliente-mês.',
            'customers': 'Exposição cliente-mês / número de meses observados.',
            'missing': 'Períodos sem observação não são preenchidos com zero.',
        },
        'forecast': 'Não calculada; validação de modelos futura e independente.',
    }
