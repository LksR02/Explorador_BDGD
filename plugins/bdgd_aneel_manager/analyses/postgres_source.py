"""Read PostgreSQL UCBT through GDAL without copying the geodatabase."""
from dataclasses import dataclass, field

from osgeo import gdal


def quote_identifier(value):
    return '"' + str(value).replace('"', '""') + '"'


def analysis_entries(entries, scope):
    """Resolve exactly one usable UCBT relation for every selected year."""
    selected = {}
    for year, schema in zip(scope['years'], scope['schemas']):
        if year in selected:
            raise ValueError(f'Selecione apenas um schema para o ano {year}.')
        candidates = {e['table']: e for e in entries
                      if e['schema'] == schema and e['year'] == year
                      and e['distributor'] == scope['distributor']
                      and e['table'].lower() in ('ucbt_tab', 'ucbt')}
        ordered = sorted(candidates.values(), key=lambda e: e['table'].lower() != 'ucbt_tab')
        if not ordered:
            raise ValueError(f'{schema}: tabela UCBT_tab ou UCBT não encontrada.')
        entry = ordered[0]
        if not entry['readable'] or entry['status'] in ('running', 'failed'):
            raise ValueError(f'{schema}.{entry["table"]}: verifique a permissão de leitura e a importação.')
        selected[year] = dict(entry)
    if not selected or not scope['connection']:
        raise ValueError('Selecione uma conexão, uma distribuidora e os anos no PostgreSQL.')
    return selected


@dataclass
class PostgresSource:
    connection_info: str = field(repr=False)
    entry: dict

    def open_layer(self):
        # GDAL errors can include connection strings. Keep them out of the UI/log.
        gdal.PushErrorHandler('CPLQuietErrorHandler')
        try:
            dataset = gdal.OpenEx('PG:' + self.connection_info,
                                  gdal.OF_VECTOR | gdal.OF_READONLY,
                                  allowed_drivers=['PostgreSQL'],
                                  open_options=['LIST_ALL_TABLES=YES'])
            if dataset is None:
                raise RuntimeError('connection unavailable')
            relation = '.'.join(quote_identifier(self.entry[k]) for k in ('schema', 'table'))
            probe = dataset.ExecuteSQL(f'SELECT * FROM {relation} LIMIT 0')
            if probe is None:
                raise RuntimeError('relation unavailable')
            columns = {f.name.upper(): f.name for f in probe.schema}
            dataset.ReleaseResultSet(probe)
            from .demand_mvp import MONTH_FIELDS, CUSTOMER_FIELDS
            required = {'COD_ID', 'GRU_TAR', 'MUN', *MONTH_FIELDS}
            missing = required - columns.keys()
            if missing:
                raise ValueError('Campos ausentes: ' + ', '.join(sorted(missing)))
            wanted = sorted({'COD_ID', *CUSTOMER_FIELDS, *MONTH_FIELDS} & columns.keys())
            projection = ', '.join(f'{quote_identifier(columns[k])} AS {quote_identifier(k)}' for k in wanted)
            keys = [k for k in self.entry.get('key', '').split(',') if k in columns.values()]
            order = ', '.join(quote_identifier(k) for k in keys) or quote_identifier(columns['COD_ID'])
            layer = dataset.ExecuteSQL(f'SELECT {projection} FROM {relation} ORDER BY {order}')
            if layer is None:
                raise RuntimeError('query failed')
            return dataset, layer
        except ValueError:
            raise
        except Exception:
            raise RuntimeError(
                f'Não foi possível ler {self.entry["schema"]}.{self.entry["table"]}. '
                'Confira o servidor, a conexão salva e as permissões no QGIS.'
            ) from None
        finally:
            gdal.PopErrorHandler()
