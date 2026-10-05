"""Marginal SA-CVA capital of a trade (CVA sensitivities + policy hedge) against a book.

* incremental: (K(book + size*trade) - K(book)) / size
* euler:       directional derivative ∇K(book)·trade. For a risk class/measure with zero capital in
               the book the derivative of K_class = m*sqrt(Q) is the stand-alone K_class(trade)
               (homogeneity), so that class falls back to its stand-alone value.
* standalone:  K(trade) on its own.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .engine import FACTOR_KEY, aggregate, compile_models

log = logging.getLogger(__name__)


@dataclass
class MarginalResult:
    incremental: float
    euler: float
    standalone: float    # K(trade) on its own
    base: float          # K(book)


def scaled(trade: pd.DataFrame, k: float) -> pd.DataFrame:
    return trade.assign(amount=trade["amount"] * k)


class MarginalEvaluator:
    """Evaluates many trades against one book.

    The book, together with every factor of the trades passed to :meth:`prepare` (at zero amount, which
    leaves capital unchanged), is aggregated and compiled once per risk class / measure. Each evaluation
    then only re-runs the compiled models of the risk classes the trade touches.
    """

    def __init__(self, book: pd.DataFrame, params: dict[str, Any], size: float = 1.0):
        if size <= 0:
            raise ValueError("size must be positive")
        self.book, self.params, self.size = book, params, size
        self._compile([])

    def _compile(self, trades: list[pd.DataFrame]) -> None:
        zero = [t.assign(amount=0.0) for t in trades if len(t)]
        f = aggregate(pd.concat([self.book, *zero], ignore_index=True) if zero else self.book, self.params)
        self.f = f
        self.cva, self.hdg = f["cva"].to_numpy(float), f["hdg"].to_numpy(float)
        self.models = compile_models(f, self.params)
        self.index = {k: i for i, k in enumerate(map(tuple, f[FACTOR_KEY].to_numpy()))}
        self.local = np.zeros(len(f), dtype=np.int64)   # position within its risk class / measure
        self.base_k, self.d_cva, self.d_hdg = {}, np.zeros(len(f)), np.zeros(len(f))
        for key, (model, idx) in self.models.items():
            self.local[idx] = np.arange(len(idx))
            K, *_, dc, dh = model.evaluate(self.cva[idx], self.hdg[idx], want_grad=True)
            self.base_k[key] = K
            self.d_cva[idx], self.d_hdg[idx] = dc, dh
        self.base = float(sum(self.base_k.values()))

    def prepare(self, trades: list[pd.DataFrame]) -> None:
        """Include every factor of ``trades`` in the compiled models (call once for a batch)."""
        missing = [t for t in trades if len(t) and not self._covered(t)]
        if missing:
            self._compile(missing + getattr(self, "_prepared", []))
            self._prepared = missing + getattr(self, "_prepared", [])

    def _covered(self, trade: pd.DataFrame) -> bool:
        return all(k in self.index for k in self.keys(trade))

    @staticmethod
    def keys(trade: pd.DataFrame) -> list[tuple]:
        return list(map(tuple, trade[FACTOR_KEY].to_numpy()))

    def positions(self, trade: pd.DataFrame) -> np.ndarray:
        """Row positions of the trade's factors in the compiled factor table (compiling them in if needed)."""
        keys = self.keys(trade)
        if any(k not in self.index for k in keys):
            self.prepare([trade])
        return np.fromiter((self.index[k] for k in keys), dtype=np.int64, count=len(keys))

    def evaluate_delta(self, key: tuple[str, str], pos_c: np.ndarray, amt_c: np.ndarray,
                       pos_h: np.ndarray, amt_h: np.ndarray) -> tuple[float, float, float]:
        """(incremental, euler, standalone) of a trade within one risk class / measure, given as CVA and
        hedge amounts at factor positions (see :meth:`positions`)."""
        model, idx = self.models[key]
        dc, dh = np.zeros(len(idx)), np.zeros(len(idx))
        np.add.at(dc, self.local[pos_c], amt_c)
        np.add.at(dh, self.local[pos_h], amt_h)
        k1 = model.evaluate(self.cva[idx] + self.size * dc, self.hdg[idx] + self.size * dh)[0]
        alone = model.evaluate(dc, dh)[0]
        base = self.base_k[key]
        # zero book capital in this class: d K = K(trade) by homogeneity
        euler = alone if base == 0.0 else float(self.d_cva[idx] @ dc + self.d_hdg[idx] @ dh)
        return (k1 - base) / self.size, euler, alone

    def evaluate(self, trade: pd.DataFrame) -> MarginalResult:
        pos = self.positions(trade)
        is_cva = (trade["source"] == "CVA").to_numpy()
        amt = trade["amount"].to_numpy(float)
        cls = list(zip(trade["risk_class"], trade["measure"]))
        inc = euler = alone = 0.0
        for key in set(cls):
            sel = np.array([c == key for c in cls])
            i, e, a = self.evaluate_delta(key, pos[sel & is_cva], amt[sel & is_cva], pos[sel & ~is_cva], amt[sel & ~is_cva])
            inc, euler, alone = inc + i, euler + e, alone + a
        return MarginalResult(incremental=inc, euler=euler, standalone=alone, base=self.base)


def marginal(book: pd.DataFrame, trade: pd.DataFrame, params: dict[str, Any], size: float = 1.0) -> MarginalResult:
    ev = MarginalEvaluator(book, params, size)
    if ev.base == 0.0:
        log.warning("book capital is zero: Euler marginal equals stand-alone capital")
    return ev.evaluate(trade)
