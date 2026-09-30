"""Split layout of the product UI (C-D59): two adjustable dividers, clamping and the client-side layout file.

Pure geometry + a tiny persistence helper. No curses, no sockets, nothing sent to the backend.

``col_ratio`` is the manager share of the width, ``row_ratio`` the top (two OMP panes) share of the body height.
``None`` keeps the default (C-D58) split exactly, so an untouched layout never changes any size.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import tempfile

LAYOUT_FILE = "ui-layout.json"
LAYOUT_VERSION = 1
MIN_INNER_COLS = 10  # every pane keeps at least this many inner columns ...
MIN_INNER_ROWS = 3  # ... and rows, whenever the window is big enough for it
MIN_BOX_COLS = MIN_INNER_COLS + 2
MIN_BOX_ROWS = MIN_INNER_ROWS + 2
MAX_FILE_BYTES = 4096


@dataclass(frozen=True, slots=True)
class Layout:
    col_ratio: float | None = None  # manager | worker
    row_ratio: float | None = None  # (manager, worker) | host shell


DEFAULT_LAYOUT = Layout()


def _round(value: float) -> int:
    return int(value + 0.5)


def height_bounds(body: int) -> tuple[int, int] | None:
    """(min, max) box height of the top row for a body of ``body`` rows; None when no valid split exists."""
    low = MIN_BOX_ROWS if body >= 2 * MIN_BOX_ROWS else max(1, body // 2)  # the smallest windows keep 2 text rows
    high = body - low
    return (low, high) if high >= low else None


def width_bounds(cols: int) -> tuple[int, int] | None:
    low, high = MIN_BOX_COLS, cols - MIN_BOX_COLS
    return (low, high) if high >= low else None


def top_height(body: int, ratio: float | None) -> int:
    """Box height of the top row (the bottom row gets ``body - result``)."""
    default = max(1, (body + 1) // 2)
    bounds = height_bounds(body)
    if ratio is None or bounds is None:
        return default
    return min(max(_round(ratio * body), bounds[0]), bounds[1])


def left_width(cols: int, ratio: float | None) -> int:
    """Box width of the manager pane (the worker pane gets ``cols - result``)."""
    default = max(1, cols // 2)
    bounds = width_bounds(cols)
    if ratio is None or bounds is None:
        return default
    return min(max(_round(ratio * cols), bounds[0]), bounds[1])


def clamp_height(body: int, height: int) -> int:
    bounds = height_bounds(body)
    return height if bounds is None else min(max(height, bounds[0]), bounds[1])


def clamp_width(cols: int, width: int) -> int:
    bounds = width_bounds(cols)
    return width if bounds is None else min(max(width, bounds[0]), bounds[1])


# -- persistence ---------------------------------------------------------------------------------------------
def _ratio(value: object) -> float | None | bool:
    """A valid stored ratio, None for JSON null, False for anything unusable."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value) or not 0.0 < value < 1.0:
        return False
    return float(value)


def load_layout(path: Path) -> tuple[Layout, bool]:
    """(layout, zoom) from ``path``; the defaults for a missing, corrupt, oversized or unknown-version file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return DEFAULT_LAYOUT, False
    try:
        raw = os.read(fd, MAX_FILE_BYTES + 1)
    except OSError:
        return DEFAULT_LAYOUT, False
    finally:
        os.close(fd)
    if len(raw) > MAX_FILE_BYTES:
        return DEFAULT_LAYOUT, False
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return DEFAULT_LAYOUT, False
    if not isinstance(data, dict) or data.get("version") != LAYOUT_VERSION:
        return DEFAULT_LAYOUT, False
    col, row, zoom = _ratio(data.get("col_ratio")), _ratio(data.get("row_ratio")), data.get("zoom", False)
    if col is False or row is False or not isinstance(zoom, bool):
        return DEFAULT_LAYOUT, False
    return Layout(col, row), zoom


def save_layout(path: Path, layout: Layout, zoom: bool) -> bool:
    """Write the layout file atomically with mode 0600; False when it could not be written."""
    body = json.dumps({"version": LAYOUT_VERSION, "col_ratio": layout.col_ratio, "row_ratio": layout.row_ratio,
                       "zoom": bool(zoom)}, sort_keys=True).encode() + b"\n"
    tmp = None
    try:
        fd, name = tempfile.mkstemp(prefix=".ui-layout.", suffix=".tmp", dir=path.parent)  # created with 0600
        tmp = name
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
        tmp = None
        return True
    except OSError:
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
