"""curses drawing of a :class:`ProductModel` (reuses the G1 cell/colour mapping)."""
from __future__ import annotations

import curses

from wcwidth import wcswidth

from workbench.contracts.v1 import PaneId
from workbench.ui.product.layout import DEFAULT_LAYOUT, Layout
from workbench.ui.product.model import HELP_LINES, ProductModel, pane_boxes
from workbench.ui.terminal_g1.app import _ColorPairs, _attributes


def pane_rects(rows: int, cols: int, layout: Layout = DEFAULT_LAYOUT,
               zoom: PaneId | None = None) -> dict[PaneId, tuple[int, int, int, int]]:
    """(top, left, height, width) including the border: OMP panes side by side on top, host shell below.

    Only visible panes: a zoomed pane is the only one listed."""
    return pane_boxes(rows, cols, layout, zoom)


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
    _put(win, 1, 0, line2, cols - 1, curses.A_BOLD | curses.A_REVERSE if model.isolation_warning() else 0)
    for pane, (top, left, height, width) in pane_rects(rows, cols, model.layout, model.zoom).items():
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
        selected = model.selection_spans(pane, inner_rows)  # drag-to-copy highlight (reverse video)
        for y, line in enumerate(model.pane_lines(pane, inner_rows)):
            span = selected.get(y)
            for x in range(min(inner_cols, screen.columns)):
                cell = line.get(x, screen.default_char)
                if not cell.data:
                    continue
                cell_width = wcswidth(cell.data)
                if cell_width < 0 or x + cell_width > inner_cols:
                    continue
                attr = _attributes(cell, colors)
                if span is not None and span[0] <= x + max(cell_width, 1) - 1 and x <= span[1]:
                    attr ^= curses.A_REVERSE
                try:
                    win.addstr(top + 1 + y, left + 1 + x, cell.data, attr)
                except curses.error:
                    pass
        _draw_exit_notice(win, model, pane, top, left, height, width)
    if model.help_open:
        _cursor(False)
        _draw_help(win, rows, cols)
    elif model.kill_confirm_open:
        _cursor(False)
        _draw_box(win, rows, cols, model.kill_confirm_lines())
        _put(win, rows - 1, 0, model.footer(), cols - 1, curses.A_BOLD)
    elif model.menu_open:
        _cursor(False)
        _draw_menu(win, rows, cols, model)
        _put(win, rows - 1, 0, model.footer(), cols - 1, curses.A_BOLD)
    else:
        _cursor(not model.scrolled(model.focus) and not model.panes[model.focus].screen.cursor.hidden)
        _put(win, rows - 1, 0, model.footer(), cols - 1, curses.A_BOLD if model.notice else 0)
        _place_cursor(win, model, rows, cols)
    try:
        win.refresh()
    except curses.error:
        pass


def _draw_exit_notice(win: curses.window, model: ProductModel, pane: PaneId, top: int, left: int, height: int,
                      width: int) -> None:
    """Exited OMP pane (C-D62): its last screen stays; the notice fills the bottom inner rows (reverse video)."""
    inner_rows, inner_cols = height - 2, width - 2
    lines = model.restart_notice_lines(pane, inner_cols)[-max(1, inner_rows // 2):]
    for i, line in enumerate(lines):
        y = top + height - 1 - len(lines) + i
        _put(win, y, left + 1, line + " " * max(0, inner_cols - wcswidth(line)), inner_cols,
             curses.A_BOLD | curses.A_REVERSE)


def _place_cursor(win: curses.window, model: ProductModel, rows: int, cols: int) -> None:
    top, left, height, width = pane_rects(rows, cols, model.layout, model.zoom)[model.focus]
    screen = model.panes[model.focus].screen
    if screen.cursor.hidden or model.scrolled(model.focus) or model.pane_exited(model.focus):
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


def _draw_box(win: curses.window, rows: int, cols: int, lines: list[str]) -> None:
    """A centred reverse-video box of text lines (the host terminal kill confirmation, C-D63)."""
    width = min(cols - 2, max(wcswidth(line) for line in lines) + 4)
    height = min(rows - 2, len(lines) + 2)
    top, left = max(0, (rows - height) // 2), max(0, (cols - width) // 2)
    for y in range(height):
        _put(win, top + y, left, " " * width, width, curses.A_REVERSE)
    for i, line in enumerate(lines[:height - 2]):
        _put(win, top + 1 + i, left + 2, line, width - 4, curses.A_REVERSE | (curses.A_BOLD if i == 0 else 0))


def _draw_menu(win: curses.window, rows: int, cols: int, model: ProductModel) -> None:
    lines = model.menu_lines()
    width = min(cols - 2, max(wcswidth(line) for line in lines) + 4)
    height = min(rows - 2, len(lines) + 2)
    top, left = max(0, (rows - height) // 2), max(0, (cols - width) // 2)
    for y in range(height):
        _put(win, top + y, left, " " * width, width, curses.A_REVERSE)
    for i, line in enumerate(lines[:height - 2]):
        selected = line.startswith(">")
        if selected:  # the chosen row is drawn un-reversed over its whole width
            _put(win, top + 1 + i, left + 1, " " * (width - 2), width - 2, curses.A_BOLD)
        _put(win, top + 1 + i, left + 2, line, width - 4, curses.A_BOLD if selected else curses.A_REVERSE)


def _cursor(visible: bool) -> None:
    """The hardware caret follows the focused pane's cursor visibility (hidden while help is open)."""
    try:
        curses.curs_set(1 if visible else 0)
    except curses.error:
        pass
