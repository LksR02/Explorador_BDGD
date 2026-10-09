import unittest

from qgis.PyQt.QtWidgets import QApplication, QCheckBox, QLabel, QWidget

from bdgd_aneel_manager.plugin import format_publication_date
from bdgd_aneel_manager.postgres_browser import br_int
from bdgd_aneel_manager.ui_kit import (
    Badge, CollapsibleSection, FlowLayout, NavRail, SegmentedControl, StatusLine, infer_level,
)


class UiKitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_brazilian_thousands_and_publication_dates(self):
        self.assertEqual(br_int(2887181), '2.887.181')
        self.assertEqual(br_int(None), '—')
        self.assertEqual(format_publication_date('Fri, 10 Oct 2025 13:51:21 GMT'), '10/10/2025 13:51')
        self.assertEqual(format_publication_date('2025-12-31'), '2025-12-31')
        self.assertEqual(format_publication_date(''), '—')

    def test_status_tone_follows_message(self):
        self.assertEqual(infer_level('Falha no download.'), 'danger')
        self.assertEqual(infer_level('Download pausado. O arquivo parcial foi preservado.'), 'warning')
        self.assertEqual(infer_level('ENEL_RJ_2025 sincronizada com sucesso.'), 'success')
        self.assertEqual(infer_level('Baixando ENEL_RJ_2025'), 'running')
        line = StatusLine('Pronto.')
        line.setText('Mensagem neutra', 'info')
        self.assertEqual((line.text(), line.level), ('Mensagem neutra', 'info'))

    def test_segmented_control_is_exclusive_and_reports_clicks(self):
        control = SegmentedControl(['A', ('B', 'dica'), 'C'])
        seen = []
        control.changed.connect(seen.append)
        control.buttons[2].click()
        self.assertEqual((control.currentIndex(), seen), (2, [2]))
        control.setCurrentIndex(1)
        self.assertEqual(sum(button.isChecked() for button in control.buttons), 1)
        self.assertEqual(control.buttons[1].toolTip(), 'dica')

    def test_navigation_rail_states_and_compact_mode(self):
        rail = NavRail('Marca', 'Sub', 'Rodapé')
        for title in ('Um', 'Dois'):
            rail.add_item(title, 'Ctrl+1')
        clicked = []
        rail.currentChanged.connect(clicked.append)
        rail.items[1].clicked.emit(1)
        rail.set_current(1)
        rail.items[0].set_status('Concluído', 'done')
        self.assertEqual(clicked, [1])
        self.assertTrue(rail.items[1].property('active'))
        self.assertEqual(rail.items[0].number.text(), '✓')
        rail.set_compact(True, user=True)
        self.assertEqual((rail.width(), rail.user_compact), (68, True))
        self.assertTrue(rail.items[0].texts.isHidden())

    def test_collapsible_section_and_badge_visibility(self):
        body = QLabel('conteúdo')
        section = CollapsibleSection('Avançado', body, expanded=False)
        self.assertTrue(body.isHidden())
        section.button.click()
        self.assertFalse(body.isHidden())
        badge = Badge('', 'success')
        self.assertTrue(badge.isHidden())
        badge.set_status('3/3', 'success')
        self.assertFalse(badge.isHidden())

    def test_flow_layout_wraps_chips(self):
        host = QWidget()
        layout = FlowLayout(host, spacing=4)
        for index in range(6):
            layout.addWidget(QCheckBox(f'Opção {index}'))
        narrow, wide = layout.heightForWidth(120), layout.heightForWidth(2000)
        self.assertGreater(narrow, wide)
        self.assertEqual(layout.count(), 6)
