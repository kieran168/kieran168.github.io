#!/usr/bin/env python3
"""
Fetch NJ Transit rail on-time performance data.

NJ Transit publishes OTP as static CSVs under Executive Order 80, one file
per line, in two flavours: all causes, and adjusted to remove delays
attributed to Amtrak. The filenames never change -- new months are appended
as rows -- so a scheduled fetch is all that's needed to stay current.

Their file server is unreliable. It intermittently returns 404 for files
that definitely exist, and a different handful fails on every run. So this
script MERGES into the existing otp.json rather than rebuilding it from
scratch. A file that fails to download keeps its last good copy, marked
with the date it was last confirmed, instead of vanishing from the site.

That distinction matters: rebuilding meant one bad Monday could silently
delete a line's entire history. Merging means a bad Monday costs freshness,
never existence.

Usage
-----
    python3 fetch_njt.py --inspect
        Downloads everything and prints each file's header row, row count,
        and first two data rows. Writes nothing.

    python3 fetch_njt.py
        Downloads everything and merges it into OUT_PATH.

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


def fetch(url: str, attempts: int = 5, backoff: float = 3.0) -> str:
    """Download a URL and return its body as text.

    Retries with a widening gap -- 3s, 6s, 9s, 12s, so half a minute in
    total before giving up. Observed 404s have survived shorter retries
    than this, which is why the merge below exists as a second line of
    defence rather than relying on retries alone.
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


# Keys this script adds to each row. Ignored when judging whether a row is
# a separator, so the check works on raw CSV rows and on records already
# written to otp.json by an earlier run.
META_KEYS = ("line_code", "line", "version")


def is_separator(row: dict) -> bool:
    """True for the dashed rule rows NJ Transit puts under some headers.

    They look like {"YEAR": "----------", "MONTH": "---------------", ...}
    and are not data. The page already filters them out client-side; this
    keeps them out of the file in the first place.
    """
    values = [v for k, v in row.items() if v and k not in META_KEYS]
    return bool(values) and all(set(str(v)) == {"-"} for v in values)


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
        if any(cleaned.values()) and not is_separator(cleaned):
            rows.append(cleaned)
    return rows


def load_previous(path: str) -> dict:
    """Read the otp.json already on disk, or an empty shell if there isn't one.

    Anything unreadable is treated as absent. A corrupt file should not stop
    a fresh fetch from succeeding -- it just means nothing to carry forward.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"records": [], "sources": {}}

    if not isinstance(payload, dict):
        return {"records": [], "sources": {}}
    payload.setdefault("records", [])
    payload.setdefault("sources", {})
    return payload


def group_by_pair(records: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Bucket a flat record list into {(line_code, version): [rows]}."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for record in records:
        key = (record.get("line_code", ""), record.get("version", ""))
        grouped.setdefault(key, []).append(record)
    return grouped


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
    """Download everything and merge it into the consolidated JSON file."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    previous = load_previous(OUT_PATH)
    previous_pairs = group_by_pair(previous["records"])
    previous_sources = previous["sources"]

    records: list[dict] = []
    sources: dict[str, dict] = {}
    failures: list[str] = []   # raw errors, as before
    stale: list[str] = []      # served from a previous run
    missing: list[str] = []    # failed and nothing to fall back on
    fresh_count = 0

    # Iterating LINES x VERSIONS in declared order keeps record order stable
    # between runs, which keeps the git diff small and readable.
    #
    # Note: a pair dropped from LINES also drops out of the file. That is
    # deliberate -- removing a line from the dict should remove it from the
    # site, not leave a fossil behind.
    for line_code, line_name in LINES.items():
        code = line_code or "SYSTEM"
        for version_name, suffix in VERSIONS.items():
            url = build_url(line_code, suffix)
            label = f"{line_name}/{version_name}"
            key = (code, version_name)
            was = previous_sources.get(code, {}).get(version_name, {})

            error = None
            rows = []
            try:
                rows = parse(fetch(url))
                if not rows:
                    error = "parsed zero rows"
            except Exception as exc:  # noqa: BLE001
                error = str(exc)

            if not error:
                # Fresh download wins outright for this pair.
                for row in rows:
                    records.append(
                        {
                            "line_code": code,
                            "line": line_name,
                            "version": version_name,
                            **row,
                        }
                    )
                sources.setdefault(code, {})[version_name] = {
                    "status": "fresh",
                    "rows": len(rows),
                    "last_success": now,
                    "checked_at": now,
                }
                fresh_count += 1
                continue

            # Download failed. Fall back to whatever we had last time.
            failures.append(f"{label}: {error}")
            # Records written by an older version of this script may still
            # contain separator rows, so filter on the way through.
            carried = [r for r in previous_pairs.get(key, [])
                       if not is_separator(r)]

            if carried:
                records.extend(carried)
                last_success = was.get("last_success")
                sources.setdefault(code, {})[version_name] = {
                    "status": "carried",
                    "rows": len(carried),
                    "last_success": last_success,
                    "checked_at": now,
                    "last_error": error,
                }
                seen = last_success[:10] if last_success else "an earlier run"
                stale.append(f"{label}: kept the copy from {seen}")
            else:
                sources.setdefault(code, {})[version_name] = {
                    "status": "missing",
                    "rows": 0,
                    "last_success": was.get("last_success"),
                    "checked_at": now,
                    "last_error": error,
                }
                missing.append(f"{label}: {error}")

    if not records:
        # Nothing downloaded and nothing to fall back on. Leave the existing
        # file untouched rather than writing an empty one over it.
        print("Aborting -- no records, and nothing on disk to carry forward.",
              file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    payload = {
        "generated_at": now,
        "source": "https://www.njtransit.com/performance-data-download",
        "note": (
            "NJ TRANSIT counts a train as on time if it operates within "
            "6 minutes of its published schedule. Published monthly under "
            "NJ Executive Order 80."
        ),
        "lines": {code or "SYSTEM": name for code, name in LINES.items()},
        "unavailable": failures,
        "stale": stale,
        "missing": missing,
        "sources": sources,
        "record_count": len(records),
        "records": records,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
        handle.write("\n")

    total_pairs = len(LINES) * len(VERSIONS)
    print(f"wrote {OUT_PATH} -- {len(records)} records")
    print(f"  {fresh_count}/{total_pairs} files downloaded fresh")

    if stale:
        print(f"  {len(stale)} served from a previous run:")
        for item in stale:
            print(f"    {item}")
    if missing:
        print(f"  {len(missing)} unavailable with no fallback:")
        for item in missing:
            print(f"    {item}")

    # Exit 0 even with stale or missing files. A non-zero exit would fail the
    # Actions job before the commit step, which would throw away a merge that
    # is strictly better than what is already published.
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
