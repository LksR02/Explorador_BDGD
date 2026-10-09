import unittest
from copy import deepcopy
from unittest.mock import patch

from bdgd_aneel_manager.analyses.distributed_generation import (
    canonical, key, link_generators, attach_gd, network_aggregates,
)
from bdgd_aneel_manager.analyses.variable_catalog import variable_value


def uc(cod='C1', **extras):
    return canonical(dict(COD_ID=cod, CEG_GD='GD.RJ.123', PN_CON='P1', PAC='A1',
                          UNI_TR_MT='T1', CTMT='F1', **extras))


def ug(cod='G1', **extras):
    row = dict(COD_ID=cod, CEG_GD='GD.RJ.123', PN_CON='P1', PAC='A1',
               UNI_TR_MT='T1', CTMT='F1', SIT_ATIV='AT', DAT_CON='15/06/2025', POT_INST=5,
               **{f'ENE_{m:02d}': m for m in range(1,13)})
    row.update(extras)
    return canonical(row)


class DistributedGenerationTests(unittest.TestCase):
    def test_placeholder_rejected_but_unique_physical_link_works(self):
        self.assertIsNone(key('GD.RJ.000.000.000'))
        c, g = uc(), ug(CEG_GD='GD.RJ.000.000.000')
        linked, uncertain, audit = link_generators([g], [c])
        self.assertEqual(linked['C1'][0][1], 'PN_CON')
        self.assertFalse(uncertain)

    def test_global_consumers_prevent_false_uniqueness_in_sample(self):
        a, b = uc(), dict(uc(), COD_ID='C2')
        linked, uncertain, _ = link_generators([ug()], [a,b])
        self.assertFalse(linked)
        self.assertEqual(uncertain, {'C1','C2'})

    def test_conflicting_ceg_and_point_cannot_be_linked(self):
        a = dict(uc(), PN_CON='P2', PAC='A2')
        b = dict(uc(), COD_ID='C2', CEG_LINK='GD.RJ.999')
        linked, uncertain, _ = link_generators([ug()], [a,b])
        self.assertFalse(linked)
        self.assertEqual(uncertain, {'C1','C2'})

    def test_duplicate_generator_ids_not_counted_twice(self):
        linked, uncertain, audit = link_generators([ug(),ug()], [uc()])
        self.assertFalse(linked)
        self.assertIn('C1', uncertain)
        self.assertEqual(audit['invalid_or_duplicate_generator_id'], 2)
        self.assertEqual(network_aggregates([ug(),ug()], 2025)['CTMT'], {})

    def test_attachment_preserves_rows_energy_and_weights(self):
        row = dict(uc(), peso_amostral=9, **{f'ENE_{m:02d}': 100+m for m in range(1,13)})
        before = deepcopy(row)
        rows = [row]
        audit, networks = attach_gd(rows, [ug(),ug('G2', CEG_GD='GD.RJ.456', PN_CON='P2', PAC='A2')], [uc()], {'C1'}, 2025)
        self.assertEqual({k:row[k] for k in before}, before)
        self.assertEqual(len(rows),1)
        self.assertEqual(row['GD_QTD'],1)
        self.assertEqual(row['GD_POT_KW'],5)
        self.assertEqual(row['GD_ENE_TOTAL'],78)
        self.assertEqual(row['GD_CTMT_QTD'],2)  # includes unsampled generator
        self.assertEqual(row['GD_CTMT_POT_KW'],10)
        self.assertEqual(row['GD_CONSUMO_ANTES'],103)
        self.assertEqual(row['GD_CONSUMO_APOS'],109.5)
        self.assertEqual(row['GD_MESES_ANTES'],5)
        self.assertEqual(row['GD_MESES_APOS'],6)

    def test_missing_energy_not_zero_and_multiple_units_aggregate_once(self):
        row = dict(uc(), **{f'ENE_{m:02d}': 1 for m in range(1,13)})
        attach_gd([row], [ug(), ug('G2', ENE_02='')], [uc()], {'C1'}, 2025)
        self.assertEqual(row['GD_QTD'],2)
        self.assertEqual(row['GD_POT_KW'],10)
        self.assertEqual(row['GD_ENE_01'],2)
        self.assertEqual(row['GD_ENE_02'],'')
        self.assertEqual(row['GD_ENE_TOTAL'],'')
        self.assertEqual(row['GD_CONSUMO_ANTES'],'')

    def test_unverified_uc_and_missing_link_remain_indeterminate(self):
        row = uc()
        attach_gd([row], [ug()], [uc()], set(), 2025)
        self.assertEqual(row['GD_PRESENCA'], 'Indeterminada')
        self.assertIsNone(variable_value(row, 'GD_PRESENCA'))
        self.assertEqual(row['GD_POT_KW'],'')

    def test_invalid_power_propagates_to_network_sum(self):
        result = network_aggregates([ug(),ug('G2', POT_INST='')], 2025)
        self.assertEqual(result['CTMT']['F1']['QTD'],2)
        self.assertEqual(result['CTMT']['F1']['POT_KW'],'')
        self.assertEqual(network_aggregates([ug(SIT_ATIV='DS')], 2025)['CTMT'], {})

    def test_customer_without_own_generator_gets_network_context(self):
        consumer = dict(uc(), COD_ID='C2', CEG_GD='', CEG_LINK='', PN_CON='P2', PAC='A2')
        rows = [dict(consumer)]
        attach_gd(rows,[ug()],[uc(),consumer],{'C2'},2025)
        self.assertEqual(rows[0]['GD_PRESENCA'],'Não identificada')
        self.assertEqual(rows[0]['GD_POT_KW'],'')
        self.assertEqual(rows[0]['GD_TRAFO_QTD'],1)
        self.assertEqual(rows[0]['GD_CTMT_POT_KW'],5)

    def test_power_ratio_not_energy_share(self):
        self.assertEqual(variable_value({'GD_POT_KW': 5, 'CAR_INST':10}, 'GD_POT_CARGA'), .5)
        self.assertIsNone(variable_value({'GD_POT_KW': 5, 'CAR_INST':0}, 'GD_POT_CARGA'))

    def test_empty_inventory_is_unknown_not_zero_generation(self):
        from bdgd_aneel_manager.analyses.distributed_generation import enrich_gd_year
        rows = [uc()]
        results = [[{'table_name':'ugbt_tab'}], [{'column_name':'cod_id'},{'column_name':'pac'}], []]
        with patch('bdgd_aneel_manager.analyses.distributed_generation.fetch_dicts', side_effect=results):
            audit, networks = enrich_gd_year(None, {'schema':'test','table':'ucbt_tab','year':2020}, {}, rows,[], {'C1'})
        self.assertFalse(audit['available'])
        self.assertIsNone(networks)
        self.assertEqual(rows[0]['GD_PRESENCA'],'Indeterminada')
        self.assertEqual(rows[0]['GD_CTMT_POT_KW'],'')
