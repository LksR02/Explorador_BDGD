from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
from email.utils import parsedate_to_datetime
from pathlib import Path

from qgis.PyQt.QtCore import QElapsedTimer, QSettings, Qt, QThread, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QColor, QDesktopServices, QIcon, QKeySequence
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QAction, QCheckBox, QComboBox, QDialog,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QShortcut, QSizePolicy, QStackedWidget,
    QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)
from qgis.core import (
    QgsDataSourceUri, QgsNetworkAccessManager, QgsProject, QgsProviderRegistry, QgsVectorLayer,
)

from .catalog import CatalogItem, load_catalog
from .storage import find_gdb, read_state, safe_extract, write_state
from .postgres_browser import PostgresBrowser, br_int
from .database_worker import DatabasePreparationThread
from .managed_database import CONNECTION_NAME
from .semantic_catalog import (
    PROFILES, apply_layer_semantics, matches_profile, theme_title,
)
from .theme import STATUS_COLORS
from .ui_kit import (
    Badge, Card, CollapsibleSection, EmptyState, FlowLayout, NavRail,
    SegmentedControl, StatusLine, fade_in, field_label, hint, stat_tile,
)


DEFAULT_CATALOG = str(Path.home() / "Downloads" / "Agência Nacional de Energia Elétrica.csv")
DEFAULT_STORAGE = str(Path.home() / "Documents" / "BDGD_ANEEL")
PLUGIN_DIR = Path(__file__).resolve().parent
UI_SETTINGS = "bdgd_aneel_manager/ui/"

PAGE_DATA, PAGE_ANALYSIS, PAGE_PROGRESS, PAGE_RESULTS = range(4)
PAGES = (
    ("Escolher dados", "Fonte, distribuidora, anos e camadas do mapa.",
     "Escolha a origem das bases, os anos de interesse e quais camadas levar ao mapa. "
     "Nomes acessíveis convivem com os códigos oficiais da BDGD."),
    ("Definir análise", "Período, método e níveis de saída.",
     "Configure o período, a amostragem e os níveis agregados. O resumo à direita mostra "
     "a estimativa e tudo o que falta antes de executar."),
    ("Acompanhar", "Progresso da execução em segundo plano.",
     "Acompanhe cada ano processado, o tempo decorrido e o registro das etapas. "
     "O cancelamento preserva o resultado anterior."),
    ("Explorar resultados", "Indicadores, gráficos e dados filtráveis.",
     "Filtre por período e dimensões; indicadores, gráfico e tabela respondem juntos. "
     "Exporte o recorte com o manifesto da visão."),
)
SAMPLING_MODES = (
    ("auto", "Automático", "Amostra apenas bases grandes; bases pequenas são lidas por completo."),
    ("stratified", "Amostragem estratificada", "Amostra todos os anos pelos estratos escolhidos."),
    ("census", "Censo completo", "Lê todos os registros. Mais lento e com arquivos maiores."),
)


def human_size(size: int) -> str:
    value = float(max(size, 0))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024


def format_publication_date(raw: str) -> str:
    """Show ArcGIS HTTP dates (``Fri, 10 Oct 2025 13:51:21 GMT``) in pt-BR."""
    if not raw:
        return "—"
    try:
        return parsedate_to_datetime(raw).strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError, IndexError):
        return raw


def plugin_icon() -> QIcon:
    path = PLUGIN_DIR / "icon.svg"
    return QIcon(str(path)) if path.exists() else QIcon()


class ExtractThread(QThread):
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, archive: str, target: str, parent=None):
        super().__init__(parent)
        self.archive = archive
        self.target = target

    def run(self):
        try:
            self.succeeded.emit(str(safe_extract(self.archive, self.target)))
        except Exception as exc:
            self.failed.emit(str(exc))


class AnalysisThread(QThread):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()
    progress = pyqtSignal(object)

    def __init__(
        self, base_dir, distributor, start_year, end_year,
        sampling_mode="auto", confidence=0.99, margin_error=0.01, parent=None,
        postgres=None, output_dir=None, output_levels=None, strata_fields=None,
    ):
        super().__init__(parent)
        self.base_dir = base_dir
        self.distributor = distributor
        self.start_year = start_year
        self.end_year = end_year
        self.sampling_mode = sampling_mode
        self.confidence = confidence
        self.margin_error = margin_error
        self.postgres = postgres
        self.output_dir = output_dir
        self.output_levels = output_levels
        self.strata_fields = strata_fields

    def run(self):
        try:
            from .analyses.demand_mvp import AnalysisCancelled, build_outputs
            sources, provenance = None, None
            if self.postgres:
                from .analyses.postgres_source import PostgresSource
                sources = {year: PostgresSource(self.postgres['connection_info'], entry)
                           for year, entry in self.postgres['entries'].items()}
                provenance = self.postgres['metadata']
            result = build_outputs(
                self.base_dir,
                self.distributor,
                self.start_year,
                self.end_year,
                sampling_mode=self.sampling_mode,
                confidence=self.confidence,
                margin_error=self.margin_error,
                output_levels=self.output_levels,
                strata_fields=self.strata_fields,
                progress_callback=self.progress.emit,
                cancel_callback=self.isInterruptionRequested,
                sources=sources,
                source_metadata=provenance,
                output_dir=self.output_dir,
            )
            self.succeeded.emit(result)
        except AnalysisCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(
                'Falha ao analisar as bases PostgreSQL. Confira a conexão, os campos UCBT e a pasta de saída.'
                if self.postgres else str(exc)
            )


class BdgdDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.items: list[CatalogItem] = []
        self.current_reply = None
        self.output_file = None
        self.extract_thread = None
        self.analysis_thread = None
        self.database_thread = None
        self.database_ready = False
        self.selected_item: CatalogItem | None = None
        self.sync_queue: list[CatalogItem] = []
        self.sync_total = 0
        self.analysis_clock = QElapsedTimer()
        self.analysis_timer = QTimer(self)
        self.analysis_timer.timeout.connect(self._update_elapsed)
        self.analysis_years: list[int] = []
        self.analysis_outcome = None
        self.result_metadata = {}
        self.result_rows: list[dict] = []
        self.result_strata_rows: list[dict] = []
        self.result_output_dir: Path | None = None
        self.feature_count_cache: dict[str, int] = {}
        self.local_size_cache: dict[str, int] = {}
        self.estimated_population = 0
        self.analysis_ready = False
        self._analysis_block_reason = ""
        self._results_scope_key = None
        self.ui_settings = QSettings()
        self.setWindowTitle("Explorador BDGD")
        self.setWindowIcon(plugin_icon())
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinMaxButtonsHint)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(720, 480)
        self.resize(1280, 820)
        self._build_ui()
        self.update_analysis_estimate()
        self.reload_catalog()
        self._go_to_page(PAGE_DATA, animate=False)
        QTimer.singleShot(0, self._fit_available_screen)

    # ------------------------------------------------------------------ shell
    def _fit_available_screen(self):
        if self.isMaximized() or self.isFullScreen():
            return
        screen = self.screen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.resize(min(self.width(), max(self.minimumWidth(), area.width() - 40)),
                    min(self.height(), max(self.minimumHeight(), area.height() - 40)))
        frame = self.frameGeometry()
        frame.moveLeft(max(area.left(), min(frame.left(), area.right() - frame.width() + 1)))
        frame.moveTop(max(area.top(), min(frame.top(), area.bottom() - frame.height() + 1)))
        self.move(frame.topLeft())

    def _build_ui(self):
        from .theme import WORKSPACE_STYLE
        self.setObjectName("BdgdWorkspace")
        self.setStyleSheet(WORKSPACE_STYLE)
        shell = QHBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        version = self._plugin_version()
        self.nav = NavRail("Explorador BDGD", "Base de Dados Geográfica da Distribuidora",
                           f"Versão {version}\nCtrl+1…4 alterna as etapas")
        for index, (title, _, _) in enumerate(PAGES):
            self.nav.add_item(title, f"Ctrl+{index + 1}")
        self.nav.currentChanged.connect(self._go_to_page)
        shell.addWidget(self.nav)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        shell.addLayout(column, 1)
        column.addWidget(self._build_top_bar())

        self.tabs = QStackedWidget()
        self.tabs.currentChanged.connect(self._page_changed)
        column.addWidget(self.tabs, 1)
        self._build_bases_tab()
        self._build_analysis_tab()
        self._build_processing_tab()
        self._build_results_tab()
        self.postgres_browser.scopeChanged.connect(self._source_changed)
        self.base_sources.currentChanged.connect(self._source_changed)
        self.base_sources.currentChanged.connect(self._remember_source)

        status_bar = QFrame()
        status_bar.setObjectName("statusBar")
        footer = QHBoxLayout(status_bar)
        footer.setContentsMargins(16, 7, 16, 7)
        self.status = StatusLine("Pronto.")
        footer.addWidget(self.status, 1)
        shortcuts = hint("Ctrl+Enter executa a análise · F11 tela cheia", wrap=False)
        footer.addWidget(shortcuts)
        column.addWidget(status_bar)

        for index in range(len(PAGES)):
            QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self, activated=lambda i=index: self._go_to_page(i))
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self._shortcut_run)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self._shortcut_run)
        QShortcut(QKeySequence("F11"), self, activated=self.fullscreen_button.toggle)

    def _build_top_bar(self):
        bar = QFrame()
        bar.setObjectName("topBar")
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(22, 14, 18, 12)
        layout.setSpacing(4)
        row = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.page_eyebrow = QLabel("")
        self.page_eyebrow.setObjectName("pageEyebrow")
        self.page_title = QLabel("")
        self.page_title.setObjectName("pageTitle")
        titles.addWidget(self.page_eyebrow)
        titles.addWidget(self.page_title)
        row.addLayout(titles, 1)
        self.fullscreen_button = QToolButton()
        self.fullscreen_button.setObjectName("iconButton")
        self.fullscreen_button.setText("Tela cheia")
        self.fullscreen_button.setCheckable(True)
        self.fullscreen_button.setToolTip("Alternar entre janela ajustável e tela cheia (F11; Esc para sair)")
        self.fullscreen_button.toggled.connect(self.toggle_fullscreen)
        row.addWidget(self.fullscreen_button, 0, Qt.AlignTop)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.page_description = QLabel("")
        self.page_description.setObjectName("pageDescription")
        self.page_description.setWordWrap(True)
        row.addWidget(self.page_description, 1)
        self.context_chips = {}
        for key, target, tip in (
            ("source", PAGE_DATA, "Origem dos dados — clique para alterar"),
            ("distributor", PAGE_DATA, "Distribuidora selecionada"),
            ("years", PAGE_ANALYSIS, "Anos considerados pela análise"),
            ("result", PAGE_RESULTS, "Resultado disponível para esta seleção"),
        ):
            chip = QToolButton()
            chip.setObjectName("contextChip")
            chip.setCursor(Qt.PointingHandCursor)
            chip.setToolTip(tip)
            chip.clicked.connect(lambda _, page=target: self._go_to_page(page))
            row.addWidget(chip, 0, Qt.AlignVCenter)
            self.context_chips[key] = chip
        layout.addLayout(row)
        return bar

    @staticmethod
    def _plugin_version() -> str:
        try:
            for line in (PLUGIN_DIR / "metadata.txt").read_text(encoding="utf-8").splitlines():
                if line.startswith("version="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            pass
        return "—"

    @staticmethod
    def _scroll(page: QWidget) -> QScrollArea:
        """Keep a form usable when the dialog is made smaller."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setMinimumSize(0, 0)
        page.setMinimumSize(0, 0)
        scroll.setWidget(page)
        return scroll

    def _add_page(self, widget: QWidget):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 16, 20, 14)
        layout.setSpacing(0)
        layout.addWidget(widget)
        self.tabs.addWidget(container)

    def _go_to_page(self, index: int, animate: bool = True):
        index = max(0, min(int(index), self.tabs.count() - 1))
        self._animate_next_page = animate
        if self.tabs.currentIndex() == index:
            self._page_changed(index)
        else:
            self.tabs.setCurrentIndex(index)

    def _page_changed(self, index: int):
        if index < 0:
            return
        title, _, description = PAGES[index]
        self.page_eyebrow.setText(f"ETAPA {index + 1} DE {len(PAGES)}")
        self.page_title.setText(title)
        self.page_description.setText(description)
        self.nav.set_current(index)
        if getattr(self, "_animate_next_page", True) and self.isVisible():
            self._page_animation = fade_in(self.tabs.currentWidget())
        self._animate_next_page = True

    def _shortcut_run(self):
        if self.tabs.currentIndex() == PAGE_ANALYSIS and self.analysis_button.isEnabled():
            self.begin_analysis()
        elif self.tabs.currentIndex() != PAGE_ANALYSIS:
            self._go_to_page(PAGE_ANALYSIS)

    def toggle_fullscreen(self, enabled: bool):
        if enabled:
            self.fullscreen_button.setText("Sair da tela cheia")
            self.showFullScreen()
        else:
            self.fullscreen_button.setText("Tela cheia")
            self.showNormal()
            QTimer.singleShot(0, self._fit_available_screen)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self.isFullScreen():
            self.fullscreen_button.setChecked(False)
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "nav") and self.nav.user_compact is None:
            compact = self.width() < 1020
            if compact != self.nav.compact:
                self.nav.set_compact(compact)

    # ------------------------------------------------------------ step 1 data
    def _build_bases_tab(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        database = Card()
        database_row = QHBoxLayout()
        self.database_status = StatusLine("Preparando o banco local automaticamente…")
        database_row.addWidget(self.database_status, 1)
        self.database_retry = QPushButton("Tentar novamente")
        self.database_retry.clicked.connect(self.prepare_database)
        self.database_retry.hide()
        database_row.addWidget(self.database_retry)
        database.add(database_row)
        self.database_progress = QProgressBar()
        self.database_progress.setRange(0, 0)
        self.database_progress.setMaximumHeight(8)
        self.database_progress.setTextVisible(False)
        database.add(self.database_progress)
        root.addWidget(database)
        switch_row = QHBoxLayout()
        self.source_switch = SegmentedControl([
            ("Banco PostgreSQL", "Bases importadas no PostgreSQL local; consulta rápida e camadas por tema."),
            ("Arquivos da ANEEL", "Baixa as geodatabases publicadas pela ANEEL e trabalha com os arquivos locais."),
        ])
        switch_row.addWidget(self.source_switch)
        self.source_hint = hint("")
        switch_row.addSpacing(10)
        switch_row.addWidget(self.source_hint, 1)
        root.addLayout(switch_row)

        self.base_sources = QStackedWidget()
        aneel = QWidget()
        aneel_layout = QVBoxLayout(aneel)
        aneel_layout.setContentsMargins(0, 0, 0, 0)
        aneel_layout.setSpacing(12)
        form, actions = self._build_aneel_page()
        aneel_layout.addWidget(self._scroll(form), 1)
        aneel_layout.addWidget(actions)
        self.base_sources.addWidget(aneel)
        self.postgres_browser = PostgresBrowser(self.iface, self)
        self.base_sources.addWidget(self.postgres_browser)
        root.addWidget(self.base_sources, 1)
        # Visual order is PostgreSQL first; stack index 1 is PostgreSQL, 0 is ANEEL.
        self.source_switch.changed.connect(lambda segment: self.base_sources.setCurrentIndex(1 - segment))
        self.base_sources.currentChanged.connect(self._sync_source_switch)
        saved = self.ui_settings.value(UI_SETTINGS + "source", "postgres")
        self.base_sources.setCurrentIndex(0 if saved == "aneel" else 1)
        self._sync_source_switch(self.base_sources.currentIndex())
        self._add_page(page)

    def prepare_database(self):
        if self.database_thread and self.database_thread.isRunning():
            return
        self.database_retry.hide()
        self.database_progress.show()
        self.database_progress.setRange(0, 0)
        self.database_status.setText("Verificando o banco local…")
        legacy = self.ui_settings.value(UI_SETTINGS + "database_legacy_root", "")
        self.database_thread = DatabasePreparationThread(legacy or None)
        self.database_thread.progress.connect(self._database_progress)
        self.database_thread.succeeded.connect(self._database_prepared)
        self.database_thread.failed.connect(self._database_failed)
        self.database_thread.finished.connect(self._database_finished)
        self.database_thread.start()

    def _database_progress(self, message, percent):
        self.database_status.setText(message)
        self.database_progress.setRange(0, 0 if percent < 0 else 100)
        if percent >= 0:
            self.database_progress.setValue(percent)

    def _database_prepared(self, result):
        try:
            uri = QgsDataSourceUri()
            # libpq reads the automatically prepared passfile. No saved password.
            uri.setConnection(result['host'], str(result['port']), result['database'], result['username'], '')
            metadata = QgsProviderRegistry.instance().providerMetadata('postgres')
            connection = metadata.createConnection(uri.uri(False), {
                'saveUsername': True, 'savePassword': False,
                'allowGeometrylessTables': True, 'estimatedMetadata': True,
            })
            metadata.saveConnection(connection, CONNECTION_NAME)
            metadata.connections(False)
            self.database_ready = True
            if result.get('legacy_root'):
                self.ui_settings.setValue(UI_SETTINGS + 'database_legacy_root', result['legacy_root'])
            self.database_status.setText("Banco local pronto · PostgreSQL e PostGIS configurados automaticamente.", "success")
            self.database_progress.hide()
            self.postgres_browser.select_connection(CONNECTION_NAME)
        except Exception:
            self._database_failed("O banco foi preparado, mas a conexão no QGIS falhou. Clique em Tentar novamente.")

    def _database_failed(self, message):
        self.database_ready = False
        self.database_status.setText(message, "warning")
        self.database_progress.hide()
        self.database_retry.show()

    def _database_finished(self):
        if self.database_thread:
            self.database_thread.deleteLater()
            self.database_thread = None

    def _sync_source_switch(self, stack_index: int):
        self.source_switch.setCurrentIndex(1 - stack_index)
        self.source_hint.setText(
            "Consulta o catálogo do banco local. Os nomes físicos permanecem inalterados."
            if stack_index == 1 else
            "Publicações oficiais em File Geodatabase; downloads retomáveis e verificação de espaço."
        )

    def _remember_source(self, stack_index: int):
        self.ui_settings.setValue(UI_SETTINGS + "source", "postgres" if stack_index == 1 else "aneel")

    def _build_aneel_page(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 4, 0)
        root.setSpacing(12)

        selector = Card("Distribuidora e ano", "Escolha a distribuidora; selecione um ou mais anos na tabela para baixar.")
        row = QHBoxLayout()
        row.setSpacing(8)
        self.distributor_combo = QComboBox()
        self.distributor_combo.setEditable(True)
        self.distributor_combo.setInsertPolicy(QComboBox.NoInsert)
        self.distributor_combo.currentTextChanged.connect(self.populate_years)
        row.addWidget(field_label("Distribuidora"))
        row.addWidget(self.distributor_combo, 1)
        self.year_combo = QComboBox()
        self.year_combo.currentTextChanged.connect(self.update_details)
        row.addSpacing(8)
        row.addWidget(field_label("Ano ativo"))
        row.addWidget(self.year_combo)
        self.friendly_label = Badge("—", "info")
        row.addSpacing(8)
        row.addWidget(self.friendly_label)
        selector.add(row)
        root.addWidget(selector)

        publications = Card("Publicações", "Estado de cada ano no armazenamento local. Ctrl/Shift seleciona vários anos.")
        self.sync_badge = publications.add_header_widget(Badge("", "neutral"))
        self.base_table = QTableWidget(0, 5)
        self.base_table.setHorizontalHeaderLabels(["Ano", "Estado", "Versão publicada", "Tamanho local", "Atualização"])
        self.base_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.base_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.base_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.base_table.setAlternatingRowColors(True)
        self.base_table.setShowGrid(False)
        self.base_table.verticalHeader().setVisible(False)
        self.base_table.verticalHeader().setDefaultSectionSize(30)
        header = self.base_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.base_table.setMinimumHeight(240)
        self.base_table.itemSelectionChanged.connect(self._base_selection_changed)
        publications.add(self.base_table, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        publications.add(self.progress)
        self.details = QLabel("")
        self.details.setObjectName("detailPanel")
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.RichText)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        publications.add(self.details)
        root.addWidget(publications, 1)

        location = QWidget()
        form = QFormLayout(location)
        form.setContentsMargins(0, 0, 0, 0)
        self.catalog_edit = QLineEdit(DEFAULT_CATALOG)
        choose_catalog = QPushButton("Escolher…")
        choose_catalog.clicked.connect(self.choose_catalog)
        catalog_row = QHBoxLayout()
        catalog_row.addWidget(self.catalog_edit, 1)
        catalog_row.addWidget(choose_catalog)
        form.addRow("Catálogo de publicações", catalog_row)
        self.storage_edit = QLineEdit(DEFAULT_STORAGE)
        choose_storage = QPushButton("Escolher…")
        choose_storage.clicked.connect(self.choose_storage)
        storage_row = QHBoxLayout()
        storage_row.addWidget(self.storage_edit, 1)
        storage_row.addWidget(choose_storage)
        form.addRow("Pasta das bases", storage_row)
        advanced = Card()
        advanced.add(CollapsibleSection("Opções avançadas de armazenamento", location, expanded=False))
        root.addWidget(advanced)

        actions = QFrame()
        actions.setObjectName("actionBar")
        bar = QHBoxLayout(actions)
        bar.setContentsMargins(12, 10, 12, 10)
        reload_button = QPushButton("Atualizar catálogo")
        reload_button.setObjectName("ghostAction")
        reload_button.clicked.connect(self.reload_catalog)
        bar.addWidget(reload_button)
        bar.addStretch(1)
        bar.addWidget(field_label("Camadas"))
        self.local_profile_combo = QComboBox()
        for code, profile in PROFILES.items():
            self.local_profile_combo.addItem(profile["title"], code)
        self.local_profile_combo.currentIndexChanged.connect(self._local_profile_changed)
        bar.addWidget(self.local_profile_combo)
        self.local_profile_note = QLabel(PROFILES["essential"]["description"])
        self.local_profile_combo.setToolTip(self.local_profile_note.text())
        self.sync_button = QPushButton("Baixar selecionadas")
        self.sync_button.clicked.connect(self.begin_sync)
        self.pause_sync_button = QPushButton("Pausar download")
        self.pause_sync_button.setEnabled(False)
        self.pause_sync_button.clicked.connect(self.pause_download)
        self.add_button = QPushButton("Adicionar ao mapa")
        self.add_button.setObjectName("primaryAction")
        self.add_button.clicked.connect(self.add_layers)
        bar.addWidget(self.sync_button)
        bar.addWidget(self.pause_sync_button)
        bar.addWidget(self.add_button)
        return page, actions

    # -------------------------------------------------------- step 2 analysis
    def _build_analysis_tab(self):
        page = QWidget()
        outer = QHBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(16)
        form_page = QWidget()
        root = QVBoxLayout(form_page)
        root.setContentsMargins(0, 0, 6, 0)
        root.setSpacing(12)

        scope = Card("Escopo", "Fonte e período analisados.")
        self.analysis_source_label = QLabel('A fonte acompanha a seleção da etapa Escolher dados.')
        self.analysis_source_label.setWordWrap(True)
        scope.add(self.analysis_source_label)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self.analysis_distributor_label = QLabel("—")
        self.analysis_distributor_label.setStyleSheet("font-weight:700; color:#0f2438;")
        grid.addWidget(field_label("Distribuidora"), 0, 0)
        grid.addWidget(self.analysis_distributor_label, 0, 1)
        period = QHBoxLayout()
        self.start_year_combo = QComboBox()
        self.end_year_combo = QComboBox()
        self.period_editor = QWidget()
        editor = QHBoxLayout(self.period_editor)
        editor.setContentsMargins(0, 0, 0, 0)
        editor.addWidget(QLabel("de"))
        editor.addWidget(self.start_year_combo)
        editor.addWidget(QLabel("até"))
        editor.addWidget(self.end_year_combo)
        period.addWidget(self.period_editor)
        self.period_label = QLabel("—")
        self.period_label.setStyleSheet("font-weight:700; color:#0f2438;")
        period.addWidget(self.period_label)
        self.change_years_button = QPushButton("Alterar anos")
        self.change_years_button.setObjectName("ghostAction")
        self.change_years_button.clicked.connect(lambda: self._go_to_page(PAGE_DATA))
        period.addWidget(self.change_years_button)
        period.addStretch(1)
        grid.addWidget(field_label("Período"), 1, 0)
        grid.addLayout(period, 1, 1)
        grid.setColumnStretch(1, 1)
        scope.add(grid)
        self.period_note = hint("")
        scope.add(self.period_note)
        root.addWidget(scope)

        method = Card("Método", "Como os registros de cada ano serão lidos.")
        self.sampling_combo = QComboBox()
        for code, title, _ in SAMPLING_MODES:
            self.sampling_combo.addItem(title, code)
        self.sampling_combo.setVisible(False)
        self.sampling_switch = SegmentedControl([(title, tip) for _, title, tip in SAMPLING_MODES])
        self.sampling_switch.changed.connect(self.sampling_combo.setCurrentIndex)
        self.sampling_combo.currentIndexChanged.connect(self._sampling_changed)
        method.add(self.sampling_switch)
        self.sampling_note = hint(SAMPLING_MODES[0][2])
        method.add(self.sampling_note)
        confidence_row = QHBoxLayout()
        self.confidence_combo = QComboBox()
        self.confidence_combo.addItem("99%", 0.99)
        self.confidence_combo.addItem("95%", 0.95)
        self.confidence_combo.addItem("90%", 0.90)
        self.margin_spin = QDoubleSpinBox()
        self.margin_spin.setRange(0.1, 10.0)
        self.margin_spin.setDecimals(1)
        self.margin_spin.setValue(1.0)
        self.margin_spin.setSuffix(" %")
        confidence_row.addWidget(field_label("Confiança"))
        confidence_row.addWidget(self.confidence_combo)
        confidence_row.addSpacing(12)
        confidence_row.addWidget(field_label("Margem para proporção"))
        confidence_row.addWidget(self.margin_spin)
        confidence_row.addStretch(1)
        method.add(confidence_row)
        method.add(hint(
            "Referência para proporções (p = 50%). Não garante essa margem para médias ou totais de energia. "
            "O mínimo por estrato pode ampliar a amostra; o tamanho final varia."
        ))
        root.addWidget(method)

        levels = Card("Níveis de saída", "Agregados mensais gerados pela análise.")
        level_flow = FlowLayout(spacing=8)
        level_options = [
            ("distributor", "Distribuidora", True),
            ("municipality", "Município", False),
            ("substation", "Subestação (SUB)", False),
            ("set", "Conjunto elétrico (CONJ)", False),
            ("feeder", "Alimentador (CTMT)", True),
            ("transformer", "Transformador (UNI_TR_MT)", False),
            ("connection", "Ponto de conexão (PN_CON)", False),
            ("customer", "Painel cliente-ano", True),
        ]
        self.output_level_checks = {}
        for key, text, checked in level_options:
            widget = self._chip(text, checked)
            level_flow.addWidget(widget)
            self.output_level_checks[key] = widget
        self.output_level_checks["transformer"].setToolTip(
            "UNI_TR_MT está disponível nas UCBTs locais de 2021 a 2025; linhas sem chave são auditadas e excluídas desse agregado."
        )
        levels.add(level_flow)
        root.addWidget(levels)

        self.strata_card = Card("Estratos da amostra", "Combinações muito detalhadas criam muitos estratos e podem aproximar a amostra de um censo.")
        strata_flow = FlowLayout(spacing=8)
        stratum_options = [
            ("patamar_consumo", "Patamar de consumo", True),
            ("GRU_TAR", "Grupo tarifário", True),
            ("MUNICIPIO", "Município", True),
            ("CLAS_SUB", "Classe e subclasse", False),
            ("TIP_CC", "Tipologia de curva (TIP_CC)", False),
            ("ARE_LOC", "Área de localização", False),
            ("FAS_CON", "Fases de conexão", False),
            ("SIT_ATIV", "Situação de ativação", False),
            ("SUB", "Subestação", False),
            ("CONJ", "Conjunto elétrico", False),
            ("CTMT", "Alimentador", False),
        ]
        self.stratum_checks = {}
        for key, text, checked in stratum_options:
            widget = self._chip(text, checked)
            strata_flow.addWidget(widget)
            self.stratum_checks[key] = widget
        self.stratum_checks["MUNICIPIO"].setToolTip("Código MUN convertido para nome pela API de Localidades do IBGE.")
        self.stratum_checks["CTMT"].setToolTip(
            "Alta cardinalidade: pode aumentar muito a amostra por causa do mínimo aplicado a cada estrato."
        )
        self.strata_card.add(strata_flow)
        root.addWidget(self.strata_card)
        root.addStretch(1)
        outer.addWidget(self._scroll(form_page), 1)

        summary = QFrame()
        summary.setObjectName("summaryPanel")
        summary.setFixedWidth(320)
        side = QVBoxLayout(summary)
        side.setContentsMargins(16, 16, 16, 16)
        side.setSpacing(10)
        title = QLabel("Resumo da execução")
        title.setObjectName("cardTitle")
        side.addWidget(title)
        side.addWidget(hint("Estimativa antes de executar; os valores reais aparecem em Acompanhar."))
        tiles = QGridLayout()
        tiles.setSpacing(8)
        self.estimate_population = QLabel("—")
        self.estimate_sample = QLabel("—")
        self.estimate_time = QLabel("—")
        self.estimate_size = QLabel("—")
        for index, (label, value) in enumerate((
            ("População", self.estimate_population), ("Amostra", self.estimate_sample),
            ("Tempo estimado", self.estimate_time), ("Saída estimada", self.estimate_size),
        )):
            tiles.addWidget(stat_tile(label, value), index // 2, index % 2)
        side.addLayout(tiles)
        checks_title = QLabel("Verificações")
        checks_title.setObjectName("cardTitle")
        side.addSpacing(4)
        side.addWidget(checks_title)
        self.analysis_checks_label = QLabel("")
        self.analysis_checks_label.setTextFormat(Qt.RichText)
        self.analysis_checks_label.setWordWrap(True)
        side.addWidget(self.analysis_checks_label)
        side.addStretch(1)
        self.analysis_button = QPushButton("Executar análise")
        self.analysis_button.setObjectName("primaryAction")
        self.analysis_button.setMinimumHeight(40)
        self.analysis_button.setCursor(Qt.PointingHandCursor)
        self.analysis_button.setToolTip("Ctrl+Enter")
        self.analysis_button.clicked.connect(self.begin_analysis)
        side.addWidget(self.analysis_button)
        outer.addWidget(summary)

        for widget in (self.start_year_combo, self.end_year_combo, self.sampling_combo, self.confidence_combo):
            widget.currentIndexChanged.connect(self.update_analysis_estimate)
        self.margin_spin.valueChanged.connect(self.update_analysis_estimate)
        self._add_page(page)

    def _chip(self, text: str, checked: bool) -> QCheckBox:
        widget = QCheckBox(text)
        widget.setProperty("chip", True)
        widget.setCursor(Qt.PointingHandCursor)
        widget.setChecked(checked)
        widget.stateChanged.connect(self.update_analysis_estimate)
        return widget

    def _sampling_changed(self, index: int):
        self.sampling_switch.setCurrentIndex(index)
        mode = self.sampling_combo.currentData() or "auto"
        self.sampling_note.setText(SAMPLING_MODES[max(0, index)][2])
        census = mode == "census"
        self.strata_card.setEnabled(not census)
        self.confidence_combo.setEnabled(not census)
        self.margin_spin.setEnabled(not census)

    # ------------------------------------------------------- step 3 progress
    def _build_processing_tab(self):
        self.processing_views = QStackedWidget()
        self.processing_empty = EmptyState(
            "▶", "Nenhuma análise em execução",
            "Defina o período e o método na etapa anterior. Durante a execução, cada ano aparece aqui "
            "com o progresso, o tempo restante e o registro das etapas.",
            [("Definir análise", lambda: self._go_to_page(PAGE_ANALYSIS), True)],
        )
        self.processing_views.addWidget(self.processing_empty)

        running = QWidget()
        root = QVBoxLayout(running)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        overview = Card()
        head = QHBoxLayout()
        self.processing_stage = QLabel("Nenhuma análise em execução.")
        self.processing_stage.setStyleSheet("font-size:17px; font-weight:700; color:#0f2438;")
        self.processing_stage.setWordWrap(True)
        head.addWidget(self.processing_stage, 1)
        self.processing_badge = Badge("", "running")
        head.addWidget(self.processing_badge, 0, Qt.AlignTop)
        overview.add(head)
        overall_row = QHBoxLayout()
        overall_row.addWidget(field_label("Progresso geral"))
        overall_row.addStretch(1)
        self.overall_percent = QLabel("0%")
        self.overall_percent.setStyleSheet("font-weight:700; color:#0b62e0;")
        overall_row.addWidget(self.overall_percent)
        overview.add(overall_row)
        self.overall_progress = QProgressBar()
        self.overall_progress.setObjectName("bigProgress")
        self.overall_progress.valueChanged.connect(lambda value: self.overall_percent.setText(f"{value}%"))
        overview.add(self.overall_progress)
        overview.add(field_label("Ano em processamento"))
        self.year_progress = QProgressBar()
        overview.add(self.year_progress)
        self.year_timeline_box = QWidget()
        self.year_timeline = FlowLayout(self.year_timeline_box, spacing=6)
        self.year_badges: dict[int, Badge] = {}
        overview.add(self.year_timeline_box)
        root.addWidget(overview)

        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        self.processing_counts = QLabel("—")
        self.processing_elapsed = QLabel("00:00")
        self.processing_remaining = QLabel("—")
        metrics.addWidget(stat_tile("Registros do ano", self.processing_counts), 2)
        metrics.addWidget(stat_tile("Decorrido", self.processing_elapsed), 1)
        metrics.addWidget(stat_tile("Restante (estimativa)", self.processing_remaining), 1)
        root.addLayout(metrics)

        log_card = Card("Registro das etapas")
        self.processing_log = QPlainTextEdit()
        self.processing_log.setReadOnly(True)
        self.processing_log.setPlaceholderText("As etapas do processamento aparecerão aqui.")
        log_card.add(self.processing_log, 1)
        root.addWidget(log_card, 1)

        actions = QFrame()
        actions.setObjectName("actionBar")
        bar = QHBoxLayout(actions)
        bar.setContentsMargins(12, 10, 12, 10)
        self.processing_outcome = StatusLine("Execução em andamento.")
        bar.addWidget(self.processing_outcome, 1)
        self.view_results_button = QPushButton("Ver resultados")
        self.view_results_button.setObjectName("primaryAction")
        self.view_results_button.setVisible(False)
        self.view_results_button.clicked.connect(lambda: self._go_to_page(PAGE_RESULTS))
        bar.addWidget(self.view_results_button)
        self.cancel_analysis_button = QPushButton("Cancelar análise")
        self.cancel_analysis_button.setObjectName("dangerAction")
        self.cancel_analysis_button.setEnabled(False)
        self.cancel_analysis_button.clicked.connect(self.cancel_analysis)
        bar.addWidget(self.cancel_analysis_button)
        root.addWidget(actions)
        self.processing_views.addWidget(running)
        self._add_page(self.processing_views)

    def _reset_year_timeline(self, years):
        for badge in self.year_badges.values():
            self.year_timeline.removeWidget(badge)
            badge.deleteLater()
        self.year_badges = {}
        for year in years:
            badge = Badge(str(year), "neutral")
            badge.setToolTip("Aguardando")
            self.year_timeline.addWidget(badge)
            self.year_badges[year] = badge

    def _mark_years(self, current=None, finished=False):
        for year, badge in self.year_badges.items():
            if finished or (current is not None and year < current):
                badge.set_status(f"✓ {year}", "success")
            elif year == current:
                badge.set_status(f"▶ {year}", "running")
            else:
                badge.set_status(str(year), "neutral")

    # -------------------------------------------------------- step 4 results
    def _build_results_tab(self):
        from .results_panel import ResultsPanel
        self.results_views = QStackedWidget()
        self.results_empty = EmptyState(
            "∑", "Nenhum resultado para esta seleção",
            "Os resultados são vinculados à fonte, à distribuidora e aos anos escolhidos. "
            "Selecione ao menos dois anos e execute a análise para explorar indicadores, gráficos e dados.",
            [("Escolher dados", lambda: self._go_to_page(PAGE_DATA), False),
             ("Definir análise", lambda: self._go_to_page(PAGE_ANALYSIS), True)],
        )
        self.results_views.addWidget(self.results_empty)
        self.results_panel = ResultsPanel(self)
        self.results_panel.openFolderRequested.connect(self.open_results_folder)
        self.results_panel.addTablesRequested.connect(self.add_result_layers)
        self.results_views.addWidget(self._scroll(self.results_panel))
        self._add_page(self.results_views)

    def _show_results_view(self, message: str | None = None):
        has_rows = bool(self.results_panel.rows)
        self.results_views.setCurrentIndex(1 if has_rows else 0)
        if not has_rows:
            self.results_empty.set_message(
                "Nenhum resultado para esta seleção" if message is None else "Resultados indisponíveis",
                message or (
                    "Os resultados são vinculados à fonte, à distribuidora e aos anos escolhidos. "
                    "Selecione ao menos dois anos e execute a análise para explorar indicadores, gráficos e dados."
                ),
            )
        self._refresh_overview()

    # --------------------------------------------------- overview and chips
    def _refresh_overview(self):
        if not hasattr(self, "nav") or not hasattr(self, "results_panel"):
            return
        postgres = self._using_postgres()
        if postgres:
            scope = self.postgres_browser.selected_scope()
            distributor, years = scope["distributor"], sorted(set(scope["years"]))
            source = f'PostgreSQL · {scope["connection"]}' if scope["connection"] else "PostgreSQL · sem conexão"
            data_ok = bool(scope["connection"] and years)
            data_text = f"{distributor} · {len(years)} ano(s)" if data_ok else "Selecione conexão e anos"
        else:
            distributor = self.distributor_combo.currentText()
            scope = self._analysis_scope()
            years = list(range(scope[1], scope[2] + 1)) if scope else []
            item = self.current_item()
            synced = bool(item and find_gdb(self.storage_edit.text(), item.friendly_name))
            source = "Arquivos da ANEEL"
            data_ok = synced
            data_text = (f"{distributor} {item.year} · " + ("sincronizada" if synced else "não baixada")) if item else "Selecione a distribuidora"
        self.nav.items[PAGE_DATA].set_status(data_text, "done" if data_ok else "attention")

        running = bool(self.analysis_thread and self.analysis_thread.isRunning())
        if running:
            self.nav.items[PAGE_ANALYSIS].set_status("Execução em andamento", "done")
        elif self.analysis_ready:
            self.nav.items[PAGE_ANALYSIS].set_status("Pronta para executar", "ready")
        else:
            reason = getattr(self, "_analysis_block_reason", "") or "Revise as configurações"
            self.nav.items[PAGE_ANALYSIS].set_status(reason, "attention")

        if running:
            self.nav.items[PAGE_PROGRESS].set_status(f"{self.overall_progress.value()}% concluído", "running")
        elif self.analysis_outcome == "success":
            self.nav.items[PAGE_PROGRESS].set_status("Última execução concluída", "done")
        elif self.analysis_outcome in ("failed", "cancelled"):
            self.nav.items[PAGE_PROGRESS].set_status(
                "Última execução falhou" if self.analysis_outcome == "failed" else "Última execução cancelada", "attention")
        else:
            self.nav.items[PAGE_PROGRESS].set_status("Nenhuma execução nesta sessão", "pending")

        cells = len(self.results_panel.rows)
        self.nav.items[PAGE_RESULTS].set_status(
            f"{br_int(cells)} células disponíveis" if cells else "Sem resultado para a seleção",
            "done" if cells else "pending")

        self.context_chips["source"].setText(source)
        self.context_chips["distributor"].setText(distributor or "Sem distribuidora")
        if years:
            span = f"{min(years)}–{max(years)}" if len(years) > 1 else str(years[0])
            self.context_chips["years"].setText(f"{span} · {len(years)} ano(s)")
        else:
            self.context_chips["years"].setText("Sem anos")
        self.context_chips["result"].setText(f"Resultado · {br_int(cells)} células")
        self.context_chips["result"].setVisible(bool(cells))

    # ------------------------------------------------------- data behaviour
    def choose_catalog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Selecionar catálogo", self.catalog_edit.text(), "CSV (*.csv)"
        )
        if path:
            self.catalog_edit.setText(path)
            self.reload_catalog()

    def choose_storage(self):
        path = QFileDialog.getExistingDirectory(
            self, "Selecionar armazenamento", self.storage_edit.text()
        )
        if path:
            self.storage_edit.setText(path)
            self.local_size_cache.clear()
            self.update_details()
            self._refresh_base_table()
            self.update_analysis_estimate()

    def _local_size(self, item: CatalogItem) -> int:
        folder = Path(self.storage_edit.text()) / item.friendly_name
        key = os.path.normcase(str(folder))
        if key in self.local_size_cache:
            return self.local_size_cache[key]
        size = 0
        if folder.exists():
            try:
                size = sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())
            except OSError:
                size = 0
        self.local_size_cache[key] = size
        return size

    def _refresh_base_table(self):
        if not hasattr(self, "base_table"):
            return
        distributor = self.distributor_combo.currentText().strip()
        items = sorted((item for item in self.items if item.distributor == distributor), key=lambda item: item.year)
        active_year = self.year_combo.currentText()
        self.base_table.blockSignals(True)
        self.base_table.setRowCount(len(items))
        counts = {"success": 0, "warning": 0, "neutral": 0}
        for row, item in enumerate(items):
            state = read_state(self.storage_edit.text(), item.friendly_name)
            gdb = find_gdb(self.storage_edit.text(), item.friendly_name)
            if gdb and state.get("item_id") == item.item_id:
                status, kind = "● Sincronizada", "success"
            elif gdb:
                status, kind = "● Desatualizada", "warning"
            else:
                status, kind = "○ Não baixada", "neutral"
            counts[kind] += 1
            values = [
                str(item.year), status, item.original_title,
                human_size(self._local_size(item)) if gdb else "—",
                format_publication_date(item.modified_raw) if item.modified_raw else item.reference_date,
            ]
            foreground, _background = STATUS_COLORS[kind]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setData(Qt.UserRole, item.year)
                if column == 1:
                    cell.setForeground(QColor(foreground))
                if column == 4 and item.modified_raw:
                    cell.setToolTip(item.modified_raw)
                self.base_table.setItem(row, column, cell)
            if str(item.year) == active_year:
                self.base_table.selectRow(row)
        self.base_table.blockSignals(False)
        if items:
            self.sync_badge.set_status(
                f'{counts["success"]}/{len(items)} sincronizadas',
                "success" if counts["success"] == len(items) else "warning" if counts["success"] else "neutral",
            )
        else:
            self.sync_badge.set_status("", "neutral")

    def _base_selection_changed(self):
        rows = sorted({index.row() for index in self.base_table.selectedIndexes()})
        if not rows:
            return
        year_item = self.base_table.item(rows[-1], 0)
        if year_item:
            self.year_combo.setCurrentText(year_item.text())
        self.sync_button.setText(f"Baixar {len(rows)} selecionada(s)" if len(rows) > 1 else "Baixar selecionada")

    def _selected_base_items(self) -> list[CatalogItem]:
        distributor = self.distributor_combo.currentText().strip()
        years = {
            int(self.base_table.item(row, 0).text())
            for row in {index.row() for index in self.base_table.selectedIndexes()}
            if self.base_table.item(row, 0)
        }
        if not years:
            current = self.current_item()
            return [current] if current else []
        return [item for item in self.items if item.distributor == distributor and item.year in years]

    def reload_catalog(self):
        if self._using_postgres() and not Path(self.catalog_edit.text()).is_file():
            self._source_changed()
            self.status.setText('PostgreSQL disponível sem catálogo CSV local.')
            return
        try:
            self.items = load_catalog(self.catalog_edit.text())
        except Exception as exc:
            QMessageBox.critical(self, "Catálogo BDGD", f"Não foi possível ler o catálogo:\n{exc}")
            return
        distributors = sorted({item.distributor for item in self.items})
        current = self.distributor_combo.currentText()
        self.distributor_combo.blockSignals(True)
        self.distributor_combo.clear()
        self.distributor_combo.addItems(distributors)
        preferred = current if current in distributors else "ENEL_RJ"
        if preferred in distributors:
            self.distributor_combo.setCurrentText(preferred)
        self.distributor_combo.blockSignals(False)
        self.populate_years()
        if self._using_postgres():
            self.status.setText("Pronto. Fonte atual: PostgreSQL.", "info")
        else:
            self.status.setText(f"Catálogo carregado: {br_int(len(self.items))} combinações de distribuidora e ano.", "info")

    def populate_years(self):
        distributor = self.distributor_combo.currentText()
        years = sorted({i.year for i in self.items if i.distributor == distributor}, reverse=True)
        self.year_combo.blockSignals(True)
        self.year_combo.clear()
        self.year_combo.addItems([str(year) for year in years])
        if 2025 in years:
            self.year_combo.setCurrentText("2025")
        self.year_combo.blockSignals(False)
        self.analysis_distributor_label.setText(distributor or "—")
        analysis_years = sorted(years)
        for combo in (self.start_year_combo, self.end_year_combo):
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems([str(year) for year in analysis_years])
            if current in [str(year) for year in analysis_years]:
                combo.setCurrentText(current)
            combo.blockSignals(False)
        if analysis_years:
            self.start_year_combo.setCurrentText(str(min(analysis_years)))
            self.end_year_combo.setCurrentText(str(max(analysis_years)))
        self._refresh_base_table()
        self.update_details()
        self._source_changed()

    def _using_postgres(self):
        return self.base_sources.currentWidget() is self.postgres_browser

    def _source_changed(self, *_):
        postgres = self._using_postgres()
        if postgres:
            scope = self.postgres_browser.selected_scope()
            distributor = scope['distributor']
            years = sorted(set(scope['years']))
            self.analysis_source_label.setText(
                f'<b>PostgreSQL</b> · {scope["connection"] or "selecione uma conexão"} · '
                f'{len(years)} ano(s) selecionado(s)'
            )
            self.period_note.setText(
                "O período acompanha os anos selecionados em Escolher dados. "
                + ("Anos: " + ", ".join(map(str, years)) + "." if years else "Nenhum ano selecionado.")
            )
            key = ("postgres", scope["connection"], distributor, tuple(years))
        else:
            distributor = self.distributor_combo.currentText()
            years = sorted({i.year for i in self.items if i.distributor == distributor})
            self.analysis_source_label.setText('<b>Arquivos locais da ANEEL</b> · escolha o período abaixo')
            self.period_note.setText("Todos os anos do período precisam estar sincronizados na etapa Escolher dados.")
            key = ("aneel", self.storage_edit.text(), distributor)
        self.change_years_button.setVisible(postgres)
        self.period_editor.setVisible(not postgres)
        self.period_label.setVisible(postgres)
        self.period_label.setText(
            (f"{min(years)}–{max(years)} · {len(years)} ano(s)" if len(years) > 1 else str(years[0])) if years else "Nenhum ano"
        )
        self.analysis_distributor_label.setText(distributor or '—')
        for combo, default in ((self.start_year_combo, min), (self.end_year_combo, max)):
            old = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems([str(y) for y in years])
            if years:
                combo.setCurrentText(str(default(years)) if postgres or old not in [str(y) for y in years] else old)
            combo.setEnabled(not postgres)
            combo.blockSignals(False)
        self.update_analysis_estimate()
        if key != self._results_scope_key:
            self._results_scope_key = key
            self.load_existing_results()
        self._refresh_overview()

    def _analysis_output_dir(self):
        scope = self._analysis_scope()
        distributor = scope[0] if scope else self.distributor_combo.currentText()
        output = Path(self.storage_edit.text()) / f'{distributor.upper()}_ANALISES'
        if self._using_postgres():
            selected = self.postgres_browser.selected_scope()
            identity = json.dumps([selected['connection'], sorted(zip(selected['years'], selected['schemas']))])
            output = output / 'postgres' / hashlib.sha256(identity.encode()).hexdigest()[:16]
        return output

    def current_item(self) -> CatalogItem | None:
        try:
            year = int(self.year_combo.currentText())
        except ValueError:
            return None
        distributor = self.distributor_combo.currentText()
        return next((i for i in self.items if i.distributor == distributor and i.year == year), None)

    def update_details(self):
        item = self.current_item()
        if item is None:
            self.friendly_label.setText("—")
            self.details.setText("Nenhuma base selecionada.")
            return
        self.friendly_label.setText(item.friendly_name)
        state = read_state(self.storage_edit.text(), item.friendly_name)
        local = "sincronizada" if find_gdb(self.storage_edit.text(), item.friendly_name) else "não baixada"
        self.details.setText(
            f"<b>Publicação</b> {item.original_title}<br>"
            f"<b>Item ArcGIS</b> {item.item_id}<br>"
            f"<b>Estado local</b> {local} ({state.get('original_title', 'sem versão local')})"
        )
        self._refresh_overview()

    def _local_profile_changed(self, *_):
        profile = self.local_profile_combo.currentData() or "essential"
        self.local_profile_note.setText(PROFILES[profile]["description"])
        self.local_profile_combo.setToolTip(PROFILES[profile]["description"])

    def _analysis_scope(self) -> tuple[str, int, int] | None:
        if self._using_postgres():
            scope = self.postgres_browser.selected_scope()
            if not scope['connection'] or not scope['years']:
                return None
            return scope['distributor'], min(scope['years']), max(scope['years'])
        distributor = self.distributor_combo.currentText().strip()
        try:
            start_year = int(self.start_year_combo.currentText())
            end_year = int(self.end_year_combo.currentText())
        except ValueError:
            return None
        if start_year > end_year:
            start_year, end_year = end_year, start_year
        return distributor, start_year, end_year

    def _analysis_checks(self, years_ready: bool, years_text: str) -> list[tuple[bool, str]]:
        sampling_mode = self.sampling_combo.currentData() or "auto"
        levels = sum(widget.isChecked() for widget in self.output_level_checks.values())
        strata = sum(widget.isChecked() for widget in self.stratum_checks.values())
        return [
            (years_ready, years_text),
            (levels > 0, f"{levels} nível(is) de saída" if levels else "Selecione ao menos um nível de saída"),
            (sampling_mode == "census" or strata > 0,
             "Censo dispensa estratos" if sampling_mode == "census" else
             f"{strata} campo(s) de estratificação" if strata else "Selecione ao menos um estrato"),
        ]

    def _apply_analysis_checks(self, checks):
        running = bool(self.analysis_thread and self.analysis_thread.isRunning())
        lines = []
        for ok, text in checks:
            color, symbol = ("#13795b", "✓") if ok else ("#b42318", "✕")
            lines.append(f'<div style="margin:2px 0;"><span style="color:{color}; font-weight:700;">{symbol}</span>'
                         f'&nbsp;&nbsp;<span style="color:#3b4f66;">{text}</span></div>')
        if running:
            lines.append('<div style="margin:2px 0; color:#6d3fd1;"><b>▶</b>&nbsp;&nbsp;Uma análise está em execução</div>')
        self.analysis_checks_label.setText("".join(lines))
        failed = [text for ok, text in checks if not ok]
        self.analysis_ready = not failed and not running
        self._analysis_block_reason = failed[0] if failed else ""
        self.analysis_button.setEnabled(self.analysis_ready)
        self._refresh_overview()

    def update_analysis_estimate(self, *_):
        if not hasattr(self, "analysis_checks_label"):
            return
        if self._using_postgres():
            from .analyses.postgres_source import analysis_entries
            from .analyses.demand_mvp import AUTO_SAMPLE_THRESHOLD, sample_size
            scope = self.postgres_browser.selected_scope()
            try:
                entries = analysis_entries(self.postgres_browser.entries, scope)
                mode = self.sampling_combo.currentData()
                confidence = float(self.confidence_combo.currentData())
                margin = self.margin_spin.value() / 100.0
                population = sum(max(0, e['rows']) for e in entries.values())
                sample = sum(sample_size(max(0, e['rows']), confidence, margin)
                             if mode == 'stratified' or (mode == 'auto' and e['rows'] >= AUTO_SAMPLE_THRESHOLD)
                             else max(0, e['rows']) for e in entries.values())
                self.estimated_population = population
                self.estimate_population.setText(f'≈ {br_int(population)}')
                self.estimate_population.setToolTip('Contagens da importação ou estimativas do catálogo; verificadas na execução.')
                self.estimate_sample.setText(f'≈ {br_int(sample)}')
                self.estimate_time.setText('Na execução')
                self.estimate_size.setText(human_size(int(sample * 340)))
                ready = len(entries) >= 2
                years_text = (f"{len(entries)} anos com UCBT no PostgreSQL" if ready else
                              "Selecione ao menos dois anos em Escolher dados")
            except ValueError as exc:
                self.estimated_population = 0
                for label in (self.estimate_population, self.estimate_sample, self.estimate_time, self.estimate_size):
                    label.setText('—')
                ready, years_text = False, str(exc)
            self._apply_analysis_checks(self._analysis_checks(ready, years_text))
            return
        scope = self._analysis_scope()
        if scope is None:
            self._apply_analysis_checks(self._analysis_checks(False, "Escolha uma distribuidora com anos disponíveis"))
            return
        distributor, start_year, end_year = scope
        from .analyses.demand_mvp import AUTO_SAMPLE_THRESHOLD, _feature_count, sample_size
        mode = self.sampling_combo.currentData() or "auto"
        confidence = float(self.confidence_combo.currentData() or 0.99)
        margin = self.margin_spin.value() / 100.0
        population = 0
        sample = 0
        sampled_years = 0
        available_years = 0
        missing = []
        for year in range(start_year, end_year + 1):
            gdb = find_gdb(self.storage_edit.text(), f"{distributor}_{year}")
            if gdb is None:
                missing.append(year)
                continue
            key = str(gdb)
            if key not in self.feature_count_cache:
                self.feature_count_cache[key] = _feature_count(gdb)
            count = self.feature_count_cache[key]
            population += count
            available_years += 1
            use_sample = mode == "stratified" or (mode == "auto" and count >= AUTO_SAMPLE_THRESHOLD)
            if use_sample:
                sample += sample_size(count, confidence, margin)
                sampled_years += 1
            else:
                sample += count
        self.estimated_population = population
        self.estimate_population.setText(br_int(population) if population else "Sem bases locais")
        self.estimate_sample.setText(f"≈ {br_int(sample)}" if sample else "—")
        if not available_years:
            seconds = 0
        elif sampled_years:
            seconds = sampled_years * 20 + (available_years - sampled_years) * 5
        else:
            seconds = max(5, int(population / 9_000))
        self.estimate_time.setText(self._format_duration(seconds) if seconds else "—")
        self.estimate_size.setText(human_size(int(sample * 340)) if sample else "—")
        span = end_year - start_year + 1
        if span < 2:
            years_text, ready = "Escolha um período com ao menos dois anos", False
        elif missing:
            years_text, ready = "Sincronize: " + ", ".join(map(str, missing)), False
        else:
            years_text, ready = f"{span} anos sincronizados ({start_year}–{end_year})", True
        self._apply_analysis_checks(self._analysis_checks(ready, years_text))

    @staticmethod
    def _format_duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        minutes, second = divmod(seconds, 60)
        hours, minute = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minute:02d}min"
        return f"{minute:02d}:{second:02d}"

    # ----------------------------------------------------------- downloads
    def begin_sync(self):
        items = self._selected_base_items()
        pending = []
        for item in items:
            state = read_state(self.storage_edit.text(), item.friendly_name)
            if state.get("item_id") != item.item_id or not find_gdb(self.storage_edit.text(), item.friendly_name):
                pending.append(item)
        if not pending:
            QMessageBox.information(self, "BDGD ANEEL", "As bases selecionadas já estão sincronizadas.")
            return
        if QMessageBox.question(
            self,
            "Confirmar sincronização",
            f"Sincronizar {len(pending)} base(s)? Os downloads serão executados em sequência e podem ser retomados.",
        ) != QMessageBox.Yes:
            return
        self.sync_queue = sorted(pending, key=lambda item: item.year)
        self.sync_total = len(self.sync_queue)
        self.sync_button.setEnabled(False)
        self.pause_sync_button.setEnabled(True)
        self.progress.setVisible(True)
        self._sync_next()

    def _sync_next(self):
        if not self.sync_queue:
            self.sync_button.setEnabled(True)
            self.pause_sync_button.setEnabled(False)
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            QTimer.singleShot(1500, lambda: self.progress.setVisible(bool(self.sync_queue)))
            self._refresh_base_table()
            self.update_analysis_estimate()
            self.status.setText("Sincronização da seleção concluída.")
            return
        item = self.sync_queue.pop(0)
        self.selected_item = item
        completed = self.sync_total - len(self.sync_queue)
        self.status.setText(f"Consultando {item.friendly_name} ({completed}/{self.sync_total})…")
        reply = QgsNetworkAccessManager.instance().head(QNetworkRequest(QUrl(item.download_url)))
        reply.finished.connect(lambda: self._head_finished(reply))

    def pause_download(self):
        self.sync_queue.clear()
        if self.current_reply is not None:
            self.current_reply.abort()
            self.status.setText("Download pausado. O arquivo parcial foi preservado.")
        else:
            self.status.setText("Fila de sincronização pausada.")
            self.sync_button.setEnabled(True)
            self.pause_sync_button.setEnabled(False)

    def _stop_sync(self, message: str):
        self.sync_queue.clear()
        self.sync_button.setEnabled(True)
        self.pause_sync_button.setEnabled(False)
        self.progress.setRange(0, 100)
        self.status.setText(message)

    def _head_finished(self, reply):
        try:
            if reply.error() != QNetworkReply.NoError:
                raise RuntimeError(reply.errorString())
            size = int(bytes(reply.rawHeader(b"Content-Length")) or b"0")
        except Exception as exc:
            reply.deleteLater()
            self._stop_sync("Falha ao consultar a base.")
            QMessageBox.critical(self, "BDGD ANEEL", f"Falha ao consultar o arquivo:\n{exc}")
            return
        reply.deleteLater()
        storage = Path(self.storage_edit.text())
        probe = storage if storage.exists() else Path.home()
        free = shutil.disk_usage(probe).free
        required = max(size * 3, size + 2 * 1024**3)
        if free < required:
            self._stop_sync("Sincronização interrompida por falta de espaço.")
            QMessageBox.critical(
                self, "Espaço insuficiente",
                f"Arquivo: {human_size(size)}\nEspaço livre: {human_size(free)}\n"
                f"Reserva recomendada: {human_size(required)}",
            )
            return
        cached_zip = Path(self.storage_edit.text()) / self.selected_item.friendly_name / f"{self.selected_item.friendly_name}.gdb.zip"
        if cached_zip.exists() and cached_zip.stat().st_size == size:
            self._start_extraction(cached_zip)
            return
        self._start_download(size)

    def _start_download(self, size: int):
        item = self.selected_item
        folder = Path(self.storage_edit.text()) / item.friendly_name
        folder.mkdir(parents=True, exist_ok=True)
        self._part_path = folder / f"{item.friendly_name}.gdb.zip.part"
        self._existing_part = self._part_path.stat().st_size if self._part_path.exists() else 0
        request = QNetworkRequest(QUrl(item.download_url))
        if self._existing_part:
            request.setRawHeader(b"Range", f"bytes={self._existing_part}-".encode("ascii"))
        self.current_reply = QgsNetworkAccessManager.instance().get(request)
        self.expected_size = size
        self.base_received = self._existing_part
        self.output_file = None
        self.current_reply.metaDataChanged.connect(self._open_output)
        self.current_reply.readyRead.connect(self._write_available)
        self.current_reply.downloadProgress.connect(self._download_progress)
        self.current_reply.finished.connect(self._download_finished)
        self.sync_button.setEnabled(False)
        self.status.setText("Baixando…")

    def _open_output(self):
        if self.output_file is not None:
            return
        status = int(self.current_reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) or 0)
        append = self._existing_part > 0 and status == 206
        self.base_received = self._existing_part if append else 0
        self.output_file = self._part_path.open("ab" if append else "wb")

    def _write_available(self):
        self._open_output()
        self.output_file.write(bytes(self.current_reply.readAll()))

    def _download_progress(self, received, total):
        absolute = self.base_received + max(int(received), 0)
        denominator = self.expected_size or self.base_received + max(int(total), 0)
        self.progress.setValue(int(min(100, absolute * 100 / denominator)) if denominator else 0)
        self.status.setText(f"Baixando {self.selected_item.friendly_name}: {human_size(absolute)} de {human_size(denominator)}")

    def _download_finished(self):
        self._write_available()
        if self.output_file:
            self.output_file.close()
        reply, self.current_reply, self.output_file = self.current_reply, None, None
        self.sync_button.setEnabled(False)
        if reply.error() != QNetworkReply.NoError:
            error = reply.errorString()
            cancelled = reply.error() == QNetworkReply.OperationCanceledError
            reply.deleteLater()
            self._stop_sync("Download pausado. O arquivo parcial foi preservado." if cancelled else "Falha no download.")
            if cancelled:
                return
            QMessageBox.warning(self, "Download interrompido", f"{error}\nO arquivo parcial foi preservado.")
            return
        reply.deleteLater()
        final_zip = self._part_path.with_suffix("")
        os.replace(self._part_path, final_zip)
        self._start_extraction(final_zip)

    def _start_extraction(self, final_zip: Path):
        self.progress.setRange(0, 0)
        self.status.setText("Extraindo a geodatabase…")
        item, base_dir = self.selected_item, self.storage_edit.text()
        target = str(Path(base_dir) / item.friendly_name / "current")
        self.extract_thread = ExtractThread(str(final_zip), target, self)

        def extracted(result):
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            write_state(base_dir, item.friendly_name, {
                "item_id": item.item_id, "original_title": item.original_title,
                "modified": item.modified_raw, "friendly_name": item.friendly_name,
                "gdb_path": result,
            })
            self.local_size_cache.pop(os.path.normcase(str(Path(base_dir) / item.friendly_name)), None)
            self.status.setText(f"{item.friendly_name} sincronizada com sucesso.")
            self.update_details()
            self._refresh_base_table()
            self._sync_next()

        def failed(message):
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self._stop_sync("Falha na extração.")
            QMessageBox.critical(self, "Extração", message)

        self.extract_thread.succeeded.connect(extracted)
        self.extract_thread.failed.connect(failed)
        self.extract_thread.finished.connect(lambda: setattr(self, "extract_thread", None))
        self.extract_thread.start()

    def add_layers(self):
        item = self.current_item()
        if item is None:
            return
        gdb = find_gdb(self.storage_edit.text(), item.friendly_name)
        if gdb is None:
            QMessageBox.information(self, "BDGD ANEEL", "Sincronize a base antes de adicionar suas camadas.")
            return
        all_details = [d for d in QgsProviderRegistry.instance().querySublayers(str(gdb)) if d.providerKey() == "ogr"]
        profile = self.local_profile_combo.currentData() or "essential"
        details = [d for d in all_details if matches_profile(d.name(), profile)]
        if not details:
            QMessageBox.warning(
                self, "Explorador BDGD",
                "Nenhuma camada da base corresponde ao perfil escolhido. Tente “Base completa — avançado”.",
            )
            return
        profile_title = PROFILES[profile]["title"]
        if QMessageBox.question(
            self, "Adicionar camadas",
            f"Adicionar {len(details)} de {len(all_details)} camadas usando o perfil “{profile_title}”?\n\n"
            "Elas serão organizadas por tema e entrarão inicialmente ocultas.",
        ) != QMessageBox.Yes:
            return
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        group_name = f"BDGD • {item.distributor} • {item.year}"
        group = root.findGroup(group_name) or root.addGroup(group_name)
        added, existing = 0, 0
        for detail in details:
            identity = json.dumps([str(gdb), detail.name(), detail.uri()])
            if any(layer.customProperty("bdgd/local_identity") == identity for layer in project.mapLayers().values()):
                existing += 1
                continue
            layer = QgsVectorLayer(detail.uri(), detail.name(), detail.providerKey())
            if layer.isValid():
                apply_layer_semantics(layer, detail.name())
                layer.setCustomProperty("bdgd/local_identity", identity)
                semantic_group_name = theme_title(detail.name())
                semantic_group = group.findGroup(semantic_group_name) or group.addGroup(semantic_group_name)
                project.addMapLayer(layer, False)
                node = semantic_group.addLayer(layer)
                node.setItemVisibilityChecked(False)
                added += 1
        self.status.setText(
            f"{added} camada(s) adicionada(s) por tema ao grupo {group_name}; "
            f"{existing} já estavam no projeto."
        )

    # ------------------------------------------------------------ analysis
    def begin_analysis(self):
        scope = self._analysis_scope()
        if scope is None:
            return
        distributor, start_year, end_year = scope
        years = list(range(start_year, end_year + 1))
        postgres = None
        if self._using_postgres():
            from .analyses.postgres_source import analysis_entries
            selected = self.postgres_browser.selected_scope()
            try:
                entries = analysis_entries(self.postgres_browser.entries, selected)
                connection = QgsProviderRegistry.instance().providerMetadata('postgres').findConnection(selected['connection'])
                uri = QgsDataSourceUri(connection.uri())
                uri.setParam('connect_timeout', '8')
                postgres = {
                    'connection_info': uri.connectionInfo(True),
                    'entries': entries,
                    'metadata': {'type': 'postgres', 'connection': selected['connection'],
                                 'tables': [{'year': y, 'schema': e['schema'], 'table': e['table']} for y, e in sorted(entries.items())]},
                }
                years = sorted(entries)
            except ValueError as exc:
                QMessageBox.information(self, 'Análise PostgreSQL', str(exc))
                return
            except Exception:
                QMessageBox.information(self, 'Análise PostgreSQL', 'Confira a conexão salva no QGIS.')
                return
        if not distributor or len(years) < 2:
            QMessageBox.information(self, "Análise BDGD", "Selecione uma distribuidora e pelo menos dois anos.")
            return
        missing = [] if postgres else [year for year in years if find_gdb(self.storage_edit.text(), f"{distributor}_{year}") is None]
        if missing:
            QMessageBox.information(self, "Análise BDGD", f"Sincronize primeiro os anos: {', '.join(map(str, missing))}.")
            return
        if self.analysis_thread and self.analysis_thread.isRunning():
            return
        sampling_mode = self.sampling_combo.currentData() or "auto"
        confidence = float(self.confidence_combo.currentData() or 0.99)
        margin_error = self.margin_spin.value() / 100.0
        output_levels = [key for key, widget in self.output_level_checks.items() if widget.isChecked()]
        strata_fields = [key for key, widget in self.stratum_checks.items() if widget.isChecked()]
        if not output_levels:
            QMessageBox.information(self, "Níveis de saída", "Selecione pelo menos um nível de saída.")
            return
        if sampling_mode != "census" and not strata_fields:
            QMessageBox.information(self, "Estratos da amostra", "Selecione pelo menos um campo de estratificação.")
            return
        if not strata_fields:
            strata_fields = ["patamar_consumo"]
        if sampling_mode == "census" and self.estimated_population > 1_000_000 and QMessageBox.question(
            self,
            "Censo completo",
            "O censo completo pode levar muito tempo e gerar arquivos grandes. Deseja continuar?",
        ) != QMessageBox.Yes:
            return
        released_layers = self._release_analysis_output_layers(distributor, start_year, end_year)
        self.analysis_years = years
        self.analysis_outcome = None
        self.base_sources.setEnabled(False)
        self.source_switch.setEnabled(False)
        self.storage_edit.setEnabled(False)
        self.analysis_button.setEnabled(False)
        self.cancel_analysis_button.setEnabled(True)
        self.view_results_button.setVisible(False)
        self.processing_outcome.setText("Execução em andamento. Você pode navegar pelas outras etapas.", "running")
        self.processing_badge.set_status("Em execução", "running")
        self.overall_progress.setValue(0)
        self.year_progress.setValue(0)
        self._reset_year_timeline(years)
        self._mark_years(current=years[0])
        self.processing_log.clear()
        if released_layers:
            self.processing_log.appendPlainText(
                f"{len(released_layers)} tabela(s) da execução anterior foram liberadas para atualização."
            )
        self.processing_stage.setText(f"Preparando análise de {distributor}")
        self.processing_counts.setText("preparando…")
        self.processing_remaining.setText("calculando…")
        self.processing_views.setCurrentIndex(1)
        self.analysis_clock.start()
        self.analysis_timer.start(1000)
        self.status.setText(f"Iniciando análise de {distributor}…")
        self.analysis_thread = AnalysisThread(
            self.storage_edit.text(), distributor, start_year, end_year,
            sampling_mode=sampling_mode,
            confidence=confidence,
            margin_error=margin_error,
            parent=self,
            postgres=postgres,
            output_dir=str(self._analysis_output_dir()),
            output_levels=output_levels,
            strata_fields=strata_fields,
        )
        self.analysis_thread.progress.connect(self._analysis_progress)
        self.analysis_thread.succeeded.connect(self._analysis_succeeded)
        self.analysis_thread.failed.connect(self._analysis_failed)
        self.analysis_thread.cancelled.connect(self._analysis_cancelled)
        self.analysis_thread.finished.connect(self._analysis_thread_finished)
        self.analysis_thread.start()
        self.update_analysis_estimate()
        self._go_to_page(PAGE_PROGRESS)

    def _analysis_thread_finished(self):
        self.analysis_thread = None
        self.update_analysis_estimate()

    def _release_analysis_output_layers(self, distributor: str, start_year: int, end_year: int) -> list[str]:
        """Remove only generated tables that would lock this run's output files on Windows."""
        output_dir = self._analysis_output_dir()
        output_dir_key = os.path.normcase(os.path.abspath(str(output_dir)))
        prefix = distributor.upper()
        target_stems = (
            f"{prefix}_customer_panel_{start_year}_{end_year}",
            f"{prefix}_sampling_strata_{start_year}_{end_year}",
            f"{prefix}_segments_monthly_{start_year}_{end_year}",
            f"{prefix}_distributor_monthly_{start_year}_{end_year}",
            f"{prefix}_municipio_monthly_{start_year}_{end_year}",
            f"{prefix}_sub_monthly_{start_year}_{end_year}",
            f"{prefix}_conj_monthly_{start_year}_{end_year}",
            f"{prefix}_ctmt_monthly_{start_year}_{end_year}",
            f"{prefix}_untrmt_monthly_{start_year}_{end_year}",
            f"{prefix}_pncon_monthly_{start_year}_{end_year}",
        )
        layer_ids, layer_names = [], []
        for layer in QgsProject.instance().mapLayers().values():
            source = (layer.source() or "").split("|", 1)[0]
            if not source:
                continue
            source_path = Path(source)
            source_dir_key = os.path.normcase(os.path.abspath(str(source_path.parent)))
            if source_dir_key != output_dir_key or source_path.suffix.lower() != ".csv":
                continue
            if any(source_path.stem == stem or source_path.stem.startswith(stem + "_recuperado_") for stem in target_stems):
                layer_ids.append(layer.id())
                layer_names.append(layer.name())
        if layer_ids:
            QgsProject.instance().removeMapLayers(layer_ids)
        return layer_names

    def _analysis_progress(self, payload):
        if isinstance(payload, str):
            payload = {"message": payload}
        message = payload.get("message", "Processando…")
        stage = payload.get("stage", "Processamento")
        year = payload.get("year")
        year_percent = int(payload.get("year_percent", 0))
        overall = payload.get("overall_percent")
        if overall is None and year in self.analysis_years:
            index = self.analysis_years.index(year)
            overall = int((index + year_percent / 100.0) * 85 / max(1, len(self.analysis_years)))
        self.processing_stage.setText(stage + (f" — {year}" if year else ""))
        self.year_progress.setValue(year_percent)
        if year in self.year_badges:
            self._mark_years(current=year)
        if overall is not None:
            self.overall_progress.setValue(int(overall))
        processed, total = payload.get("processed"), payload.get("total")
        if processed is not None and total:
            self.processing_counts.setText(f"{br_int(processed)} de {br_int(total)}")
        if not self.processing_log.toPlainText().endswith(message):
            self.processing_log.appendPlainText(message)
        self.status.setText(message, "running")
        self._update_elapsed()
        self._refresh_overview()

    def _update_elapsed(self):
        if not self.analysis_clock.isValid():
            return
        elapsed = self.analysis_clock.elapsed() / 1000.0
        self.processing_elapsed.setText(self._format_duration(elapsed))
        progress = self.overall_progress.value()
        if progress > 2:
            remaining = elapsed * (100 - progress) / progress
            self.processing_remaining.setText(f"~{self._format_duration(remaining)}")

    def cancel_analysis(self):
        if self.analysis_thread and self.analysis_thread.isRunning():
            self.analysis_thread.requestInterruption()
            self.cancel_analysis_button.setEnabled(False)
            self.processing_stage.setText("Cancelando com segurança…")
            self.processing_badge.set_status("Cancelando", "warning")
            self.processing_log.appendPlainText("Cancelamento solicitado; finalizando o lote atual.")

    def _finish_analysis_ui(self):
        self.analysis_timer.stop()
        self.cancel_analysis_button.setEnabled(False)
        self.base_sources.setEnabled(True)
        self.source_switch.setEnabled(True)
        self.storage_edit.setEnabled(True)
        self.update_analysis_estimate()

    def _analysis_cancelled(self):
        self.analysis_outcome = "cancelled"
        self._finish_analysis_ui()
        self.processing_stage.setText("Análise cancelada")
        self.processing_badge.set_status("Cancelada", "warning")
        self.processing_outcome.setText("Análise cancelada. O resultado anterior foi preservado.", "warning")
        self.processing_log.appendPlainText("Análise cancelada. O resultado anterior foi preservado.")
        self.status.setText("Análise cancelada; nenhum painel parcial foi publicado.")
        self._refresh_overview()

    def _analysis_succeeded(self, result):
        self.analysis_outcome = "success"
        self._finish_analysis_ui()
        self.overall_progress.setValue(100)
        self.year_progress.setValue(100)
        self._mark_years(finished=True)
        self.processing_stage.setText("Análise concluída")
        self.processing_badge.set_status("Concluída", "success")
        distributor = str(result.get("distributor", self.distributor_combo.currentText())).upper()
        years = result.get("years", [])
        start_year, end_year = (min(years), max(years)) if years else (2017, 2025)
        self.result_output_dir = Path(result.get('output_dir') or self._analysis_output_dir())
        self._populate_results(result, start_year, end_year)
        sampling_years = result.get("sampling", {}).get("years", [])
        sampled = sum(item.get("actual_sample", 0) for item in sampling_years)
        population = sum(item.get("population", 0) for item in sampling_years)
        sample_note = f" Amostra: {br_int(sampled)} de {br_int(population)}." if sampled and sampled < population else ""
        publication_warnings = result.get("publication_warnings", [])
        publication_note = " Alguns arquivos bloqueados receberam nomes de recuperação." if publication_warnings else ""
        message = f"Análise de {distributor} concluída.{sample_note}{publication_note}"
        self.processing_outcome.setText(message, "warning" if publication_warnings else "success")
        self.view_results_button.setVisible(True)
        self.status.setText(message, "success")
        self._go_to_page(PAGE_RESULTS)

    def _analysis_failed(self, message):
        self.analysis_outcome = "failed"
        self._finish_analysis_ui()
        self.processing_stage.setText("Falha na análise")
        self.processing_badge.set_status("Falhou", "danger")
        self.processing_outcome.setText(message, "danger")
        self.processing_log.appendPlainText(message)
        self.status.setText("Falha na análise.")
        self._refresh_overview()
        QMessageBox.critical(self, "Análise BDGD", message)

    # ------------------------------------------------------------- results
    def _populate_results(self, result: dict, start_year: int, end_year: int):
        self.result_metadata = result
        files = result.get("files", [])
        segment_name = next(
            (name for name in files if "_segments_monthly_" in name),
            f"{result['distributor']}_segments_monthly_{start_year}_{end_year}.csv",
        )
        segment_path = self.result_output_dir / segment_name
        self.result_rows = []
        if segment_path.exists():
            with segment_path.open("r", encoding="utf-8-sig", newline="") as handle:
                self.result_rows = list(csv.DictReader(handle))
            from .analyses.results_model import input_fingerprint
            result = dict(result, view_input=input_fingerprint(segment_path))
            self.result_metadata = result
        self.result_legacy = bool(self.result_rows and "MUNICIPIO" not in self.result_rows[0])
        strata_name = next(
            (name for name in files if "_sampling_strata_" in name),
            f"{result['distributor']}_sampling_strata_{start_year}_{end_year}.csv",
        )
        strata_path = self.result_output_dir / strata_name
        self.result_strata_rows = []
        if strata_path.exists():
            with strata_path.open("r", encoding="utf-8-sig", newline="") as handle:
                self.result_strata_rows = list(csv.DictReader(handle))
        self.results_panel.set_results(self.result_rows, result, self.result_strata_rows, self.result_output_dir)
        self._show_results_view()

    def _clear_results(self, message: str | None = None):
        self.result_metadata = {}
        self.result_rows = []
        self.result_strata_rows = []
        self.result_output_dir = None
        self.results_panel.set_results([], {})
        self._show_results_view(message)

    def load_existing_results(self):
        output = self._analysis_output_dir()
        metadata_path = output / "run_metadata.json"
        if not metadata_path.exists():
            self._clear_results()
            return
        try:
            result = json.loads(metadata_path.read_text(encoding="utf-8"))
            years = result.get("years", [])
            if not years:
                self._clear_results()
                return
            self.result_output_dir = output
            self._populate_results(result, min(years), max(years))
        except (OSError, ValueError, TypeError):
            self._clear_results('Não foi possível ler os resultados desta seleção. Verifique os arquivos de saída.')
            return

    def _filtered_result_rows(self) -> list[dict]:
        self.results_panel.refresh()
        return self.results_panel.filtered

    def refresh_results_view(self):
        self.results_panel.refresh()

    def add_result_layers(self):
        if not self.result_output_dir or not self.result_metadata:
            return
        existing = {layer.name() for layer in QgsProject.instance().mapLayers().values()}
        added = 0
        for filename in self.result_metadata.get("files", []):
            path = self.result_output_dir / filename
            name = path.stem.replace("_", " ")
            if path.exists() and name not in existing:
                layer = QgsVectorLayer(str(path), name, "ogr")
                if layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    added += 1
        self.status.setText(f"{added} tabela(s) de resultado adicionada(s) ao projeto.")

    def export_filtered_results(self):
        self.results_panel.export_csv()

    def open_results_folder(self):
        if self.result_output_dir and self.result_output_dir.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.result_output_dir)))


class BdgdAneelPlugin:
    def __init__(self, iface):
        self.iface, self.action, self.dialog = iface, None, None

    def initGui(self):
        self.action = QAction(plugin_icon(), "Explorador BDGD", self.iface.mainWindow())
        self.action.setToolTip("Explorador BDGD — bases, camadas e análises da BDGD")
        self.action.triggered.connect(self.run)
        self.iface.addPluginToDatabaseMenu("BDGD ANEEL", self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginDatabaseMenu("BDGD ANEEL", self.action)
            self.iface.removeToolBarIcon(self.action)
        if self.dialog:
            if self.dialog.database_thread and self.dialog.database_thread.isRunning():
                self.dialog.database_thread.requestInterruption()
            self.dialog.close()

    def run(self):
        if self.dialog is None:
            self.dialog = BdgdDialog(self.iface, self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        self.dialog.prepare_database()

    def show_tab(self, index):
        """Open the plugin dialog on a specific workflow step."""
        self.run()
        index = max(0, min(int(index), self.dialog.tabs.count() - 1))
        self.dialog._go_to_page(index)
        return index
