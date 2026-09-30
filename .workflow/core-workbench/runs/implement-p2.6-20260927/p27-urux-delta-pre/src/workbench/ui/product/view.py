"""curses drawing of a :class:`ProductModel` (reuses the G1 cell/colour mapping)."""
from __future__ import annotations

import curses

from wcwidth import wcswidth

from workbench.contracts.v1 import PaneId
from workbench.ui.product.model import (FOOTER_ROWS, HEADER_ROWS, HELP_LINES, PANES, ProductModel)
from workbench.ui.terminal_g1.app import _ColorPairs, _attributes


def pane_rects(rows: int, cols: int) -> dict[PaneId, tuple[int, int, int, int]]:
    """(top, left, height, width) including the border."""
    height = max(1, rows - HEADER_ROWS - FOOTER_ROWS)
    base = max(1, cols // 3)
    widths = (base, base, max(1, cols - 2 * base))
    return {pane: (HEADER_ROWS, base * i, height, widths[i]) for i, pane in enumerate(PANES)}


def _put(win: curses.window, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    try:
        win.addnstr(y, x, text, max(0, width), attr)
    except curses.error:
        pass


def draw(win: curses.window, model: ProductModel, colors: _ColorPairs) -> None:
    rows, cols = win.getmaxyx()
    win.erase()
    if model.too_small():
        _cursor(False)
        _put(win, 0, 0, f"terminal too small ({cols}x{rows}); resize", cols - 1)
        win.refresh()
        return
    line1, line2 = model.status_lines()
    _put(win, 0, 0, line1, cols - 1, curses.A_BOLD)
    _put(win, 1, 0, line2, cols - 1)
    for pane, (top, left, height, width) in pane_rects(rows, cols).items():
        focused = pane is model.focus
        border = curses.A_BOLD if focused else curses.A_DIM
        try:
            win.addch(top, left, curses.ACS_ULCORNER, border)
            win.addch(top + height - 1, left, curses.ACS_LLCORNER, border)
            win.addch(top, left + width - 1, curses.ACS_URCORNER, border)
            win.addch(top + height - 1, left + width - 1, curses.ACS_LRCORNER, border)
            for x in range(left + 1, left + width - 1):
                win.addch(top, x, curses.ACS_HLINE, border)
                win.addch(top + height - 1, x, curses.ACS_HLINE, border)
            for y in range(top + 1, top + height - 1):
                win.addch(y, left, curses.ACS_VLINE, border)
                win.addch(y, left + width - 1, curses.ACS_VLINE, border)
        except curses.error:
            pass
        _put(win, top, left + 2, model.pane_title(pane), width - 4, curses.A_BOLD | (curses.A_REVERSE if focused else 0))
        screen = model.panes[pane].screen
        inner_rows, inner_cols = height - 2, width - 2
        for y in range(min(inner_rows, screen.lines)):
            line = screen.buffer.get(y, {})
            for x in range(min(inner_cols, screen.columns)):
                cell = line.get(x, screen.default_char)
                if not cell.data:
                    continue
                cell_width = wcswidth(cell.data)
                if cell_width < 0 or x + cell_width > inner_cols:
                    continue
                try:
                    win.addstr(top + 1 + y, left + 1 + x, cell.data, _attributes(cell, colors))
                except curses.error:
                    pass
    if model.help_open:
        _cursor(False)
        _draw_help(win, rows, cols)
    else:
        _cursor(not model.panes[model.focus].screen.cursor.hidden)
        _put(win, rows - 1, 0, model.footer(), cols - 1, curses.A_BOLD if model.notice else 0)
        _place_cursor(win, model, rows, cols)
    try:
        win.refresh()
    except curses.error:
        pass


def _place_cursor(win: curses.window, model: ProductModel, rows: int, cols: int) -> None:
    top, left, height, width = pane_rects(rows, cols)[model.focus]
    screen = model.panes[model.focus].screen
    if screen.cursor.hidden:
        return
    try:
        win.move(top + 1 + min(height - 3, max(0, screen.cursor.y)),
                 left + 1 + min(width - 3, max(0, screen.cursor.x)))
    except curses.error:
        pass


def _draw_help(win: curses.window, rows: int, cols: int) -> None:
    width = min(cols - 2, max(len(line) for line in HELP_LINES) + 4)
    height = min(rows - 2, len(HELP_LINES) + 2)
    top, left = max(0, (rows - height) // 2), max(0, (cols - width) // 2)
    for y in range(height):
        _put(win, top + y, left, " " * width, width, curses.A_REVERSE)
    for i, line in enumerate(HELP_LINES[:height - 2]):
        _put(win, top + 1 + i, left + 2, line, width - 4, curses.A_REVERSE)


def _cursor(visible: bool) -> None:
    """The hardware caret follows the focused pane's cursor visibility (hidden while help is open)."""
    try:
        curses.curs_set(1 if visible else 0)
    except curses.error:
        pass
