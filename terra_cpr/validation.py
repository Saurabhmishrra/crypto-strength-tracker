"""Configuration validation shared by the pure engines and adapters."""
import math


def positive_int(name, value):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def finite_number(name, value, minimum=0.0, inclusive=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < minimum or (not inclusive and value == minimum):
        raise ValueError(f"{name} must be {'at least' if inclusive else 'greater than'} {minimum}")
