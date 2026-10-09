"""UGBT covariates with audited links and full-inventory network aggregates."""
from collections import Counter, defaultdict
from datetime import datetime, date
import math

MONTHS = [f'ENE_{m:02d}' for m in range(1, 13)]
GD_FIELDS = ['GD_VINCULO', 'GD_PRESENCA', 'GD_QTD', 'GD_POT_KW', 'GD_IDADE_ANOS',
             'GD_ANO_CONEXAO', 'GD_MES_CONEXAO', 'GD_SITUACAO', 'GD_ENE_TOTAL',
             *[f'GD_ENE_{m:02d}' for m in range(1, 13)],
             'GD_CONSUMO_ANTES', 'GD_CONSUMO_APOS', 'GD_MESES_ANTES', 'GD_MESES_APOS',
             *[f'GD_{level}_{metric}' for level in ('TRAFO', 'CTMT')
               for metric in ('QTD', 'POT_KW', 'NOVAS', 'DELTA_POT_KW')]]


def key(value):
    text = str(value or '').strip()
    if text.upper() in ('', '0', '00', '000', '-', 'NA', 'N/A', 'NONE', 'NULL'):
        return None
    # Placeholder observed in the ENEL RJ UGBT; not a real generator identifier.
    if text.upper().startswith('GD.'):
        parts = text.split('.')[2:]
        if parts and all(p.isdigit() and int(p) == 0 for p in parts):
            return None
    return text


def numeric(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def connection_date(value, year):
    for pattern in ('%d/%m/%Y', '%Y-%m-%d', '%Y/%m/%d', '%d-%m-%Y'):
        try:
            parsed = datetime.strptime(str(value)[:10], pattern).date()
            if 0 <= (date(int(year), 12, 31) - parsed).days <= 120 * 366:
                return parsed
        except (ValueError, TypeError):
            pass
    return None


def canonical(record):
    result = dict(record)
    result['CEG_LINK'] = record.get('CEG_GD') or record.get('CEG', '')
    result['TRAFO_LINK'] = record.get('UNI_TR_MT') or record.get('UNI_TR_D', '')
    return result


def link_generators(generators, consumers):
    """All candidate UCBTs, not just sampled customers, establish uniqueness."""
    fields = ('CEG_LINK', 'PN_CON', 'PAC')
    indexes = {field: defaultdict(set) for field in fields}
    for consumer in consumers:
        cod = key(consumer.get('COD_ID'))
        if cod:
            for field in fields:
                value = key(consumer.get(field))
                if value:
                    indexes[field][value].add(cod)
    id_counts = Counter(key(g.get('COD_ID')) for g in generators)
    linked = defaultdict(list)
    uncertain = set()
    audit = Counter()
    for generator in generators:
        candidates = {field: indexes[field].get(key(generator.get(field)), set()) for field in fields}
        involved = set().union(*candidates.values())
        if not key(generator.get('COD_ID')) or id_counts[key(generator.get('COD_ID'))] != 1:
            uncertain.update(involved)
            audit['invalid_or_duplicate_generator_id'] += 1
            continue
        unique = {next(iter(ids)) for ids in candidates.values() if len(ids) == 1}
        if len(unique) == 1:
            consumer = next(iter(unique))
            if all(not ids or consumer in ids for ids in candidates.values()):
                method = next(field for field in fields if candidates[field] == {consumer})
                linked[consumer].append((generator, method))
                audit['linked_' + method] += 1
                continue
        uncertain.update(involved)
        audit['conflicting_or_ambiguous' if involved else 'unmatched'] += 1
    return linked, uncertain, dict(audit)


def network_aggregates(generators, year):
    counts = Counter(key(g.get('COD_ID')) for g in generators)
    networks = {level: {} for level in ('TRAFO', 'CTMT')}
    for g in generators:
        if not key(g.get('COD_ID')) or counts[key(g.get('COD_ID'))] != 1:
            continue
        if str(g.get('SIT_ATIV', '')).strip().upper() != 'AT':
            continue
        for level, field in [('TRAFO', 'TRAFO_LINK'), ('CTMT', 'CTMT')]:
            code = key(g.get(field))
            if not code:
                continue
            bucket = networks[level].setdefault(code, dict(QTD=0, POT_KW=0., NOVAS=0, invalid_power=0, invalid_date=0))
            bucket['QTD'] += 1
            power = numeric(g.get('POT_INST'))
            if power is None or power <= 0:
                bucket['invalid_power'] += 1
            else:
                bucket['POT_KW'] += power
            connected = connection_date(g.get('DAT_CON'), year)
            if connected is None:
                bucket['invalid_date'] += 1
            else:
                bucket['NOVAS'] += int(connected.year == int(year))
    for level in networks.values():
        for bucket in level.values():
            if bucket['invalid_power']:
                bucket['POT_KW'] = ''
            if bucket['invalid_date']:
                bucket['NOVAS'] = ''
    return networks


def attach_gd(rows, generators, consumers, verified_ids, year):
    generators = [canonical(g) for g in generators]
    consumers = [canonical(c) for c in consumers]
    linked, uncertain, audit = link_generators(generators, consumers)
    networks = network_aggregates(generators, year)
    consumer_lookup = {c['COD_ID']: c for c in consumers}
    # The matched sample may include no GD candidates, so use its verified source links too.
    audit['sample_status'] = dict()
    statuses = Counter()
    for row in rows:
        for field in GD_FIELDS:
            row[field] = ''
        cod = row.get('COD_ID')
        if cod not in verified_ids:
            row['GD_VINCULO'] = 'UCBT não conferida'
            row['GD_PRESENCA'] = 'Indeterminada'
            statuses[row['GD_VINCULO']] += 1
            continue
        consumer = consumer_lookup.get(cod, canonical(row))
        for level, field in [('TRAFO', 'TRAFO_LINK'), ('CTMT', 'CTMT')]:
            code = key(consumer.get(field))
            if code:
                bucket = networks[level].get(code, dict(QTD=0, POT_KW=0., NOVAS=0))
                for metric in ('QTD', 'POT_KW', 'NOVAS'):
                    row[f'GD_{level}_{metric}'] = bucket[metric]
        if cod in uncertain:
            row['GD_VINCULO'] = 'Ambíguo/conflitante'
            row['GD_PRESENCA'] = 'Indeterminada'
        elif cod not in linked:
            declared = key(consumer.get('CEG_LINK'))
            placeholder = str(consumer.get('CEG_LINK', '') or '').strip().upper().startswith('GD.') and not declared
            row['GD_VINCULO'] = 'CEG sem correspondência' if declared else ('CEG inválido sem vínculo físico' if placeholder else 'Não identificada na UGBT')
            row['GD_PRESENCA'] = 'Indeterminada' if declared or placeholder else 'Não identificada'
        else:
            matches = linked[cod]
            units = [g for g, _ in matches]
            row['GD_VINCULO'] = ' + '.join(sorted({method for _, method in matches}))
            row['GD_PRESENCA'] = 'Vinculada'
            row['GD_QTD'] = len(units)
            states = {str(g.get('SIT_ATIV', '')).strip().upper() for g in units}
            row['GD_SITUACAO'] = next(iter(states)) if len(states) == 1 else 'Mista/indeterminada'
            powers = [numeric(g.get('POT_INST')) for g in units]
            if all(p is not None and p > 0 for p in powers):
                row['GD_POT_KW'] = math.fsum(powers)
            dates = [connection_date(g.get('DAT_CON'), year) for g in units]
            if all(d is not None for d in dates):
                first = min(dates)
                row['GD_IDADE_ANOS'] = (date(int(year), 12, 31) - first).days / 365.2425
                row['GD_ANO_CONEXAO'], row['GD_MES_CONEXAO'] = first.year, first.month
                if first.year == int(year) and len(units) == 1:
                    before = [numeric(row.get(f'ENE_{m:02d}')) for m in range(1, first.month)]
                    after = [numeric(row.get(f'ENE_{m:02d}')) for m in range(first.month + 1, 13)]
                    row['GD_MESES_ANTES'], row['GD_MESES_APOS'] = len(before), len(after)
                    if len(before) >= 3 and all(v is not None for v in before):
                        row['GD_CONSUMO_ANTES'] = math.fsum(before) / len(before)
                    if len(after) >= 3 and all(v is not None for v in after):
                        row['GD_CONSUMO_APOS'] = math.fsum(after) / len(after)
            for month in range(1, 13):
                energies = [numeric(g.get(f'ENE_{month:02d}')) for g in units]
                if all(v is not None for v in energies):
                    row[f'GD_ENE_{month:02d}'] = math.fsum(energies)
            energies = [numeric(row[f'GD_ENE_{m:02d}']) for m in range(1, 13)]
            if all(v is not None for v in energies):
                row['GD_ENE_TOTAL'] = math.fsum(energies)
        statuses[row['GD_VINCULO']] += 1
    audit.update(sample_status=dict(statuses), generator_rows=len(generators),
                 active_generator_rows=sum(str(g.get('SIT_ATIV', '')).strip().upper() == 'AT' for g in generators),
                 placeholder_ceg=sum(not key(g.get('CEG_LINK')) for g in generators))
    return audit, networks


def fetch_dicts(dataset, sql):
    layer = dataset.ExecuteSQL(sql)
    if layer is None:
        raise RuntimeError('Consulta de geração indisponível')
    try:
        names = [f.name for f in layer.schema]
        rows = []
        for feature in layer:
            values = [feature.GetField(i) for i in range(len(names))]
            rows.append({name: value if value is not None else '' for name, value in zip(names, values)})
        return rows
    finally:
        dataset.ReleaseResultSet(layer)


def enrich_gd_year(dataset, entry, uc_columns, rows, verified_records, verified_ids):
    from .postgres_source import quote_identifier as qi
    from .panel_enrichment import sql_literal
    schema = entry['schema']
    tables = fetch_dicts(dataset, 'SELECT table_name FROM information_schema.tables WHERE table_schema = '
                         + sql_literal(schema) + " AND lower(table_name) IN ('ugbt_tab', 'ugbt')")
    if not tables:
        for row in rows:
            row.update({field: '' for field in GD_FIELDS})
            row.update(GD_VINCULO='UGBT indisponível', GD_PRESENCA='Indeterminada')
        return dict(available=False, year=entry['year']), None
    table = sorted((r['table_name'] for r in tables), key=lambda name: name.lower() != 'ugbt_tab')[0]
    relation = qi(schema) + '.' + qi(table)
    cols = fetch_dicts(dataset, 'SELECT column_name FROM information_schema.columns WHERE table_schema = '
                       + sql_literal(schema) + ' AND table_name = ' + sql_literal(table))
    cols = {r['column_name'].upper(): r['column_name'] for r in cols}
    if 'COD_ID' not in cols or not {'CEG_GD','CEG','PN_CON','PAC'} & cols.keys():
        for row in rows:
            row.update({field: '' for field in GD_FIELDS})
            row.update(GD_VINCULO='Estrutura UGBT incompleta', GD_PRESENCA='Indeterminada')
        return dict(available=False, year=entry['year'], reason='Campos de identificação/vínculo ausentes'), None
    wanted = ['COD_ID', 'CEG_GD', 'CEG', 'PN_CON', 'PAC', 'UNI_TR_MT', 'UNI_TR_D',
              'CTMT', 'SIT_ATIV', 'DAT_CON', 'POT_INST', *MONTHS]
    projection = ', '.join(qi(cols[k]) + ' AS ' + qi(k) for k in wanted if k in cols)
    generators = [canonical(r) for r in fetch_dicts(dataset, f'SELECT {projection} FROM {relation}')]
    if not generators:
        for row in rows:
            row.update({field: '' for field in GD_FIELDS})
            row.update(GD_VINCULO='UGBT sem registros', GD_PRESENCA='Indeterminada')
        return dict(available=False, year=entry['year'], schema=schema, table=table,
                    reason='UGBT sem registros; não comprova ausência de geração'), None
    predicates = []
    for field, alias in [('CEG_GD' if 'CEG_GD' in uc_columns else 'CEG', 'CEG_LINK'), ('PN_CON', 'PN_CON'), ('PAC', 'PAC')]:
        values = {key(g.get(alias)) for g in generators} - {None}
        if field in uc_columns and values:
            predicates.append('trim(' + qi(uc_columns[field]) + ') IN (' + ','.join(sql_literal(v) for v in sorted(values)) + ')')
    consumer_fields = [k for k in ('COD_ID','CEG_GD','CEG','PN_CON','PAC','CTMT','UNI_TR_MT','UNI_TR_D') if k in uc_columns]
    projection = ', '.join(qi(uc_columns[k]) + ' AS ' + qi(k) for k in consumer_fields)
    consumers = fetch_dicts(dataset, f'SELECT {projection} FROM {qi(schema)}.{qi(entry["table"])} WHERE ' + ' OR '.join(predicates)) if predicates else []
    # Duplicate UCBT COD_IDs cannot establish a reliable association.
    counts = Counter(str(c.get('COD_ID')) for c in consumers)
    duplicate_ids = {cod for cod, n in counts.items() if n > 1}
    lookup = {str(c['COD_ID']): c for c in consumers}
    for r in verified_records:
        lookup.setdefault(str(r['COD_ID']), r)
    audit, networks = attach_gd(rows, generators, list(lookup.values()), verified_ids - duplicate_ids, entry['year'])
    for level, required in [('TRAFO', {'COD_ID','SIT_ATIV'} | ({'UNI_TR_MT'} if 'UNI_TR_MT' in cols else {'UNI_TR_D'})),
                            ('CTMT', {'COD_ID','SIT_ATIV','CTMT'})]:
        if not required <= cols.keys():
            networks[level] = {}
            for row in rows:
                for metric in ('QTD','POT_KW','NOVAS'):
                    row[f'GD_{level}_{metric}'] = ''
    audit.update(available=True, year=entry['year'], schema=schema, table=table,
                 duplicate_consumer_ids=len(duplicate_ids))
    return audit, networks
