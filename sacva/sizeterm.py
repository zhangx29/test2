"""Euler base plus a size term: the RW of a trade at its actual size from three numbers per cell.

Treating capital as one quadratic form, K(B + xT)^2 = K^2 + 2 x K e + x^2 s^2, so the incremental RW per unit at
size x is

    RW(x) = (sqrt(K^2 + 2 x K e + x^2 s^2) - K) / x = (2 K e + x s^2) / (sqrt(K^2 + 2 x K e + x^2 s^2) + K)

with e the Euler RW, s the stand-alone RW and K the book capital. K is replaced by an effective K_eff, calibrated
per cell so that RW(x_ref) equals the incremental RW i_ref measured at a reference size x_ref:

    K_eff = x_ref (s^2 - i_ref^2) / (2 (i_ref - e))

RW(x) -> e as x -> 0, equals i_ref at x_ref, tends to s as x -> inf, and is monotone in between. x, e, s and
i_ref share one user unit (e.g. $/bp CR01), K is in capital currency.
"""
from __future__ import annotations

import numpy as np


def k_eff(e, s, i_ref, x_ref, k_class):
    """Effective book capital passing the closed form through (x_ref, i_ref).

    No measurable curvature (i_ref <= e) falls back to ``k_class``; i_ref >= s gives 0 (stand-alone pricing)."""
    e, s, i_ref, k_class = np.broadcast_arrays(*(np.asarray(v, float) for v in (e, s, i_ref, k_class)))
    tol = 1e-9 * np.maximum(np.abs(s), 1.0)
    flat = i_ref - e <= tol
    with np.errstate(divide="ignore", invalid="ignore"):
        k = np.where(flat, k_class, x_ref * (s * s - i_ref * i_ref) / (2.0 * (i_ref - e)))
    k = np.where(~flat & (i_ref >= s), 0.0, k)
    k = np.where(np.isnan(e) | np.isnan(s) | np.isnan(i_ref), np.nan, k)
    return k if k.ndim else float(k)


def size_rw(x, e, s, k, floor: float = 0.0):
    """RW per user unit of a trade of size x (> 0) under the closed form with book capital k, floored at floor * s."""
    x, e, s, k = np.broadcast_arrays(*(np.asarray(v, float) for v in (x, e, s, k)))
    if (x <= 0).any():
        raise ValueError("trade size must be positive")
    q = np.maximum(k * k + 2.0 * x * k * e + x * x * s * s, 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        rw = np.where(k > 0, (2.0 * k * e + x * s * s) / (np.sqrt(q) + k), s)
    if floor:
        rw = np.maximum(rw, floor * s)
    return rw if rw.ndim else float(rw)
