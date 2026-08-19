#!/usr/bin/env python3
"""Legg månedlige Nordnet-eksporter inn i ett voksende panel.

    python3 fond/ingest.py nedlastinger/fond_*.csv

Panelet (fond/data/snapshots.csv) er én rad per (snapshot-dato, ISIN).
Kjøringen er idempotent: samme fil to ganger gir ingen duplikater, og en
snapshot-dato som allerede finnes blir overskrevet av den nye lesningen
i stedet for lagt til.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nordnet import FIELDNAMES, load_overrides, parse_file, snapshot_records  # noqa: E402

DEFAULT_PANEL = Path(__file__).resolve().parent / "data" / "snapshots.csv"
DEFAULT_OVERRIDES = Path(__file__).resolve().parent / "overrides.csv"


def load_panel(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_panel(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Sorter kronologisk, så navn -- gjør diffene i git lesbare.
    rows.sort(key=lambda r: (r["snapshot_dato"], r.get("navn", ""), r.get("isin", "")))
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("filer", nargs="+", type=Path, help="Nordnet-CSV-eksport(er)")
    ap.add_argument("--panel", type=Path, default=DEFAULT_PANEL, help=f"panelfil (standard: {DEFAULT_PANEL})")
    ap.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES, help="manuell omklassifisering")
    ap.add_argument("--tørrkjør", "--dry-run", dest="tørrkjør", action="store_true", help="vis hva som ville skjedd")
    args = ap.parse_args(argv)

    overrides = load_overrides(args.overrides)
    if overrides:
        print(f"  bruker {len(overrides)} overstyring(er) fra {args.overrides.name}")

    panel = load_panel(args.panel)
    by_date: dict[str, list[dict]] = {}
    for row in panel:
        by_date.setdefault(row["snapshot_dato"], []).append(row)
    before = set(by_date)

    for path in args.filer:
        try:
            snap = parse_file(path, overrides)
        except (OSError, ValueError) as exc:
            print(f"  ! hopper over {path.name}: {exc}", file=sys.stderr)
            continue
        records = snapshot_records(snap)
        stamp = snap.dato.isoformat()
        verb = "erstatter" if stamp in by_date else "legger til"
        by_date[stamp] = records
        print(f"  {verb} {stamp}: {len(records)} fond  ({path.name})")

    rows = [r for stamp in sorted(by_date) for r in by_date[stamp]]
    nye = sorted(set(by_date) - before)

    if args.tørrkjør:
        print(f"\n[tørrkjør] panelet ville hatt {len(rows)} rader over {len(by_date)} snapshots")
        return 0

    write_panel(args.panel, rows)
    print(f"\n{args.panel}: {len(rows)} rader, {len(by_date)} snapshots ({min(by_date)} .. {max(by_date)})")
    if nye:
        print(f"nye snapshot-datoer: {', '.join(nye)}")
    if len(by_date) < 4:
        mangler = 4 - len(by_date)
        print(
            f"\nMerk: NAV-baserte mål (volatilitet, korrelasjon, tracking) slås på "
            f"ved 4 snapshots -- {mangler} måned(er) igjen."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
