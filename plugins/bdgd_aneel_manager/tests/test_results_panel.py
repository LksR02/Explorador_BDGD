import csv
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qgis.PyQt.QtWidgets import QApplication
from qgis.PyQt.QtCore import QSettings, Qt
from bdgd_aneel_manager.results_panel import ResultsPanel, TABLE_FIELDS


class ResultsPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.panel = ResultsPanel(settings=QSettings(str(Path(self.tmp.name) / 'prefs.ini'), QSettings.IniFormat))
        rows = [dict(ano_mes=f'{2023 + i // 12}-{i % 12 + 1:02d}', energia=(i + 1) * 10,
                     clientes=10, MUNICIPIO='A', GRU_TAR='B1', patamar_consumo='100_500',
                     TIP_CC='RES-Tipo01' if i % 2 == 0 else 'RES-Tipo02',
                     GD_CADASTRO='Com indicação em CEG_GD' if i % 3 == 0 else 'Sem indicação em CEG_GD')
                for i in range(24)]
        self.panel.set_results(rows, {'distributor': 'TEST', 'years': [2023, 2024]})

    def tearDown(self):
        self.panel.timer.stop()
        self.panel.table_search_timer.stop()
        self.panel.close()
        self.panel.settings.sync()
        self.panel.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def test_filter_changes_metrics_and_table_together(self):
        self.panel.filters['year'].set_values(['2024'])
        self.panel.refresh()
        self.assertEqual(len(self.panel.filtered), 12)
        self.assertEqual(len(self.panel.table_model.rows), 12)
        self.assertEqual(self.panel.cards['average'][0].text(), '18,50')

    def test_numeric_sort_applies_before_pagination(self):
        model = self.panel.table_model
        model.page_size = 5
        energy_column = TABLE_FIELDS.index('energia')
        model.sort(energy_column, Qt.DescendingOrder)
        self.assertEqual(model.rows[0]['energia'], 240)
        model.set_page(1)
        self.assertEqual(model.data(model.index(0, energy_column)), '190,00')

    def test_filters_load_curve_type_and_gd_registry_status(self):
        self.panel.filters['TIP_CC'].set_values(['RES-Tipo01'])
        self.panel.filters['GD_CADASTRO'].set_values(['Com indicação em CEG_GD'])
        self.panel.refresh()
        self.assertEqual(len(self.panel.filtered), 4)
        self.assertTrue(all(row['TIP_CC'] == 'RES-Tipo01' for row in self.panel.filtered))
        self.assertTrue(all(row['GD_CADASTRO'] == 'Com indicação em CEG_GD' for row in self.panel.filtered))

    def test_export_includes_all_rows_not_only_current_page(self):
        self.panel.table_model.page_size = 5
        path = Path(self.tmp.name) / 'view.csv'
        self.panel.write_export(path)
        with path.open(encoding='utf-8-sig') as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 24)
        self.assertTrue(path.with_suffix('.view.json').exists())

    def test_empty_selection_clears_chart_table_and_cards(self):
        self.panel.filters['year'].set_values([])
        self.panel.refresh()
        self.assertEqual(self.panel.filtered, [])
        self.assertEqual(self.panel.table_model.rowCount(), 0)
        self.assertEqual(self.panel.cards['average'][0].text(), '—')

    def test_overlay_and_bar_chart_and_unit_preferences(self):
        self.panel.compare.setChecked(True)
        self.panel.chart_type.setCurrentIndex(1)
        self.panel.refresh()
        self.assertFalse(self.panel.frequency.isEnabled())
        self.assertEqual(set(self.panel.chart_series), {'2023', '2024'})
        self.assertEqual(len(self.panel.chart_labels), 12)

    def test_table_search_does_not_change_metrics(self):
        before = self.panel.cards['energy'][0].text()
        self.panel.table_search.setText('2024-01')
        self.panel.refresh_table()
        self.assertEqual(len(self.panel.table_model.rows), 1)
        self.assertEqual(self.panel.cards['energy'][0].text(), before)

    def test_load_new_result_does_not_keep_old_filters(self):
        self.panel.filters['year'].set_values(['2024'])
        self.panel.set_results([], {})
        self.assertEqual(self.panel.filtered, [])
        self.assertEqual(self.panel.selection()['year'], [])

    def test_saved_view_restores_filters_and_chart(self):
        self.panel.filters['year'].set_values(['2024'])
        self.panel.compare.setChecked(True)
        with patch('bdgd_aneel_manager.results_panel.QInputDialog.getText', return_value=('Teste', True)):
            self.panel.save_preset()
        self.panel.reset_filters()
        self.panel.compare.setChecked(False)
        self.panel.load_preset()
        self.assertEqual(self.panel.selection()['year'], ['2024'])
        self.assertTrue(self.panel.compare.isChecked())

    def test_at_least_one_export_column_remains_visible(self):
        for action in self.panel.column_actions:
            action.setChecked(False)
        self.assertEqual(sum(action.isChecked() for action in self.panel.column_actions), 1)

    def test_manifest_records_sort_and_column_order(self):
        self.panel.table_model.sort(TABLE_FIELDS.index('energia'), Qt.DescendingOrder)
        self.panel.table.horizontalHeader().moveSection(0, 3)
        controls = self.panel.manifest()['controls']
        self.assertTrue(controls['table_sort']['descending'])
        self.assertEqual(controls['table_sort']['field'], 'energia')
        self.assertEqual(controls['column_order'][3], 'ano_mes')
