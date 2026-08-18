"""``python -m blacktop.sim`` — the matched-baseline lift report.

Prints the PRD 4 north star (net dollars per online hour, after true vehicle
cost and after tax reserve) for the incumbent $/mi heuristic and for BLACKTOP
over an identical offer stream, alongside the clairvoyant ceiling so the
capture fraction is visible rather than implied.
"""

from __future__ import annotations

import argparse

from .runner import DEFAULT_ORACLE_GRID, oracle_frontier, run_season

BAR = "─" * 78


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m blacktop.sim",
                                 description="BLACKTOP matched-baseline lift report")
    ap.add_argument("--days", type=int, default=20, help="measured days (default 20)")
    ap.add_argument("--warmup", type=int, default=5,
                    help="PRD Phase 0 days: old heuristic drives, BLACKTOP logs (default 5)")
    ap.add_argument("--hours", type=float, default=8.0, help="hours per shift (default 8)")
    ap.add_argument("--seed", type=int, default=20260818)
    ap.add_argument("--no-oracle", action="store_true",
                    help="skip the clairvoyant ceiling sweep (much faster)")
    args = ap.parse_args(argv)

    overall, by_zone, policy = run_season(days=args.days, warmup_days=args.warmup,
                                          hours_per_shift=args.hours, seed=args.seed)
    frontier = ({} if args.no_oracle else
                oracle_frontier(days=args.days, warmup_days=args.warmup,
                                hours_per_shift=args.hours, seed=args.seed,
                                grid=DEFAULT_ORACLE_GRID))

    print(f"\nBLACKTOP — matched-baseline shift simulation")
    print(f"{args.days} measured days x {args.hours:.0f}h x 3 zones, "
          f"after {args.warmup} Phase 0 warm-up days.  seed={args.seed}")
    print(BAR)
    head = f"{'zone':<26} {'baseline':>9} {'BLACKTOP':>9} {'lift':>8}"
    if frontier:
        head += f" {'ceiling':>9} {'capture':>8}"
    print(head)
    print(BAR)
    for zk, comp in by_zone.items():
        b, t = comp.baseline.net_hourly, comp.blacktop.net_hourly
        row = f"{comp.zone_label:<26} {b:>8.2f} {t:>9.2f} {comp.net_hourly_lift:>+8.1%}"
        if zk in frontier:
            w, res = frontier[zk]
            cap = (t - b) / (res.net_hourly - b) if res.net_hourly > b else float("nan")
            row += f" {res.net_hourly:>9.2f} {cap:>8.0%}"
        print(row)
        print(f"{'':<26} {'AR':>9} {comp.baseline.acceptance_rate:>8.0%} "
              f"-> {comp.blacktop.acceptance_rate:.0%}"
              f"   util {comp.baseline.utilization:.0%} -> {comp.blacktop.utilization:.0%}")
    print(BAR)
    b, t = overall.baseline, overall.blacktop
    print(f"{'OVERALL net $/online-hour':<26} {b.net_hourly:>8.2f} {t.net_hourly:>9.2f} "
          f"{overall.net_hourly_lift:>+8.1%}")
    print(f"{'  gross $/online-hour':<26} {b.gross_hourly:>8.2f} {t.gross_hourly:>9.2f} "
          f"{overall.gross_hourly_lift:>+8.1%}")
    print(f"{'  acceptance rate':<26} {b.acceptance_rate:>8.0%} {t.acceptance_rate:>9.0%}")
    print(f"{'  utilization':<26} {b.utilization:>8.0%} {t.utilization:>9.0%}")
    print(f"{'  deliveries':<26} {b.accepted:>8d} {t.accepted:>9d}")
    print(BAR)

    print("\nPRD 4 targets")
    gate = 0.15
    status = "MET" if overall.net_hourly_lift >= 0.20 else (
        "Phase 3 gate met" if overall.net_hourly_lift >= gate else "NOT MET")
    print(f"  net $/hr lift vs. baseline   target +20%  "
          f"(Phase 3 gate +15%)   measured {overall.net_hourly_lift:+.1%}  [{status}]")
    print(f"  shielded income (mileage)    ${t.shielded_income:,.0f} of "
          f"${t.gross:,.0f} gross tax-free")
    print(f"  business miles captured      {t.miles:,.0f} mi "
          f"(deduction ${t.deduction:,.0f})")

    tips = getattr(policy, "tips", None)
    if tips is not None:
        cap = tips.cap_detector.cap_value
        print("\nLearned state after the season")
        print(f"  display cap detected         "
              f"{('$%.2f' % cap) if cap else 'not yet — needs more repeats'}")
        print(f"  tip labels observed          {tips.label_count}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
