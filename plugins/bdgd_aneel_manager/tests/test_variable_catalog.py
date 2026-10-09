from copy import deepcopy
import unittest

from bdgd_aneel_manager.analyses.variable_catalog import variable_value, profile_variables
from bdgd_aneel_manager.analyses.panel_enrichment import merge_year, sql_literal


class VariableCatalogTests(unittest.TestCase):
    def test_codes_remain_categories_and_identifiers_are_not_predictors(self):
        from bdgd_aneel_manager.analyses.variable_catalog import CATALOG
        self.assertEqual(variable_value({'TEN_FORN': '127'}, 'TEN_FORN'), '127')
        self.assertEqual(variable_value({'CLAS_SUB': '001'}, 'CLAS_SUB'), '001')
        self.assertNotIn('COD_ID', CATALOG)
        self.assertNotIn('CTMT', CATALOG)
        self.assertEqual(variable_value({'CNAE': '0111-3/01'}, 'CNAE_DIVISAO'), '01')

    def test_connection_age_uses_data_year_and_rejects_future_sentinels(self):
        row = {'DAT_CON': '31/12/2020', 'source_year': '2025'}
        self.assertAlmostEqual(variable_value(row, 'IDADE_CONEXAO'), 5, delta=.01)
        for value in ('31/12/2099', '31/12/1800', 'not a date'):
            self.assertIsNone(variable_value(dict(row, DAT_CON=value), 'IDADE_CONEXAO'))

    def test_quality_metrics_keep_zero_but_not_missing_or_negative(self):
        self.assertEqual(variable_value({'DIC_01': 0}, 'DIC_01'), 0)
        self.assertIsNone(variable_value({'DIC_01': -1}, 'DIC_01'))
        self.assertIsNone(variable_value({'CAR_INST': 0}, 'CAR_INST'))
        self.assertIsNone(variable_value({'FIC_01': .5}, 'FIC_01'))
        row = {f'DIC_{m:02d}': m for m in range(1, 13)}
        self.assertEqual(variable_value(row, 'DIC_TOTAL'), 78)
        row.pop('DIC_12')
        self.assertIsNone(variable_value(row, 'DIC_TOTAL'))
        self.assertIsNone(variable_value({'DIC': 78}, 'DIC_TOTAL'))

    def test_profile_reports_constant_incomplete_and_year_coverage(self):
        rows = [dict(CAR_INST=10, source_year='2025'), dict(CAR_INST='', source_year='2017')]
        profile = {p['key']: p for p in profile_variables(rows)}
        self.assertEqual(profile['CAR_INST']['coverage'], .5)
        self.assertEqual(profile['CAR_INST']['distinct'], 1)
        self.assertEqual(profile['CAR_INST']['years'], ['2025'])

    def test_enrichment_does_not_multiply_rows_or_change_sample_energy(self):
        def row(key):
            return dict(COD_ID=key, peso_amostral=17, **{f'ENE_{m:02d}': m for m in range(1, 13)})
        rows = [row(key) for key in ('A', 'B', 'C', 'D')]
        original = deepcopy(rows)
        records = [dict(row('A'), CAR_INST=10), dict(row('B'), CAR_INST=20),
                   dict(row('B'), CAR_INST=30), dict(row('C'), ENE_01=999, CAR_INST=40)]
        counts = merge_year(rows, records, ['CAR_INST'])
        self.assertEqual(counts, dict(matched=1, unmatched=1, ambiguous=1, changed_energy=1))
        self.assertEqual(rows[0]['CAR_INST'], 10)
        self.assertTrue(all(r['CAR_INST'] == '' for r in rows[1:]))
        self.assertEqual([{k: r[k] for k in original[i]} for i, r in enumerate(rows)], original)
        self.assertEqual(sql_literal("client'1"), "'client''1'")
