"""Small widget kit shared by the Explorador BDGD pages.

The widgets are deliberately thin wrappers around standard Qt classes so the
workflow code keeps using familiar APIs (``setText``, ``setChecked``...).
"""
from __future__ import annotations

from qgis.PyQt.QtCore import (
    QEasingCurve, QPoint, QPropertyAnimation, QRect, QSize, Qt, pyqtSignal,
)
from qgis.PyQt.QtWidgets import (
    QButtonGroup, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLayout,
    QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from .theme import STATUS_COLORS


def fade_in(widget: QWidget, duration: int = 170):
    """Fade a freshly shown page in; the effect is removed afterwards."""
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    animation = QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(duration)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setEasingCurve(QEasingCurve.OutCubic)
    animation.finished.connect(lambda: widget.setGraphicsEffect(None))
    animation.start(QPropertyAnimation.DeleteWhenStopped)
    return animation


def hint(text: str, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(wrap)
    return label


def field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("fieldLabel")
    return label


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("divider")
    line.setFrameShape(QFrame.NoFrame)
    return line


class Card(QFrame):
    """White rounded container with an optional title row."""

    def __init__(self, title: str = "", subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)
        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        self.title_label = self.subtitle_label = None
        if title or subtitle:
            texts = QVBoxLayout()
            texts.setSpacing(1)
            if title:
                self.title_label = QLabel(title)
                self.title_label.setObjectName("cardTitle")
                texts.addWidget(self.title_label)
            if subtitle:
                self.subtitle_label = QLabel(subtitle)
                self.subtitle_label.setObjectName("cardSubtitle")
                self.subtitle_label.setWordWrap(True)
                texts.addWidget(self.subtitle_label)
            self.header.addLayout(texts, 1)
            outer.addLayout(self.header)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        outer.addLayout(self.body, 1)

    def add_header_widget(self, widget: QWidget):
        if self.header.parent() is None:
            self.layout().insertLayout(0, self.header)
        self.header.addWidget(widget)
        return widget

    def add(self, item, stretch: int = 0):
        if isinstance(item, QLayout):
            self.body.addLayout(item, stretch)
        else:
            self.body.addWidget(item, stretch)
        return item


class Badge(QLabel):
    """Rounded status pill whose colours come from the theme tokens."""

    def __init__(self, text: str = "", kind: str = "neutral", parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.set_status(text, kind)

    def set_status(self, text: str, kind: str = "neutral"):
        foreground, background = STATUS_COLORS.get(kind, STATUS_COLORS["neutral"])
        self.setText(text)
        self.setStyleSheet(
            f"QLabel {{background:{background}; color:{foreground}; border-radius:10px;"
            f" padding:2px 9px; font-size:11px; font-weight:600;}}"
        )
        self.setVisible(bool(text))


def infer_level(text: str) -> str:
    lowered = (text or "").casefold()
    if any(word in lowered for word in ("falha", "erro", "não foi possível", "indisponível", "insuficiente")):
        return "danger"
    if any(word in lowered for word in ("pausad", "cancel", "interromp", "verificar", "sem permissão", "selecione")):
        return "warning"
    if any(word in lowered for word in ("concluí", "sucesso", "sincronizada", "adicionada", "conectado", "pronto")):
        return "success"
    if any(word in lowered for word in ("baixando", "extraindo", "consultando", "iniciando", "processando", "analisando")):
        return "running"
    return "info"


class StatusLine(QLabel):
    """QLabel that colours itself by the tone of the message it shows."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextFormat(Qt.PlainText)
        self.level = "info"
        self.setText(text)

    def setText(self, text, level: str | None = None):  # noqa: N802 - Qt API
        self.level = level or infer_level(text)
        foreground, background = STATUS_COLORS.get(self.level, STATUS_COLORS["info"])
        self.setStyleSheet(
            f"QLabel {{background:{background}; color:{foreground}; border-left:3px solid {foreground};"
            f" border-radius:4px; padding:5px 10px;}}"
        )
        super().setText(text)


class SegmentedControl(QFrame):
    """Mutually exclusive buttons that read better than a combo box."""

    changed = pyqtSignal(int)

    def __init__(self, options, parent=None):
        super().__init__(parent)
        self.setObjectName("segmented")
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = []
        for index, option in enumerate(options):
            label, tooltip = (option, "") if isinstance(option, str) else (option[0], option[1])
            button = QPushButton(label)
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            if tooltip:
                button.setToolTip(tooltip)
            self.group.addButton(button, index)
            layout.addWidget(button)
            self.buttons.append(button)
        if self.buttons:
            self.buttons[0].setChecked(True)
        self.group.buttonClicked.connect(lambda button: self.changed.emit(self.buttons.index(button)))

    def currentIndex(self) -> int:  # noqa: N802 - Qt naming
        return self.group.checkedId()

    def setCurrentIndex(self, index: int):  # noqa: N802 - Qt naming
        if 0 <= index < len(self.buttons):
            self.buttons[index].setChecked(True)


class CollapsibleSection(QWidget):
    """Header button that shows or hides a body widget."""

    toggled = pyqtSignal(bool)

    def __init__(self, title: str, body: QWidget, expanded: bool = False, parent=None):
        super().__init__(parent)
        self.title = title
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.button = QToolButton()
        self.button.setObjectName("collapseHeader")
        self.button.setCheckable(True)
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.button.toggled.connect(self.set_expanded)
        layout.addWidget(self.button)
        self.body = body
        layout.addWidget(body)
        self.button.setChecked(expanded)
        self.set_expanded(expanded)

    def set_expanded(self, expanded: bool):
        self.button.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.button.setText(self.title)
        self.body.setVisible(expanded)
        if self.button.isChecked() != expanded:
            self.button.setChecked(expanded)
        self.toggled.emit(expanded)


class EmptyState(QWidget):
    """Centered illustration, message and next-step buttons."""

    def __init__(self, symbol: str, title: str, text: str, actions=(), parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        icon = QLabel(symbol)
        icon.setObjectName("emptyIcon")
        icon.setFixedSize(64, 64)
        icon.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon, 0, Qt.AlignHCenter)
        layout.addSpacing(8)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("emptyTitle")
        self.title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.title_label)
        self.text_label = QLabel(text)
        self.text_label.setObjectName("emptyText")
        self.text_label.setAlignment(Qt.AlignCenter)
        self.text_label.setWordWrap(True)
        self.text_label.setMinimumWidth(380)
        self.text_label.setMaximumWidth(540)
        layout.addWidget(self.text_label, 0, Qt.AlignHCenter)
        layout.addSpacing(10)
        row = QHBoxLayout()
        row.addStretch(1)
        self.buttons = []
        for label, callback, primary in actions:
            button = QPushButton(label)
            if primary:
                button.setObjectName("primaryAction")
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(callback)
            row.addWidget(button)
            self.buttons.append(button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(2)

    def set_message(self, title: str, text: str):
        self.title_label.setText(title)
        self.text_label.setText(text)


def stat_tile(label: str, value: QLabel, tip: str = "") -> QFrame:
    tile = QFrame()
    tile.setObjectName("tile")
    if tip:
        tile.setToolTip(tip)
    layout = QVBoxLayout(tile)
    layout.setContentsMargins(12, 9, 12, 10)
    layout.setSpacing(2)
    heading = QLabel(label)
    heading.setObjectName("tileLabel")
    value.setObjectName("tileValue")
    value.setWordWrap(True)
    layout.addWidget(heading)
    layout.addWidget(value)
    return tile


class FlowLayout(QLayout):
    """Wrap child widgets onto new lines, like chips in a web form."""

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):  # noqa: N802 - Qt API
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):  # noqa: N802 - Qt API
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):  # noqa: N802 - Qt API
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802 - Qt API
        return Qt.Orientations(0)

    def hasHeightForWidth(self):  # noqa: N802 - Qt API
        return True

    def heightForWidth(self, width):  # noqa: N802 - Qt API
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):  # noqa: N802 - Qt API
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self):  # noqa: N802 - Qt API
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802 - Qt API
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _arrange(self, rect, apply):
        x, y, line_height = rect.x(), rect.y(), 0
        for item in self._items:
            if item.widget() is not None and not item.widget().isVisible() and item.widget().parent() is not None \
                    and item.widget().parent().isVisible():
                continue
            hint = item.sizeHint()
            if x + hint.width() > rect.right() + 1 and line_height > 0:
                x = rect.x()
                y += line_height + self._spacing
                line_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


NAV_STATE_STYLE = {
    "pending": ("transparent", "#9fb4cb", "#4c6782"),
    "active": ("white", "#0b62e0", "white"),
    "ready": ("#2c5a88", "white", "#2c5a88"),
    "done": ("#1f9d74", "white", "#1f9d74"),
    "running": ("#7c4ee4", "white", "#7c4ee4"),
    "attention": ("#d48806", "white", "#d48806"),
}


class NavItem(QFrame):
    clicked = pyqtSignal(int)

    def __init__(self, index: int, title: str, shortcut: str = "", parent=None):
        super().__init__(parent)
        self.index, self.title, self.state = index, title, "pending"
        self.setObjectName("navItem")
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("active", False)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(10)
        self.number = QLabel(str(index + 1))
        self.number.setFixedSize(26, 26)
        self.number.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.number, 0, Qt.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(1)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("navTitle")
        self.subtitle_label = QLabel("")
        self.subtitle_label.setObjectName("navSubtitle")
        self.subtitle_label.setWordWrap(True)
        texts.addWidget(self.title_label)
        texts.addWidget(self.subtitle_label)
        self.texts = QWidget()
        self.texts.setLayout(texts)
        self.texts.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(self.texts, 1)
        self.shortcut = shortcut
        self._paint_number()

    def mousePressEvent(self, event):  # noqa: N802 - Qt API
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.index)
        super().mousePressEvent(event)

    def set_active(self, active: bool):
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)
        for label in (self.title_label, self.subtitle_label):
            label.style().unpolish(label)
            label.style().polish(label)
        self._paint_number()

    def set_status(self, subtitle: str, state: str = "pending"):
        self.state = state
        self.subtitle_label.setText(subtitle)
        tip = f"{self.title}\n{subtitle}" + (f"\nAtalho: {self.shortcut}" if self.shortcut else "")
        self.setToolTip(tip)
        self._paint_number()

    def set_compact(self, compact: bool):
        self.texts.setVisible(not compact)

    def _paint_number(self):
        active = bool(self.property("active"))
        state = "active" if active and self.state in ("pending", "ready") else self.state
        background, foreground, border = NAV_STATE_STYLE.get(state, NAV_STATE_STYLE["pending"])
        symbol = {"done": "✓", "attention": "!"}.get(self.state, str(self.index + 1))
        if self.state == "running":
            symbol = "▶"
        self.number.setText(symbol)
        ring = "2px solid #8fb6ff" if active else f"1px solid {border}"
        self.number.setStyleSheet(
            f"QLabel {{background:{background}; color:{foreground}; border:{ring}; border-radius:13px;"
            f" font-weight:700; font-size:12px;}}"
        )


class NavRail(QFrame):
    """Vertical step navigation with live status for every page."""

    currentChanged = pyqtSignal(int)

    def __init__(self, brand: str, brand_subtitle: str, footer: str, parent=None):
        super().__init__(parent)
        self.setObjectName("navRail")
        self.items: list[NavItem] = []
        self.compact = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 16, 12, 12)
        layout.setSpacing(4)
        head = QHBoxLayout()
        brand_box = QVBoxLayout()
        brand_box.setSpacing(0)
        self.brand = QLabel(brand)
        self.brand.setObjectName("navBrand")
        self.brand_subtitle = QLabel(brand_subtitle)
        self.brand_subtitle.setObjectName("navBrandSub")
        self.brand_subtitle.setWordWrap(True)
        brand_box.addWidget(self.brand)
        brand_box.addWidget(self.brand_subtitle)
        self.brand_widget = QWidget()
        self.brand_widget.setLayout(brand_box)
        head.addWidget(self.brand_widget, 1)
        self.collapse_button = QToolButton()
        self.collapse_button.setObjectName("navCollapse")
        self.collapse_button.setText("«")
        self.collapse_button.setToolTip("Recolher ou expandir a navegação")
        self.collapse_button.setCursor(Qt.PointingHandCursor)
        self.collapse_button.clicked.connect(lambda: self.set_compact(not self.compact, user=True))
        head.addWidget(self.collapse_button, 0, Qt.AlignTop)
        layout.addLayout(head)
        layout.addSpacing(14)
        self.items_layout = QVBoxLayout()
        self.items_layout.setSpacing(4)
        layout.addLayout(self.items_layout)
        layout.addStretch(1)
        self.footer = QLabel(footer)
        self.footer.setObjectName("navFooter")
        self.footer.setWordWrap(True)
        layout.addWidget(self.footer)
        self.user_compact = None
        self.set_compact(False)

    def add_item(self, title: str, shortcut: str = "") -> NavItem:
        item = NavItem(len(self.items), title, shortcut)
        item.clicked.connect(self.currentChanged.emit)
        self.items_layout.addWidget(item)
        self.items.append(item)
        return item

    def set_current(self, index: int):
        for item in self.items:
            item.set_active(item.index == index)

    def set_compact(self, compact: bool, user: bool = False):
        if user:
            self.user_compact = compact
        self.compact = compact
        self.brand_widget.setVisible(not compact)
        self.footer.setVisible(not compact)
        self.collapse_button.setText("»" if compact else "«")
        for item in self.items:
            item.set_compact(compact)
        self.setFixedWidth(68 if compact else 236)
