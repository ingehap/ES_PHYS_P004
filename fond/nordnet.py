"""Lesing og normalisering av Nordnets fondslister (CSV-eksport).

Nordnet eksporterer fondslisten som UTF-16LE, tabulatorseparert, med
norsk tallformat: desimalkomma, tusenskille som mellomrom, og unicode
MINUS SIGN (U+2212) i stedet for bindestrek for negative tall.  Modulen
oversetter dette til et flatt, maskinlesbart format ("snapshot rows")
der alle tall er float og alle datoer er ISO-8601.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Lavnivå-parsing
# ---------------------------------------------------------------------------

# Nordnet bruker U+2212 MINUS SIGN, ikke ASCII '-'.  Tusenskille kan være
# vanlig mellomrom eller NBSP/NNBSP.
_MINUS = "−–—"
_SPACE = "   "


def parse_number(raw: str | None) -> float | None:
    """'−1 234,56' -> -1234.56.  Tom/ugyldig streng -> None."""
    if raw is None:
        return None
    s = raw.strip().strip('"')
    for ch in _MINUS:
        s = s.replace(ch, "-")
    for ch in _SPACE:
        s = s.replace(ch, "")
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def parse_date(raw: str | None) -> date | None:
    """'18.8.2026' -> date(2026, 8, 18).  Tom/ugyldig streng -> None."""
    if raw is None:
        return None
    s = raw.strip().strip('"')
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if not m:
        return None
    d, mo, y = (int(g) for g in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def read_text(path: Path) -> str:
    """Les fila uansett om Nordnet ga oss UTF-16 eller UTF-8."""
    blob = path.read_bytes()
    if blob[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return blob.decode("utf-16")
    return blob.decode("utf-8-sig")


# ---------------------------------------------------------------------------
# Kolonner
# ---------------------------------------------------------------------------

# Nordnet-overskrift -> feltnavn i snapshot-tabellen.  Kolonnenavnene har
# variert litt mellom eksporter (norsk/engelsk blanding i periodefeltene),
# så vi slår opp normalisert og godtar flere varianter per felt.
_COLUMNS: dict[str, tuple[str, ...]] = {
    "navn": ("navn", "name"),
    "isin": ("isin",),
    "valuta": ("valuta", "currency"),
    "kategori": ("kategori", "category"),
    "kostnad_pct": ("total pris %", "totalpris %", "total cost %", "årlig kostnad %"),
    "rangering": ("rangering", "rating"),
    "risiko": ("risiko", "risk"),
    "baerekraft": ("bærekraft", "barekraft", "sustainability"),
    "belaaningsgrad_pct": ("belåningsgrad %", "belaningsgrad %"),
    "antall_eiere": ("antall eiere", "owners"),
    "avk_1d_pct": ("1 d %", "1d %"),
    "avk_1u_pct": ("1 uke %", "1 week %"),
    "avk_1m_pct": ("1 måned %", "1 maned %", "1 month %"),
    "avk_3m_pct": ("3 måneder %", "3 maneder %", "3 months %"),
    "avk_iar_pct": ("i år %", "i ar %", "ytd %"),
    "avk_1y_pct": ("1 år %", "1 ar %", "1 year %"),
    "avk_3y_ann_pct": ("3 yrs. ann.", "3 år ann.", "3 years ann."),
    "avk_5y_ann_pct": ("5 yrs. ann.", "5 år ann.", "5 years ann."),
    "avk_10y_ann_pct": ("10 yrs. ann.", "10 år ann.", "10 years ann."),
    "forvalter": ("ansvarlig forvalter", "manager"),
    "nav": ("siste", "last"),
    "nav_dato": ("tid", "time", "date"),
    "startdato": ("startdato", "inception"),
    "min_kjoep": ("min. kjøp", "min. kjop", "min. purchase"),
    "handles": ("handles", "trading"),
}

_NUMERIC = {
    "kostnad_pct", "rangering", "risiko", "belaaningsgrad_pct", "antall_eiere",
    "avk_1d_pct", "avk_1u_pct", "avk_1m_pct", "avk_3m_pct", "avk_iar_pct",
    "avk_1y_pct", "avk_3y_ann_pct", "avk_5y_ann_pct", "avk_10y_ann_pct",
    "nav", "min_kjoep",
}
_DATES = {"nav_dato", "startdato"}

# Felter utledet av derive_exposure(), lagret i panelet slik at analysen
# ikke må gjette på nytt -- og slik at en git-diff viser når en
# omklassifisering endret seg.
DERIVED = [
    "aktivaklasse", "marked", "sektor", "valutasikret",
    "smallcap", "tilt", "giret", "eksponering",
]

# Rekkefølgen radene skrives ut i.  'snapshot_dato' først slik at panelet
# sorterer kronologisk uten ekstra nøkler.
FIELDNAMES = ["snapshot_dato", *_COLUMNS.keys(), *DERIVED, "kilde"]


def _header_index(header: list[str]) -> dict[str, int]:
    """Map feltnavn -> kolonneindeks for denne fila."""
    seen = {h.strip().strip('"').lower(): i for i, h in enumerate(header)}
    idx: dict[str, int] = {}
    for field_name, aliases in _COLUMNS.items():
        for alias in aliases:
            if alias in seen:
                idx[field_name] = seen[alias]
                break
    missing = {"navn", "isin", "nav"} - idx.keys()
    if missing:
        raise ValueError(f"mangler påkrevde kolonner: {sorted(missing)}")
    return idx


# ---------------------------------------------------------------------------
# Avledede felter: eksponering
# ---------------------------------------------------------------------------

# Morningstar-kategorien er for grov til å sammenligne innenfor: både
# Norge-, Sverige-, Danmark-, Norden- og bred Europa-indeks havner i
# "Europe Equity Large Cap".  Vi utleder derfor et eget marked fra navnet.
# Mer spesifikke mønstre først -- første treff vinner.
# Marked utledes fra navn + kategori.  Merk fraværet av \b foran de fleste
# tokenene: norske fondsnavn er sammensatte ord ("KLP AksjeNorge Indeks",
# "AksjeUSA", "AksjeVerden"), så et ordgrense-anker ville aldri truffet og
# fondet hadde falt tilbake på kategorien -- som legger Norge, Sverige,
# Danmark og bred Europa i samme sekk.  Korte/tvetydige tokener beholder
# ankeret.  Mest spesifikke mønster først: første treff vinner.
_MARKETS: tuple[tuple[str, str], ...] = (
    (r"norge|norsk|norway", "Norge"),
    (r"sverige|svensk|sweden", "Sverige"),
    (r"danmark|denmark", "Danmark"),
    (r"suomi|finland", "Finland"),
    (r"norden|nordic", "Norden"),
    (r"tyskland|germany|\bdax\b", "Tyskland"),
    # "Asia ex-Japan" må sjekkes før "japan", ellers fanger Japan-regelen
    # kategorien til fond som eksplisitt holder Japan utenfor.
    (r"ex-?\s?japan", "Asia ex-Japan"),
    (r"pacific|stillehav", "Stillehavet"),
    (r"japan", "Japan"),
    (r"kina|china", "Kina"),
    (r"\bindia", "India"),
    (r"fremvoksende|emerging|nye markeder|\bem\b", "Fremvoksende"),
    (r"asia", "Asia"),
    (r"usa|\bus\b|amerika|\bs&p|nasdaq", "USA"),
    (r"europa|europe", "Europa"),
    (r"global|verden|world|all countries|alle markeder|acwi", "Global"),
)

# Sektor leses fra Morningstar-kategorien, ikke fra navnet: navnematching
# gjør "Handelsbanken"/"SpareBank 1" til finanssektor.  Kategorien sier
# "<Sektor> Sector Equity" og er entydig.
_SECTOR_FROM_CATEGORY = re.compile(r"^(.*?)\s+Sector Equity$", re.IGNORECASE)
_SECTOR_NO = {
    "technology": "Teknologi",
    "real estate": "Eiendom",
    "energy": "Energi",
    "industrials": "Industri",
    "healthcare": "Helse",
    "financials": "Finans",
    "consumer goods & services": "Forbruk",
    "utilities": "Forsyning",
}

# Aktivaklasse -- et rentefond skal aldri rangeres mot et aksjefond.
_ASSET_CLASSES: tuple[tuple[str, str], ...] = (
    (r"fixed income|bond|obligasjon|rente|money market", "Renter"),
    (r"allocation|kombinasjon|balanced", "Kombinasjon"),
    (r"equity|aksje", "Aksjer"),
)

_HEDGED = r"valutasikret|valutasikr|hedged|currency hedge|sikret"
_SMALLCAP = r"småbolag|smabolag|small cap|smallcap|mid/small|småselskap"
# Fond med bærekrafts- eller faktortilt følger ikke den brede indeksen og
# skal ikke rangeres mot den.
# Uten ordgrenser av samme grunn som markedene: Amundi forkorter til
# "EurSRIClmtPrsAlgd" i ett ord.  "plus" krever derimot at ordet ikke er
# først i navnet, ellers blir forvalteren PLUS Fonder feilmerket.
_TILTED = (
    r"sri|clmt|climate|paris|mer samfunnsansvar|samf\b|klima|sustainable|"
    r"framtid|\besg\b|enhanced|flerfaktor|multifactor|\bfactor|"
    r"(?<!^)\bplus\b|investmentbolag|barnefond"
)
_LEVERAGED = r"\b1[2-9]\d\b|\b[2-9]x\b|gearing"


def _flag(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


def derive_exposure(navn: str, kategori: str, valuta: str) -> dict[str, object]:
    """Utled markeds-/stileksponering fra fondsnavn og kategori.

    Dette er nøkkelen sammenligningen bygger på: to fond er bare
    sammenlignbare hvis de har samme aktivaklasse, følger samme marked,
    samme størrelsessegment, samme valutasikring og har samme (fravær av)
    tilt.
    """
    text = f"{navn} {kategori}"
    asset = next(
        (label for pat, label in _ASSET_CLASSES if _flag(pat, kategori)), "Annet"
    )

    m = _SECTOR_FROM_CATEGORY.match(kategori.strip())
    if m:
        key = m.group(1).strip().lower()
        sector = _SECTOR_NO.get(key, m.group(1).strip())
    else:
        sector = ""

    # Navnet slår kategorien: "KLP AksjeUSA Indeks N" ligger i Morningstars
    # "US Equity Large Cap Blend", men "KLP AksjeVerden Indeks N" kan ligge
    # i en kategori som nevner Europe.  Fondets eget navn er alltid mer
    # spesifikt enn bøtta Morningstar la det i.
    market = next(
        (label for pat, label in _MARKETS if _flag(pat, navn)),
        next((label for pat, label in _MARKETS if _flag(pat, kategori)), "Ukjent"),
    )
    hedged = _flag(_HEDGED, navn)
    smallcap = _flag(_SMALLCAP, text) or "mid/small" in kategori.lower()
    tilted = _flag(_TILTED, navn)
    leveraged = _flag(_LEVERAGED, navn)

    # Eksponeringsnøkkelen er sammenligningsgruppa.  Valutasikring holdes
    # som eget ledd fordi den endrer NOK-avkastningen fullstendig -- et
    # sikret og et usikret USA-indeksfond følger samme indeks, men gir
    # helt ulikt resultat for en norsk sparer.
    parts = [sector or market]
    if asset != "Aksjer":
        parts.insert(0, asset)
    if smallcap:
        parts.append("SmallCap")
    if hedged:
        parts.append("NOK-sikret")
    if tilted:
        parts.append("tilt")
    if leveraged:
        parts.append("giret")
    return {
        "aktivaklasse": asset,
        "marked": market,
        "sektor": sector,
        "valutasikret": hedged,
        "smallcap": smallcap,
        "tilt": tilted,
        "giret": leveraged,
        "eksponering": " / ".join(parts),
    }


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Manuell overstyring
# ---------------------------------------------------------------------------

# Navnebasert klassifisering treffer ikke alltid.  "Alfred Berg Indeks R
# (NOK)" følger Oslo Børs, men navnet nevner ikke Norge og Morningstar
# kaller den "Europe Equity Large Cap" -- uten overstyring rangeres den
# mot brede Europa-fond og ser ut til å slå dem med seks prosentpoeng.
OVERRIDE_FIELDS = {"marked", "sektor", "eksponering", "tilt", "smallcap", "valutasikret"}


def load_overrides(path: Path) -> dict[str, dict[str, object]]:
    """Les ISIN -> overstyrte felter fra overrides.csv.  Mangler fila: {}."""
    path = Path(path)
    if not path.exists():
        return {}
    # Hopp over ledende '#'-kommentarer så fila kan dokumentere seg selv
    # uten at kommentaren blir lest som overskriftsrad.
    linjer = [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")]
    out: dict[str, dict[str, object]] = {}
    with io.StringIO("\n".join(linjer)) as fh:
        for row in csv.DictReader(fh):
            isin = (row.get("isin") or "").strip()
            if not isin or isin.startswith("#"):
                continue
            felter = {}
            for k, v in row.items():
                if k in OVERRIDE_FIELDS and (v or "").strip():
                    felter[k] = v.strip().lower() == "true" if v.strip().lower() in ("true", "false") else v.strip()
            if felter:
                out[isin] = felter
    return out


def apply_overrides(row: dict, overrides: dict[str, dict[str, object]]) -> dict:
    """Legg overstyringer over en rad og bygg eksponeringsnøkkelen på nytt."""
    over = overrides.get(str(row.get("isin", "")))
    if not over:
        return row
    row.update(over)
    if "eksponering" not in over:
        # Bygg nøkkelen på nytt fra de (nå overstyrte) delfeltene.
        parts = [row.get("sektor") or row.get("marked")]
        if row.get("aktivaklasse") not in (None, "", "Aksjer"):
            parts.insert(0, row["aktivaklasse"])
        for flagg, etikett in (("smallcap", "SmallCap"), ("valutasikret", "NOK-sikret"),
                               ("tilt", "tilt"), ("giret", "giret")):
            if row.get(flagg):
                parts.append(etikett)
        row["eksponering"] = " / ".join(str(p) for p in parts if p)
    return row


@dataclass
class Snapshot:
    """Én Nordnet-eksport: dato + radene i den."""

    dato: date
    rader: list[dict] = field(default_factory=list)
    kilde: str = ""


def parse_file(path: Path, overrides: dict[str, dict[str, object]] | None = None) -> Snapshot:
    """Les én Nordnet-CSV til en Snapshot med normaliserte rader."""
    text = read_text(Path(path))
    reader = csv.reader(io.StringIO(text), delimiter="\t", quotechar='"')
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValueError(f"{path}: tom fil") from exc
    idx = _header_index(header)

    rader: list[dict] = []
    for raw in reader:
        if not any(cell.strip() for cell in raw):
            continue
        row: dict[str, object] = {}
        for field_name, i in idx.items():
            value = raw[i].strip().strip('"') if i < len(raw) else ""
            if field_name in _NUMERIC:
                row[field_name] = parse_number(value)
            elif field_name in _DATES:
                d = parse_date(value)
                row[field_name] = d.isoformat() if d else ""
            else:
                row[field_name] = value
        for field_name in _COLUMNS:
            row.setdefault(field_name, None if field_name in _NUMERIC else "")
        row.update(derive_exposure(str(row["navn"]), str(row["kategori"]), str(row["valuta"])))
        if overrides:
            apply_overrides(row, overrides)
        rader.append(row)

    if not rader:
        raise ValueError(f"{path}: ingen datarader")

    # Snapshot-datoen er NAV-datoen radene faktisk gjelder for, ikke
    # nedlastingstidspunktet i filnavnet -- laster du ned lørdag er
    # kursene fra fredag.
    navdatoer = [str(r["nav_dato"]) for r in rader if r["nav_dato"]]
    if navdatoer:
        # Hyppigste, ikke seneste: noen få fond prises et døgn etter
        # resten, og de skal ikke flytte hele snapshotets dato.
        stamp = date.fromisoformat(Counter(navdatoer).most_common(1)[0][0])
    else:
        stamp = _date_from_filename(Path(path)) or date.today()
    return Snapshot(dato=stamp, rader=rader, kilde=Path(path).name)


def _date_from_filename(path: Path) -> date | None:
    """Reservenøkkel: 'fond_19.8.2026_2120.csv' -> date(2026, 8, 19)."""
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", path.name)
    if not m:
        return None
    d, mo, y = (int(g) for g in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def snapshot_records(snap: Snapshot) -> list[dict]:
    """Snapshot -> rader klare for panel-CSV."""
    out = []
    for r in snap.rader:
        rec = {"snapshot_dato": snap.dato.isoformat(), "kilde": snap.kilde}
        rec.update(r)
        out.append(rec)
    return out


__all__ = [
    "Snapshot",
    "apply_overrides",
    "derive_exposure",
    "parse_date",
    "parse_file",
    "parse_number",
    "load_overrides",
    "snapshot_records",
]
