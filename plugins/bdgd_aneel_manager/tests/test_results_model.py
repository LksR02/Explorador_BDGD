import math
import unittest

from bdgd_aneel_manager.analyses.results_model import (
    br_number, filter_rows, make_series, normalize_rows, selection_manifest, summarize,
)


def cell(month='2023-01', energy=100, customers=10, municipality='A', group='B1',
         tip_cc='RES-Tipo01', gd='Sem indicação em CEG_GD'):
    return dict(ano_mes=month, energia=energy, clientes=customers, MUNICIPIO=municipality,
                GRU_TAR=group, patamar_consumo='100_500', TIP_CC=tip_cc, GD_CADASTRO=gd)


class ResultsModelTests(unittest.TestCase):
    def test_brazilian_numbers_and_undefined_values(self):
        self.assertEqual(br_number(1234.567), '1.234,57')
        self.assertEqual(br_number(None), '—')
        self.assertEqual(br_number(float('nan')), '—')

    def test_weighted_mean_not_mean_of_means(self):
        rows, issues = normalize_rows([cell(energy=100, customers=10), cell(energy=900, customers=30, municipality='B')])
        self.assertEqual(issues, {})
        stats = summarize(rows)
        self.assertEqual(stats['average'], 25)  # (100+900)/(10+30), not (10+30)/2
        self.assertEqual(stats['customers'], 40)

    def test_exposure_is_not_unique_customers(self):
        rows, _ = normalize_rows([cell('2023-01', 100, 10), cell('2023-02', 900, 30)])
        stats = summarize(rows)
        self.assertEqual(stats['exposure'], 40)
        self.assertEqual(stats['customers'], 20)
        _, series, coverage = make_series(rows, metric='average', frequency='quarter')
        self.assertEqual(series['Total'], [25])
        self.assertEqual(coverage['Total'], [2])

    def test_filters_multiple_values_years_and_explicit_empty(self):
        rows, _ = normalize_rows([cell('2023-01'), cell('2024-01'), cell('2025-01', municipality='B')])
        scoped = filter_rows(rows, {'year': ['2023', '2025'], 'MUNICIPIO': ['A', 'B']})
        self.assertEqual(len(scoped), 2)
        self.assertEqual(filter_rows(rows, {'year': []}), [])
        self.assertEqual(len(filter_rows(rows, {})), 3)

    def test_filters_load_curve_type_and_gd_registry_status(self):
        rows, _ = normalize_rows([
            cell(tip_cc='RES-Tipo01', gd='Sem indicação em CEG_GD'),
            cell(municipality='B', tip_cc='RES-Tipo02', gd='Com indicação em CEG_GD'),
        ])
        scoped = filter_rows(rows, {
            'TIP_CC': ['RES-Tipo02'],
            'GD_CADASTRO': ['Com indicação em CEG_GD'],
        })
        self.assertEqual(len(scoped), 1)
        self.assertEqual(scoped[0]['MUNICIPIO'], 'B')

    def test_calendar_gaps_remain_none(self):
        rows, _ = normalize_rows([cell('2023-01'), cell('2025-01', energy=0)])
        labels, series, _ = make_series(rows)
        self.assertEqual(len(labels), 25)
        self.assertEqual(series['Total'][1:-1], [None] * 23)
        self.assertEqual(series['Total'][-1], 0)

    def test_year_overlay_does_not_pair_different_months(self):
        rows, _ = normalize_rows([cell('2023-01'), cell('2025-02', energy=200)])
        labels, series, _ = make_series(rows, compare_years=True)
        self.assertEqual(labels, [f'{m:02d}' for m in range(1, 13)])
        self.assertEqual(series['2023'][:2], [100, None])
        self.assertEqual(series['2025'][:2], [None, 200])

    def test_top_n_preserves_energy_in_residual(self):
        rows, _ = normalize_rows([cell(energy=10 + i, municipality=str(i)) for i in range(10)])
        _, series, _ = make_series(rows, group='MUNICIPIO', top_n=3)
        self.assertEqual(len(series), 4)
        self.assertAlmostEqual(sum(values[0] for values in series.values()), sum(r['energia'] for r in rows))

    def test_duplicate_cells_and_nonfinite_data_are_reported(self):
        rows, issues = normalize_rows([cell(), cell(), cell('invalid'), cell('2023-02', energy=math.inf),
                                      cell('2023-03', customers=-1), cell('2023-04', energy=20, customers=0)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(issues['celulas_duplicadas_excluidas'], 2)
        self.assertEqual(issues['valor_invalido'], 2)
        self.assertEqual(issues['periodo_invalido'], 1)
        self.assertIsNone(rows[0]['consumo_medio'])
        self.assertIsNone(summarize(rows)['average'])

    def test_export_manifest_contains_definitions_and_no_connection_uri(self):
        manifest = selection_manifest({'source': {'type': 'postgres', 'connection': 'BDGD', 'uri': 'password=secret'}}, {}, {}, {})
        self.assertNotIn('uri', manifest['source'])
        self.assertIn('exposure', manifest['definitions'])

    def test_annual_aggregation_and_monthly_sum_agree(self):
        rows, _ = normalize_rows([cell(f'2023-{m:02d}', energy=m * 10, customers=m) for m in range(1, 13)])
        _, series, coverage = make_series(rows, frequency='year')
        self.assertEqual(series['Total'], [780])
        self.assertEqual(coverage['Total'], [12])
        _, series, _ = make_series(rows, metric='customers', frequency='year')
        self.assertEqual(series['Total'], [6.5])
