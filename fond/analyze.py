#!/usr/bin/env python3
"""Rangér fondene i panelet innenfor sammenlignbare eksponeringsgrupper.

    python3 fond/analyze.py                  # rapport til skjerm
    python3 fond/analyze.py --csv ut.csv     # samme rangering som CSV
    python3 fond/analyze.py --endringer      # hva som endret seg siste måned

Premisset: å rangere alle fondene mot hverandre måler hvilket *marked*
som gikk best, ikke hvilket *fond* som er best.  Sammenligningen må skje
mellom fond som følger samme marked, med samme valutasikring, samme
størrelsessegment og samme (fravær av) tilt.  Innenfor en slik gruppe er
forskjellene små og reelle, og da er kostnad og etterslep mot gruppa de
signalene som faktisk bærer informasjon.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics as st
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_PANEL = Path(__file__).resolve().parent / "data" / "snapshots.csv"

# Antall snapshots før NAV-avledede mål (volatilitet, korrelasjon,
# tracking) er verdt å regne på.  Under dette faller vi tilbake på
# Nordnets egne avkastningskolonner.
MIN_SNAPSHOTS_FOR_NAV = 4

# Vekter i totalscoren.  Kostnad veier tyngst fordi den er det eneste
# feltet i fila som er kjent på forhånd og gjelder framover; historisk
# avkastning er et svakt signal og vektes deretter.
WEIGHTS = {
    "kostnad": 0.45,      # lav totalpris
    "etterslep": 0.35,    # avkastning mot gruppemedianen (fanger skjulte kostnader)
    "konsistens": 0.20,   # slår gruppa i flere perioder, ikke bare én
}

# To indeksfond på samme marked skal ligge tett.  Et avvik større enn
# dette mot gruppemedianen betyr nesten alltid at fondet er feilplassert,
# ikke at forvalteren er dyktig -- rapporten flagger det som mistenkelig
# i stedet for å belønne det.
MISKLASSIFISERING_PP = 3.0

# Perioder brukt til etterslep/konsistens, korteste først.  Korte
# perioder er støy; lange er mest informative, men mangler for unge fond.
PERIODER = [
    ("avk_3m_pct", "3m", 0.10),
    ("avk_1y_pct", "1år", 0.25),
    ("avk_3y_ann_pct", "3år", 0.35),
    ("avk_5y_ann_pct", "5år", 0.30),
]


# ---------------------------------------------------------------------------
# Innlesing
# ---------------------------------------------------------------------------


def load_panel(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"finner ikke {path} -- kjør fond/ingest.py først")
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit(f"{path} er tom")
    for r in rows:
        for k in list(r):
            if k.endswith(("_pct", "_ann_pct")) or k in {"nav", "antall_eiere", "rangering", "risiko", "min_kjoep", "belaaningsgrad_pct"}:
                r[k] = _f(r[k])
        for k in ("valutasikret", "smallcap", "tilt", "giret"):
            r[k] = str(r.get(k, "")).lower() == "true"
    return rows


def _f(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# NAV-panel: det de månedlige filene gir deg som én fil ikke kan
# ---------------------------------------------------------------------------


def nav_series(rows: list[dict]) -> dict[str, list[tuple[date, float]]]:
    """ISIN -> [(dato, NAV)] sortert kronologisk.

    NAV for norske og nordiske verdipapirfond er totalavkastning
    (utbytte reinvestert), så prisendringen *er* avkastningen.
    """
    series: dict[str, dict[date, float]] = defaultdict(dict)
    for r in rows:
        nav = r.get("nav")
        if nav is None or nav <= 0:
            continue
        stamp = r.get("nav_dato") or r["snapshot_dato"]
        try:
            d = date.fromisoformat(str(stamp))
        except ValueError:
            continue
        series[r["isin"]][d] = nav
    return {isin: sorted(pts.items()) for isin, pts in series.items()}


def returns_from_nav(series: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """[(dato, NAV)] -> [(dato, periodeavkastning)] som desimaltall."""
    out = []
    for (_, prev), (d, cur) in zip(series, series[1:]):
        if prev > 0:
            out.append((d, cur / prev - 1.0))
    return out


def annualised_vol(rets: list[float], perioder_pr_aar: float) -> float | None:
    if len(rets) < 3:
        return None
    return st.stdev(rets) * math.sqrt(perioder_pr_aar) * 100.0


def max_drawdown(series: list[tuple[date, float]]) -> float | None:
    """Største fall fra topp til bunn i panelet, i prosent."""
    if len(series) < 2:
        return None
    peak, worst = series[0][1], 0.0
    for _, nav in series:
        peak = max(peak, nav)
        worst = min(worst, nav / peak - 1.0)
    return worst * 100.0


def correlation(a: list[float], b: list[float]) -> float | None:
    if len(a) != len(b) or len(a) < 3:
        return None
    try:
        return st.correlation(a, b)
    except st.StatisticsError:
        return None


def nav_metrics(rows: list[dict]) -> dict[str, dict]:
    """ISIN -> NAV-avledede mål.  Tomt før panelet er langt nok."""
    series = nav_series(rows)
    stamps = sorted({r["snapshot_dato"] for r in rows})
    if len(stamps) < MIN_SNAPSHOTS_FOR_NAV:
        return {}

    # Snapshots per år, målt på faktisk kadens i stedet for å anta
    # månedlig -- laster du ned annenhver uke skal vol skaleres deretter.
    d0, d1 = date.fromisoformat(stamps[0]), date.fromisoformat(stamps[-1])
    spenn = max((d1 - d0).days, 1)
    pr_aar = 365.25 / (spenn / max(len(stamps) - 1, 1))

    out: dict[str, dict] = {}
    for isin, pts in series.items():
        if len(pts) < 2:
            continue
        rets = returns_from_nav(pts)
        verdier = [r for _, r in rets]
        aar = max((pts[-1][0] - pts[0][0]).days, 1) / 365.25
        total = pts[-1][1] / pts[0][1] - 1.0
        ann = ((1.0 + total) ** (1.0 / aar) - 1.0) * 100.0 if aar > 0 else None
        vol = annualised_vol(verdier, pr_aar)
        out[isin] = {
            "nav_n": len(pts),
            "nav_ann_pct": ann,
            "nav_vol_pct": vol,
            "nav_mdd_pct": max_drawdown(pts),
            "nav_avk_vol": (ann / vol) if (ann is not None and vol) else None,
            "_rets": dict(rets),
        }
    return out


# ---------------------------------------------------------------------------
# Rangering innenfor eksponeringsgruppe
# ---------------------------------------------------------------------------


def latest_snapshot(rows: list[dict]) -> tuple[str, list[dict]]:
    stamp = max(r["snapshot_dato"] for r in rows)
    return stamp, [r for r in rows if r["snapshot_dato"] == stamp]


def _z(value: float, verdier: list[float]) -> float:
    """Z-score, med 0 når gruppa er for liten eller helt flat."""
    if value is None or len(verdier) < 2:
        return 0.0
    spread = st.pstdev(verdier)
    if spread == 0:
        return 0.0
    return (value - st.mean(verdier)) / spread


def score_group(gruppe: list[dict], navm: dict[str, dict]) -> list[dict]:
    """Gi hvert fond i gruppa en score og en begrunnelse."""
    kostnader = [r["kostnad_pct"] for r in gruppe if r["kostnad_pct"] is not None]

    # Etterslep: fondets avkastning minus gruppemedianen, per periode.
    # To indeksfond på samme marked skal ligge likt; avviket er
    # forvaltningskvalitet og kostnader som ikke står i prisfeltet.
    medianer: dict[str, float] = {}
    for felt, _, _ in PERIODER:
        verdier = [r[felt] for r in gruppe if r.get(felt) is not None]
        if len(verdier) >= 3:
            medianer[felt] = st.median(verdier)

    resultater = []
    for r in gruppe:
        diffs: list[tuple[str, float, float]] = []
        for felt, navn, vekt in PERIODER:
            if felt in medianer and r.get(felt) is not None:
                diffs.append((navn, r[felt] - medianer[felt], vekt))

        vektsum = sum(v for _, _, v in diffs)
        etterslep = sum(d * v for _, d, v in diffs) / vektsum if vektsum else None
        # Konsistens: andel målte perioder der fondet slår medianen.
        konsistens = (sum(1 for _, d, _ in diffs if d > 0) / len(diffs)) if diffs else None

        resultater.append({
            **r,
            "etterslep_pp": etterslep,
            "konsistens": konsistens,
            "perioder_maalt": len(diffs),
            **{k: v for k, v in navm.get(r["isin"], {}).items() if not k.startswith("_")},
        })

    # Z-score innenfor gruppa.  Kostnad snus (lavere er bedre).
    e_verdier = [x["etterslep_pp"] for x in resultater if x["etterslep_pp"] is not None]
    k_verdier = [x["konsistens"] for x in resultater if x["konsistens"] is not None]
    for x in resultater:
        z_kost = -_z(x["kostnad_pct"], kostnader) if x["kostnad_pct"] is not None else 0.0
        z_ett = _z(x["etterslep_pp"], e_verdier) if x["etterslep_pp"] is not None else 0.0
        z_kon = _z(x["konsistens"], k_verdier) if x["konsistens"] is not None else 0.0
        score = (
            WEIGHTS["kostnad"] * z_kost
            + WEIGHTS["etterslep"] * z_ett
            + WEIGHTS["konsistens"] * z_kon
        )
        # Fond uten målt historikk får ikke score på historikk-leddene.
        # Straff dem lett i stedet for å la 0.0 se ut som "gjennomsnittlig".
        if x["perioder_maalt"] == 0:
            score -= 0.25
            x["merknad"] = "for kort historikk"
        elif abs(x["etterslep_pp"] or 0) >= MISKLASSIFISERING_PP and len(resultater) >= 3:
            # Et fond vi sannsynligvis ikke kan sammenligne skal ikke
            # rangeres som om vi kunne: det sorteres sist med sin advarsel,
            # i stedet for å toppe gruppa på det som trolig er en
            # klassifiseringsfeil.
            x["mistenkt"] = True
            x["merknad"] = "avvik mot gruppa – sjekk om fondet hører hjemme her"
        x.setdefault("mistenkt", False)
        x["z_kostnad"] = z_kost
        x["z_etterslep"] = z_ett
        x["z_konsistens"] = z_kon
        x["score"] = score

    resultater.sort(key=lambda x: (x["mistenkt"], -x["score"]))
    for i, x in enumerate(resultater, 1):
        x["plass"] = i
    return resultater


def rank(rows: list[dict], min_gruppe: int = 2) -> tuple[str, list[list[dict]]]:
    """Rangér siste snapshot, gruppe for gruppe."""
    stamp, siste = latest_snapshot(rows)
    navm = nav_metrics(rows)

    grupper: dict[str, list[dict]] = defaultdict(list)
    for r in siste:
        grupper[r["eksponering"]].append(r)

    ut = [score_group(g, navm) for g in grupper.values() if len(g) >= min_gruppe]
    enslige = [score_group(g, navm) for g in grupper.values() if len(g) < min_gruppe]
    # Store grupper først -- der er valget reelt.
    ut.sort(key=lambda g: (-len(g), g[0]["eksponering"]))
    ut.extend(sorted(enslige, key=lambda g: g[0]["eksponering"]))
    return stamp, ut


# ---------------------------------------------------------------------------
# Overlapp
# ---------------------------------------------------------------------------


def overlap_pairs(rows: list[dict], terskel: float = 0.98) -> list[tuple[str, str, float]]:
    """Fondspar som beveger seg tilnærmet likt -- kandidater for kutt.

    Krever NAV-panel.  To fond med korrelasjon over terskelen gir samme
    eksponering, og da er det billigste av dem det åpenbare valget.
    """
    navm = nav_metrics(rows)
    if not navm:
        return []
    _, siste = latest_snapshot(rows)
    navn = {r["isin"]: r["navn"] for r in siste}
    kost = {r["isin"]: r["kostnad_pct"] for r in siste}

    par = []
    isins = [i for i in navm if i in navn]
    for a_i, a in enumerate(isins):
        for b in isins[a_i + 1:]:
            ra, rb = navm[a]["_rets"], navm[b]["_rets"]
            felles = sorted(set(ra) & set(rb))
            if len(felles) < 3:
                continue
            c = correlation([ra[d] for d in felles], [rb[d] for d in felles])
            if c is not None and c >= terskel:
                billigst = a if (kost.get(a) or 9) <= (kost.get(b) or 9) else b
                dyrest = b if billigst == a else a
                par.append((navn[billigst], navn[dyrest], c))
    par.sort(key=lambda p: -p[2])
    return par


# ---------------------------------------------------------------------------
# Endringer mellom snapshots
# ---------------------------------------------------------------------------


def changes(rows: list[dict]) -> dict:
    """Hva som endret seg mellom de to siste snapshotene."""
    stamps = sorted({r["snapshot_dato"] for r in rows})
    if len(stamps) < 2:
        return {}
    forrige, siste = stamps[-2], stamps[-1]
    f = {r["isin"]: r for r in rows if r["snapshot_dato"] == forrige}
    s = {r["isin"]: r for r in rows if r["snapshot_dato"] == siste}

    kostnadsendring = []
    for isin in set(f) & set(s):
        a, b = f[isin].get("kostnad_pct"), s[isin].get("kostnad_pct")
        if a is not None and b is not None and abs(b - a) >= 0.005:
            kostnadsendring.append((s[isin]["navn"], a, b))
    return {
        "fra": forrige,
        "til": siste,
        "nye": sorted(s[i]["navn"] for i in set(s) - set(f)),
        "borte": sorted(f[i]["navn"] for i in set(f) - set(s)),
        "kostnad": sorted(kostnadsendring, key=lambda x: x[2] - x[1]),
    }


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------


def _fmt(v, bredde=6, desimaler=2, tom="  –  "):
    if v is None:
        return tom.rjust(bredde)
    return f"{v:{bredde}.{desimaler}f}"


def suspects(grupper: list[list[dict]]) -> list[dict]:
    """Fond som skiller seg for mye fra gruppa til at gruppa kan stemme."""
    return [
        x for g in grupper for x in g
        if x.get("mistenkt")
    ]


def report(stamp: str, grupper: list[list[dict]], rows: list[dict], topp: int = 0) -> None:
    n_snapshots = len({r["snapshot_dato"] for r in rows})
    print(f"\nFondsrangering per {stamp}  ({len(rows)} rader, {n_snapshots} snapshot(s))")
    print("Rangert innenfor eksponeringsgruppe -- aldri på tvers.\n")

    har_nav = bool(nav_metrics(rows))
    for gruppe in grupper:
        navn = gruppe[0]["eksponering"]
        if len(gruppe) < 2:
            r = gruppe[0]
            print(f"── {navn}  (bare {r['navn']}, ingen å sammenligne mot)")
            continue
        print(f"── {navn}  (n={len(gruppe)})")
        hodet = f"   {'#':>2} {'score':>6} {'pris':>5} {'ettersl':>7} {'kons':>5} {'1år':>6} {'3år':>6}"
        if har_nav:
            hodet += f" {'vol':>6} {'MDD':>6}"
        print(hodet + "  fond")
        vist = gruppe[:topp] if topp else gruppe
        for x in vist:
            kons = f"{x['konsistens']*100:4.0f}%" if x["konsistens"] is not None else "   –"
            linje = (
                f"   {x['plass']:2} {x['score']:6.2f} {_fmt(x['kostnad_pct'], 5)} "
                f"{_fmt(x['etterslep_pp'], 7)} {kons:>5} "
                f"{_fmt(x.get('avk_1y_pct'))} {_fmt(x.get('avk_3y_ann_pct'))}"
            )
            if har_nav:
                linje += f" {_fmt(x.get('nav_vol_pct'))} {_fmt(x.get('nav_mdd_pct'))}"
            merk = f"  [{x['merknad']}]" if x.get("merknad") else ""
            print(f"{linje}  {x['navn'][:44]}{merk}")
        if topp and len(gruppe) > topp:
            print(f"      … {len(gruppe) - topp} til")
        print()

    mistenkte = suspects(grupper)
    if mistenkte:
        print("Mulig feilklassifisering -- avviket mot gruppa er for stort til å være")
        print("forvaltningskvalitet.  Sjekk hva fondet følger, og legg eventuelt en")
        print("linje i fond/overrides.csv:")
        for x in sorted(mistenkte, key=lambda x: -abs(x["etterslep_pp"])):
            print(f"  {x['etterslep_pp']:+6.1f} pp mot «{x['eksponering']}»  {x['navn']}  ({x['isin']})")
        print()

    if not har_nav:
        print(
            f"NAV-baserte mål (volatilitet, maks fall, overlapp) krever "
            f"{MIN_SNAPSHOTS_FOR_NAV} snapshots -- du har {n_snapshots}.\n"
            "Til da hviler rangeringen på Nordnets egne avkastningstall, som er\n"
            "de samme uansett hvor mange filer du har lastet ned."
        )


def write_csv(path: Path, grupper: list[list[dict]]) -> None:
    felter = [
        "eksponering", "plass", "score", "navn", "isin", "valuta", "kostnad_pct",
        "etterslep_pp", "konsistens", "perioder_maalt", "avk_1y_pct",
        "avk_3y_ann_pct", "avk_5y_ann_pct", "nav_ann_pct", "nav_vol_pct",
        "nav_mdd_pct", "nav_avk_vol", "rangering", "risiko", "antall_eiere",
        "kategori", "forvalter",
    ]
    def rundet(rad: dict) -> dict:
        # Fire desimaler holder; rå float-haler gjør CSV-en uleselig.
        return {
            k: (round(v, 4) if isinstance(v, float) else v)
            for k, v in rad.items() if k in felter
        }

    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=felter, extrasaction="ignore")
        w.writeheader()
        for g in grupper:
            w.writerows(rundet(x) for x in g)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    ap.add_argument("--csv", type=Path, help="skriv rangeringen til CSV")
    ap.add_argument("--topp", type=int, default=0, help="vis bare N per gruppe (0 = alle)")
    ap.add_argument("--endringer", action="store_true", help="vis endringer siden forrige snapshot")
    ap.add_argument("--overlapp", action="store_true", help="vis fondspar som gir samme eksponering")
    args = ap.parse_args(argv)

    rows = load_panel(args.panel)
    stamp, grupper = rank(rows)

    if args.endringer:
        ch = changes(rows)
        if not ch:
            print("Trenger minst 2 snapshots for å vise endringer.")
            return 0
        print(f"\nEndringer {ch['fra']} → {ch['til']}")
        print(f"  nye i lista:    {', '.join(ch['nye']) or 'ingen'}")
        print(f"  falt ut:        {', '.join(ch['borte']) or 'ingen'}")
        for navn, a, b in ch["kostnad"]:
            print(f"  pris {a:.2f} → {b:.2f}  {navn}")
        return 0

    if args.overlapp:
        par = overlap_pairs(rows)
        if not par:
            print(f"Overlappsjekk krever {MIN_SNAPSHOTS_FOR_NAV} snapshots med NAV-historikk.")
            return 0
        print("\nFondspar med tilnærmet identisk bevegelse (behold den første):")
        for billig, dyr, c in par:
            print(f"  r={c:.4f}  {billig}  ⟷  {dyr}")
        return 0

    report(stamp, grupper, rows, topp=args.topp)
    if args.csv:
        write_csv(args.csv, grupper)
        print(f"Skrev {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
