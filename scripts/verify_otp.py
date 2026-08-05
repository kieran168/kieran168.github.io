#!/usr/bin/env python3
"""
Check the on-time performance figures independently.

This script exists so you never have to take a number on trust. It recomputes
every figure that has been claimed, from your own otp.json, and fails loudly
if any of them is wrong. It also writes a CSV of every intermediate number so
you can rebuild the whole thing in a spreadsheet and check the arithmetic
yourself.

Three levels of checking, from cheapest to most thorough:

  python3 verify_otp.py
      Recompute every claimed figure from trains/data/otp.json.
      Prints PASS/FAIL per claim and writes verify_by_year.csv.
      Catches: arithmetic errors, wrong method, misread data.

  python3 verify_otp.py --against-source
      Re-download the CSVs straight from NJ Transit and diff them against
      what is stored in otp.json, row by row.
      Catches: a broken fetcher, stale data, corrupted storage.

  python3 verify_otp.py --show RARV 2026
      Dump the raw monthly rows behind a single line-year so you can open
      NJ Transit's CSV yourself and check them by eye.
      Catches: everything above, but by hand.

Standard library only.
"""

import argparse
import csv
import io
import json
import sys
import urllib.request
from collections import defaultdict

DATA_PATH = "trains/data/otp.json"
CSV_OUT = "verify_by_year.csv"

MONTHS = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
          "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]

# January-June. Every year is cut to the same six months so that 2026, which
# only runs through June, is compared like for like against full years.
H1 = set(range(0, 6))

NAMES = {
    "SYSTEM": "System-wide", "ACRL": "Atlantic City", "MNBN": "Main-Bergen",
    "BNTN": "Montclair-Boonton", "MNE": "Morris & Essex",
    "NEC": "Northeast Corridor", "NJCL": "North Jersey Coast",
    "PASC": "Pascack Valley", "RARV": "Raritan Valley",
}

BASE_URL = "https://content.njtransit.com/sites/default/files/OTP/datafiles"
FILE_CODES = {"SYSTEM": "", "ACRL": "ACRL_", "MNBN": "MNBN_", "BNTN": "BNTN_",
              "MNE": "MNE_", "NEC": "NEC_", "NJCL": "NJCL_", "PASC": "PASC_",
              "RARV": "RARV_"}
SUFFIX = {"all_causes": "", "amtrak_adjusted": "_AMTRAK_ADJUSTED"}

# ---------------------------------------------------------------------------
# Every figure that has been claimed, so this script can mark its own homework.
# pct = January-June, weighted by trains run. Tolerance is 0.05 because the
# published figures are quoted to one decimal place.
# ---------------------------------------------------------------------------
CLAIMS = {
    # (line_code, year): claimed percentage
    ("SYSTEM", 2026): 88.6,
    ("NJCL", 2026): 87.4,
    ("NEC", 2026): 91.5,
    ("RARV", 2026): 91.7,
    ("RARV", 2025): 91.5,
    ("MNBN", 2026): 96.3,
    ("ACRL", 2026): 90.3,
    ("BNTN", 2026): 90.1,
    ("MNE", 2026): 88.5,
    ("PASC", 2026): 94.6,
}

# Claimed 2017-2019 pre-COVID baselines, same method.
BASELINE_CLAIMS = {
    "SYSTEM": 90.9, "NJCL": 94.5, "NEC": 95.7, "RARV": 94.9,
    "PASC": 95.4, "MNE": 89.0, "BNTN": 89.6, "ACRL": 89.0, "MNBN": 94.8,
}

TOLERANCE = 0.05


def load(path=DATA_PATH):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        sys.exit(f"Could not find {path}. Run this from the repo root.")


def usable_rows(payload):
    """Real monthly observations only -- no header rules, no blank years."""
    out = []
    for r in payload.get("records", []):
        year = str(r.get("YEAR", "")).strip()
        month = str(r.get("MONTH", "")).strip().upper()
        if not year.isdigit() or month not in MONTHS:
            continue
        try:
            count, total = int(r["COUNT"]), int(r["TOTAL"])
        except (KeyError, ValueError, TypeError):
            continue
        if total <= 0:
            continue
        out.append({
            "line_code": r.get("line_code", ""), "version": r.get("version", ""),
            "year": int(year), "month_index": MONTHS.index(month),
            "month": month.title(), "count": count, "total": total,
            "published_pct": r.get("PERCENTAGE", ""),
        })
    return out


def version_for(rows, code):
    """Prefer the all-causes file. Fall back to adjusted only if it is absent.

    This matters: the adjusted file has Amtrak-attributed delays removed, so a
    figure computed from it is more flattering than what riders experienced.
    Anywhere this falls back, the result is a floor, not the real number.
    """
    have = {r["version"] for r in rows if r["line_code"] == code}
    if "all_causes" in have:
        return "all_causes"
    if "amtrak_adjusted" in have:
        return "amtrak_adjusted"
    return None


def aggregate(rows, code, version, years, months=H1):
    """Weighted on-time percentage: sum(on-time trains) / sum(trains run).

    NOT the mean of the monthly percentages. Those two differ whenever months
    have different train volumes, and a February with fewer trains should not
    count as much as a busy March.
    """
    on_time = run = 0
    for r in rows:
        if r["line_code"] != code or r["version"] != version:
            continue
        if r["year"] not in years or r["month_index"] not in months:
            continue
        on_time += r["count"]
        run += r["total"]
    return (100.0 * on_time / run, on_time, run) if run else (None, 0, 0)


def cmd_verify(payload):
    rows = usable_rows(payload)
    print(f"Reading {DATA_PATH}")
    print(f"  generated_at : {payload.get('generated_at')}")
    print(f"  records      : {len(payload.get('records', []))} raw, "
          f"{len(rows)} usable monthly observations")

    missing = [c for c in NAMES if version_for(rows, c) == "amtrak_adjusted"]
    if missing:
        print("\n  !! ALL-CAUSES FILE MISSING for: "
              + ", ".join(NAMES[c] for c in missing))
        print("     Figures for these lines use the Amtrak-adjusted file, which")
        print("     already has Amtrak delays removed. They are BEST CASE.")
        print("     The real decline on these lines is at least this steep.")

    all_years = sorted({r["year"] for r in rows})
    print(f"\n  years present: {all_years[0]}-{all_years[-1]}")

    # ---- claimed single-year figures -------------------------------------
    print("\n" + "=" * 66)
    print("CLAIMED FIGURES (Jan-Jun, weighted by trains run)")
    print("=" * 66)
    print(f"{'line':22s}{'year':>6s}{'claimed':>9s}{'actual':>9s}{'diff':>8s}  result")
    failures = 0
    for (code, year), claimed in sorted(CLAIMS.items(), key=lambda x: (x[0][0], x[0][1])):
        version = version_for(rows, code)
        actual, on_time, run = aggregate(rows, code, version, {year})
        if actual is None:
            print(f"{NAMES[code]:22s}{year:6d}{claimed:9.1f}{'--':>9s}{'--':>8s}  NO DATA")
            failures += 1
            continue
        diff = actual - claimed
        ok = abs(diff) <= TOLERANCE
        failures += 0 if ok else 1
        print(f"{NAMES[code]:22s}{year:6d}{claimed:9.1f}{actual:9.1f}"
              f"{diff:+8.2f}  {'PASS' if ok else 'FAIL'}")

    # ---- claimed baselines and changes ------------------------------------
    print("\n" + "=" * 66)
    print("PRE-COVID BASELINE 2017-2019 vs 2026 (Jan-Jun, weighted)")
    print("=" * 66)
    print(f"{'line':22s}{'claimed':>9s}{'actual':>9s}{'2026':>8s}{'change':>9s}  result")
    for code, claimed in sorted(BASELINE_CLAIMS.items()):
        version = version_for(rows, code)
        base, _, _ = aggregate(rows, code, version, {2017, 2018, 2019})
        now, _, _ = aggregate(rows, code, version, {2026})
        if base is None or now is None:
            print(f"{NAMES[code]:22s}{claimed:9.1f}{'--':>9s}{'--':>8s}{'--':>9s}  NO DATA")
            failures += 1
            continue
        ok = abs(base - claimed) <= TOLERANCE
        failures += 0 if ok else 1
        print(f"{NAMES[code]:22s}{claimed:9.1f}{base:9.1f}{now:8.1f}{now - base:+9.1f}"
              f"  {'PASS' if ok else 'FAIL'}")

    # ---- independent check: is 2026 really the worst on record? -----------
    print("\n" + "=" * 66)
    print("RANK OF 2026 AMONG ALL YEARS (1 = worst on record, Jan-Jun)")
    print("=" * 66)
    for code in ["SYSTEM", "NEC", "RARV", "NJCL", "MNBN", "MNE", "BNTN",
                 "PASC", "ACRL"]:
        version = version_for(rows, code)
        if not version:
            continue
        per_year = {}
        for y in all_years:
            pct, _, _ = aggregate(rows, code, version, {y})
            if pct is not None:
                per_year[y] = pct
        if 2026 not in per_year:
            continue
        order = sorted(per_year, key=lambda y: per_year[y])
        rank = order.index(2026) + 1
        worst_year = order[0]
        note = "worst on record" if rank == 1 else f"worst was {worst_year} ({per_year[worst_year]:.1f}%)"
        print(f"  {NAMES[code]:22s} rank {rank}/{len(per_year)}   {note}")

    # ---- spreadsheet dump -------------------------------------------------
    with open(CSV_OUT, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["line_code", "line", "version", "year",
                         "months_included", "on_time_trains", "trains_run",
                         "pct_weighted"])
        for code in NAMES:
            version = version_for(rows, code)
            if not version:
                continue
            for y in all_years:
                pct, on_time, run = aggregate(rows, code, version, {y})
                if pct is None:
                    continue
                months = sorted({r["month_index"] for r in rows
                                 if r["line_code"] == code and r["version"] == version
                                 and r["year"] == y and r["month_index"] in H1})
                writer.writerow([code, NAMES[code], version, y, len(months),
                                 on_time, run, round(pct, 4)])
    print(f"\nWrote {CSV_OUT} -- open it in a spreadsheet and check the division "
          f"yourself.\n  pct_weighted should equal on_time_trains / trains_run * 100.")

    print("\n" + "=" * 66)
    if failures:
        print(f"{failures} CHECK(S) FAILED -- do not publish these figures.")
    else:
        print("All checks passed.")
    print("=" * 66)
    return 1 if failures else 0


def cmd_against_source(payload):
    """Re-download from NJ Transit and diff against what is stored."""
    stored = defaultdict(dict)
    for r in usable_rows(payload):
        stored[(r["line_code"], r["version"])][(r["year"], r["month_index"])] = \
            (r["count"], r["total"])

    headers = {"User-Agent": "Mozilla/5.0 (compatible; njt-otp-verify/1.0)"}
    total_mismatch = 0

    for code, prefix in FILE_CODES.items():
        for version, suffix in SUFFIX.items():
            key = (code, version)
            if key not in stored:
                continue
            url = f"{BASE_URL}/RAIL_{prefix}OTP_DATA{suffix}.csv"
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=30) as response:
                    text = response.read().decode("utf-8-sig")
            except Exception as exc:  # noqa: BLE001
                print(f"  {NAMES[code]:22s} {version:16s} could not fetch: {exc}")
                continue

            live = {}
            for row in csv.DictReader(io.StringIO(text)):
                row = {(k or "").strip(): (v or "").strip()
                       for k, v in row.items() if k is not None}
                year, month = row.get("YEAR", ""), row.get("MONTH", "").upper()
                if not year.isdigit() or month not in MONTHS:
                    continue
                try:
                    live[(int(year), MONTHS.index(month))] = \
                        (int(row["COUNT"]), int(row["TOTAL"]))
                except (KeyError, ValueError):
                    continue

            ours = stored[key]
            only_live = set(live) - set(ours)
            only_ours = set(ours) - set(live)
            differ = [k for k in set(live) & set(ours) if live[k] != ours[k]]
            total_mismatch += len(only_ours) + len(differ)

            status = "match" if not (only_ours or differ) else "MISMATCH"
            print(f"  {NAMES[code]:22s} {version:16s} {len(ours):4d} stored  "
                  f"{len(live):4d} live   {status}")
            if only_live:
                newest = sorted(only_live)[-1]
                print(f"      {len(only_live)} month(s) published but not stored "
                      f"(newest {MONTHS[newest[1]].title()} {newest[0]}) "
                      f"-- fetcher has not run since")
            for k in sorted(differ)[:5]:
                print(f"      {MONTHS[k[1]].title()} {k[0]}: "
                      f"stored {ours[k]} vs live {live[k]}")
            for k in sorted(only_ours)[:5]:
                print(f"      {MONTHS[k[1]].title()} {k[0]}: stored but NOT in "
                      f"NJ Transit's file")

    print()
    if total_mismatch:
        print(f"{total_mismatch} discrepancy/discrepancies against the published "
              f"source. Investigate before publishing.")
    else:
        print("Everything stored matches what NJ Transit is publishing right now.")
    return 1 if total_mismatch else 0


def cmd_show(payload, code, year):
    """Dump the raw monthly rows behind one line-year, for hand-checking."""
    rows = usable_rows(payload)
    code = code.upper()
    if code not in NAMES:
        sys.exit(f"Unknown line code {code}. One of: {', '.join(NAMES)}")
    version = version_for(rows, code)
    picked = sorted([r for r in rows if r["line_code"] == code
                     and r["version"] == version and r["year"] == year],
                    key=lambda r: r["month_index"])
    if not picked:
        sys.exit(f"No rows for {NAMES[code]} in {year}.")

    prefix = FILE_CODES[code]
    print(f"{NAMES[code]} -- {year} -- {version}")
    print(f"Source: {BASE_URL}/RAIL_{prefix}OTP_DATA{SUFFIX[version]}.csv")
    print("Open that CSV and check these rows line by line.\n")
    print(f"{'month':12s}{'on time':>10s}{'run':>9s}{'computed':>10s}{'published':>11s}")
    on_time = run = 0
    for r in picked:
        pct = 100.0 * r["count"] / r["total"]
        print(f"{r['month']:12s}{r['count']:10d}{r['total']:9d}{pct:9.1f}%"
              f"{str(r['published_pct']):>11s}")
        if r["month_index"] in H1:
            on_time += r["count"]
            run += r["total"]
    print(f"\nJan-Jun weighted: {on_time} / {run} = {100.0 * on_time / run:.2f}%")
    print("This is the figure used in the year-by-year table.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--against-source", action="store_true",
                        help="re-download from NJ Transit and diff row by row")
    parser.add_argument("--show", nargs=2, metavar=("LINE", "YEAR"),
                        help="dump raw monthly rows, e.g. --show RARV 2026")
    args = parser.parse_args()

    payload = load()
    if args.against_source:
        return cmd_against_source(payload)
    if args.show:
        return cmd_show(payload, args.show[0], int(args.show[1]))
    return cmd_verify(payload)


if __name__ == "__main__":
    raise SystemExit(main())
