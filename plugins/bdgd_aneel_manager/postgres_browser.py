"""Read-only PostgreSQL catalog and layer selection for the BDGD manager."""
from __future__ import annotations

import json
import re

from qgis.PyQt.QtCore import QSettings, QThread, QTimer, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from qgis.core import (
    Qgis, QgsDataSourceUri, QgsFeedback, QgsGradientColorRamp, QgsHeatmapRenderer,
    QgsMarkerSymbol, QgsProject, QgsProviderRegistry, QgsSingleSymbolRenderer,
    QgsVectorLayer,
)

from .semantic_catalog import (
    PROFILES, apply_layer_semantics, display_name, entity_info, matches_profile,
    theme_title,
)
from .theme import STATUS_COLORS
from .ui_kit import Badge, Card, SegmentedControl, StatusLine, field_label, hint

PREFIX = "bdgd_aneel_manager/postgres/"


def br_int(value) -> str:
    """Format an integer with Brazilian thousands separators."""
    try:
        return f"{int(value):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"
CATALOG_SQL = """
SELECT n.nspname, c.relname, c.reltuples::bigint,
       COALESCE(g.attname, ''),
       COALESCE((
         SELECT string_agg(a.attname, ',' ORDER BY a.attnum)
         FROM pg_index i JOIN pg_attribute a
           ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
         WHERE i.indrelid=c.oid AND i.indisprimary
       ), ''),
       has_table_privilege(c.oid, 'SELECT'),
       EXISTS (
         SELECT 1 FROM pg_attribute r
         WHERE r.attrelid=c.oid AND r.attnum>0 AND NOT r.attisdropped
           AND lower(r.attname)='pn_con'
       ),
       COALESCE((
         SELECT pg.attname
         FROM pg_class pc
         JOIN pg_attribute pg ON pg.attrelid=pc.oid
           AND pg.attnum>0 AND NOT pg.attisdropped
           AND pg.atttypid IN (SELECT oid FROM pg_type WHERE typname IN ('geometry','geography'))
         WHERE pc.relnamespace=n.oid AND lower(pc.relname)='ponnot'
         ORDER BY pg.attnum LIMIT 1
       ), '')
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
LEFT JOIN pg_attribute g ON g.attrelid=c.oid AND g.attnum>0 AND NOT g.attisdropped
  AND g.atttypid IN (SELECT oid FROM pg_type WHERE typname IN ('geometry','geography'))
WHERE c.relkind IN ('r','p')
  AND n.nspname ~ '^[a-zA-Z0-9_]+_[0-9]{4}$'
  AND has_schema_privilege(n.oid, 'USAGE')
ORDER BY n.nspname DESC, c.relname, g.attname
"""


def catalog_entries(rows, imports):
    """Combine actual relations with optional import provenance."""
    provenance = {(str(r[0]), str(r[1])): r for r in imports}
    entries = []
    for row in rows:
        schema, table, estimate, geometry, key, readable, *semantic = row
        references_ponnot = (
            semantic[0] is True or str(semantic[0]).lower() in ("true", "t", "1")
        ) if semantic else False
        ponnot_geometry = str(semantic[1] or '') if len(semantic) > 1 else ''
        match = re.fullmatch(r"(.+)_(\d{4})", str(schema))
        if not match:
            continue
        logged = provenance.get((str(schema), str(table)))
        entries.append({
            "schema": str(schema), "table": str(table),
            "distributor": str(logged[2]) if logged else match[1].upper(),
            "year": int(logged[3]) if logged else int(match[2]),
            "rows": int(logged[5]) if logged and logged[4] == "success" and logged[5] is not None else int(estimate or 0),
            "count_source": "na importação" if logged and logged[4] == "success" and logged[5] is not None else "estimativa",
            "status": str(logged[4]) if logged else "sem registro de importação",
            "geometry": str(geometry or ""), "key": str(key or ""),
            "references_ponnot": references_ponnot and bool(ponnot_geometry),
            "ponnot_geometry": ponnot_geometry,
            "readable": readable is True or str(readable).lower() in ("true", "t", "1"),
        })
    return entries


def quote_identifier(value):
    """Quote a PostgreSQL identifier without treating it as SQL text."""
    return '"' + str(value).replace('"', '""') + '"'


def ponnot_spatial_query(entry):
    """Build a lossless one-row-per-source-record spatial query.

    LEFT JOIN deliberately preserves orphaned source rows with null geometry.
    There is no DISTINCT or aggregation: several records may share one PONNOT.
    """
    schema = quote_identifier(entry["schema"])
    table = quote_identifier(entry["table"])
    geometry = quote_identifier(entry["ponnot_geometry"])
    return (
        f'SELECT t.*, p.{geometry} AS "__bdgd_geom" '
        f'FROM {schema}.{table} AS t '
        f'LEFT JOIN {schema}."ponnot" AS p ON p."cod_id" = t."pn_con"'
    )


def ponnot_density_query(entry):
    """Build an overview query weighted by records at each real PONNOT.

    Aggregation is deliberately restricted to the overview representation. The
    companion detail layer still exposes every source row and its attributes.
    """
    schema = quote_identifier(entry["schema"])
    table = quote_identifier(entry["table"])
    geometry = quote_identifier(entry["ponnot_geometry"])
    return (
        f'SELECT p."cod_id" AS "__bdgd_fid", '
        f'p.{geometry} AS "__bdgd_geom", c."__bdgd_count" '
        f'FROM (SELECT t."pn_con", COUNT(*)::bigint AS "__bdgd_count" '
        f'FROM {schema}.{table} AS t '
        f'WHERE t."pn_con" IS NOT NULL AND btrim(t."pn_con"::text) <> \'\' '
        f'GROUP BY t."pn_con") AS c '
        f'JOIN {schema}."ponnot" AS p ON p."cod_id" = c."pn_con"'
    )


def detail_visibility_scale(row_count):
    """Choose a conservative zoom-out limit from the source table volume."""
    rows = max(0, int(row_count or 0))
    if rows >= 1_000_000:
        return 10_000
    if rows >= 250_000:
        return 20_000
    if rows >= 100_000:
        return 30_000
    if rows >= 10_000:
        return 75_000
    return 200_000


def _configure_detail_layer(layer, entry):
    """Style exact records without changing or displacing their coordinates."""
    detail_scale = detail_visibility_scale(entry.get("rows", 0))
    symbol = QgsMarkerSymbol.createSimple({
        "name": "circle", "color": "37,99,235,105", "outline_style": "no",
        "size": "1.15",
    })
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.setScaleBasedVisibility(True)
    layer.setMinimumScale(detail_scale)
    layer.setMaximumScale(0)
    layer.setCustomProperty("bdgd/detail_visibility_scale", detail_scale)
    layer.setCustomProperty("bdgd/representation", "exact_records")


def _configure_density_layer(layer, entry):
    """Style exact PONNOT sites as a continuous weighted density surface."""
    detail_scale = detail_visibility_scale(entry.get("rows", 0))
    renderer = QgsHeatmapRenderer()
    renderer.setRadius(5.0)
    renderer.setRadiusUnit(Qgis.RenderUnit.Millimeters)
    renderer.setWeightExpression('"__bdgd_count"')
    renderer.setRenderQuality(2)
    renderer.setColorRamp(QgsGradientColorRamp(
        QColor(37, 99, 235, 0), QColor(220, 38, 38, 225)
    ))
    layer.setRenderer(renderer)
    layer.setScaleBasedVisibility(True)
    layer.setMinimumScale(0)
    layer.setMaximumScale(detail_scale)
    layer.setCustomProperty("bdgd/detail_visibility_scale", detail_scale)
    layer.setCustomProperty("bdgd/representation", "density_overview")


def postgres_layer(entry, connection_uri):
    """Open a physical table or the exact PONNOT-backed detail layer."""
    uri = QgsDataSourceUri(connection_uri)
    derived = not entry.get("geometry") and entry.get("references_ponnot", False)
    if derived:
        uri.setDataSource(
            "", f'({ponnot_spatial_query(entry)})', "__bdgd_geom", "", entry.get("key") or "objectid"
        )
    else:
        uri.setDataSource(
            entry["schema"], entry["table"], entry.get("geometry", ""), "", entry.get("key", "")
        )
    layer = QgsVectorLayer(uri.uri(False), display_name(entry["table"]), "postgres")
    if layer.isValid() and derived:
        layer.setCustomProperty("bdgd/georeferenced_via", "PN_CON → PONNOT.COD_ID")
        layer.setCustomProperty("bdgd/source_rows_preserved", True)
        _configure_detail_layer(layer, entry)
    return layer, derived


def postgres_density_layer(entry, connection_uri):
    """Open the weighted overview companion for a PONNOT-backed entity."""
    uri = QgsDataSourceUri(connection_uri)
    uri.setDataSource(
        "", f'({ponnot_density_query(entry)})', "__bdgd_geom", "", "__bdgd_fid"
    )
    layer = QgsVectorLayer(
        uri.uri(False), f'Visão geral · densidade — {display_name(entry["table"])}', "postgres"
    )
    if layer.isValid():
        layer.setCustomProperty("bdgd/georeferenced_via", "PN_CON → PONNOT.COD_ID")
        layer.setCustomProperty("bdgd/source_rows_preserved", False)
        _configure_density_layer(layer, entry)
    return layer


class CatalogThread(QThread):
    succeeded = pyqtSignal(object, str)
    failed = pyqtSignal(str)

    def __init__(self, uri, configuration, parent):
        super().__init__(parent)
        self.uri, self.configuration = uri, configuration
        self.feedback = QgsFeedback()

    def run(self):
        try:
            connection = QgsProviderRegistry.instance().providerMetadata("postgres").createConnection(
                self.uri, self.configuration
            )
            rows = connection.executeSql(CATALOG_SQL, self.feedback)
            if self.isInterruptionRequested():
                return
            imports, note = [], ""
            accessible = connection.executeSql(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='bdgd_admin' AND table_name='import_log')", self.feedback
            )
            if accessible and accessible[0][0]:
                try:
                    imports = connection.executeSql(
                        "SELECT schema_destino,tabela_destino,distribuidora,ano,status,row_count "
                        "FROM bdgd_admin.import_log", self.feedback
                    )
                except Exception:
                    note = "Histórico de importação indisponível; nomes de schemas e estimativas utilizados."
            else:
                note = "Sem histórico de importação; nomes de schemas e estimativas utilizados."
            if not self.isInterruptionRequested():
                self.succeeded.emit(catalog_entries(rows, imports), note)
        except Exception:
            if not self.isInterruptionRequested():
                # Connection exceptions may contain credentials; don't render raw exceptions.
                self.failed.emit(
                    "Não foi possível consultar a conexão. Verifique se o PostgreSQL está ligado "
                    "e confira a conexão e as permissões no Navegador do QGIS."
                )

    def cancel(self):
        self.requestInterruption()
        self.feedback.cancel()


class PostgresBrowser(QWidget):
    scopeChanged = pyqtSignal(object)

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.entries = []
        self.visible_entries = []
        self.worker = None
        self.pending_connection = None
        self.connection_uri = ""
        self.loaded_connection = ""
        self.settings = QSettings()
        self._last_scope = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        connection = Card()
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(field_label("Conexão"))
        self.connection_combo = QComboBox()
        self.connection_combo.setMinimumWidth(180)
        row.addWidget(self.connection_combo, 1)
        self.refresh_button = QPushButton("Atualizar conexões e bases")
        self.refresh_button.clicked.connect(self.refresh_connections)
        row.addWidget(self.refresh_button)
        self.cancel_button = QPushButton("Cancelar consulta")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_query)
        row.addWidget(self.cancel_button)
        connection.add(row)
        self.status = StatusLine("Consultando conexões salvas no QGIS…")
        connection.add(self.status)
        root.addWidget(connection)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(12)
        root.addWidget(splitter, 1)

        bases = Card("Bases", "Anos selecionados definem as camadas listadas e o período da análise.")
        row = QHBoxLayout()
        row.addWidget(field_label("Distribuidora"))
        self.distributor_combo = QComboBox()
        self.distributor_combo.currentTextChanged.connect(self.populate_years)
        row.addWidget(self.distributor_combo, 1)
        bases.add(row)
        self.years_table = self.make_table(["Ano", "Schema", "Tabelas", "Importação"])
        self.years_table.setMinimumHeight(150)
        self.years_table.itemSelectionChanged.connect(self.populate_tables)
        bases.add(self.years_table, 1)
        year_tools = QHBoxLayout()
        for text, callback in (("Todos os anos", self.years_table.selectAll),
                               ("Ano mais recente", self.select_latest_year)):
            button = QPushButton(text)
            button.setObjectName("ghostAction")
            button.clicked.connect(callback)
            year_tools.addWidget(button)
        year_tools.addStretch(1)
        bases.add(year_tools)
        self.scope_label = hint("Nenhuma base selecionada.")
        bases.add(self.scope_label)
        bases.setMinimumWidth(260)
        splitter.addWidget(bases)

        layers = Card("Camadas", "Escolha um perfil ou selecione manualmente; Ctrl/Shift seleciona várias.")
        self.layer_count_badge = layers.add_header_widget(Badge("", "info"))
        self.profile_combo = QComboBox()
        for code, profile in PROFILES.items():
            self.profile_combo.addItem(profile["title"], code)
        self.profile_combo.setVisible(False)
        self.profile_switch = SegmentedControl(
            [(profile["title"].replace(" — avançado", ""), profile["description"]) for profile in PROFILES.values()]
        )
        self.profile_switch.changed.connect(self.profile_combo.setCurrentIndex)
        self.profile_combo.currentIndexChanged.connect(self.profile_switch.setCurrentIndex)
        self.profile_combo.currentIndexChanged.connect(self.select_profile)
        layers.add(self.profile_switch)
        self.profile_note = hint(PROFILES["essential"]["description"])
        layers.add(self.profile_note)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Buscar por significado ou código — ex.: consumidor, transformador, UCBT")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.populate_tables)
        layers.add(self.search_edit)
        self.tables = self.make_table(["Ano", "Camada", "Código BDGD", "Geometria", "Registros¹", "Disponibilidade"])
        self.tables.setMinimumHeight(170)
        self.tables.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tables.horizontalHeader().setStretchLastSection(False)
        self.tables.itemSelectionChanged.connect(self.selection_changed)
        layers.add(self.tables, 1)
        self.entity_description = QLabel("Selecione uma camada para ver o que ela representa.")
        self.entity_description.setObjectName("detailPanel")
        self.entity_description.setWordWrap(True)
        layers.add(self.entity_description)
        layers.add(hint("¹ Contagens da importação ou estimativas do PostgreSQL; não são uma recontagem atual."))
        splitter.addWidget(layers)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([330, 820])

        actions = QFrame()
        actions.setObjectName("actionBar")
        bar = QHBoxLayout(actions)
        bar.setContentsMargins(12, 10, 12, 10)
        self.selection_label = QLabel("Nenhuma camada selecionada.")
        self.selection_label.setWordWrap(True)
        bar.addWidget(self.selection_label, 1)
        clear = QPushButton("Limpar seleção")
        clear.setObjectName("ghostAction")
        clear.clicked.connect(lambda: self.tables.clearSelection())
        bar.addWidget(clear)
        self.add_button = QPushButton("Adicionar ao mapa")
        self.add_button.setObjectName("primaryAction")
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self.add_selected)
        bar.addWidget(self.add_button)
        root.addWidget(actions)
        self.connection_combo.currentTextChanged.connect(self.load_catalog)
        QTimer.singleShot(0, self.refresh_connections)

    @staticmethod
    def make_table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(28)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    @staticmethod
    def _status_item(text, kind):
        item = QTableWidgetItem(text)
        item.setForeground(QColor(STATUS_COLORS[kind][0]))
        return item

    def refresh_connections(self):
        if self.worker and self.worker.isRunning():
            return
        current = self.connection_combo.currentText() or self.settings.value(PREFIX + "connection", "BDGD Local")
        self.connection_combo.blockSignals(True)
        self.connection_combo.clear()
        try:
            names = sorted(QgsProviderRegistry.instance().providerMetadata("postgres").connections().keys())
            self.connection_combo.addItems(names)
            if current in names:
                self.connection_combo.setCurrentText(current)
        except Exception:
            pass
        finally:
            self.connection_combo.blockSignals(False)
        self.load_catalog()

    def load_catalog(self, *_):
        if self.worker and self.worker.isRunning():
            return
        self.entries = []
        self.loaded_connection = ""
        self.distributor_combo.clear()
        self.populate_years()
        name = self.connection_combo.currentText()
        if not name:
            self.status.setText("O banco local está sendo preparado automaticamente. Você também pode usar Arquivos da ANEEL.")
            return
        try:
            connection = QgsProviderRegistry.instance().providerMetadata("postgres").findConnection(name)
            uri = QgsDataSourceUri(connection.uri())
            uri.setParam("connect_timeout", "8")
            self.connection_uri = uri.uri(False)
            self.worker = CatalogThread(self.connection_uri, connection.configuration(), self)
            self.worker.succeeded.connect(self.catalog_loaded)
            self.worker.failed.connect(self.status.setText)
            self.worker.finished.connect(self.query_finished)
            self.connection_combo.setEnabled(False)
            self.refresh_button.setEnabled(False)
            self.cancel_button.setEnabled(True)
            self.status.setText("Consultando catálogo do PostgreSQL…")
            self.worker.start()
        except Exception:
            self.status.setText("Conexão indisponível. Atualize as conexões ou configure-a no Navegador do QGIS.")
            self.query_finished()

    def cancel_query(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.status.setText("Cancelando consulta…")
            self.cancel_button.setEnabled(False)

    def select_connection(self, name):
        """Select the provisioned connection after any current query finishes."""
        if self.worker and self.worker.isRunning():
            self.pending_connection = name
            self.cancel_query()
            return
        self.settings.setValue(PREFIX + 'connection', name)
        self.connection_combo.blockSignals(True)
        self.connection_combo.clear()
        self.connection_combo.blockSignals(False)
        self.refresh_connections()

    def query_finished(self):
        cancelled = self.worker and self.worker.isInterruptionRequested()
        self.connection_combo.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if cancelled:
            self.status.setText("Consulta cancelada. Use Atualizar conexões e bases para tentar novamente.")
        if self.worker:
            self.worker.deleteLater()
            self.worker = None
        if self.pending_connection:
            name, self.pending_connection = self.pending_connection, None
            QTimer.singleShot(0, lambda: self.select_connection(name))

    def catalog_loaded(self, entries, note):
        self.entries = entries
        self.loaded_connection = self.connection_combo.currentText()
        self.settings.setValue(PREFIX + "connection", self.loaded_connection)
        distributors = sorted({e["distributor"] for e in entries})
        saved = self.settings.value(PREFIX + "distributor", "")
        self.distributor_combo.blockSignals(True)
        self.distributor_combo.clear()
        self.distributor_combo.addItems(distributors)
        if saved in distributors:
            self.distributor_combo.setCurrentText(saved)
        self.distributor_combo.blockSignals(False)
        self.populate_years()
        bases = len({(e["distributor"], e["year"], e["schema"]) for e in entries})
        tables = len({(e["schema"], e["table"]) for e in entries})
        if entries:
            self.status.setText(
                f"Conectado a {self.loaded_connection} · {bases} bases · {br_int(tables)} tabelas. {note}".strip(),
                "warning" if note else "success",
            )
        else:
            self.status.setText("Banco pronto. Ainda não há bases importadas. Use Arquivos da ANEEL para baixar e explorar as bases públicas.", "warning")

    def select_latest_year(self):
        if self.years_table.rowCount():
            self.years_table.clearSelection()
            self.years_table.selectRow(0)

    def populate_years(self, *_):
        distributor = self.distributor_combo.currentText()
        bases = sorted({(e["year"], e["schema"]) for e in self.entries if e["distributor"] == distributor}, reverse=True)
        try:
            saved = json.loads(self.settings.value(PREFIX + "years/" + distributor, "[]"))
        except (ValueError, TypeError):
            saved = []
        self.years_table.blockSignals(True)
        self.years_table.setRowCount(len(bases))
        for index, (year, schema) in enumerate(bases):
            entries = [e for e in self.entries if e["schema"] == schema and e["distributor"] == distributor]
            states = {e["status"] for e in entries}
            complete = states == {"success"}
            status = "● Concluída" if complete else "● Verificar: " + ", ".join(sorted(states))
            values = [str(year), schema, str(len({e["table"] for e in entries})), status]
            for col, value in enumerate(values):
                item = self._status_item(value, "success" if complete else "warning") if col == 3 else QTableWidgetItem(value)
                self.years_table.setItem(index, col, item)
                if year in saved or (not saved and index == 0):
                    item.setSelected(True)
        if bases and not self.years_table.selectedItems():
            self.years_table.selectRow(0)
        self.years_table.blockSignals(False)
        self.populate_tables()

    def selected_scope(self):
        indexes = sorted({i.row() for i in self.years_table.selectedIndexes()})
        return {
            "source": "postgres",
            "connection": self.loaded_connection,
            "distributor": self.distributor_combo.currentText(),
            "years": [int(self.years_table.item(i, 0).text()) for i in indexes],
            "schemas": [self.years_table.item(i, 1).text() for i in indexes],
        }

    def populate_tables(self):
        scope = self.selected_scope()
        query = self.search_edit.text().strip().casefold() if hasattr(self, 'search_edit') else ''
        self.visible_entries = [
            e for e in self.entries
            if e["schema"] in scope["schemas"] and e["distributor"] == scope["distributor"]
            and (not query or query in (
                display_name(e["table"]) + " " + entity_info(e["table"])["description"] + " " + e["table"]
            ).casefold())
        ]
        self.tables.setRowCount(len(self.visible_entries))
        for row, entry in enumerate(self.visible_entries):
            available, kind = ("● Disponível", "success") if entry["readable"] else ("● Sem permissão SELECT", "danger")
            if entry["status"] in ("running", "failed"):
                available, kind = (("● Importação em andamento", "running") if entry["status"] == "running"
                                   else ("● Importação com falha", "danger"))
            info = entity_info(entry["table"])
            if entry["geometry"]:
                geometry = "Espacial"
            elif entry.get("references_ponnot"):
                geometry = 'PONNOT · densidade ↔ registros'
            else:
                geometry = "Não espacial"
            values = [str(entry["year"]), info["name"], info["code"], geometry,
                      "—" if entry["rows"] < 0 else br_int(entry["rows"]), available]
            tip = info["description"] + f'\nTabela física: {entry["schema"]}.{entry["table"]}'
            if entry["geometry"]:
                tip += f'\nColuna de geometria: {entry["geometry"]}'
            elif entry.get("references_ponnot"):
                tip += (f'\nDensidade em escalas gerais; registros exatos a partir de '
                        f'1:{br_int(detail_visibility_scale(entry.get("rows")))}.')
            for col, value in enumerate(values):
                cell = self._status_item(value, kind) if col == 5 else QTableWidgetItem(value)
                if col == 4:
                    cell.setTextAlignment(int(Qt.AlignRight | Qt.AlignVCenter))
                    cell.setToolTip(f'Contagem {entry["count_source"]}.')
                else:
                    cell.setToolTip(tip)
                self.tables.setItem(row, col, cell)
        years = ", ".join(map(str, sorted(scope["years"])))
        self.scope_label.setText(
            f'{scope["distributor"]} · {len(scope["years"])} ano(s): {years}'
            if years else "Selecione um ou mais anos."
        )
        self.layer_count_badge.set_status(f'{br_int(len(self.visible_entries))} camadas', "info")
        if scope["distributor"] and scope["connection"]:
            self.settings.setValue(PREFIX + "distributor", scope["distributor"])
            self.settings.setValue(PREFIX + "years/" + scope["distributor"], json.dumps(scope["years"]))
        # Searching only filters the list; the analysis scope is unchanged.
        key = (scope["connection"], scope["distributor"], tuple(scope["years"]), tuple(scope["schemas"]))
        if key != self._last_scope:
            self._last_scope = key
            self.scopeChanged.emit(scope)
        self.select_profile()

    def select_profile(self, *_):
        if not hasattr(self, 'profile_combo'):
            return
        profile = self.profile_combo.currentData() or 'essential'
        self.profile_note.setText(PROFILES[profile]["description"])
        self.tables.clearSelection()
        for row, entry in enumerate(self.visible_entries):
            if matches_profile(entry["table"], profile):
                for column in range(self.tables.columnCount()):
                    item = self.tables.item(row, column)
                    if item:
                        item.setSelected(True)
        self.update_add_button()

    def selection_changed(self):
        rows = sorted({index.row() for index in self.tables.selectedIndexes()})
        if rows and rows[-1] < len(self.visible_entries):
            entry = self.visible_entries[rows[-1]]
            info = entity_info(entry["table"])
            voltage = f' • {info["voltage"]}' if info["voltage"] else ''
            representation = ""
            if entry.get("references_ponnot") and not entry["geometry"]:
                representation = (
                    "<br><span style='color:#0b62e0;'>Georreferenciada por PN_CON → PONNOT: densidade em escalas gerais, "
                    f"registros exatos a partir de 1:{br_int(detail_visibility_scale(entry.get('rows')))}.</span>"
                )
            self.entity_description.setText(
                f'<b>{display_name(entry["table"])}</b>{voltage}<br>{info["description"]}{representation}<br>'
                f'<small style="color:#64788f;">Fonte preservada: {entry["schema"]}.{entry["table"]}</small>'
            )
        else:
            self.entity_description.setText("Selecione uma camada para ver o que ela representa.")
        self.update_add_button()

    def update_add_button(self):
        indexes = {i.row() for i in self.tables.selectedIndexes()}
        blocked = [i for i in indexes if i >= len(self.visible_entries) or not self.visible_entries[i]["readable"]
                   or self.visible_entries[i]["status"] in ("running", "failed")]
        self.add_button.setEnabled(bool(indexes) and bool(self.loaded_connection) and not blocked)
        if hasattr(self, "selection_label"):
            if not indexes:
                self.selection_label.setText("Nenhuma camada selecionada.")
            else:
                years = sorted({self.visible_entries[i]["year"] for i in indexes if i < len(self.visible_entries)})
                text = (f"<b>{len(indexes)}</b> camada(s) selecionada(s) em {len(years)} ano(s). "
                        "Entram ocultas e organizadas por tema.")
                if blocked:
                    text += f" <span style='color:#b42318;'>{len(blocked)} indisponível(is) na seleção.</span>"
                self.selection_label.setText(text)

    def add_selected(self):
        added, georeferenced, existing, failed = 0, 0, 0, []
        project = QgsProject.instance()
        tree = project.layerTreeRoot()
        for index in sorted({i.row() for i in self.tables.selectedIndexes()}):
            entry = self.visible_entries[index]
            if not entry["readable"] or entry["status"] in ("running", "failed"):
                continue
            group_name = f'BDGD • {entry["distributor"]} • {entry["year"]}'
            group = tree.findGroup(group_name) or tree.addGroup(group_name)
            semantic_group_name = theme_title(entry["table"])
            semantic_group = group.findGroup(semantic_group_name) or group.addGroup(semantic_group_name)

            derived = entry.get("references_ponnot") and not entry["geometry"]
            if derived:
                detail_scale = detail_visibility_scale(entry.get("rows", 0))
                entity_group = semantic_group.findGroup(display_name(entry["table"]))
                if entity_group is None:
                    entity_group = semantic_group.addGroup(display_name(entry["table"]))
                    entity_group.setItemVisibilityChecked(False)
                representations = (
                    ("ponnot_density", postgres_density_layer, "Visão geral · densidade"),
                    ("ponnot_detail", lambda item, uri: postgres_layer(item, uri)[0],
                     "Detalhe · registros exatos"),
                )
                entity_added = False
                for spatial_mode, factory, layer_name in representations:
                    identity = json.dumps([
                        self.loaded_connection, entry["schema"], entry["table"], "", spatial_mode,
                    ])
                    if any(layer.customProperty("bdgd/postgres_identity") == identity
                           for layer in project.mapLayers().values()):
                        existing += 1
                        continue
                    layer = factory(entry, self.connection_uri)
                    if not layer.isValid():
                        failed.append(f'{entry["table"]} ({layer_name})')
                        continue
                    apply_layer_semantics(layer, entry["table"])
                    layer.setName(layer_name)
                    layer.setCustomProperty("bdgd/postgres_identity", identity)
                    layer.setCustomProperty("bdgd/schema", entry["schema"])
                    if spatial_mode == "ponnot_detail":
                        layer.setAbstract(
                            layer.abstract() + "\nGeometria associada por PN_CON → PONNOT.COD_ID. "
                            "Cada registro da tabela de origem permanece uma feição, inclusive em pontos compartilhados. "
                            f"Aparece automaticamente em escalas mais próximas que 1:{detail_scale:,}."
                        )
                    else:
                        layer.setAbstract(
                            layer.abstract() + "\nVisão geral de densidade ponderada pela quantidade de registros "
                            "em cada PONNOT real. Não substitui a camada de registros exatos. "
                            f"Aparece automaticamente em 1:{detail_scale:,} e escalas mais gerais."
                        )
                    project.addMapLayer(layer, False)
                    entity_group.addLayer(layer).setItemVisibilityChecked(True)
                    added += 1
                    entity_added = True
                georeferenced += int(entity_added)
                continue

            identity = json.dumps([
                self.loaded_connection, entry["schema"], entry["table"], entry["geometry"], "source",
            ])
            if any(layer.customProperty("bdgd/postgres_identity") == identity
                   for layer in project.mapLayers().values()):
                existing += 1
                continue
            layer, _ = postgres_layer(entry, self.connection_uri)
            if not layer.isValid():
                failed.append(entry["table"])
                continue
            apply_layer_semantics(layer, entry["table"])
            layer.setCustomProperty("bdgd/postgres_identity", identity)
            layer.setCustomProperty("bdgd/schema", entry["schema"])
            project.addMapLayer(layer, False)
            semantic_group.addLayer(layer).setItemVisibilityChecked(False)
            added += 1
        message = f"{added} representação(ões) adicionada(s) por tema; {existing} já estavam no projeto."
        if georeferenced:
            message += (
                f" {georeferenced} entidade(s) alternam automaticamente entre densidade e registros exatos."
            )
        if failed:
            message += " Não foi possível abrir: " + ", ".join(failed)
        self.status.setText(message)
