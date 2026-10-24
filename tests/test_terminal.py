"""Offscreen checks of terminal behaviour, persistence and bounded rendering."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from pint_terminal.terminal import DiskScreen, TerminalStream, TerminalWidget, key_sequence


class SQLiteRows:
    """A real disk store to verify that the renderer does not retain history."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS rows (session TEXT, ordinal INTEGER, cells TEXT, PRIMARY KEY(session, ordinal))")
        self.read_sizes = []

    def append_line(self, session, cells):
        self.db.execute("INSERT INTO rows VALUES (?, ?, ?)", (session, self.line_count(session), json.dumps(cells)))

    def line_count(self, session):
        return self.db.execute("SELECT COUNT(*) FROM rows WHERE session = ?", (session,)).fetchone()[0]

    def read_lines(self, session, start, limit):
        self.read_sizes.append(limit)
        return [json.loads(row[0]) for row in self.db.execute("SELECT cells FROM rows WHERE session = ? AND ordinal >= ? ORDER BY ordinal LIMIT ?", (session, start, limit))]

    def close(self):
        self.db.commit()
        self.db.close()


def line_text(cells):
    return "".join(cell["data"] for cell in cells).rstrip()


class TerminalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "history.sqlite3")
        self.store = SQLiteRows(self.path)
        self.widget = TerminalWidget(self.store, "session")

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()
        self.store.close()
        self.directory.cleanup()

    def test_thousands_of_rows_survive_and_render_only_a_page(self):
        self.widget.screen.resize(lines=4, columns=24)
        for number in range(12_000):
            self.widget.feed(f"row {number:05d}\r\n")
        self.assertEqual(self.widget.history_count, 11_997)
        self.assertEqual(line_text(self.store.read_lines("session", 0, 1)[0]), "row 00000")
        self.assertEqual(line_text(self.store.read_lines("session", 11_996, 1)[0]), "row 11996")
        self.assertLessEqual(len(self.widget.screen.buffer), 4)
        self.widget.resize(500, 250)
        self.widget.show()
        self.app.processEvents()
        self.store.read_sizes.clear()
        self.widget.scroll_to_line(10)
        self.widget.grab()
        self.assertTrue(self.store.read_sizes)
        self.assertLessEqual(max(self.store.read_sizes), self.widget._visible_rows() + 1)

    def test_history_can_be_reopened_without_replaying_output(self):
        self.widget.screen.resize(lines=2, columns=20)
        self.widget.feed("saved first\r\nsaved second\r\nlast")
        self.store.db.commit()
        other = SQLiteRows(self.path)
        try:
            reopened = TerminalWidget(other, "session")
            self.assertEqual(reopened.history_count, 1)
            self.assertEqual(line_text(reopened._rows(0, 1)[0]), "saved first")
            reopened.close()
            reopened.deleteLater()
        finally:
            other.close()

    def test_escape_sequences_colours_cursor_and_replies(self):
        replies = []
        self.widget.input_ready.connect(replies.append)
        self.widget.feed("\x1b[31;1mred\x1b[0m\r\n\x1b[2;5H!\x1b[6n")
        self.assertEqual(self.widget.screen.buffer[0][0].fg, "red")
        self.assertTrue(self.widget.screen.buffer[0][0].bold)
        self.assertEqual(self.widget.screen.buffer[1][4].data, "!")
        self.assertEqual(replies[-1], "\x1b[2;6R")

    def test_alternate_screen_does_not_pollute_history_and_restores_cursor(self):
        self.widget.screen.resize(lines=3, columns=20)
        self.widget.feed("shell> ")
        before = self.widget.screen.display[:]
        self.widget.feed("\x1b[?1049h")
        self.assertTrue(self.widget.screen.alternate)
        self.widget.feed("".join(f"redraw {n}\r\n" for n in range(100)))
        self.assertEqual(self.widget.history_count, 0)
        self.widget.feed("\x1b[?1049l")
        self.assertEqual(self.widget.screen.display, before)
        self.assertEqual(self.widget.screen.cursor.x, 7)
        self.assertFalse(self.widget.screen.alternate)

    def test_partial_scrolling_region_is_not_normal_history(self):
        self.widget.screen.resize(lines=5, columns=20)
        self.widget.feed("\x1b[2;4r\x1b[4;1H" + "inner\r\n" * 10)
        self.assertEqual(self.widget.history_count, 0)
        self.widget.feed("\x1b[r\x1b[5;1Hbottom\r\n")
        self.assertEqual(self.widget.history_count, 1)

    def test_clear_preserves_normal_screen_and_scroll_up_is_supported(self):
        self.widget.screen.resize(lines=3, columns=20)
        self.widget.feed("one\r\ntwo\x1b[2J\x1b[H")
        self.assertEqual(self.widget.history_count, 2)
        self.assertEqual([line_text(r) for r in self.store.read_lines("session", 0, 2)], ["one", "two"])
        self.widget.feed("three\x1b[S")
        self.assertEqual(line_text(self.store.read_lines("session", 2, 1)[0]), "three")

    def test_copy_selection_can_cross_disk_and_live_screen(self):
        self.widget.screen.resize(lines=2, columns=20)
        self.widget.feed("alpha\r\nbeta\r\ngamma")
        self.widget._selection_start = (0, 2)
        self.widget._selection_end = (2, 3)
        self.assertEqual(self.widget.selected_text(), "pha\nbeta\ngam")
        self.widget.copy_selection()
        self.assertEqual(self.app.clipboard().text(), "pha\nbeta\ngam")

    def test_input_and_bracketed_paste_only_when_connected(self):
        emitted = []
        self.widget.input_ready.connect(emitted.append)
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, "a")
        self.widget.keyPressEvent(event)
        self.assertEqual(emitted, [])
        self.widget.set_connected(True)
        self.widget.keyPressEvent(event)
        self.assertEqual(emitted[-1], "a")
        self.widget.feed("\x1b[?2004h")
        self.app.clipboard().setText("first\nsecond")
        self.widget.paste()
        self.assertEqual(emitted[-1], "\x1b[200~first\nsecond\x1b[201~")

    def test_key_mappings(self):
        def key(code, modifiers=Qt.KeyboardModifier.NoModifier, text="", app=False):
            return key_sequence(QKeyEvent(QEvent.Type.KeyPress, code, modifiers, text), app)
        control = Qt.KeyboardModifier.MetaModifier if sys.platform == "darwin" else Qt.KeyboardModifier.ControlModifier
        self.assertEqual(key(Qt.Key.Key_C, control), "\x03")
        self.assertEqual(key(Qt.Key.Key_Up), "\x1b[A")
        self.assertEqual(key(Qt.Key.Key_Up, app=True), "\x1bOA")
        self.assertEqual(key(Qt.Key.Key_Right, control), "\x1b[1;5C")
        self.assertEqual(key(Qt.Key.Key_F5), "\x1b[15~")
        self.assertEqual(key(Qt.Key.Key_Backtab), "\x1b[Z")
        self.assertEqual(key(Qt.Key.Key_Return), "\r")
        self.assertEqual(key(Qt.Key.Key_Backspace), "\x7f")

    def test_windows_altgr_printable_keys_reach_the_session_unchanged(self):
        emitted = []
        self.widget.input_ready.connect(emitted.append)
        self.widget.set_connected(True)
        altgr = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
        cases = [
            (Qt.Key.Key_Q, altgr, "@"),
            (Qt.Key.Key_At, altgr, "@"),
            (Qt.Key.Key_BracketLeft, altgr, "["),
            (Qt.Key.Key_BracketRight, altgr, "]"),
            (Qt.Key.Key_Backslash, altgr, "\\"),
            (Qt.Key.Key_C, altgr | Qt.KeyboardModifier.ShiftModifier, "Ć"),
            (Qt.Key.Key_BracketLeft, Qt.KeyboardModifier.GroupSwitchModifier | Qt.KeyboardModifier.AltModifier, "["),
        ]
        with patch("pint_terminal.terminal.sys.platform", "win32"):
            for key, modifiers, text in cases:
                with self.subTest(key=key, text=text):
                    event = QKeyEvent(QEvent.Type.KeyPress, key, modifiers, text)
                    QApplication.sendEvent(self.widget, event)
                    self.assertEqual(emitted[-1], text)
        self.assertEqual(emitted, [text for _, _, text in cases])

    def test_windows_control_and_alt_shortcuts_are_preserved(self):
        with patch("pint_terminal.terminal.sys.platform", "win32"):
            control = Qt.KeyboardModifier.ControlModifier
            alt = Qt.KeyboardModifier.AltModifier
            self.assertEqual(key_sequence(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C, control, "\x03")), "\x03")
            self.assertEqual(key_sequence(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C, control | alt, "\x03")), "\x03")
            self.assertEqual(key_sequence(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left, control | alt)), "\x1b[1;7D")
            self.assertEqual(key_sequence(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_X, alt, "x")), "\x1bx")

    def test_macos_control_and_option_are_not_treated_as_altgr(self):
        with patch("pint_terminal.terminal.sys.platform", "darwin"):
            control = Qt.KeyboardModifier.MetaModifier
            option = Qt.KeyboardModifier.AltModifier
            self.assertEqual(key_sequence(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C, control, "\x03")), "\x03")
            self.assertEqual(key_sequence(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Q, control | option, "@")), "\x11")
            self.assertEqual(key_sequence(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_E, option, "é")), "\x1bé")

    def test_row_navigation_is_not_capped_by_qt_integer_range(self):
        self.widget._history_count = 3_000_000_000
        self.widget._update_range()
        self.widget.scroll_to_line(2_900_000_005)
        self.assertEqual(self.widget._top_line, 2_900_000_005)
        self.assertEqual(self.widget.verticalScrollBar().maximum(), 2_147_483_647)

    def test_default_trailing_blanks_are_not_stored(self):
        self.widget.screen.resize(lines=2, columns=80)
        self.widget.feed("short\r\nnext\r\n")
        self.assertEqual(len(self.store.read_lines("session", 0, 1)[0]), 5)

    def test_history_failure_is_exposed(self):
        errors = []
        self.widget.failed.connect(errors.append)
        self.widget.screen.resize(lines=2, columns=20)
        self.store.append_line = lambda *_: (_ for _ in ()).throw(OSError("disk full"))
        self.widget.feed("one\r\ntwo\r\n")
        self.assertEqual(len(errors), 1)
        self.assertIn("disk full", errors[0])

    def test_resize_keeps_prompt_and_moves_cursor_with_scrolled_rows(self):
        self.widget.screen.resize(lines=5, columns=20)
        self.widget.feed("one\r\ntwo\r\nthree")
        self.widget.screen.resize(lines=3)
        self.assertEqual([r.rstrip() for r in self.widget.screen.display], ["one", "two", "three"])
        self.assertEqual(self.widget.screen.cursor.y, 2)
        self.assertEqual(self.widget.history_count, 0)
        self.widget.screen.resize(lines=2)
        self.assertEqual([r.rstrip() for r in self.widget.screen.display], ["two", "three"])
        self.assertEqual(self.widget.screen.cursor.y, 1)
        self.assertEqual(line_text(self.store.read_lines("session", 0, 1)[0]), "one")

    def test_resize_during_alternate_screen_preserves_original_normal_rows(self):
        self.widget.screen.resize(lines=5, columns=20)
        self.widget.feed("one\r\ntwo\r\nthree\r\nfour\r\nfive")
        self.widget.feed("\x1b[?1049hredraw")
        self.widget.screen.resize(lines=3)
        self.widget.feed("\x1b[?1049l")
        self.assertEqual([r.rstrip() for r in self.widget.screen.display], ["three", "four", "five"])
        self.assertEqual([line_text(r) for r in self.store.read_lines("session", 0, 2)], ["one", "two"])
        self.assertEqual(self.widget.screen.cursor.y, 2)

    def test_oversized_split_control_strings_are_bounded_and_not_executed(self):
        replies = []
        self.widget.input_ready.connect(replies.append)
        stream = self.widget.stream
        stream.feed("\x1b")
        stream.feed("]0;" + "x" * 65536)
        self.assertLessEqual(len(stream._control_buffer), 8192)
        stream.feed("x" * 65536 + "\x1b[6n\x1b")
        self.assertLessEqual(len(stream._control_buffer), 8192)
        stream.feed("\\safe")
        self.assertEqual(replies, [])
        self.assertTrue(self.widget.screen.display[0].startswith("safe"))
        stream.feed("\x1b[" + "9" * 5000)
        self.assertLessEqual(len(stream._control_buffer), 1024)
        stream.feed("m text")
        self.assertTrue(self.widget.screen.display[0].startswith("safe text"))
        # Ordinary OSC/ST, SGR and status requests still work across every split.
        for char in "\x1b]0;title\x1b\\\x1b[31mR\x1b[6n":
            stream.feed(char)
        self.assertEqual(self.widget.screen.title, "title")
        self.assertEqual(self.widget.screen.buffer[0][9].fg, "red")
        self.assertEqual(replies, ["\x1b[1;11R"])

    def test_cursor_save_is_single_slot_and_can_be_restored_repeatedly(self):
        self.widget.feed("abc" + "\x1b7" * 5000)
        self.assertEqual(len(self.widget.screen.savepoints), 1)
        self.widget.feed("more\x1b8")
        self.assertEqual(self.widget.screen.cursor.x, 3)
        self.widget.feed("different\x1b8")
        self.assertEqual(self.widget.screen.cursor.x, 3)


if __name__ == "__main__":
    unittest.main()
