"""Native terminal rendering with a fixed active screen and disk-backed history."""

from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
from collections import defaultdict
import math
import sys
from typing import Callable

import pyte
from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QKeyEvent, QKeySequence, QPainter
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QMenu


BACKGROUND = "#101722"
FOREGROUND = "#EDF3FA"
ACCENT = "#62D6C7"
ANSI_COLOURS = {
    "black": "#17212F", "red": "#F07886", "green": "#78D6A5",
    "brown": "#E9C779", "yellow": "#E9C779", "blue": "#76AEED",
    "magenta": "#C49AE8", "cyan": "#62D6C7", "white": "#D8E3F0",
    "brightblack": "#718096", "brightred": "#FF9FAA",
    "brightgreen": "#A0EAC0", "brightbrown": "#F6D992",
    "brightyellow": "#F6D992", "brightblue": "#9BC8FF",
    "brightmagenta": "#DCBBFF", "brightcyan": "#8BEFE1",
    "brightwhite": "#FFFFFF",
}


class DiskScreen(pyte.Screen):
    """Persist rows as they leave the normal screen, never an alternate screen."""

    _STATE = (
        "buffer", "cursor", "margins", "savepoints", "tabstops", "mode",
        "charset", "g0_charset", "g1_charset", "columns", "lines", "saved_columns",
    )

    def __init__(self, columns: int, lines: int, append: Callable, reply: Callable):
        self._append = append
        self._reply = reply
        self.alternate = False
        self._normal_state = None
        super().__init__(columns, lines)

    def cells(self, row: int) -> list[dict]:
        return [self.buffer[row][column]._asdict() for column in range(self.columns)]

    def index(self):
        top, bottom = self.margins or (0, self.lines - 1)
        if not self.alternate and top == 0 and bottom == self.lines - 1 and self.cursor.y == bottom:
            self._append(self.cells(top))
        super().index()

    def resize(self, lines=None, columns=None):
        new_lines, new_columns = lines or self.lines, columns or self.columns
        if (new_lines, new_columns) == (self.lines, self.columns):
            return
        # Consume unused rows below the cursor before moving visible content into
        # history. If rows must leave the top, move the cursor with their content.
        last_used = self.cursor.y
        for row, cells in self.buffer.items():
            if any(cell != self.default_char for cell in cells.values()):
                last_used = max(last_used, row)
        reduction = max(0, self.lines - new_lines)
        bottom_drop = min(reduction, max(0, self.lines - 1 - last_used))
        top_drop = reduction - bottom_drop
        if not self.alternate:
            for row in range(top_drop):
                self._append(self.cells(row))
        self.buffer = defaultdict(self.buffer.default_factory, {
            row - top_drop: cells for row, cells in self.buffer.items()
            if top_drop <= row < self.lines - bottom_drop
        })
        if new_columns < self.columns:
            for cells in self.buffer.values():
                for column in list(cells):
                    if column >= new_columns:
                        cells.pop(column)
        self.cursor.y = max(0, min(self.cursor.y - top_drop, new_lines - 1))
        if new_columns != self.columns:
            self.cursor.x = min(self.cursor.x, new_columns - 1)
        self.lines, self.columns = new_lines, new_columns
        self.set_margins()
        self.dirty.update(range(self.lines))

    def save_cursor(self):
        # DECSC has one saved cursor. A remote peer cannot grow a cursor stack.
        self.savepoints.clear()
        super().save_cursor()

    def restore_cursor(self):
        saved = deepcopy(self.savepoints[-1]) if self.savepoints else None
        super().restore_cursor()
        if saved is not None:
            self.savepoints[:] = [saved]

    def erase_in_display(self, how=0, *args, **kwargs):
        # Keep completed normal-screen content available after a shell's clear.
        if how == 2 and not self.alternate:
            last = -1
            for row in range(self.lines):
                if any(cell.data.strip() for cell in self.buffer[row].values()):
                    last = row
            for row in range(last + 1):
                self._append(self.cells(row))
        super().erase_in_display(how, *args, **kwargs)

    def _enter_alternate(self):
        if self.alternate:
            return
        self._normal_state = {key: deepcopy(getattr(self, key)) for key in self._STATE}
        self.alternate = True
        super().reset()

    def _leave_alternate(self):
        if not self.alternate or self._normal_state is None:
            return
        columns, lines = self.columns, self.lines
        for key, value in self._normal_state.items():
            setattr(self, key, value)
        self._normal_state = None
        self.alternate = False
        # These are original normal-screen rows, so preserve any removed rows.
        self.resize(columns=columns, lines=lines)
        self.dirty.update(range(self.lines))

    def set_mode(self, *modes, **kwargs):
        if kwargs.get("private"):
            for mode in modes:
                if mode in (47, 1047, 1049):
                    self._enter_alternate()
                elif mode == 1048:
                    self.save_cursor()
            modes = tuple(mode for mode in modes if mode not in (47, 1047, 1048, 1049))
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        if kwargs.get("private"):
            for mode in modes:
                if mode in (47, 1047, 1049):
                    self._leave_alternate()
                elif mode == 1048:
                    self.restore_cursor()
            modes = tuple(mode for mode in modes if mode not in (47, 1047, 1048, 1049))
        super().reset_mode(*modes, **kwargs)

    def write_process_input(self, data):
        self._reply(data)

    def scroll_up(self, count=1):
        top, bottom = self.margins or (0, self.lines - 1)
        cursor_y = self.cursor.y
        self.cursor.y = bottom
        for _ in range(min(count or 1, bottom - top + 1)):
            self.index()
        self.cursor.y = cursor_y

    def scroll_down(self, count=1):
        top, bottom = self.margins or (0, self.lines - 1)
        cursor_y = self.cursor.y
        self.cursor.y = top
        for _ in range(min(count or 1, bottom - top + 1)):
            self.reverse_index()
        self.cursor.y = cursor_y

    @property
    def application_cursor(self):
        return (1 << 5) in self.mode

    @property
    def bracketed_paste(self):
        return (2004 << 5) in self.mode


class TerminalStream(pyte.Stream):
    csi = dict(pyte.Stream.csi, S="scroll_up", T="scroll_down")
    events = pyte.Stream.events | {"scroll_up", "scroll_down"}

    def __init__(self, *args, **kwargs):
        self._control_state = "text"
        self._control_buffer = ""
        self._control_discard = False
        super().__init__(*args, **kwargs)

    def feed(self, data):
        """Bound incomplete remote control strings across arbitrary read chunks.

        Oversized OSC payloads are discarded through their terminator. Raw output
        is still retained by the recording layer. CSI parameters are bounded
        before pyte converts them to integers or stores its parameter list.
        """
        if self._control_state == "text" and not any(c in data for c in ("\x1b", "\x9b", "\x9d")):
            return super().feed(data)
        output = []
        for char in data:
            state = self._control_state
            if state == "text":
                if char in ("\x1b", "\x9b", "\x9d"):
                    self._control_state = {"\x1b": "escape", "\x9b": "csi", "\x9d": "osc"}[char]
                    self._control_buffer = char
                    self._control_discard = False
                else:
                    output.append(char)
                continue
            if state == "escape":
                self._control_buffer += char
                if char in ("]", "["):
                    self._control_state = "osc" if char == "]" else "csi"
                else:
                    output.append(self._control_buffer)
                    self._control_state, self._control_buffer = "text", ""
                continue
            if not self._control_discard:
                self._control_buffer += char
                limit = 8192 if state in ("osc", "osc_escape") else 1024
                if len(self._control_buffer) > limit:
                    self._control_buffer = ""
                    self._control_discard = True
            complete = False
            if state in ("osc", "osc_escape"):
                complete = char in ("\x07", "\x9c") or (state == "osc_escape" and char == "\\")
                self._control_state = "osc_escape" if char == "\x1b" else "osc"
            else:
                complete = "@" <= char <= "~"
                if char == "\x1b":
                    self._control_state, self._control_buffer = "escape", char
                    self._control_discard = False
                    continue
                if char in ("\x18", "\x1a") or ord(char) > 126:
                    complete, self._control_discard = True, True
            if complete:
                if not self._control_discard:
                    output.append(self._control_buffer)
                self._control_state, self._control_buffer = "text", ""
                self._control_discard = False
        if output:
            super().feed("".join(output))


def _windows_altgr_text(event: QKeyEvent) -> str:
    """Keep text translated by an AltGr layout instead of treating it as Ctrl."""
    if sys.platform != "win32":
        return ""
    modifiers, text = event.modifiers(), event.text()
    altgr = bool(modifiers & Qt.KeyboardModifier.GroupSwitchModifier) or bool(
        modifiers & Qt.KeyboardModifier.ControlModifier
        and modifiers & Qt.KeyboardModifier.AltModifier
    )
    return text if altgr and text.isprintable() else ""


def key_sequence(event: QKeyEvent, application_cursor=False) -> str:
    """Translate Qt keys into terminal input, including physical Ctrl on macOS."""
    key, modifiers = event.key(), event.modifiers()
    control = Qt.KeyboardModifier.MetaModifier if sys.platform == "darwin" else Qt.KeyboardModifier.ControlModifier
    alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
    shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
    ctrl = bool(modifiers & control)
    modifier_number = 1 + int(shift) + 2 * int(alt) + 4 * int(ctrl)
    arrows = {Qt.Key.Key_Up: "A", Qt.Key.Key_Down: "B", Qt.Key.Key_Right: "C", Qt.Key.Key_Left: "D", Qt.Key.Key_Home: "H", Qt.Key.Key_End: "F"}
    if key in arrows:
        if modifier_number > 1:
            return f"\x1b[1;{modifier_number}{arrows[key]}"
        return "\x1b" + ("O" if application_cursor else "[") + arrows[key]
    tilde = {Qt.Key.Key_Insert: 2, Qt.Key.Key_Delete: 3, Qt.Key.Key_PageUp: 5, Qt.Key.Key_PageDown: 6, Qt.Key.Key_F5: 15, Qt.Key.Key_F6: 17, Qt.Key.Key_F7: 18, Qt.Key.Key_F8: 19, Qt.Key.Key_F9: 20, Qt.Key.Key_F10: 21, Qt.Key.Key_F11: 23, Qt.Key.Key_F12: 24}
    if key in tilde:
        suffix = f";{modifier_number}" if modifier_number > 1 else ""
        return f"\x1b[{tilde[key]}{suffix}~"
    if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F4:
        suffix = chr(ord("P") + key - Qt.Key.Key_F1)
        return f"\x1b[1;{modifier_number}{suffix}" if modifier_number > 1 else "\x1bO" + suffix
    if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        return "\r"
    if key == Qt.Key.Key_Backspace:
        return "\x1b\x7f" if alt else "\x7f"
    if key == Qt.Key.Key_Escape:
        return "\x1b"
    if key == Qt.Key.Key_Backtab or (key == Qt.Key.Key_Tab and shift):
        return "\x1b[Z"
    if key == Qt.Key.Key_Tab:
        return "\t"
    altgr_text = _windows_altgr_text(event)
    if altgr_text:
        return altgr_text
    if ctrl:
        if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            return chr(key - Qt.Key.Key_A + 1)
        control_keys = {Qt.Key.Key_Space: "\0", Qt.Key.Key_At: "\0", Qt.Key.Key_BracketLeft: "\x1b", Qt.Key.Key_Backslash: "\x1c", Qt.Key.Key_BracketRight: "\x1d", Qt.Key.Key_AsciiCircum: "\x1e", Qt.Key.Key_Underscore: "\x1f"}
        return control_keys.get(key, "")
    text = event.text()
    return ("\x1b" if alt else "") + text if text else ""


class TerminalWidget(QAbstractScrollArea):
    input_ready = Signal(str)
    resized = Signal(int, int)
    failed = Signal(str)

    def __init__(self, store, session_id: str, parent=None):
        super().__init__(parent)
        self.store, self.session_id = store, session_id
        self._history_count = store.line_count(session_id)
        self._connected = False
        self._selection_start = self._selection_end = None
        self._top_line = 0
        self._logical_maximum = 0
        self._last_error = None
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(12)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)
        metrics = QFontMetrics(font)
        self._cell_width = max(1, metrics.horizontalAdvance("M"))
        self._cell_height = max(1, metrics.height() + 2)
        self._ascent = metrics.ascent() + 1
        self._padding = 8
        self.screen = DiskScreen(80, 24, self._append_line, self.input_ready.emit)
        self.stream = TerminalStream(self.screen)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
        self.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        self.verticalScrollBar().valueChanged.connect(self._scroll_changed)
        self.setStyleSheet(f"QAbstractScrollArea {{ background: {BACKGROUND}; border: 0; }} QScrollBar:vertical {{ background: {BACKGROUND}; width: 10px; }} QScrollBar::handle:vertical {{ background: #344457; border-radius: 4px; min-height: 24px; }} QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}")
        self._update_range(True)

    def sizeHint(self):
        return QSize(80 * self._cell_width + self._padding * 2 + 10, 24 * self._cell_height + self._padding * 2)

    def _append_line(self, cells):
        blank = pyte.screens.Char(" ")._asdict()
        while cells and cells[-1] == blank:
            cells.pop()
        self.store.append_line(self.session_id, cells)
        self._history_count += 1

    def _error(self, error):
        message = f"Terminal history could not be read or saved: {error}"
        if message != self._last_error:
            self._last_error = message
            self.failed.emit(message)

    def feed(self, text: str):
        follow = self._top_line >= self._logical_maximum
        old_alternate = self.screen.alternate
        try:
            with getattr(self.store, "transaction", nullcontext)():
                self.stream.feed(text)
        except Exception as error:
            try:
                self._history_count = self.store.line_count(self.session_id)
            except Exception:
                pass
            self._error(error)
        if old_alternate != self.screen.alternate:
            self._selection_start = self._selection_end = None
            follow = True
        self._update_range(follow)
        self.viewport().update()

    def set_connected(self, connected: bool):
        self._connected = connected
        self.viewport().update()

    @property
    def history_count(self):
        return self._history_count

    def _visible_rows(self):
        return max(1, (self.viewport().height() - self._padding * 2) // self._cell_height)

    def _total_rows(self):
        return self.screen.lines + (0 if self.screen.alternate else self._history_count)

    def _update_range(self, follow=False):
        self._logical_maximum = max(0, self._total_rows() - self._visible_rows())
        bar = self.verticalScrollBar()
        bar.blockSignals(True)
        bar.setRange(0, min(self._logical_maximum, 2_147_483_647))
        bar.setPageStep(self._visible_rows())
        bar.blockSignals(False)
        self.scroll_to_line(self._logical_maximum if follow else self._top_line)

    def scroll_to_line(self, row):
        """Python row indices have no history cap, including beyond Qt's int range."""
        self._top_line = max(0, min(int(row), self._logical_maximum))
        bar = self.verticalScrollBar()
        position = round(self._top_line * bar.maximum() / self._logical_maximum) if self._logical_maximum else 0
        bar.blockSignals(True)
        bar.setValue(position)
        bar.blockSignals(False)
        self.viewport().update()

    def _scroll_changed(self, value):
        maximum = self.verticalScrollBar().maximum()
        self._top_line = round(value * self._logical_maximum / maximum) if maximum else 0
        self.viewport().update()

    def scrollContentsBy(self, dx, dy):
        self.viewport().update()

    def _rows(self, start, count):
        history = 0 if self.screen.alternate else self._history_count
        result = []
        if start < history:
            result.extend(self.store.read_lines(self.session_id, start, min(count, history - start)))
        live_start = max(0, start - history)
        live_count = count - len(result)
        for row in range(live_start, min(self.screen.lines, live_start + live_count)):
            result.append(self.screen.cells(row))
        return result

    @staticmethod
    def _colour(value, default):
        if value in (None, "default"):
            return QColor(default)
        value = ANSI_COLOURS.get(value, value)
        if len(value) == 6 and not value.startswith("#"):
            value = "#" + value
        colour = QColor(value)
        return colour if colour.isValid() else QColor(default)

    def _selection(self):
        if self._selection_start is None or self._selection_end is None:
            return None
        return tuple(sorted((self._selection_start, self._selection_end)))

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), QColor(BACKGROUND))
        try:
            rows = self._rows(self._top_line, self._visible_rows() + 1)
        except Exception as error:
            painter.end()
            self._error(error)
            return
        selection = self._selection()
        base_font = self.font()
        for row_offset, cells in enumerate(rows):
            y = self._padding + row_offset * self._cell_height
            logical_row = self._top_line + row_offset
            for column, cell in enumerate(cells):
                data = cell.get("data", " ")
                if data == "":
                    # pyte reserves the second cell of a full-width character.
                    continue
                x = self._padding + column * self._cell_width
                if x >= self.viewport().width():
                    break
                fg = self._colour(cell.get("fg"), FOREGROUND)
                bg = self._colour(cell.get("bg"), BACKGROUND)
                if cell.get("reverse"):
                    fg, bg = bg, fg
                if selection and selection[0] <= (logical_row, column) < selection[1]:
                    bg, fg = QColor("#315F66"), QColor(FOREGROUND)
                width = 2 if column + 1 < len(cells) and cells[column + 1].get("data") == "" else 1
                rect = QRect(x, y, self._cell_width * width, self._cell_height)
                painter.fillRect(rect, bg)
                font = QFont(base_font)
                font.setBold(bool(cell.get("bold")))
                font.setItalic(bool(cell.get("italics")))
                font.setUnderline(bool(cell.get("underscore")))
                font.setStrikeOut(bool(cell.get("strikethrough")))
                painter.setFont(font)
                painter.setPen(fg)
                painter.drawText(x, y + self._ascent, data)
        cursor = self.screen.cursor
        cursor_row = cursor.y + (0 if self.screen.alternate else self._history_count) - self._top_line
        if self._connected and not cursor.hidden and 0 <= cursor_row < self._visible_rows():
            x = self._padding + min(cursor.x, self.screen.columns - 1) * self._cell_width
            y = self._padding + cursor_row * self._cell_height
            painter.setPen(QColor(ACCENT))
            painter.drawRect(x, y, self._cell_width - 1, self._cell_height - 1)
        painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, "screen"):
            return
        columns = max(2, (self.viewport().width() - self._padding * 2) // self._cell_width)
        lines = self._visible_rows()
        if (columns, lines) != (self.screen.columns, self.screen.lines):
            follow = self._top_line >= self._logical_maximum
            try:
                with getattr(self.store, "transaction", nullcontext)():
                    self.screen.resize(columns=columns, lines=lines)
            except Exception as error:
                self._error(error)
            self._selection_start = self._selection_end = None
            self._update_range(follow)
            self.resized.emit(columns, lines)

    def focusNextPrevChild(self, next):
        return False

    def keyPressEvent(self, event):
        modifiers, key = event.modifiers(), event.key()
        ctrl_shift = bool(modifiers & Qt.KeyboardModifier.ControlModifier and modifiers & Qt.KeyboardModifier.ShiftModifier) and not _windows_altgr_text(event)
        if (sys.platform == "darwin" and event.matches(QKeySequence.StandardKey.Copy)) or (ctrl_shift and key == Qt.Key.Key_C):
            self.copy_selection()
        elif (sys.platform == "darwin" and event.matches(QKeySequence.StandardKey.Paste)) or (ctrl_shift and key == Qt.Key.Key_V) or (key == Qt.Key.Key_Insert and modifiers & Qt.KeyboardModifier.ShiftModifier):
            self.paste()
        elif modifiers == Qt.KeyboardModifier.ShiftModifier and key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            direction = -1 if key == Qt.Key.Key_PageUp else 1
            self.scroll_to_line(self._top_line + direction * self._visible_rows())
        elif self._connected:
            sequence = key_sequence(event, self.screen.application_cursor)
            if sequence:
                self._selection_start = self._selection_end = None
                self.scroll_to_line(self._logical_maximum)
                self.input_ready.emit(sequence)
        event.accept()

    def inputMethodEvent(self, event):
        if self._connected and event.commitString():
            self.input_ready.emit(event.commitString())
        event.accept()

    def paste(self):
        if not self._connected:
            return
        text = QApplication.clipboard().text().replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        if self.screen.bracketed_paste:
            text = "\x1b[200~" + text + "\x1b[201~"
        else:
            text = text.replace("\n", "\r")
        self.input_ready.emit(text)

    def selected_text(self):
        selection = self._selection()
        if not selection or selection[0] == selection[1]:
            return ""
        (first_row, first_col), (last_row, last_col) = selection
        output = []
        # Only an explicit copy materialises the requested text for the clipboard.
        for start in range(first_row, last_row + 1, 256):
            for offset, row in enumerate(self._rows(start, min(256, last_row + 1 - start))):
                number = start + offset
                left = first_col if number == first_row else 0
                right = last_col if number == last_row else len(row)
                output.append("".join(cell.get("data", " ") for cell in row[left:right]).rstrip())
        return "\n".join(output)

    def copy_selection(self):
        try:
            text = self.selected_text()
        except Exception as error:
            self._error(error)
            return
        if text:
            QApplication.clipboard().setText(text)

    def _point_cell(self, point):
        column = max(0, min(self.screen.columns, (point.x() - self._padding) // self._cell_width))
        row = self._top_line + max(0, (point.y() - self._padding) // self._cell_height)
        return min(self._total_rows() - 1, row), column

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            self._selection_start = self._selection_end = self._point_cell(event.position().toPoint())
            self.viewport().update()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and self._selection_start is not None:
            point = event.position().toPoint()
            if point.y() < 0:
                self.scroll_to_line(self._top_line - 1)
            elif point.y() > self.viewport().height():
                self.scroll_to_line(self._top_line + 1)
            self._selection_end = self._point_cell(point)
            self.viewport().update()
            event.accept()

    def mouseDoubleClickEvent(self, event):
        row, column = self._point_cell(event.position().toPoint())
        try:
            rows = self._rows(row, 1)
            text = "".join(cell.get("data", " ") for cell in rows[0]) if rows else ""
            start, end = min(column, len(text)), min(column, len(text))
            while start > 0 and not text[start - 1].isspace():
                start -= 1
            while end < len(text) and not text[end].isspace():
                end += 1
            self._selection_start, self._selection_end = (row, start), (row, end)
            self.viewport().update()
        except Exception as error:
            self._error(error)

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120
        if not steps and event.pixelDelta().y():
            steps = event.pixelDelta().y() / self._cell_height
        self.scroll_to_line(self._top_line - math.ceil(steps * 3))
        event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        copy = menu.addAction("Copy selection", self.copy_selection)
        copy.setEnabled(bool(self._selection() and self._selection_start != self._selection_end))
        paste = menu.addAction("Paste", self.paste)
        paste.setEnabled(self._connected)
        menu.addSeparator()
        menu.addAction("Jump to live output", lambda: self.scroll_to_line(self._logical_maximum))
        menu.exec(event.globalPos())
