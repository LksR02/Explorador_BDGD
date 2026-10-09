import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from osgeo import gdal, ogr
from bdgd_aneel_manager.analyses import demand_mvp as demand
from bdgd_aneel_manager.analyses.postgres_source import analysis_entries, quote_identifier, PostgresSource


class MemorySource:
    def open_layer(self):
        ds = ogr.GetDriverByName('Memory').CreateDataSource('')
        layer = ds.CreateLayer('UCBT_tab', geom_type=ogr.wkbNone)
        for name in ['COD_ID', *demand.CUSTOMER_FIELDS, *demand.MONTH_FIELDS]:
            layer.CreateField(ogr.FieldDefn(name, ogr.OFTReal if name in demand.MONTH_FIELDS else ogr.OFTString))
        for index in range(6):
            feature = ogr.Feature(layer.GetLayerDefn())
            for name in ['COD_ID', *demand.CUSTOMER_FIELDS]:
                feature.SetField(name, str(index) if name == 'COD_ID' else 'A')
            for name in demand.MONTH_FIELDS:
                feature.SetField(name, 10.0 + index)
            layer.CreateFeature(feature)
        return ds, layer


class PostgresAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.scope = dict(connection='Test', distributor='TEST', years=[2025, 2023], schemas=['test_2025', 'test_2023'])
        self.entries = [dict(schema=f'test_{y}', table='ucbt_tab', year=y, distributor='TEST',
                             readable=True, status='success', rows=6, key='fid') for y in (2023, 2025)]

    def test_resolves_only_selected_years_and_prefers_tab(self):
        entries = self.entries + [dict(self.entries[0], table='ucbt')]
        selected = analysis_entries(entries, self.scope)
        self.assertEqual(sorted(selected), [2023, 2025])
        self.assertEqual(selected[2023]['table'], 'ucbt_tab')

    def test_rejects_missing_failed_unreadable_and_duplicate_years(self):
        for overrides in ({'status': 'failed'}, {'status': 'running'}, {'readable': False}, {'table': 'other'}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                analysis_entries([dict(e, **overrides) for e in self.entries], self.scope)
        with self.assertRaises(ValueError):
            analysis_entries(self.entries, dict(self.scope, years=[2023, 2023]))

    def test_identifier_quoting_and_secret_repr(self):
        self.assertEqual(quote_identifier('a"b'), '"a""b"')
        self.assertNotIn('secret', repr(PostgresSource('password=secret', self.entries[0])))

    def test_normalizes_lowercase_postgres_columns(self):
        class Probe:
            schema = [type('Field', (), {'name': k.lower()}) for k in ['COD_ID', *demand.CUSTOMER_FIELDS, *demand.MONTH_FIELDS]]
        class Dataset:
            def __init__(self):
                self.queries = []
            def ExecuteSQL(self, sql):
                self.queries.append(sql)
                return Probe()
            def ReleaseResultSet(self, layer):
                pass
        dataset = Dataset()
        with patch.object(gdal, 'OpenEx', return_value=dataset):
            PostgresSource('service=test', self.entries[0]).open_layer()
        self.assertIn('"ene_01" AS "ENE_01"', dataset.queries[1])
        self.assertIn('"test_2023"."ucbt_tab"', dataset.queries[1])
        self.assertNotIn('geom', dataset.queries[1])

    def test_census_and_sample_preserve_totals_on_small_source(self):
        rows, census = demand.read_year(MemorySource(), 2023, {})
        sampled, ctmt, strata, diagnostics = demand.read_year_stratified(MemorySource(), 2023, {})
        self.assertEqual(len(rows), 6)
        self.assertEqual(len(sampled), 6)
        self.assertEqual(census['A']['energy'], ctmt['A']['energy'])
        self.assertEqual(sum(s['populacao'] for s in strata), 6)

    def test_gd_registry_status_does_not_treat_placeholder_as_generation(self):
        self.assertEqual(demand.gd_registry_status('GD.RJ.123'), demand.GD_WITH_REGISTRY)
        self.assertEqual(demand.gd_registry_status(''), demand.GD_WITHOUT_REGISTRY)
        self.assertEqual(demand.gd_registry_status('GD.RJ.000.000.000'), demand.GD_WITHOUT_REGISTRY)

    def test_nonconsecutive_years_and_provenance_in_outputs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(demand, 'load_ibge_municipalities', return_value=({}, {})):
            result = demand.build_outputs(tmp, 'TEST', 2023, 2025, sampling_mode='census',
                                          sources={2023: MemorySource(), 2025: MemorySource()},
                                          source_metadata={'type': 'postgres', 'connection': 'Test'})
            self.assertEqual(result['years'], [2023, 2025])
            self.assertEqual(result['source']['type'], 'postgres')
            output = Path(result['output_dir'])
            with (output / 'TEST_customer_panel_2023_2025.csv').open(encoding='utf-8-sig') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 12)
            self.assertEqual({r['source_year'] for r in rows}, {'2023', '2025'})
            self.assertNotIn('password', (output / 'run_metadata.json').read_text())

    def test_selected_output_levels_and_strata_are_materialized(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(demand, 'load_ibge_municipalities', return_value=({}, {})):
            result = demand.build_outputs(
                tmp, 'TEST', 2023, 2025, sampling_mode='stratified',
                output_levels=('distributor', 'substation', 'set', 'transformer', 'connection'),
                strata_fields=('TIP_CC', 'ARE_LOC'),
                sources={2023: MemorySource(), 2025: MemorySource()},
            )
            output = Path(result['output_dir'])
            expected = {
                'TEST_distributor_monthly_2023_2025.csv', 'TEST_sub_monthly_2023_2025.csv',
                'TEST_conj_monthly_2023_2025.csv', 'TEST_untrmt_monthly_2023_2025.csv',
                'TEST_pncon_monthly_2023_2025.csv', 'TEST_sampling_strata_2023_2025.csv',
                'TEST_segments_monthly_2023_2025.csv',
            }
            self.assertEqual(set(result['files']), expected)
            self.assertFalse((output / 'TEST_customer_panel_2023_2025.csv').exists())
            self.assertEqual(result['sampling']['stratification'], ['TIP_CC', 'ARE_LOC'])
            with (output / 'TEST_sampling_strata_2023_2025.csv').open(encoding='utf-8-sig') as handle:
                first = next(csv.DictReader(handle))
            self.assertEqual(first['TIP_CC'], 'A')
            self.assertEqual(first['ARE_LOC'], 'A')
            with (output / 'TEST_distributor_monthly_2023_2025.csv').open(encoding='utf-8-sig') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 24)
            self.assertEqual(float(rows[0]['energia']), 75.0)

    def test_cancel_keeps_previous_panel(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(demand, 'load_ibge_municipalities', return_value=({}, {})):
            output = Path(tmp) / 'TEST_ANALISES'
            output.mkdir()
            panel = output / 'TEST_customer_panel_2023_2025.csv'
            panel.write_text('previous')
            with self.assertRaises(demand.AnalysisCancelled):
                demand.build_outputs(tmp, 'TEST', 2023, 2025, sources={2023: MemorySource(), 2025: MemorySource()},
                                     cancel_callback=lambda: True)
            self.assertEqual(panel.read_text(), 'previous')
            self.assertFalse(panel.with_suffix('.csv.tmp').exists())

    def test_cancel_after_last_year_keeps_previous_panel(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(demand, 'load_ibge_municipalities', return_value=({}, {})):
            output = Path(tmp) / 'TEST_ANALISES'
            output.mkdir()
            panel = output / 'TEST_customer_panel_2023_2025.csv'
            panel.write_text('previous')
            cancelled = []
            def progress(payload):
                if payload.get('stage') == 'Ano concluído' and payload.get('year') == 2025:
                    cancelled.append(True)
            with self.assertRaises(demand.AnalysisCancelled):
                demand.build_outputs(tmp, 'TEST', 2023, 2025, sources={2023: MemorySource(), 2025: MemorySource()},
                                     cancel_callback=lambda: bool(cancelled), progress_callback=progress)
            self.assertEqual(panel.read_text(), 'previous')


if __name__ == '__main__':
    unittest.main()
