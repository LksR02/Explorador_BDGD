"""Interactive, descriptive results workspace for the QGIS plugin."""
from __future__ import annotations

import csv
import json
import math
from html import escape as html_escape
from pathlib import Path

from qgis.PyQt.QtCore import QAbstractTableModel, QModelIndex, QSettings, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QLayout, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QPushButton, QScrollArea, QSpinBox, QSplitter, QTableView,
    QSizePolicy, QTabWidget, QToolButton, QVBoxLayout, QWidget, QWidgetAction,
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, MaxNLocator

from .analyses.results_model import (
    DIMENSIONS, KEY_FIELDS, METRICS, br_number, filter_rows, make_series,
    normalize_rows, selection_manifest, summarize,
)

SETTINGS = 'bdgd_aneel_manager/results_v1/'
FIELD_LABELS = {'patamar_consumo': 'Patamar de consumo', 'GRU_TAR': 'Grupo tarifário',
                'MUNICIPIO': 'Município', 'TIP_CC': 'Tipo de curva de carga',
                'GD_CADASTRO': 'Indicação de GD no cadastro',
                'year': 'Anos', 'month': 'Meses'}
TABLE_FIELDS = (*KEY_FIELDS, 'energia', 'clientes', 'consumo_medio')
TABLE_LABELS = ('Mês', 'Patamar', 'Grupo\ntarifário', 'Município', 'Tipo de\ncurva',
                'Indicação\nde GD', 'Consumo\n(kWh)',
                'Clientes estimados\nno mês', 'Média mensal\n(kWh/cliente)')
METRIC_START_COLUMN = len(KEY_FIELDS)
MONTHS = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun', 'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']
PALETTES = {
    'Azul e verde': ['#2463a6', '#168277', '#cb7945', '#825ca6', '#b65378', '#697888'],
    'Alto contraste': ['#0072B2', '#D55E00', '#009E73', '#CC79A7', '#E69F00', '#555555'],
    'Tons de azul': ['#12385b', '#2167a3', '#4a8ec8', '#73a4bd', '#73859a', '#344253'],
}


class MultiSelect(QToolButton):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPopupMode(QToolButton.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setMinimumWidth(190)
        menu = QMenu(self)
        container = QWidget()
        layout = QVBoxLayout(container)
        self.search = QLineEdit()
        self.search.setPlaceholderText('Buscar opções…')
        layout.addWidget(self.search)
        actions = QHBoxLayout()
        for text, checked in (('Todos', True), ('Nenhum', False)):
            button = QPushButton(text)
            button.clicked.connect(lambda _, c=checked: self.select_all(c))
            actions.addWidget(button)
        layout.addLayout(actions)
        self.items = QListWidget()
        self.items.setMinimumSize(270, 235)
        layout.addWidget(self.items)
        action = QWidgetAction(menu)
        action.setDefaultWidget(container)
        menu.addAction(action)
        self.setMenu(menu)
        self.items.itemChanged.connect(self._changed)
        self.search.textChanged.connect(self._search)
        self._changed()

    def set_options(self, values, display=None):
        self.items.blockSignals(True)
        self.items.clear()
        self.search.clear()
        for value in values:
            item = QListWidgetItem(display.get(value, value) if display else value)
            item.setData(Qt.UserRole, value)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.items.addItem(item)
        self.items.blockSignals(False)
        self._changed()

    def values(self):
        return [self.items.item(i).data(Qt.UserRole) for i in range(self.items.count())
                if self.items.item(i).checkState() == Qt.Checked]

    def set_values(self, values):
        selected = set(values)
        self.items.blockSignals(True)
        for i in range(self.items.count()):
            item = self.items.item(i)
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in selected else Qt.Unchecked)
        self.items.blockSignals(False)
        self._changed()

    def select_all(self, checked=True):
        self.set_values([self.items.item(i).data(Qt.UserRole) for i in range(self.items.count())] if checked else [])

    def _search(self, text):
        for i in range(self.items.count()):
            item = self.items.item(i)
            item.setHidden(text.casefold() not in item.text().casefold())

    def _changed(self, *_):
        values = self.values()
        everything = bool(values) and len(values) == self.items.count()
        labels = [self.items.item(i).text() for i in range(self.items.count())
                  if self.items.item(i).checkState() == Qt.Checked]
        self.setText('Todos' if everything else
                     (str(len(values)) + ' selecionados' if len(values) > 1 else labels[0] if labels else 'Nenhum'))
        self.setToolTip(', '.join(labels) or 'Nenhuma opção selecionada')
        partial = self.items.count() > 0 and not everything
        if self.property('partial') != partial:
            self.setProperty('partial', partial)
            self.style().unpolish(self)
            self.style().polish(self)
        self.changed.emit()


class PagedTableModel(QAbstractTableModel):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows, self.page, self.page_size, self.decimals = [], 0, 100, 2
        self.sort_column, self.sort_order = 0, Qt.AscendingOrder

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows, self.page = list(rows), 0
        self._sort()
        self.endResetModel()
        self.changed.emit()

    def _sort(self):
        key = TABLE_FIELDS[self.sort_column]
        values = [r for r in self.rows if r[key] is not None]
        missing = [r for r in self.rows if r[key] is None]
        values.sort(key=lambda r: r[key], reverse=self.sort_order == Qt.DescendingOrder)
        self.rows = values + missing

    def sort(self, column, order=Qt.AscendingOrder):
        if column < 0:
            return
        self.beginResetModel()
        self.sort_column, self.sort_order, self.page = column, order, 0
        self._sort()
        self.endResetModel()
        self.changed.emit()

    def set_page(self, page):
        self.beginResetModel()
        self.page = max(0, min(page, max(0, (len(self.rows) - 1) // self.page_size)))
        self.endResetModel()
        self.changed.emit()

    def rowCount(self, parent=QModelIndex()):
        return max(0, min(self.page_size, len(self.rows) - self.page * self.page_size)) if not parent.isValid() else 0

    def columnCount(self, parent=QModelIndex()):
        return len(TABLE_FIELDS) if not parent.isValid() else 0

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[self.page * self.page_size + index.row()]
        value = row[TABLE_FIELDS[index.column()]]
        if role in (Qt.DisplayRole, Qt.ToolTipRole):
            return br_number(value, self.decimals) if index.column() >= METRIC_START_COLUMN else value
        if role == Qt.TextAlignmentRole and index.column() >= METRIC_START_COLUMN:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return TABLE_LABELS[section]
        return None


class ChartToolbar(NavigationToolbar2QT):
    toolitems = tuple(t for t in NavigationToolbar2QT.toolitems if t[0] in ('Home', 'Back', 'Forward', 'Pan', 'Zoom'))


class ResultsPanel(QWidget):
    filtersChanged = pyqtSignal()
    openFolderRequested = pyqtSignal()
    addTablesRequested = pyqtSignal()

    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self.rows, self.filtered, self.metadata, self.strata, self.issues = [], [], {}, [], {}
        self.output_dir, self.loading = None, False
        self.settings = settings if settings is not None else QSettings()
        self.setObjectName('ResultsWorkspace')
        self.setStyleSheet('''
          QWidget#ResultsWorkspace {color:#203a55;}
          QFrame#resultCard {background:white; border:1px solid #dbe3ec; border-radius:10px;}
          QFrame#resultCard[accent="true"] {border-top:3px solid #0b62e0;}
          QLabel#cardTitle {color:#64788f; font-size:11px; font-weight:600;}
          QLabel#cardValue {color:#0f2438; font-size:22px; font-weight:700;}
          QFrame#filterPanel {background:white; border:1px solid #dbe3ec; border-radius:10px;}
          QLabel#filterHeading {color:#0f2438; font-size:13px; font-weight:700;}
          QLabel#filterLabel {color:#3b4f66; font-size:11px; font-weight:600; padding-top:4px;}
          QLabel#sourcePill {background:#e6efff; color:#084bb0; border-radius:11px; padding:3px 11px; font-weight:600;}
          QToolButton#multiSelect {background:white; color:#203a55; border:1px solid #b9c7d6; border-radius:6px;
                                   padding:5px 22px 5px 9px; text-align:left;}
          QToolButton#multiSelect:hover {border-color:#0b62e0;}
          QToolButton#multiSelect[partial="true"] {background:#e6efff; border-color:#0b62e0; color:#084bb0; font-weight:600;}
          QToolButton#multiSelect::menu-indicator {subcontrol-position:right center; subcontrol-origin:padding; right:7px;}
          QToolButton#menuButton {background:white; color:#1d3a58; border:1px solid #b9c7d6; border-radius:6px; padding:6px 12px;}
          QToolButton#menuButton:hover {background:#e6efff; border-color:#0b62e0;}
          QToolButton, QComboBox, QLineEdit, QSpinBox {min-height:24px;}
          QTableView {background:white; alternate-background-color:#f6f9fc; border:1px solid #dbe3ec;
                      selection-background-color:#d7e6ff; selection-color:#0f2438; gridline-color:#edf1f6;}
          QHeaderView::section {background:#f3f6fa; padding:7px; border:0; border-bottom:1px solid #dbe3ec; font-weight:600;}
        ''')
        root = QVBoxLayout(self)
        root.setSizeConstraint(QLayout.SetMinimumSize)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        header = QHBoxLayout()
        self.source_label = QLabel('Nenhum resultado carregado.')
        self.source_label.setObjectName('sourcePill')
        header.addWidget(self.source_label)
        header.addStretch()
        self.filter_toggle = QPushButton('Filtros')
        self.filter_toggle.setCheckable(True)
        self.filter_toggle.setChecked(True)
        self.filter_toggle.setToolTip('Mostrar ou ocultar o painel de filtros')
        header.addWidget(self.filter_toggle)
        self.export_button = QPushButton('Exportar recorte CSV')
        self.export_button.setObjectName('primaryAction')
        self.export_button.clicked.connect(self.export_csv)
        header.addWidget(self.export_button)
        more = QToolButton()
        more.setObjectName('menuButton')
        more.setText('Mais ações')
        more.setPopupMode(QToolButton.InstantPopup)
        actions = QMenu(more)
        actions.addAction('Abrir pasta dos resultados', self.openFolderRequested.emit)
        actions.addAction('Adicionar tabelas ao QGIS', self.addTablesRequested.emit)
        actions.addSeparator()
        actions.addAction('Salvar gráfico como PNG', self.export_png)
        more.setMenu(actions)
        header.addWidget(more)
        root.addLayout(header)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(12)
        root.addWidget(splitter, 1)
        sidebar = QFrame()
        sidebar.setObjectName('filterPanel')
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(12, 12, 12, 12)
        side.setSpacing(3)
        heading = QLabel('Filtros')
        heading.setObjectName('filterHeading')
        side.addWidget(heading)
        self.filters = {}
        for field in ('year', 'month', *DIMENSIONS):
            label = QLabel(FIELD_LABELS[field])
            label.setObjectName('filterLabel')
            side.addWidget(label)
            control = MultiSelect()
            control.setObjectName('multiSelect')
            control.changed.connect(self.schedule_refresh)
            self.filters[field] = control
            side.addWidget(control)
        side.addSpacing(8)
        reset = QPushButton('Limpar filtros')
        reset.setObjectName('ghostAction')
        reset.clicked.connect(self.reset_filters)
        side.addWidget(reset)
        side.addSpacing(10)
        heading = QLabel('Visões salvas')
        heading.setObjectName('filterHeading')
        side.addWidget(heading)
        self.presets = QComboBox()
        if hasattr(self.presets, 'setPlaceholderText'):
            self.presets.setPlaceholderText('Nenhuma visão salva')
        side.addWidget(self.presets)
        preset_actions = QHBoxLayout()
        for label, callback in (('Aplicar', self.load_preset), ('Salvar atual', self.save_preset)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            preset_actions.addWidget(button)
        side.addLayout(preset_actions)
        self.filter_summary = QLabel('Selecione os recortes desejados.')
        self.filter_summary.setWordWrap(True)
        self.filter_summary.setStyleSheet('color:#64788f; font-size:11px; padding-top:10px;')
        side.addWidget(self.filter_summary)
        side.addStretch()
        self.sidebar_scroll = QScrollArea()
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setFrameShape(QFrame.NoFrame)
        self.sidebar_scroll.setWidget(sidebar)
        self.sidebar_scroll.setMinimumWidth(215)
        self.sidebar_scroll.setMaximumWidth(280)
        splitter.addWidget(self.sidebar_scroll)
        self.filter_toggle.toggled.connect(self.sidebar_scroll.setVisible)
        main = QWidget()
        center = QVBoxLayout(main)
        center.setSizeConstraint(QLayout.SetMinimumSize)
        center.setContentsMargins(8, 0, 0, 0)
        cards = QHBoxLayout()
        self.cards = {}
        for key, label, tip in (
            ('energy', 'Consumo do recorte', 'Soma do consumo nas células e meses selecionados.'),
            ('average', 'Média mensal por cliente', 'Soma da energia dividida pela exposição cliente-mês; não é uma média simples das médias.'),
            ('customers', 'Clientes mensais médios', 'Exposição cliente-mês dividida pelo número de meses observados. Não representa clientes únicos entre anos.'),
        ):
            card = QFrame()
            card.setObjectName('resultCard')
            card.setToolTip(tip)
            layout = QVBoxLayout(card)
            heading, value, unit = QLabel(label), QLabel('—'), QLabel('')
            heading.setWordWrap(True)
            unit.setWordWrap(True)
            heading.setObjectName('cardTitle')
            value.setObjectName('cardValue')
            unit.setStyleSheet('color:#60748a; font-size:11px;')
            layout.addWidget(heading)
            layout.addWidget(value)
            layout.addWidget(unit)
            self.cards[key] = (value, unit)
            cards.addWidget(card)
        center.addLayout(cards)
        self.coverage_label = QLabel('Execute ou carregue uma análise para começar.')
        self.coverage_label.setWordWrap(True)
        self.coverage_label.setStyleSheet('color:#60748a; padding:4px 0;')
        center.addWidget(self.coverage_label)
        self.tabs = QTabWidget()
        center.addWidget(self.tabs, 1)
        self._build_chart()
        self._build_table()
        quality_page = QWidget()
        quality_layout = QVBoxLayout(quality_page)
        self.quality_label = QLabel()
        self.quality_label.setWordWrap(True)
        self.quality_label.setTextFormat(Qt.RichText)
        self.quality_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        quality_layout.addWidget(self.quality_label)
        quality_layout.addStretch()
        quality_scroll = QScrollArea()
        quality_scroll.setWidgetResizable(True)
        quality_scroll.setWidget(quality_page)
        self.tabs.addTab(quality_scroll, 'Qualidade e método')
        self.tabs.setMinimumHeight(self.tabs.minimumSizeHint().height())
        splitter.addWidget(main)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([230, 900])
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)
        self._load_preferences()
        self._populate_presets()
        self.refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'tabs'):
            self.tabs.setMinimumHeight(self.tabs.minimumSizeHint().height())
        if not hasattr(self, 'filter_toggle'):
            return
        compact = self.width() < max(900, self.fontMetrics().horizontalAdvance('M') * 115)
        if compact != getattr(self, '_compact_layout', None):
            previous = getattr(self, '_compact_layout', None)
            self._compact_layout = compact
            if compact:
                self._wide_filters_visible = self.filter_toggle.isChecked()
                self.filter_toggle.setChecked(False)
            elif previous is not None:
                self.filter_toggle.setChecked(getattr(self, '_wide_filters_visible', True))

    @staticmethod
    def combo(options):
        combo = QComboBox()
        for label, value in options:
            combo.addItem(label, value)
        return combo

    def _build_chart(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        row = QHBoxLayout()
        self.metric = self.combo([(v[0], k) for k, v in METRICS.items()])
        self.frequency = self.combo([('Mensal', 'month'), ('Trimestral', 'quarter'), ('Anual', 'year')])
        self.chart_type = self.combo([('Linhas', 'line'), ('Barras', 'bar')])
        row.addWidget(self.metric, 3)
        row.addWidget(self.frequency, 1)
        row.addWidget(self.chart_type, 1)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.group = self.combo([('Total do recorte', None)] + [(FIELD_LABELS[k], k) for k in DIMENSIONS])
        self.top_n = QSpinBox()
        self.top_n.setRange(1, 10)
        self.top_n.setValue(5)
        self.top_n.setPrefix('Top ')
        self.compare = QCheckBox('Sobrepor anos')
        self.legend = QCheckBox('Legenda')
        self.legend.setChecked(True)
        row.addWidget(QLabel('Séries:'))
        row.addWidget(self.group, 1)
        row.addWidget(self.top_n)
        row.addWidget(self.compare)
        row.addWidget(self.legend)
        layout.addLayout(row)
        self.figure = Figure(figsize=(8, 4), facecolor='white', constrained_layout=True)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setMinimumHeight(230)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        layout.addWidget(self.canvas, 1)
        toolbar_row = QHBoxLayout()
        self.toolbar = ChartToolbar(self.canvas, page, coordinates=False)
        for action in self.toolbar.actions():
            translations = {'Home': 'Restaurar escala', 'Back': 'Voltar', 'Forward': 'Avançar',
                            'Pan': 'Mover gráfico', 'Zoom': 'Ampliar área'}
            if action.text() in translations:
                action.setToolTip(translations[action.text()])
        toolbar_row.addWidget(self.toolbar)
        toolbar_row.addStretch()
        self.palette = self.combo([(k, k) for k in PALETTES])
        self.units = self.combo([(k, k) for k in ('Automática', 'kWh', 'MWh', 'GWh')])
        toolbar_row.addWidget(self.palette)
        toolbar_row.addWidget(self.units)
        png = QPushButton('Salvar PNG')
        png.clicked.connect(self.export_png)
        toolbar_row.addWidget(png)
        layout.addLayout(toolbar_row)
        self.chart_note = QLabel('')
        self.chart_note.setWordWrap(True)
        self.chart_note.setStyleSheet('color:#60748a; font-size:11px;')
        layout.addWidget(self.chart_note)
        self.canvas.mpl_connect('motion_notify_event', self._hover)
        for control in (self.metric, self.frequency, self.chart_type, self.group, self.palette, self.units):
            control.currentIndexChanged.connect(self.schedule_refresh)
        for control in (self.compare, self.legend):
            control.toggled.connect(self.schedule_refresh)
        self.top_n.valueChanged.connect(self.schedule_refresh)
        self.tabs.addTab(page, 'Gráficos')

    def _build_table(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        row = QHBoxLayout()
        self.table_search = QLineEdit()
        self.table_search.setPlaceholderText('Buscar na tabela — não altera os indicadores ou gráficos')
        self.table_search_timer = QTimer(self)
        self.table_search_timer.setSingleShot(True)
        self.table_search_timer.timeout.connect(self.refresh_table)
        self.table_search.textChanged.connect(lambda: self.table_search_timer.start(250) if not self.loading else None)
        row.addWidget(self.table_search, 1)
        self.decimals = QSpinBox()
        self.decimals.setRange(0, 6)
        self.decimals.setValue(2)
        self.decimals.setSuffix(' casas')
        self.decimals.valueChanged.connect(self.schedule_refresh)
        row.addWidget(self.decimals)
        self.columns_button = QToolButton()
        self.columns_button.setText('Colunas')
        self.columns_button.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self)
        self.column_actions = []
        for index, title in enumerate(TABLE_LABELS):
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(True)
            action.toggled.connect(lambda checked, col=index: self._column_visibility(col, checked))
            self.column_actions.append(action)
        self.columns_button.setMenu(menu)
        row.addWidget(self.columns_button)
        layout.addLayout(row)
        self.table = QTableView()
        self.table_model = PagedTableModel(self)
        self.table.setModel(self.table_model)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionsMovable(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setWordWrap(False)
        for i, width in enumerate((72, 140, 78, 175, 105, 190, 120, 140)):
            self.table.setColumnWidth(i, width)
        layout.addWidget(self.table, 1)
        footer = QHBoxLayout()
        self.previous = QPushButton('Anterior')
        self.next = QPushButton('Próxima')
        self.previous.clicked.connect(lambda: self.table_model.set_page(self.table_model.page - 1))
        self.next.clicked.connect(lambda: self.table_model.set_page(self.table_model.page + 1))
        self.page_label = QLabel()
        self.page_size = self.combo([(str(n) + ' linhas', n) for n in (100, 250, 500)])
        self.page_size.currentIndexChanged.connect(self._page_size_changed)
        self.table_model.changed.connect(self._page_status)
        footer.addWidget(self.page_label, 1)
        footer.addWidget(self.page_size)
        footer.addWidget(self.previous)
        footer.addWidget(self.next)
        layout.addLayout(footer)
        self.tabs.addTab(page, 'Dados')

    def selection(self):
        return {k: w.values() for k, w in self.filters.items()}

    def controls(self):
        return {**{k: getattr(self, k).currentData() for k in
                   ('metric', 'frequency', 'chart_type', 'group', 'palette', 'units')},
                'compare': self.compare.isChecked(), 'legend': self.legend.isChecked(),
                'top_n': self.top_n.value(), 'decimals': self.decimals.value(),
                'columns': [i for i, action in enumerate(self.column_actions) if action.isChecked()]}

    def set_results(self, rows, metadata, strata=(), output_dir=None):
        self.loading = True
        self.rows, self.issues = normalize_rows(rows)
        self.metadata, self.strata, self.output_dir = metadata, list(strata), output_dir
        for field, control in self.filters.items():
            values = sorted({r['ano_mes'][:4] if field == 'year' else r['ano_mes'][5:] if field == 'month'
                             else r[field] for r in self.rows})
            control.set_options(values, {f'{i+1:02d}': name for i, name in enumerate(MONTHS)} if field == 'month' else None)
        source = metadata.get('source', {})
        origin = 'PostgreSQL · ' + source.get('connection', '') if source.get('type') == 'postgres' else 'Arquivos locais'
        self.source_label.setText(f'{metadata.get("distributor", "")}  /  {origin}' if metadata else 'Nenhum resultado carregado.')
        self.table_search.clear()
        self.loading = False
        self.refresh()

    def schedule_refresh(self, *_):
        if not self.loading and hasattr(self, 'timer'):
            self.timer.start(250)

    def reset_filters(self):
        self.loading = True
        for widget in self.filters.values():
            widget.select_all()
        self.loading = False
        self.refresh()

    def refresh(self):
        if self.loading:
            return
        self.timer.stop()
        selection = {k: values for k, values in self.selection().items()
                     if not values or len(values) != self.filters[k].items.count()}
        self.filtered = filter_rows(self.rows, selection)
        stats = summarize(self.filtered)
        divisor, unit = self._energy_unit(stats['energy'])
        self.cards['energy'][0].setText(br_number(stats['energy'] / divisor) if self.filtered else '—')
        self.cards['energy'][1].setText(unit)
        for key in ('average', 'customers'):
            self.cards[key][0].setText(br_number(stats[key], 2 if key == 'average' else 0))
        self.cards['average'][1].setText('kWh/cliente/mês')
        self.cards['customers'][1].setText('média nos meses observados')
        months = stats['months']
        self.coverage_label.setText(
            f'{len(months)} meses observados · {months[0]} a {months[-1]} · {br_number(len(self.filtered), 0)} células no recorte'
            if months else 'Nenhum dado corresponde aos filtros selecionados.'
        )
        self.filter_summary.setText(' · '.join(f'{FIELD_LABELS[k]}: {len(v)}' for k, v in self.selection().items()))
        self.refresh_table()
        self._draw_chart()
        self._quality(stats)
        self.export_button.setEnabled(bool(self.filtered))
        self.settings.setValue(SETTINGS + 'preferences', json.dumps(self.controls(), ensure_ascii=False))
        self.filtersChanged.emit()

    def _energy_unit(self, maximum):
        name = self.units.currentData()
        if name == 'Automática':
            name = 'GWh' if abs(maximum) >= 1e6 else 'MWh' if abs(maximum) >= 1e3 else 'kWh'
        return {'kWh': 1, 'MWh': 1000, 'GWh': 1e6}[name], name

    def _draw_chart(self):
        metric = self.metric.currentData()
        comparison = self.compare.isChecked()
        self.frequency.setEnabled(not comparison)
        self.group.setEnabled(not comparison)
        self.top_n.setEnabled(not comparison and self.group.currentData() is not None)
        self.units.setEnabled(metric == 'energy')
        labels, series, coverage = make_series(self.filtered, metric, self.frequency.currentData(),
                                              self.group.currentData(), self.top_n.value(), comparison)
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        self.ax, self.chart_labels, self.chart_series = ax, labels, series
        self.chart_artists = []
        maximum = max([abs(v) for values in series.values() for v in values if v is not None] or [0])
        divisor, unit = self._energy_unit(maximum) if metric == 'energy' else (1, METRICS[metric][1])
        self.chart_divisor, self.chart_unit = divisor, unit
        colors = PALETTES[self.palette.currentData()]
        for index, (name, values) in enumerate(series.items()):
            ys = [v / divisor if v is not None else float('nan') for v in values]
            color = colors[index % len(colors)]
            if self.chart_type.currentData() == 'bar':
                width = 0.8 / max(1, len(series))
                xs = [i - 0.4 + width * (index + 0.5) for i in range(len(labels))]
                ax.bar(xs, ys, width=width, color=color, label=name, alpha=.9)
            else:
                ax.plot(range(len(labels)), ys, color=color, linewidth=1.9,
                        marker='o' if len(labels) <= 36 else None, markersize=3, label=name)
        ax.set_title(METRICS[metric][0], loc='left', fontsize=12, color='#183b5b', pad=16)
        ax.set_ylabel(unit, fontsize=9, color='#52657b')
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: br_number(value, 1)))
        ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
        step = 1 if comparison else max(1, math.ceil(len(labels) / 10))
        ticks = list(range(0, len(labels), step))
        if not comparison and self.frequency.currentData() == 'month' and len(labels) > 24:
            ticks = [i for i, value in enumerate(labels) if value.endswith('-01')]
            ticks = ticks[::max(1, math.ceil(len(ticks) / 10))]
        ax.set_xticks(ticks)
        ax.set_xticklabels([MONTHS[int(labels[i]) - 1] if comparison else labels[i] for i in ticks], fontsize=8)
        ax.tick_params(colors='#52657b', labelsize=8)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        for side in ('left', 'bottom'):
            ax.spines[side].set_color('#dce4ef')
        ax.grid(axis='y', color='#e5ecf4', linewidth=.7)
        ax.set_axisbelow(True)
        if maximum and all(v is None or v >= 0 for values in series.values() for v in values):
            ax.set_ylim(bottom=0)
        if not self.filtered:
            ax.text(.5, .5, 'Nenhum dado neste recorte', transform=ax.transAxes, ha='center', color='#60748a')
        if self.legend.isChecked() and series:
            ax.legend(loc='upper left', fontsize=8, frameon=False, ncol=1 if len(series) < 4 else 2)
        self.annotation = ax.annotate('', xy=(0, 0), xytext=(14, 16), textcoords='offset points',
                                      fontsize=8, bbox=dict(boxstyle='round,pad=.6', fc='white', ec='#dce4ef'))
        self.annotation.set_visible(False)
        self.toolbar.update()
        self.canvas.draw_idle()
        notes = ['Lacunas ficam sem valor; zero só é exibido quando observado.']
        if comparison:
            notes.append('Cada linha representa um ano, com os mesmos meses no eixo horizontal.')
        elif self.group.currentData():
            notes.append('Top por consumo total; demais grupos são reunidos para preservar o total.')
        expected = 12 if self.frequency.currentData() == 'year' else 3 if self.frequency.currentData() == 'quarter' else 1
        if not comparison and any(0 < n < expected for values in coverage.values() for n in values):
            notes.append('Há períodos parciais: totais refletem apenas os meses disponíveis no recorte.')
        self.chart_note.setText(' '.join(notes))

    def _hover(self, event):
        if not hasattr(self, 'annotation'):
            return
        if event.inaxes is not self.ax or event.xdata is None or not self.chart_labels:
            if self.annotation.get_visible():
                self.annotation.set_visible(False)
                self.canvas.draw_idle()
            return
        index = max(0, min(len(self.chart_labels) - 1, round(event.xdata)))
        lines = [self.chart_labels[index]]
        for name, values in self.chart_series.items():
            value = values[index]
            lines.append(f'{name}: {br_number(value / self.chart_divisor if value is not None else None, self.decimals.value())} {self.chart_unit}')
        self.annotation.xy = (event.xdata, event.ydata)
        right = event.xdata > sum(self.ax.get_xlim()) / 2
        self.annotation.set_position((-14 if right else 14, 16))
        self.annotation.set_horizontalalignment('right' if right else 'left')
        self.annotation.set_text('\n'.join(lines))
        self.annotation.set_visible(True)
        self.canvas.draw_idle()

    def refresh_table(self, *_):
        if self.loading:
            return
        self.table_search_timer.stop()
        text = self.table_search.text().casefold().strip()
        rows = self.filtered if not text else [r for r in self.filtered if
                                               text in ' '.join(str(r[k]) for k in TABLE_FIELDS).casefold()]
        self.table_model.decimals = self.decimals.value()
        self.table_model.set_rows(rows)

    def _page_size_changed(self):
        self.table_model.page_size = self.page_size.currentData()
        self.table_model.set_page(0)

    def _page_status(self):
        model = self.table_model
        total = len(model.rows)
        pages = max(1, math.ceil(total / model.page_size))
        self.page_label.setText(f'{br_number(total, 0)} linhas · página {model.page + 1}/{pages}')
        self.previous.setEnabled(model.page > 0)
        self.next.setEnabled(model.page + 1 < pages)

    def _column_visibility(self, column, checked):
        if hasattr(self, 'table'):
            if not checked and not any(action.isChecked() for action in self.column_actions):
                action = self.column_actions[column]
                action.blockSignals(True)
                action.setChecked(True)
                action.blockSignals(False)
                checked = True
            self.table.setColumnHidden(column, not checked)
            self.settings.setValue(SETTINGS + 'preferences', json.dumps(self.controls()))

    def _quality(self, stats):
        from .analyses.results_model import calendar_periods
        expected = calendar_periods(stats['months'], 'month')
        missing = sorted(set(expected) - set(stats['months']))
        years = self.metadata.get('sampling', {}).get('years', [])
        population = sum(y.get('population', 0) for y in years)
        sample = sum(y.get('actual_sample', 0) for y in years)
        selected = self.selection()
        small = 0
        for row in self.strata:
            if str(row.get('source_year')) not in selected['year']:
                continue
            shared_dimensions = [field for field in DIMENSIONS if row.get(field) not in (None, '')]
            if not all(str(row.get(field)) in selected[field] for field in shared_dimensions):
                continue
            try:
                small += float(row.get('amostra', 0)) < 30
            except (TypeError, ValueError):
                pass
        self.diagnostics = dict(exclusions=self.issues, missing_months=missing,
                                zero_exposure=stats['zero_exposure'], negative_energy=stats['negative_energy'],
                                small_strata_in_scope=small)
        descriptions = {'periodo_invalido': 'Período inválido', 'valor_invalido': 'Valor ausente, não finito ou contagem negativa',
                        'celulas_duplicadas_excluidas': 'Células com chave mensal duplicada (todas as ocorrências excluídas)'}
        n = lambda value: br_number(value, 0)  # noqa: E731 - local formatter
        checks = [
            (not missing, f'{len(stats["months"])} meses observados',
             ('Meses sem observação ou excluídos pelos filtros: ' + ', '.join(missing)) if missing
             else 'Sem lacunas mensais entre os extremos do recorte.'),
            (not small, f'{n(small)} estratos com menos de 30 observações',
             'Limiar apenas descritivo; recortes pequenos merecem cautela na leitura.'),
            (not stats['zero_exposure'], f'{n(stats["zero_exposure"])} células com energia e exposição zero',
             'Médias dessas células são indefinidas.'),
            (not stats['negative_energy'], f'{n(stats["negative_energy"])} células com energia negativa',
             'Valores preservados para revisão.'),
            *[(False, f'{n(v)} excluídas na leitura', descriptions.get(k, k)) for k, v in self.issues.items()],
        ]
        definitions = [
            ('Consumo total', 'soma da energia das células selecionadas.'),
            ('Média mensal por cliente', 'soma da energia ÷ soma das contagens mensais estimadas.'),
            ('Clientes mensais médios', 'exposição cliente-mês ÷ meses observados. Não equivale a clientes únicos.'),
            ('Indicação de GD', "presença de um CEG_GD válido na UCBT. 'Sem indicação' não comprova ausência física "
                                'de geração nem de compensação remota.'),
        ]
        html = ['<div style="color:#3b4f66; line-height:140%;">']

        def section(title):
            html.append(f'<p style="margin:14px 0 6px 0; color:#0f2438; font-size:13px; font-weight:700;">{title}</p>')

        section('Cobertura e consistência do recorte')
        for ok, title, detail in checks:
            color, background, symbol = ('#13795b', '#e3f5ee', '✓') if ok else ('#9a5b00', '#fff3dc', '!')
            html.append(
                f'<table cellspacing="0" cellpadding="6" width="100%" style="margin:3px 0;"><tr>'
                f'<td width="26" align="center" style="background:{background}; color:{color}; font-weight:700;">{symbol}</td>'
                f'<td style="background:#f6f9fc;"><b style="color:#0f2438;">{title}</b><br>'
                f'<span style="color:#64788f;">{detail}</span></td></tr></table>')
        section('Execução de origem (todos os anos e grupos)')
        html.append(f'<p>Registros de origem: <b>{n(population)}</b> · amostrados/processados: <b>{n(sample)}</b>.<br>'
                    '<span style="color:#64788f;">Descrevem a execução completa, sem acompanhar os filtros; '
                    'não são clientes distintos entre anos.</span></p>')
        audited = [y for y in years if 'kish_effective_n' in y]
        if audited:
            section('Diagnóstico da amostragem')
            html.append('<p style="color:#64788f;">A margem configurada dimensiona proporções (p=50%); não garante precisão '
                        'para energia. n de Kish resume apenas a desigualdade dos pesos.</p>')
            html.append('<table cellspacing="0" cellpadding="5" width="100%" style="border:1px solid #dbe3ec;">'
                        '<tr style="background:#f3f6fa; color:#3b4f66; font-weight:700;">'
                        '<td>Ano</td><td align="right">Fração</td><td align="right">Estratos cobertos</td>'
                        '<td align="right">Censitários</td><td align="right">Pesos</td><td align="right">n de Kish</td>'
                        '<td align="right">Sem ID</td></tr>')
            for index, year in enumerate(audited):
                shade = '#ffffff' if index % 2 == 0 else '#f6f9fc'
                html.append(
                    f'<tr style="background:{shade};"><td>{year["year"]}</td>'
                    f'<td align="right">{br_number(100 * year["sampling_fraction"], 2)}%</td>'
                    f'<td align="right">{n(year["covered_strata"])}/{n(year["strata"])}</td>'
                    f'<td align="right">{n(year["census_strata"])}</td>'
                    f'<td align="right">{br_number(year["weight_min"], 2)}–{br_number(year["weight_max"], 2)}</td>'
                    f'<td align="right">{n(year["kish_effective_n"])}</td>'
                    f'<td align="right">{n(year["excluded_empty_id"])}</td></tr>')
            html.append('</table>')
        warnings = [*self.metadata.get('warnings', []), *self.metadata.get('publication_warnings', [])]
        if warnings:
            section('Avisos da execução')
            html.append('<ul style="margin-left:-20px;">' + ''.join(f'<li>{html_escape(str(w))}</li>' for w in warnings) + '</ul>')
        section('Definições')
        html.append('<p>' + '<br>'.join(f'<b style="color:#0f2438;">{t}:</b> {d}' for t, d in definitions) + '</p>')
        section('Base para modelos futuros')
        html.append('<p style="color:#64788f;">Este painel é descritivo: não estima intervalos de confiança nem valida '
                    'previsões. Séries temporais exigirão definição do alvo, tratamento explícito das lacunas e validação '
                    'fora do tempo de treino; modelos lineares generalizados exigirão unidade de observação, família, '
                    'ligação, exposição e avaliação dos pesos.</p></div>')
        self.quality_label.setText(''.join(html))
        count = sum(self.issues.values()) + len(missing) + small + stats['zero_exposure'] + stats['negative_energy']
        self.tabs.setTabText(2, 'Qualidade e método' + (' • revisar' if count else ''))

    def _load_preferences(self):
        try:
            values = json.loads(self.settings.value(SETTINGS + 'preferences', '{}'))
            if isinstance(values, dict):
                self._apply_controls(values)
        except (ValueError, TypeError):
            pass
        finally:
            self.loading = False

    def _apply_controls(self, state):
        self.loading = True
        for key in ('metric', 'frequency', 'chart_type', 'group', 'palette', 'units'):
            control = getattr(self, key)
            if key in state and control.findData(state[key]) >= 0:
                control.setCurrentIndex(control.findData(state[key]))
        for key in ('compare', 'legend'):
            if key in state:
                getattr(self, key).setChecked(bool(state[key]))
        for key in ('top_n', 'decimals'):
            if key in state:
                getattr(self, key).setValue(int(state[key]))
        if 'columns' in state:
            for i, action in enumerate(self.column_actions):
                action.setChecked(i in state['columns'])
        self.loading = False

    def _preset_values(self):
        try:
            values = json.loads(self.settings.value(SETTINGS + 'presets', '{}'))
            return values if isinstance(values, dict) else {}
        except (ValueError, TypeError):
            return {}

    def _populate_presets(self):
        self.presets.clear()
        self.presets.addItems(sorted(self._preset_values()))

    def save_preset(self):
        name, ok = QInputDialog.getText(self, 'Salvar visão', 'Nome da visão:')
        if ok and name.strip():
            values = self._preset_values()
            values[name.strip()] = dict(selection=self.selection(), controls=self.controls())
            self.settings.setValue(SETTINGS + 'presets', json.dumps(values, ensure_ascii=False))
            self._populate_presets()
            self.presets.setCurrentText(name.strip())

    def load_preset(self):
        value = self._preset_values().get(self.presets.currentText())
        if not value:
            return
        self._apply_controls(value.get('controls', {}))
        self.loading = True
        for key, values in value.get('selection', {}).items():
            if key in self.filters:
                self.filters[key].set_values(values)
        self.loading = False
        self.refresh()

    def manifest(self):
        return selection_manifest(self.metadata, self.selection(),
                                  dict(self.controls(), table_search=self.table_search.text(),
                                       table_sort=dict(field=TABLE_FIELDS[self.table_model.sort_column],
                                                       descending=self.table_model.sort_order == Qt.DescendingOrder),
                                       chart_limits={'x': list(map(float, self.ax.get_xlim())),
                                                     'y': list(map(float, self.ax.get_ylim()))},
                                       column_order=[TABLE_FIELDS[self.table.horizontalHeader().logicalIndex(i)]
                                                     for i in range(len(TABLE_FIELDS))]), self.diagnostics)

    def export_csv(self):
        self.refresh()
        rows = self.table_model.rows
        if not rows:
            QMessageBox.information(self, 'Exportar', 'Não há linhas para exportar com esta seleção e busca.')
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Exportar recorte completo',
                                             str(Path(self.output_dir or Path.home()) / 'recorte_resultados.csv'), 'CSV (*.csv)')
        if not path:
            return
        try:
            self.write_export(path)
        except OSError as exc:
            QMessageBox.warning(self, 'Exportar', f'Não foi possível salvar a exportação: {exc}')

    def write_export(self, path):
        """Export all matching table rows, independently of pagination."""
        path = Path(path)
        fields = [TABLE_FIELDS[self.table.horizontalHeader().logicalIndex(i)] for i in range(len(TABLE_FIELDS))
                  if not self.table.isColumnHidden(self.table.horizontalHeader().logicalIndex(i))]
        if not fields:
            fields = list(TABLE_FIELDS)
        with path.open('w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(self.table_model.rows)
        path.with_suffix('.view.json').write_text(json.dumps(self.manifest(), ensure_ascii=False, indent=2), encoding='utf-8')

    def export_png(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Salvar gráfico',
                                             str(Path(self.output_dir or Path.home()) / 'grafico.png'), 'PNG (*.png)')
        if path:
            try:
                self.annotation.set_visible(False)
                self.figure.savefig(path, dpi=180, bbox_inches='tight', facecolor='white')
                Path(path).with_suffix('.view.json').write_text(json.dumps(self.manifest(), ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError as exc:
                QMessageBox.warning(self, 'Salvar gráfico', str(exc))
