"""Incremental plain-text rendering of a CLI recording.

This is a readable line transcript, not a full-screen terminal replay. Editing
controls affect the unfinished line; completed lines are retained in time order.
"""

from __future__ import annotations

from wcwidth import wcwidth


class TranscriptRenderer:
    """Interpret line editing while keeping escape and unfinished-line state bounded.

    Call ``drain`` after each stored output chunk. It returns completed lines,
    including their newlines. ``current_line`` is the edited, unfinished tail.
    Feeding a later chunk continues any escape sequence or line begun earlier.
    A visible marker identifies printable text omitted by preview safety limits.
    """

    MAX_COLUMNS = 16384
    MAX_CONTROL = 1024
    MAX_CELL_CODEPOINTS = 64
    TRUNCATION_MARKER = " [line truncated in preview]"

    def __init__(self):
        self._cells: list[str | None] = []
        self._column = 0
        self._saved_column = 0
        self._truncated_at: int | None = None
        self._completed: list[str] = []
        self._state = "text"
        self._control = ""
        self._discard_control = False
        self._string_is_osc = False

    @property
    def current_line(self) -> str:
        # None marks the second display column of a wide character.
        line = "".join(cell for cell in self._cells if cell is not None).rstrip(" ")
        return line + (self.TRUNCATION_MARKER if self._truncated_at is not None else "")

    def drain(self) -> str:
        result = "".join(self._completed)
        self._completed.clear()
        return result

    def _newline(self):
        self._completed.append(self.current_line + "\n")
        self._cells.clear()
        self._column = 0
        self._truncated_at = None

    def _mark_truncated(self, column: int):
        self._truncated_at = column if self._truncated_at is None else min(self._truncated_at, column)

    def _clear_cell(self, column: int):
        if column >= len(self._cells):
            return
        if self._cells[column] is None and column:
            self._cells[column - 1] = " "
        elif column + 1 < len(self._cells) and self._cells[column + 1] is None:
            self._cells[column + 1] = " "
        self._cells[column] = " "

    def _draw(self, char: str):
        width = wcwidth(char)
        if width < 0:
            return
        if width == 0:
            column = min(self._column, len(self._cells)) - 1
            if column >= 0 and self._cells[column] is None:
                column -= 1
            if column >= 0:
                if len(self._cells[column] or "") < self.MAX_CELL_CODEPOINTS:
                    self._cells[column] += char
                else:
                    self._mark_truncated(column)
            return
        end = self._column + width
        if end > self.MAX_COLUMNS:
            self._mark_truncated(self._column)
            self._column = self.MAX_COLUMNS
            return
        if end > len(self._cells):
            self._cells.extend(" " for _ in range(end - len(self._cells)))
        for column in range(self._column, end):
            self._clear_cell(column)
        self._cells[self._column] = char
        if width == 2:
            self._cells[self._column + 1] = None
        self._column = end

    def _plain(self, char: str):
        if char == "\n":
            self._newline()
        elif char == "\r":
            self._column = 0
        elif char == "\b":
            self._column = max(0, self._column - 1)
        elif char == "\t":
            self._column = min(self.MAX_COLUMNS, (self._column // 8 + 1) * 8)
        elif ord(char) >= 32 and not 127 <= ord(char) <= 159:
            self._draw(char)

    def _erase(self, start: int, end: int):
        for column in range(max(0, start), min(end, len(self._cells))):
            self._clear_cell(column)

    def _csi(self, command: str):
        if self._discard_control:
            return
        # Only ordinary numeric parameters belong to these line-edit controls.
        # Private modes, styles and intermediates must never become visible text.
        parts = self._control.split(";")
        if len(parts) > 16 or any(part and (not part.isascii() or not part.isdigit()) for part in parts):
            return
        first = min(self.MAX_COLUMNS, int(parts[0] or "0")) if len(parts[0]) < 6 else self.MAX_COLUMNS
        count = first or 1
        if command in ("C", "a"):
            self._column = min(self.MAX_COLUMNS, self._column + count)
        elif command == "D":
            self._column = max(0, self._column - count)
        elif command in ("G", "`"):
            self._column = min(self.MAX_COLUMNS, count - 1)
        elif command in ("H", "f"):
            # A readable transcript has no prior screen rows to reposition into.
            column = parts[1] if len(parts) > 1 else ""
            self._column = min(self.MAX_COLUMNS, int(column or "1") - 1) if len(column) < 6 else self.MAX_COLUMNS
            self._column = max(0, self._column)
        elif command in ("E", "F"):
            self._column = 0
        elif command == "K":
            if first == 0:
                self._erase(self._column, len(self._cells))
                # Erasing the omitted tail also removes its preview warning.
                if self._truncated_at is not None and self._column <= self._truncated_at:
                    self._truncated_at = None
            elif first == 1:
                self._erase(0, self._column + 1)
            elif first == 2:
                self._cells.clear()
                self._truncated_at = None
        elif command == "X":
            self._erase(self._column, self._column + count)
        elif command == "P":
            self._erase(self._column, self._column + count)
            del self._cells[self._column:self._column + count]
        elif command == "@" and self._column < len(self._cells):
            if self._cells[self._column] is None:
                self._clear_cell(self._column)
            self._cells[self._column:self._column] = [" "] * min(count, self.MAX_COLUMNS - self._column)
            del self._cells[self.MAX_COLUMNS:]
        elif command == "s":
            self._saved_column = self._column
        elif command == "u":
            self._column = self._saved_column

    def feed(self, text: str) -> None:
        for char in text:
            state = self._state
            if state in ("string", "string_escape"):
                if char == "\x9c" or (self._string_is_osc and char == "\x07") or (state == "string_escape" and char == "\\"):
                    self._state = "text"
                elif char in ("\x18", "\x1a"):
                    self._state = "text"
                else:
                    self._state = "string_escape" if char == "\x1b" else "string"
                continue
            if char == "\x1b":
                self._state = "escape"
                continue
            if char == "\x9b":
                self._state, self._control, self._discard_control = "csi", "", False
                continue
            if char in ("\x90", "\x98", "\x9d", "\x9e", "\x9f"):
                self._state, self._string_is_osc = "string", char == "\x9d"
                continue
            if state == "escape":
                self._state = "text"
                if char == "[":
                    self._state, self._control, self._discard_control = "csi", "", False
                elif char in ("]", "P", "X", "^", "_"):
                    self._state, self._string_is_osc = "string", char == "]"
                elif " " <= char <= "/":
                    self._state = "escape_intermediate"
                elif char == "7":
                    self._saved_column = self._column
                elif char == "8":
                    self._column = self._saved_column
                elif char in ("D", "E"):
                    self._newline()
                elif char == "c":
                    self._cells.clear()
                    self._column = self._saved_column = 0
                    self._truncated_at = None
                continue
            if state == "escape_intermediate":
                if not " " <= char <= "/":
                    self._state = "text"
                continue
            if state == "csi":
                if "@" <= char <= "~":
                    self._csi(char)
                    self._state, self._control = "text", ""
                elif char in ("\x18", "\x1a") or ord(char) > 126:
                    self._state, self._control = "text", ""
                elif ord(char) < 32:
                    self._plain(char)
                elif not self._discard_control:
                    if len(self._control) < self.MAX_CONTROL:
                        self._control += char
                    else:
                        self._control, self._discard_control = "", True
                continue
            self._plain(char)
