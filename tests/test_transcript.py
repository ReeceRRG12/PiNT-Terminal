"""Recorded CLI output must interpret editing bytes instead of displaying boxes."""

from pint_terminal.transcript import TranscriptRenderer


def render(text):
    transcript = TranscriptRenderer()
    transcript.feed(text)
    return transcript.drain() + transcript.current_line


def test_switch_command_correction_and_pager_erasure():
    prompt = "--More--, next page: Space, next line: Return key, quit: Control-c"
    raw = ("switch#show int eth 1.1/0\b \b\b \b/1/1\r\n"
           "GigabitEthernet1/1/1 is up\r\n" + prompt + "\b \b" * len(prompt)
           + "20 input errors, 0 CRC\r\n")
    assert render(raw) == ("switch#show int eth 1.1/1/1\n"
                           "GigabitEthernet1/1/1 is up\n"
                           "20 input errors, 0 CRC\n")


def test_backspace_moves_cursor_without_deleting_until_overwritten():
    assert render("abc\b") == "abc"
    assert render("abc\bX\n") == "abX\n"
    assert render("abc\b \b\n") == "ab\n"


def test_carriage_return_overwrite_erase_and_tabs():
    assert render("progress 10%\rprogress 100%\r\n") == "progress 100%\n"
    assert render("long status\rOK\x1b[K\r\n") == "OK\n"
    assert render("old\x1b[2Knew\r\n") == "   new\n"
    assert render("a\tb\r\n") == "a       b\n"
    assert render("abcdef\x1b[3DXY\x1b[K\n") == "abcXY\n"
    assert render("abcdef\x1b[2G\x1b[2P\n") == "adef\n"


def test_ansi_strings_and_editing_work_at_every_chunk_split():
    raw = ("\x1b]0;private title\x07\x1b[32mport 1/2\x1b[0m"
           "\x1b[1D1\r\n\x1bPdiscarded payload\x1b\\next\x9dtitle\x9c!")
    expected = "port 1/1\nnext!"
    for split in range(len(raw) + 1):
        transcript = TranscriptRenderer()
        transcript.feed(raw[:split])
        prefix = transcript.drain()
        transcript.feed(raw[split:])
        assert prefix + transcript.drain() + transcript.current_line == expected
    transcript = TranscriptRenderer()
    output = ""
    for char in raw:
        transcript.feed(char)
        output += transcript.drain()
    assert output + transcript.current_line == expected


def test_page_boundary_keeps_partial_line_and_control_state():
    transcript = TranscriptRenderer()
    transcript.feed("complete\r\ncommand 12\x1b[")
    assert transcript.drain() == "complete\n"
    assert transcript.current_line == "command 12"
    transcript.feed("D3\r")
    assert transcript.drain() == ""
    assert transcript.current_line == "command 13"
    transcript.feed("\nnext")
    assert transcript.drain() == "command 13\n"
    assert transcript.current_line == "next"


def test_nonprinting_controls_do_not_become_missing_glyph_boxes():
    raw = "a\x00\x07\x7fb\x01\x02\x0e\x0f\x11\x13\x18\x1a\x80\x85\x9cc\r\x00\n"
    assert render(raw) == "abc\n"
    assert render("\x9b31mgreen\x9b0m\n") == "green\n"
    assert render("\x1b(0ascii\x1b(B\n") == "ascii\n"


def test_unicode_and_wide_display_columns_survive_editing():
    assert render("café cafe\u0301 日本語🙂\r\n") == "café cafe\u0301 日本語🙂\n"
    assert render("界X\bY\r\n") == "界Y\n"
    assert render("界\b\bA\x1b[K\n") == "A\n"
    assert render("A界B\x1b[2Gx\n") == "Ax B\n"
    assert render("a\u0301\bX\n") == "X\n"


def test_malformed_sequences_and_cursor_requests_are_bounded():
    transcript = TranscriptRenderer()
    transcript.feed("\x1b[" + "9" * 100000)
    assert len(transcript._control) <= transcript.MAX_CONTROL
    transcript.feed("Gsafe\n")
    assert transcript.drain() == "safe\n"
    transcript.feed("\x1b[" + "9" * 900 + "Gend")
    assert len(transcript._cells) <= transcript.MAX_COLUMNS
    transcript.feed("\r\x1b[2K" + "x" * 100000)
    assert len(transcript._cells) == transcript.MAX_COLUMNS
    transcript.feed("\u0301" * 100000)
    assert all(cell is None or len(cell) <= transcript.MAX_CELL_CODEPOINTS for cell in transcript._cells)
    transcript.feed("\x1b]" + "x" * 100000 + "\x1b[31m\x1b\\\rOK\x1b[K\n")
    assert transcript.drain() == "OK\n"


def test_overlong_printable_lines_are_visibly_marked_and_next_line_is_clean():
    transcript = TranscriptRenderer()
    visible = "x" * transcript.MAX_COLUMNS
    transcript.feed(visible)
    assert transcript.current_line == visible
    transcript.feed("omitted output")
    assert transcript.current_line == visible + transcript.TRUNCATION_MARKER
    assert len(transcript._cells) == transcript.MAX_COLUMNS
    transcript.feed("\r\nnext line\n")
    assert transcript.drain() == visible + transcript.TRUNCATION_MARKER + "\nnext line\n"
    assert transcript.current_line == ""


def test_combining_overflow_is_marked_and_line_clear_or_reset_removes_marker():
    transcript = TranscriptRenderer()
    source = "a" + "\u0301" * transcript.MAX_CELL_CODEPOINTS
    transcript.feed(source)
    assert transcript.current_line == source[:transcript.MAX_CELL_CODEPOINTS] + transcript.TRUNCATION_MARKER
    assert len(transcript._cells[0]) == transcript.MAX_CELL_CODEPOINTS
    transcript.feed("\r\x1b[2Kclean")
    assert transcript.current_line == "clean"
    transcript.feed("\r\x1b[2K" + source + "\x1bcreset")
    assert transcript.current_line == "reset"
