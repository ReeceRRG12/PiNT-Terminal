"""PiNT Desktop design tokens, adapted to Qt widgets."""

import sys
from PySide6.QtGui import QFont, QPalette, QColor

BG = "#101722"
PANEL = "#192635"
ACCENT = "#62D6C7"
FG = "#EDF3FA"
DIM = "#A7B6C8"

def apply_theme(app):
    app.setStyle("Fusion")
    app.setFont(QFont("Helvetica Neue" if sys.platform == "darwin" else "Segoe UI", 12))
    palette = QPalette()
    for role, colour in [(QPalette.Window, BG), (QPalette.WindowText, FG),
                         (QPalette.Base, "#131D2A"), (QPalette.AlternateBase, PANEL),
                         (QPalette.Text, FG), (QPalette.Button, PANEL),
                         (QPalette.ButtonText, FG), (QPalette.Highlight, "#2B4957"),
                         (QPalette.HighlightedText, FG), (QPalette.ToolTipBase, PANEL),
                         (QPalette.ToolTipText, FG)]:
        palette.setColor(role, QColor(colour))
    app.setPalette(palette)
    app.setStyleSheet("""
    QWidget { color: #EDF3FA; }
    QMainWindow, QDialog { background: #101722; }
    QLabel { background: transparent; }
    QLabel#eyebrow { color: #62D6C7; font-size: 11px; font-weight: 700; letter-spacing: 2px; }
    QLabel#title { font-size: 27px; font-weight: 700; }
    QLabel#muted { color: #A7B6C8; }
    QLabel#section { font-size: 16px; font-weight: 600; }
    QLabel#brand { font-size: 36px; font-weight: 800; }
    QFrame#sidebar { background: #131D2A; border-right: 1px solid #2A3B4E; }
    QFrame#panel { background: #192635; border: 1px solid #2A3B4E; border-radius: 10px; }
    QPushButton { background: #192635; border: 1px solid #2A3B4E; border-radius: 7px;
                  padding: 9px 14px; min-height: 18px; font-weight: 500; }
    QPushButton:hover { background: #233D46; border-color: #4B7A80; }
    QPushButton:pressed { background: #2B4957; }
    QPushButton:disabled { color: #687A8D; background: #151F2C; }
    QPushButton#primary { background: #62D6C7; color: #101722; border: 0; font-weight: 700; }
    QPushButton#primary:hover { background: #80E2D5; }
    QPushButton#danger { color: #FF9C9C; }
    QPushButton#nav { text-align: left; border: 0; background: transparent; padding: 12px 14px; }
    QPushButton#nav:checked { color: #62D6C7; background: #233D46; }
    QPushButton#nav:hover { background: #192F3D; }
    QLineEdit, QComboBox, QSpinBox { background: #131D2A; border: 1px solid #2A3B4E;
             border-radius: 6px; padding: 9px; selection-background-color: #2B4957; }
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #62D6C7; }
    QComboBox::drop-down { border: 0; width: 24px; }
    QPlainTextEdit { background: #0D141E; border: 1px solid #2A3B4E; border-radius: 6px; padding: 10px; }
    QTableWidget { background: #101722; alternate-background-color: #15202E; border: 0;
                   gridline-color: #2A3B4E; selection-background-color: #233D46; }
    QTableWidget::item { padding: 8px; border-bottom: 1px solid #233344; }
    QHeaderView::section { background: #131D2A; color: #A7B6C8; border: 0;
                           border-bottom: 1px solid #2A3B4E; padding: 12px 8px; font-size: 11px; }
    QTabWidget::pane { border: 1px solid #2A3B4E; border-radius: 4px; }
    QTabBar::tab { background: #192635; color: #A7B6C8; padding: 10px 20px; margin-right: 3px; }
    QTabBar::tab:selected { background: #233D46; color: #62D6C7; }
    QScrollBar:vertical { background: #101722; width: 10px; border: 0; }
    QScrollBar::handle:vertical { background: #3C5368; border-radius: 4px; min-height: 24px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    QSplitter::handle { background: #2A3B4E; width: 1px; }
    QStatusBar { background: #131D2A; color: #A7B6C8; border-top: 1px solid #2A3B4E; }
    QCheckBox { spacing: 10px; }
    QToolTip { color: #EDF3FA; background: #192635; border: 1px solid #2A3B4E; padding: 6px; }
    """)
