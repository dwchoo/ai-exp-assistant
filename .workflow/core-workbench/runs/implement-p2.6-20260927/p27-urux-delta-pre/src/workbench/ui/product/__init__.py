"""CW-06 product UI: a ui_v1 client that never owns PTYs (exit = detach)."""
from workbench.ui.product.app import run_product

__all__ = ["run_product"]
