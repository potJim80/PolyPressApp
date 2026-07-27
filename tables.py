"""Test tables for the polynomial-compression experiment.

Each table is a list of already-formatted cell strings, because that is what a
real data file contains. Compressing the *printed* table is the honest target:
it is what gzip sees too.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import List


@dataclass
class Table:
    name: str
    note: str
    columns: List[str]
    rows: List[List[str]]

    def to_csv(self) -> str:
        lines = [",".join(self.columns)]
        lines.extend(",".join(row) for row in self.rows)
        return "\n".join(lines) + "\n"

    @property
    def shape(self):
        return len(self.rows), len(self.columns)


def _fmt(value: float, decimals: int) -> str:
    return "{:.{d}f}".format(value, d=decimals)


def saturated_steam() -> Table:
    """Thermodynamic-style table. Smooth analytic columns, fixed decimals.

    Vapour pressure from the Antoine equation, enthalpy/entropy from smooth
    empirical fits. This is the shape of table the idea should do best on.
    """
    rows = []
    for i in range(400):
        t = 5.0 + 0.5 * i  # degC
        # Antoine (water, mmHg -> kPa)
        p = 10.0 ** (8.07131 - 1730.63 / (233.426 + t)) * 0.1333224
        h_l = 4.1868 * t - 0.0012 * t * t
        h_v = 2500.9 + 1.82 * t - 0.0015 * t * t
        s_v = 9.1555 - 0.0281 * t + 3.1e-5 * t * t
        rows.append([
            _fmt(t, 1),
            _fmt(p, 4),
            _fmt(h_l, 2),
            _fmt(h_v, 2),
            _fmt(s_v, 5),
        ])
    return Table(
        name="saturated_steam",
        note="smooth analytic columns (Antoine eqn + polynomial fits)",
        columns=["T_C", "P_kPa", "h_liq", "h_vap", "s_vap"],
        rows=rows,
    )


def blackbody() -> Table:
    """Planck's law sampled on a wavelength grid, several temperatures."""
    h, c, kB = 6.62607015e-34, 2.99792458e8, 1.380649e-23

    def planck(lam_nm: float, temp: float) -> float:
        lam = lam_nm * 1e-9
        num = 2.0 * h * c * c / lam ** 5
        return num / (math.exp(h * c / (lam * kB * temp)) - 1.0)

    rows = []
    for i in range(500):
        lam = 200.0 + 4.0 * i
        cells = [_fmt(lam, 1)]
        for temp in (3000.0, 4500.0, 6000.0):
            # scale to a human-sized number, 5 decimals
            cells.append(_fmt(planck(lam, temp) / 1e12, 5))
        rows.append(cells)
    return Table(
        name="blackbody",
        note="Planck spectral radiance, three temperatures",
        columns=["lambda_nm", "B_3000K", "B_4500K", "B_6000K"],
        rows=rows,
    )


def noisy_measurements(seed: int = 7) -> Table:
    """Smooth trend plus instrument noise in the last digit or two."""
    rng = random.Random(seed)
    rows = []
    for i in range(600):
        x = 0.05 * i
        signal = 12.0 + 3.4 * math.sin(0.11 * x) + 0.02 * x * x
        drift = 101.325 - 0.004 * x
        rows.append([
            _fmt(x, 2),
            _fmt(signal + rng.gauss(0.0, 0.004), 3),
            _fmt(drift + rng.gauss(0.0, 0.02), 3),
            _fmt(rng.gauss(0.0, 1.0) * 0.5 + 50.0, 2),
        ])
    return Table(
        name="noisy_measurements",
        note="smooth trend + gaussian noise, one pure-noise column",
        columns=["time_s", "sensor_A", "pressure_kPa", "noise_only"],
        rows=rows,
    )


def random_control(seed: int = 11) -> Table:
    """Incompressible control. Nothing should beat raw here."""
    rng = random.Random(seed)
    rows = []
    for _ in range(500):
        rows.append([str(rng.randrange(0, 10 ** 8)) for _ in range(4)])
    return Table(
        name="random_control",
        note="uniform random integers -- the no-free-lunch control",
        columns=["r1", "r2", "r3", "r4"],
        rows=rows,
    )


def all_tables() -> List[Table]:
    return [saturated_steam(), blackbody(), noisy_measurements(), random_control()]
