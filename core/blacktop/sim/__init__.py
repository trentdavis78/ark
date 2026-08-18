"""Shift simulator — the falsifiability harness for the BLACKTOP thesis.

PRD 11 lists "the lift doesn't materialize" as a live risk and PRD 10 gives
Phase 3 a hard exit criterion of >=15% measured lift. This package builds the
measurement first: a generative North/Central NJ market (``world``), the
incumbent and product policies (``policies``), and a matched-baseline runner
(``runner``).

Run it:  python -m blacktop.sim
"""

from .policies import BlacktopPolicy, DollarPerMilePolicy
from .runner import Comparison, ShiftResult, run_season, run_shift
from .world import Market, SimOffer, ZoneProfile

__all__ = ["BlacktopPolicy", "DollarPerMilePolicy", "Comparison", "ShiftResult",
           "run_season", "run_shift", "Market", "SimOffer", "ZoneProfile"]
