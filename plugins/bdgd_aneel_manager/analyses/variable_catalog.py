"""Typed UCBT variables: codes are factors, never arbitrary numeric predictors."""
from datetime import datetime, date
import math

MONTH_NAMES = 'Janeiro Fevereiro Março Abril Maio Junho Julho Agosto Setembro Outubro Novembro Dezembro'.split()
EXTRA_FIELDS = ['CAR_INST', 'CNAE', 'FAS_CON', 'TEN_FORN', 'GRU_TEN', 'ARE_LOC',
                'TIP_CC', 'DAT_CON', 'LIV', 'TIP_SIST', 'SUB', 'CONJ', 'DIC', 'FIC', 'CEG_GD', 'CEG', 'UNI_TR_D',
                *[f'{prefix}_{m:02d}' for prefix in ('DIC', 'FIC') for m in range(1, 13)]]
CATALOG = {}


def register(key, name, kind, group, fields=None, note=''):
    CATALOG[key] = dict(key=key, label=name, kind=kind, group=group,
                        fields=fields or [key], note=note)


register('CAR_INST', 'Carga instalada · CAR_INST (kW)', 'numeric', 'Características técnicas',
         note='Potência cadastrada; valores ≤ 0 são indisponíveis. Cadastro do ano não garante disponibilidade em meses anteriores.')
register('IDADE_CONEXAO', 'Tempo desde a conexão até 31/12 (anos)', 'numeric', 'Características técnicas',
         ['DAT_CON', 'source_year'], 'Derivado de DAT_CON e ano da base, nunca da data atual. Datas futuras e idades acima de 120 anos são excluídas.')
for key, name in [('CLAS_SUB', 'Classe e subclasse'), ('FAS_CON', 'Fases de conexão'),
                  ('ARE_LOC', 'Área de localização'), ('GRU_TAR', 'Grupo tarifário'),
                  ('CNAE_DIVISAO', 'Divisão CNAE (2 dígitos)'), ('CNAE', 'Atividade econômica CNAE'),
                  ('TIP_CC', 'Tipologia de curva de carga'), ('TEN_FORN', 'Tensão de fornecimento (código)'),
                  ('GRU_TEN', 'Grupo de tensão'), ('SIT_ATIV', 'Situação de ativação'),
                  ('LIV', 'Consumidor livre (código)'), ('TIP_SIST', 'Tipo de sistema'),
                  ('MUNICIPIO', 'Município'), ('source_year', 'Ano da base')]:
    register(key, name + f' · {key}', 'category', 'Categorias e contexto',
             ['CNAE'] if key == 'CNAE_DIVISAO' else None,
             'Categoria nominal: códigos não são magnitudes. Rótulos originais preservados; domínio ANEEL não decodificado automaticamente.')
for prefix, name, unit, kind in [('DIC', 'Duração de interrupções', 'h', 'numeric'),
                                  ('FIC', 'Frequência de interrupções', 'interrupções', 'count')]:
    fields = [f'{prefix}_{m:02d}' for m in range(1, 13)]
    register(prefix + '_TOTAL', name + f' · soma dos 12 meses ({unit})', kind, 'Qualidade do serviço', fields,
             'Requer os 12 indicadores mensais válidos. Medida do mesmo ano não é informação antecipada para previsão.')
    register(prefix, name + f' · {prefix} legado ({unit}; período a conferir)', kind, 'Qualidade do serviço',
             note='Campo sem sufixo das estruturas antigas; não é misturado à soma dos campos mensais.')
    for m, month in enumerate(MONTH_NAMES, 1):
        register(f'{prefix}_{m:02d}', f'{name} · {month} ({unit})', kind, 'Qualidade do serviço',
                 note='Indicador apurado no período; zero é preservado. Negativos são inválidos.')
energy = [f'ENE_{m:02d}' for m in range(1, 13)]
register('energia_anual', 'Energia anual (kWh)', 'numeric', 'Consumo', energy)
register('consumo_medio_mensal', 'Consumo médio mensal (kWh/mês)', 'numeric', 'Consumo', energy)
for m, month in enumerate(MONTH_NAMES, 1):
    register(f'ENE_{m:02d}', f'Consumo · {month} (kWh)', 'numeric', 'Consumo')

from .distributed_generation import GD_FIELDS

GD_NOTE = ('Inventário UGBT, não cadastro completo de todas as modalidades de GD. Vínculo auditado; '
           'não identificada não comprova ausência de GD. Cadastro anual não garante disponibilidade histórica.')
for key, name in [('GD_PRESENCA', 'Presença de geração vinculada'), ('GD_VINCULO', 'Qualidade do vínculo UGBT–UCBT'),
                  ('GD_SITUACAO', 'Situação das geradoras vinculadas'), ('GD_ANO_CONEXAO', 'Ano da primeira conexão GD'),
                  ('GD_MES_CONEXAO', 'Mês da primeira conexão GD')]:
    register(key, name, 'category', 'Geração vinculada', note=GD_NOTE)
for key, name, kind in [('GD_QTD', 'Geradoras vinculadas (quantidade)', 'count'),
                       ('GD_POT_KW', 'Potência instalada da geração vinculada (kW)', 'numeric'),
                       ('GD_IDADE_ANOS', 'Tempo desde a primeira conexão GD (anos)', 'numeric'),
                       ('GD_ENE_TOTAL', 'Energia registrada na UGBT · anual (kWh)', 'numeric')]:
    register(key, name, kind, 'Geração vinculada', note=GD_NOTE)
register('GD_POT_CARGA', 'Potência GD / carga instalada (kW/kW)', 'numeric', 'Geração vinculada',
         ['GD_POT_KW', 'CAR_INST'], GD_NOTE + ' Razão de potências, não fração de energia suprida pela GD.')
for month, name in enumerate(MONTH_NAMES, 1):
    register(f'GD_ENE_{month:02d}', f'Energia UGBT · {name} (kWh)', 'numeric', 'Energia da geração',
             note='Energia registrada na UGBT; não presumir geração bruta, autoconsumo ou energia compensada. Não somar automaticamente à UCBT.')
for level, name in [('TRAFO', 'transformador'), ('CTMT', 'alimentador')]:
    for metric, text, kind in [('QTD', 'Geradoras ativas', 'count'), ('POT_KW', 'Potência GD ativa (kW)', 'numeric'),
                               ('NOVAS', 'Geradoras ativas conectadas no ano', 'count'),
                               ('DELTA_POT_KW', 'Variação anual de potência GD ativa (kW)', 'numeric')]:
        register(f'GD_{level}_{metric}', f'{text} no {name}', kind, 'Contexto de rede',
                 note='Agregado do inventário completo de UGBT, não da amostra de clientes. Repetido entre clientes da mesma rede: não somar essas linhas. '
                      'Ativas = SIT_ATIV AT; IDs de geradora inválidos/duplicados excluídos. Delta exige ano anterior consecutivo e a mesma chave de rede.')
for key, name, kind in [('GD_CONSUMO_ANTES', 'Consumo médio antes da conexão GD (kWh/mês)', 'numeric'),
                       ('GD_CONSUMO_APOS', 'Consumo médio após a conexão GD (kWh/mês)', 'numeric'),
                       ('GD_MESES_ANTES', 'Meses anteriores à conexão no ano', 'count'),
                       ('GD_MESES_APOS', 'Meses posteriores à conexão no ano', 'count')]:
    register(key, name, kind, 'Histórico da geração', note='Somente o ano de conexão de uma única geradora vinculada. Exclui o mês de conexão; '
             'médias exigem ≥ 3 meses completos por período. Sazonalidade e seleção impedem interpretação causal sem grupo de comparação.')

VARIABLE_LABELS = {key: spec['label'] for key, spec in CATALOG.items()}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def variable_value(row, key):
    spec = CATALOG[key]
    if spec['kind'] == 'category':
        value = str(row.get('CNAE' if key == 'CNAE_DIVISAO' else key, '') or '').strip()
        if value.lower() in ('', 'nan', 'none', 'null', 'não informado'):
            return None
        if key == 'GD_PRESENCA' and value == 'Indeterminada':
            return None
        if key == 'CNAE_DIVISAO':
            digits = ''.join(c for c in value if c.isdigit())
            return digits[:2] if len(digits) >= 2 and int(digits) else None
        return value
    if key == 'IDADE_CONEXAO':
        value = str(row.get('DAT_CON', '')).strip()
        try:
            year_end = date(int(row['source_year']), 12, 31)
        except (ValueError, TypeError, KeyError):
            return None
        for pattern in ('%d/%m/%Y', '%Y-%m-%d', '%Y/%m/%d', '%d-%m-%Y'):
            try:
                age = (year_end - datetime.strptime(value[:10], pattern).date()).days / 365.2425
                return age if 0 <= age <= 120 else None
            except ValueError:
                continue
        return None
    values = [number(row.get(field)) for field in spec['fields']]
    if any(v is None for v in values):
        return None
    if key.startswith(('DIC', 'FIC')) and any(v < 0 for v in values):
        return None
    if spec['kind'] == 'count' and any(not v.is_integer() for v in values):
        return None
    if key == 'GD_POT_CARGA':
        return values[0] / values[1] if values[0] >= 0 and values[1] > 0 else None
    value = sum(values)
    if key == 'consumo_medio_mensal':
        value /= 12
    if key == 'CAR_INST' and value <= 0:
        return None
    return value if math.isfinite(value) else None


def profile_variables(rows):
    """Full-panel coverage, cardinality and year coverage; independent of UI filters."""
    profiles = []
    for key, spec in CATALOG.items():
        valid, unique, years = 0, set(), set()
        for row in rows:
            value = variable_value(row, key)
            if value is not None:
                valid += 1
                unique.add(value)
                years.add(str(row.get('source_year', '')))
        profiles.append(dict(spec, valid=valid, missing=len(rows)-valid, distinct=len(unique),
                             years=sorted(years), coverage=valid/len(rows) if rows else 0))
    return profiles
