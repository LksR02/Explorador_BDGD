import unittest

from qgis.PyQt.QtWidgets import QApplication
from qgis.core import QgsVectorLayer

from bdgd_aneel_manager.semantic_catalog import (
    apply_layer_semantics, display_name, entity_code, entity_info, matches_profile, theme_title,
)
from bdgd_aneel_manager.postgres_browser import PostgresBrowser
from bdgd_aneel_manager.postgres_browser import (
    _configure_density_layer, _configure_detail_layer, catalog_entries,
    detail_visibility_scale, ponnot_density_query, ponnot_spatial_query,
)


class SemanticCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_normalizes_common_table_suffix(self):
        self.assertEqual(entity_code('public.UCBT_tab'), 'UCBT')

    def test_keeps_official_code_in_friendly_name(self):
        self.assertEqual(display_name('UGBT'), 'Unidades geradoras de baixa tensão (UGBT)')

    def test_unknown_entity_has_safe_fallback(self):
        info = entity_info('ABC_X_tab')
        self.assertEqual(info['code'], 'ABC_X')
        self.assertEqual(info['theme'], 'auxiliary')
        self.assertFalse(info['known'])

    def test_profiles(self):
        self.assertTrue(matches_profile('SUB', 'essential'))
        self.assertFalse(matches_profile('UCBT', 'essential'))
        self.assertTrue(matches_profile('UCBT', 'customers'))
        self.assertTrue(matches_profile('anything', 'complete'))

    def test_theme_title_is_ordered(self):
        self.assertEqual(theme_title('UGMT'), '05 · Geração')

    def test_applies_layer_name_aliases_and_traceability(self):
        layer = QgsVectorLayer('Point?field=COD_ID:string&field=ENE_01:double', 'UCBT', 'memory')
        apply_layer_semantics(layer, 'UCBT_tab')
        self.assertEqual(layer.name(), 'Unidades consumidoras de baixa tensão (UCBT)')
        self.assertEqual(layer.attributeDisplayName(0), 'Identificador do registro')
        self.assertEqual(layer.attributeDisplayName(1), 'Energia — mês 01 (kWh)')
        self.assertEqual(layer.customProperty('bdgd/source_table'), 'UCBT_tab')

    def test_postgres_browser_shows_meaning_and_applies_default_profile(self):
        browser = PostgresBrowser(None)
        browser.loaded_connection = 'Teste'
        browser.entries = [
            dict(schema='demo_2025', table=table, distributor='DEMO', year=2025,
                 rows=10, count_source='estimativa', status='success', geometry='geom',
                 key='COD_ID', readable=True)
            for table in ('SUB', 'CTMT', 'UCBT')
        ]
        browser.distributor_combo.addItem('DEMO')
        browser.populate_years()
        self.assertEqual(browser.tables.item(0, 1).text(), 'Subestações')
        self.assertEqual(browser.tables.item(0, 2).text(), 'SUB')
        selected = {index.row() for index in browser.tables.selectedIndexes()}
        self.assertEqual(selected, {0, 1})
        browser.close()
        browser.deleteLater()

    def test_catalog_detects_table_georeferenceable_by_ponnot(self):
        rows = [('demo_2025', 'ucbt_tab', 12, '', 'objectid', True, True, 'geom')]
        entry = catalog_entries(rows, [])[0]
        self.assertTrue(entry['references_ponnot'])
        self.assertEqual(entry['ponnot_geometry'], 'geom')

    def test_ponnot_query_preserves_every_source_record(self):
        entry = dict(schema='demo_2025', table='ucbt_tab', ponnot_geometry='geom')
        query = ponnot_spatial_query(entry)
        self.assertIn('LEFT JOIN "demo_2025"."ponnot"', query)
        self.assertIn('p."cod_id" = t."pn_con"', query)
        self.assertIn('SELECT t.*', query)
        self.assertNotIn('DISTINCT', query.upper())
        self.assertNotIn('GROUP BY', query.upper())

    def test_density_query_aggregates_only_the_overview_by_real_point(self):
        entry = dict(schema='demo_2025', table='ucbt_tab', ponnot_geometry='geom')
        query = ponnot_density_query(entry)
        self.assertIn('COUNT(*)::bigint AS "__bdgd_count"', query)
        self.assertIn('GROUP BY t."pn_con"', query)
        self.assertIn('p."cod_id" = c."pn_con"', query)
        self.assertIn('p."geom" AS "__bdgd_geom"', query)
        self.assertNotIn('ST_CLUSTER', query.upper())

    def test_dense_layers_require_a_closer_zoom(self):
        self.assertEqual(detail_visibility_scale(2_887_181), 10_000)
        self.assertEqual(detail_visibility_scale(679_141), 20_000)
        self.assertEqual(detail_visibility_scale(119_565), 30_000)
        self.assertEqual(detail_visibility_scale(5_769), 200_000)

    def test_density_and_exact_records_exchange_visibility_at_the_boundary(self):
        entry = {'rows': 2_887_181}
        detail = QgsVectorLayer('Point?crs=EPSG:4674', 'detail', 'memory')
        density = QgsVectorLayer('Point?field=__bdgd_count:integer&crs=EPSG:4674', 'density', 'memory')
        _configure_detail_layer(detail, entry)
        _configure_density_layer(density, entry)
        self.assertEqual(detail.renderer().type(), 'singleSymbol')
        self.assertEqual(density.renderer().type(), 'heatmapRenderer')
        self.assertFalse(detail.isInScaleRange(50_000))
        self.assertTrue(density.isInScaleRange(50_000))
        self.assertFalse(detail.isInScaleRange(10_000))
        self.assertTrue(density.isInScaleRange(10_000))
        self.assertTrue(detail.isInScaleRange(5_000))
        self.assertFalse(density.isInScaleRange(5_000))


if __name__ == '__main__':
    unittest.main()
