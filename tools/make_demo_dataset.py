#!/usr/bin/env python3
# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""Generate a synthetic demonstration dataset.

Purpose: let anyone try the platform, or be shown it, without uploading real
participant data. Every value here is generated from a fixed random seed — no
measurement from any monitored home is used, reproduced, or derived from.

The output mimics the column layout and 1 Hz cadence of a Convergence
Instruments NSRT_W_mk4 export so it exercises the same ingestion path as a real
file, and is shaped to exercise the parts of the platform worth demonstrating:

  * a clear diurnal cycle, so the hourly profile, heatmap and radar are legible
  * short transient events above the background, so L10/L90 separate and the
    top-events table has something to show
  * one deliberate two-hour recording gap, so gap detection, the completeness
    figure and the broken chart traces all demonstrate themselves
  * levels that land modestly above the WHO Lden and Lnight guidelines, so the
    compliance path renders a real verdict rather than an all-clear

Nothing here should be read as representative of any real location.

Usage
-----
    python tools/make_demo_dataset.py                     # 4 days, parquet + csv
    python tools/make_demo_dataset.py --days 7 --outdir .
"""
from __future__ import annotations

import argparse
import os

import numpy as np
import pandas as pd

SEED = 20260101
FS_SECONDS = 1  # 1 Hz, matching the real loggers


def build(days: int = 4, start: str = "2026-04-06 00:00:00") -> pd.DataFrame:
    """Build the synthetic 1 Hz record."""
    rng = np.random.default_rng(SEED)
    n = int(days * 24 * 3600 / FS_SECONDS)
    ts = pd.date_range(start, periods=n, freq=f"{FS_SECONDS}s")
    hour = ts.hour.to_numpy() + ts.minute.to_numpy() / 60.0

    # Diurnal shape: quiet overnight, rising through the morning, easing after
    # the evening peak. Built from two smooth harmonics rather than a step so
    # the hourly profile looks like a measurement, not a square wave.
    base = (
        48.5
        - 5.0 * np.cos((hour - 4.0) / 24.0 * 2 * np.pi)
        - 1.6 * np.cos((hour - 8.0) / 12.0 * 2 * np.pi)
    )

    # Weekends about 2 dB quieter by day, unchanged at night.
    is_weekend = ts.dayofweek.to_numpy() >= 5
    daytime = (hour >= 7) & (hour < 22)
    base = base - np.where(is_weekend & daytime, 2.0, 0.0)

    # Slow drift (weather, distant activity) plus second-to-second variation.
    drift = 1.1 * np.sin(np.linspace(0, 6 * np.pi, n) + 0.7)
    leq = base + drift + rng.normal(0.0, 1.5, n)

    # Transient events: short passages a few dB up, more frequent by day. These
    # are what separate L10 from L90 and give the events table content.
    n_events = int(days * 90)
    for _ in range(n_events):
        i = int(rng.integers(0, n - 400))
        if not daytime[i] and rng.random() < 0.65:
            continue  # most events happen in daylight hours
        dur = int(rng.integers(8, 90))
        amp = float(rng.uniform(4.0, 16.0))
        shape = np.hanning(dur * 2)[:dur]
        leq[i:i + dur] += amp * shape

    # A handful of louder, longer passages so the top-events table is not all
    # near-identical, and so the box plot shows a real tail.
    for _ in range(max(2, days // 2)):
        i = int(rng.integers(0, n - 900))
        dur = int(rng.integers(120, 600))
        leq[i:i + dur] += float(rng.uniform(10.0, 18.0)) * np.hanning(dur * 2)[:dur]

    # Instrument floor: the MK4 does not read below roughly 30 dB(A).
    leq = np.clip(leq, 30.5, None)

    # L-Max and L-Min bracket the interval LEQ, as a real logger reports.
    lmax = leq + np.abs(rng.normal(3.2, 1.4, n))
    lmin = leq - np.abs(rng.normal(2.6, 1.1, n))
    lmin = np.clip(lmin, 30.2, None)

    df = pd.DataFrame({
        "Time (Date hh:mm:ss.ms)": ts,
        " L-Max dB -A ": np.round(lmax, 6),
        " LEQ dB -A ": np.round(leq, 6),
        " L-Min dB -A ": np.round(lmin, 6),
    })

    # One deliberate two-hour outage, so completeness, the gap inventory and the
    # broken chart traces all have something real to report.
    gap_start = ts[0] + pd.Timedelta(days=max(1, days // 2), hours=2)
    gap_end = gap_start + pd.Timedelta(hours=2)
    keep = ~((df["Time (Date hh:mm:ss.ms)"] >= gap_start) &
             (df["Time (Date hh:mm:ss.ms)"] < gap_end))
    return df.loc[keep].reset_index(drop=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=4)
    ap.add_argument("--outdir", default="demo_data")
    ap.add_argument("--name", default="DEMO-SITE-01")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    df = build(args.days)

    written = []
    pq = os.path.join(args.outdir, f"{args.name}.parquet")
    try:
        df.to_parquet(pq, index=False, compression="brotli")
        written.append(pq)
    except Exception as exc:
        print(f"  parquet skipped ({exc})")
    csv = os.path.join(args.outdir, f"{args.name}.csv")
    df.to_csv(csv, index=False)
    written.append(csv)

    leq = df[" LEQ dB -A "].to_numpy()
    energy_mean = 10 * np.log10(np.mean(10 ** (leq / 10)))
    print(f"Synthetic demonstration dataset — {args.name}")
    print(f"  rows          : {len(df):,} at 1 Hz over {args.days} days")
    print(f"  span          : {df.iloc[0, 0]} to {df.iloc[-1, 0]}")
    print(f"  LAeq          : {energy_mean:.1f} dB(A)")
    print(f"  L10 / L50 / L90: {np.percentile(leq, 90):.1f} / "
          f"{np.percentile(leq, 50):.1f} / {np.percentile(leq, 10):.1f} dB(A)")
    print(f"  built-in gap  : 2 hours, to exercise gap detection")
    for p in written:
        print(f"  wrote         : {p}  ({os.path.getsize(p) / 1e6:.1f} MB)")
    print("\nGenerated from a fixed seed. Contains no measured data from any "
          "monitored location, and is not representative of any real site.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
