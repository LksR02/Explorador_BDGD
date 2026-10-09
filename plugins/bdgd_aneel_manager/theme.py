"""Scoped Qt theme for the Explorador BDGD workspace.

Tokens follow a calm, data-dense palette (navy navigation, white cards, a
single blue accent and semantic status colours).  Every rule is scoped to the
plugin's own widgets so QGIS itself is never restyled.
"""

INK = "#0f2438"
INK_SOFT = "#3b4f66"
MUTED = "#64788f"
LINE = "#dbe3ec"
LINE_STRONG = "#b9c7d6"
SURFACE = "#ffffff"
CANVAS = "#f2f5f9"
ACCENT = "#0b62e0"
ACCENT_DARK = "#084bb0"
ACCENT_SOFT = "#e6efff"
NAV_BG = "#0d2136"
NAV_HOVER = "#173450"
NAV_ACTIVE = "#1f4670"

STATUS_COLORS = {
    "neutral": ("#5b6f86", "#eef2f6"),
    "info": ("#0b62e0", "#e6efff"),
    "success": ("#13795b", "#e3f5ee"),
    "warning": ("#9a5b00", "#fff3dc"),
    "danger": ("#b42318", "#fde8e6"),
    "running": ("#6d3fd1", "#efe9fd"),
}

WORKSPACE_STYLE = f"""
QWidget {{font-family:'Segoe UI';}}
QDialog#BdgdWorkspace {{background:{CANVAS}; color:{INK}; font-family:'Segoe UI'; font-size:12px;}}
QLabel {{color:{INK_SOFT};}}

/* Navigation rail */
QFrame#navRail {{background:{NAV_BG}; border:0;}}
QLabel#navBrand {{color:white; font-size:16px; font-weight:700; padding:2px 0;}}
QLabel#navBrandSub {{color:#8fa6bf; font-size:11px;}}
QLabel#navFooter {{color:#7f97b2; font-size:10px;}}
QFrame#navItem {{background:transparent; border-radius:8px;}}
QFrame#navItem:hover {{background:{NAV_HOVER};}}
QFrame#navItem[active="true"] {{background:{NAV_ACTIVE};}}
QFrame#navItem[enabledState="false"] {{background:transparent;}}
QLabel#navTitle {{color:#e8eff7; font-size:13px; font-weight:600;}}
QLabel#navSubtitle {{color:#91a8c2; font-size:11px;}}
QFrame#navItem[active="true"] QLabel#navTitle {{color:white;}}
QFrame#navItem[active="true"] QLabel#navSubtitle {{color:#c4d6ea;}}
QToolButton#navCollapse {{background:transparent; color:#9fb4cb; border:0; padding:4px; font-size:14px;}}
QToolButton#navCollapse:hover {{color:white;}}

/* Header */
QFrame#topBar {{background:{SURFACE}; border:0; border-bottom:1px solid {LINE};}}
QLabel#pageEyebrow {{color:{MUTED}; font-size:11px; font-weight:600; letter-spacing:0.5px;}}
QLabel#pageTitle {{color:{INK}; font-size:20px; font-weight:700;}}
QLabel#pageDescription {{color:{MUTED};}}
QToolButton#contextChip {{background:{CANVAS}; color:{INK_SOFT}; border:1px solid {LINE}; border-radius:13px;
                          padding:4px 11px; font-size:11px;}}
QToolButton#contextChip:hover {{background:{ACCENT_SOFT}; border-color:{ACCENT}; color:{ACCENT_DARK};}}
QToolButton#iconButton {{background:transparent; border:1px solid transparent; border-radius:6px; padding:5px 8px; color:{INK_SOFT};}}
QToolButton#iconButton:hover {{background:{CANVAS}; border-color:{LINE};}}
QToolButton#iconButton:checked {{background:{ACCENT_SOFT}; border-color:{ACCENT}; color:{ACCENT_DARK};}}

/* Cards and sections */
QFrame#card {{background:{SURFACE}; border:1px solid {LINE}; border-radius:10px;}}
QFrame#card QLabel {{border:0;}}
QLabel#cardTitle {{color:{INK}; font-size:13px; font-weight:700;}}
QLabel#cardSubtitle {{color:{MUTED}; font-size:11px;}}
QLabel#hint {{color:{MUTED}; font-size:11px;}}
QLabel#fieldLabel {{color:{INK_SOFT}; font-weight:600;}}
QFrame#actionBar {{background:{SURFACE}; border:1px solid {LINE}; border-radius:10px;}}
QFrame#summaryPanel {{background:{SURFACE}; border:1px solid {LINE}; border-radius:10px;}}
QFrame#tile {{background:{CANVAS}; border:1px solid {LINE}; border-radius:8px;}}
QLabel#tileLabel {{color:{MUTED}; font-size:11px;}}
QLabel#tileValue {{color:{INK}; font-size:15px; font-weight:700;}}
QLabel#tileHint {{color:{MUTED}; font-size:10px;}}
QLabel#detailPanel {{background:{CANVAS}; border:1px solid {LINE}; border-radius:8px; padding:10px; color:{INK_SOFT};}}
QFrame#divider {{background:{LINE}; border:0; max-height:1px; min-height:1px;}}
QToolButton#collapseHeader {{background:transparent; border:0; color:{INK}; font-weight:700; font-size:13px; padding:2px 0;}}
QToolButton#collapseHeader:hover {{color:{ACCENT_DARK};}}

/* Empty states */
QLabel#emptyIcon {{background:{ACCENT_SOFT}; color:{ACCENT}; border-radius:32px; font-size:26px; font-weight:700;}}
QLabel#emptyTitle {{color:{INK}; font-size:17px; font-weight:700;}}
QLabel#emptyText {{color:{MUTED}; font-size:12px;}}

/* Segmented control */
QFrame#segmented {{background:{CANVAS}; border:1px solid {LINE}; border-radius:9px;}}
QPushButton#segment {{background:transparent; border:0; border-radius:7px; padding:7px 14px; color:{INK_SOFT}; font-weight:600;}}
QPushButton#segment:hover {{background:#e4ebf3;}}
QPushButton#segment:checked {{background:{SURFACE}; color:{ACCENT_DARK}; border:1px solid {LINE_STRONG};}}
QPushButton#segment:disabled {{color:#9aabbd;}}

/* Chip-style check boxes */
QCheckBox[chip="true"] {{background:{SURFACE}; border:1px solid {LINE_STRONG}; border-radius:14px; padding:5px 12px 5px 10px; color:{INK_SOFT};}}
QCheckBox[chip="true"]:hover {{border-color:{ACCENT};}}
QCheckBox[chip="true"]:checked {{background:{ACCENT_SOFT}; border-color:{ACCENT}; color:{ACCENT_DARK}; font-weight:600;}}
QCheckBox[chip="true"]:disabled {{color:#9aabbd; border-color:{LINE};}}
QCheckBox[chip="true"]::indicator {{width:12px; height:12px; border-radius:6px; border:1px solid {LINE_STRONG}; background:white;}}
QCheckBox[chip="true"]::indicator:checked {{background:{ACCENT}; border-color:{ACCENT};}}

/* Buttons */
QPushButton {{background:{SURFACE}; color:#1d3a58; border:1px solid {LINE_STRONG}; border-radius:6px; padding:7px 13px;}}
QPushButton:hover {{background:{ACCENT_SOFT}; border-color:{ACCENT};}}
QPushButton:pressed {{background:#d3e3ff;}}
QPushButton#primaryAction {{background:{ACCENT}; color:white; border:1px solid {ACCENT}; font-weight:700; padding:8px 16px;}}
QPushButton#primaryAction:hover {{background:{ACCENT_DARK}; border-color:{ACCENT_DARK};}}
QPushButton#dangerAction {{background:{SURFACE}; color:#b42318; border:1px solid #f0b4ae;}}
QPushButton#dangerAction:hover {{background:#fde8e6; border-color:#b42318;}}
QPushButton#ghostAction {{background:transparent; border:1px solid transparent; color:{ACCENT_DARK}; font-weight:600;}}
QPushButton#ghostAction:hover {{background:{ACCENT_SOFT};}}
QPushButton:disabled, QPushButton#primaryAction:disabled, QPushButton#dangerAction:disabled {{
    background:#eef1f5; color:#8a99aa; border-color:{LINE};}}
QToolButton {{color:#1d3a58;}}

/* Inputs */
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {{background:white; color:#203a55; border:1px solid {LINE_STRONG};
    border-radius:6px; padding:5px 8px; min-height:20px;}}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {{border:1px solid {ACCENT};}}
QLineEdit:disabled, QComboBox:disabled, QDoubleSpinBox:disabled {{background:#f1f4f7; color:#8a99aa;}}
QComboBox::drop-down {{border:0; width:22px;}}
QCheckBox {{color:{INK_SOFT}; spacing:7px;}}
QCheckBox:disabled {{color:#8a99aa;}}

/* Tables */
QTableView, QTableWidget {{background:white; alternate-background-color:#f6f9fc; color:#203a55; gridline-color:#edf1f6;
    selection-background-color:#d7e6ff; selection-color:{INK}; border:1px solid {LINE}; border-radius:8px;}}
QHeaderView::section {{background:#f3f6fa; color:{INK_SOFT}; padding:7px 8px; border:0;
    border-bottom:1px solid {LINE}; font-weight:600;}}
QTableCornerButton::section {{background:#f3f6fa; border:0;}}

/* Progress, logs and tooltips */
QProgressBar {{background:#e5ebf2; border:0; border-radius:5px; min-height:10px; max-height:10px; text-align:center; color:transparent;}}
QProgressBar::chunk {{background:{ACCENT}; border-radius:5px;}}
QProgressBar#bigProgress {{min-height:14px; max-height:14px; border-radius:7px;}}
QProgressBar#bigProgress::chunk {{border-radius:7px;}}
QPlainTextEdit {{background:#0f1d2c; color:#d5e2f0; border:0; border-radius:8px; padding:8px;
    font-family:'Cascadia Mono','Consolas',monospace; font-size:11px;}}
QToolTip {{background:{INK}; color:white; border:0; padding:6px;}}

/* Inner tabs become pill tabs */
QTabWidget::pane {{border:1px solid {LINE}; background:{SURFACE}; border-radius:10px; top:-1px;}}
QTabBar::tab {{background:transparent; color:{MUTED}; padding:8px 16px; margin-right:4px; border:0;
    border-bottom:2px solid transparent;}}
QTabBar::tab:selected {{color:{ACCENT_DARK}; border-bottom:2px solid {ACCENT};}}
QTabBar::tab:hover {{color:{INK};}}

/* Scrollbars */
QScrollArea {{background:transparent; border:0;}}
QScrollArea > QWidget > QWidget {{background:transparent;}}
QScrollBar:vertical {{background:transparent; width:10px; margin:2px;}}
QScrollBar::handle:vertical {{background:#c4d0dd; border-radius:4px; min-height:30px;}}
QScrollBar::handle:vertical:hover {{background:#9fb1c4;}}
QScrollBar:horizontal {{background:transparent; height:10px; margin:2px;}}
QScrollBar::handle:horizontal {{background:#c4d0dd; border-radius:4px; min-width:30px;}}
QScrollBar::add-line, QScrollBar::sub-line {{width:0; height:0;}}
QScrollBar::add-page, QScrollBar::sub-page {{background:transparent;}}
QSplitter::handle {{background:transparent;}}

/* Status bar */
QFrame#statusBar {{background:{SURFACE}; border-top:1px solid {LINE};}}
"""
