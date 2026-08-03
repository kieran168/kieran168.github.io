#!/usr/bin/env python3
"""
Fetch NJ Transit rail on-time performance data.

NJ Transit publishes OTP as static CSVs under Executive Order 80, one file
per line, in two flavours: all causes, and adjusted to remove delays
attributed to Amtrak. The filenames never change -- new months are appended
as rows -- so a scheduled fetch is all that's needed to stay current.

Usage
-----
    python3 fetch_njt.py --inspect
        Downloads everything and prints each file's header row, row count,
        and first two data rows. Writes nothing. Run this first.

    python3 fetch_njt.py
        Downloads everything and writes OUT_PATH as JSON.

Standard library only -- no pip install, works on a bare Actions runner.
"""

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = "https://content.njtransit.com/sites/default/files/OTP/datafiles"

# NJ Transit's file code -> human readable line name.
# "SYSTEM" is the agency-wide rollup, not a line.
LINES = {
    "": "System-wide",
    "ACRL": "Atlantic City",
    "MNBN": "Main-Bergen County",
    "BNTN": "Montclair-Boonton",
    "MNE": "Morris & Essex",
    "NEC": "Northeast Corridor",
    "NJCL": "North Jersey Coast",
    "PASC": "Pascack Valley",
    "RARV": "Raritan Valley",
}

# The two published versions of every OTP file.
VERSIONS = {
    "all_causes": "",
    "amtrak_adjusted": "_AMTRAK_ADJUSTED",
}

OUT_PATH = "trains/data/otp.json"

# NJ Transit's file server rejects the default urllib user agent.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; njt-otp-fetch/1.0; +https://kieranyuen.com)"
}

TIMEOUT = 30


def build_url(line_code: str, version_suffix: str) -> str:
    """Assemble the CSV URL for one line + version combination."""
    middle = f"{line_code}_" if line_code else ""
    return f"{BASE}/RAIL_{middle}OTP_DATA{version_suffix}.csv"


def fetch(url: str, attempts: int = 4, backoff: float = 2.0) -> str:
    """Download a URL and return its body as text.

    NJ Transit's file server intermittently returns 404 for files that do
    exist -- a different handful fails on every run -- so a single attempt
    silently loses whole lines. Retry before believing a failure.
    """
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
            # These files have been seen with a UTF-8 BOM; utf-8-sig strips it
            # if present and behaves like plain utf-8 if not.
            return raw.decode("utf-8-sig")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(backoff * (attempt + 1))

    raise last_error  # type: ignore[misc]


def parse(text: str) -> list[dict]:
    """Parse CSV text into a list of dicts keyed by header name."""
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        # Drop fully blank rows and strip whitespace from every value.
        cleaned = {
            (k or "").strip(): (v or "").strip()
            for k, v in row.items()
            if k is not None
        }
        if any(cleaned.values()):
            rows.append(cleaned)
    return rows


def inspect() -> int:
    """Print the shape of every file so we can design the transform."""
    failures = 0
    for line_code, line_name in LINES.items():
        for version_name, suffix in VERSIONS.items():
            url = build_url(line_code, suffix)
            label = f"{line_name} / {version_name}"
            print("=" * 70)
            print(label)
            print(url)
            try:
                text = fetch(url)
            except urllib.error.HTTPError as exc:
                print(f"  !! HTTP {exc.code}")
                failures += 1
                continue
            except Exception as exc:  # noqa: BLE001
                print(f"  !! {type(exc).__name__}: {exc}")
                failures += 1
                continue

            rows = parse(text)
            if not rows:
                print("  !! parsed zero rows")
                failures += 1
                continue

            print(f"  columns ({len(rows[0])}): {list(rows[0].keys())}")
            print(f"  rows: {len(rows)}")
            for sample in rows[:2]:
                print(f"  first: {sample}")
            print(f"  last:  {rows[-1]}")
    print("=" * 70)
    print(f"done -- {failures} failure(s)")
    return 1 if failures else 0


def build() -> int:
    """Download everything and write the consolidated JSON file."""
    records = []
    failures = []

    for line_code, line_name in LINES.items():
        for version_name, suffix in VERSIONS.items():
            url = build_url(line_code, suffix)
            try:
                rows = parse(fetch(url))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{line_name}/{version_name}: {exc}")
                continue

            for row in rows:
                records.append(
                    {
                        "line_code": line_code or "SYSTEM",
                        "line": line_name,
                        "version": version_name,
                        **row,
                    }
                )

    if not records:
        # Nothing at all came back -- something is genuinely wrong.
        print("Aborting -- every file failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    if failures:
        # Some of NJ Transit's own published links 404. Note it and carry on
        # rather than discarding the files that did work.
        print(f"Warning -- {len(failures)} file(s) unavailable:")
        for failure in failures:
            print(f"  {failure}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "https://www.njtransit.com/performance-data-download",
        "note": (
            "NJ TRANSIT counts a train as on time if it operates within "
            "6 minutes of its published schedule. Published monthly under "
            "NJ Executive Order 80."
        ),
        "lines": {code or "SYSTEM": name for code, name in LINES.items()},
        "unavailable": failures,
        "record_count": len(records),
        "records": records,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
        handle.write("\n")

    print(f"wrote {OUT_PATH} -- {len(records)} records")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="print each file's columns and row count instead of writing output",
    )
    args = parser.parse_args()
    return inspect() if args.inspect else build()


if __name__ == "__main__":
    raise SystemExit(main())
