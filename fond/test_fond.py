#!/usr/bin/env python3
"""Tester for fondsverktøyet.

    python3 fond/test_fond.py          # stdlib, ingen avhengigheter
    python3 -m pytest fond/test_fond.py

Ligger under fond/ og ikke tests/ fordi pyproject setter testpaths til
tests/ -- petrolib-suiten skal ikke plukke opp disse.
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyze  # noqa: E402
import nordnet  # noqa: E402

HEADER = [
    "Navn", "ISIN", "Valuta", "Kategori", "Total pris %", "Rangering", "Risiko",
    "Bærekraft", "Belåningsgrad %", "Antall eiere", "1 d %", "1 uke %", "1 måned %",
    "3 måneder %", "i år %", "1 år %", "3 yrs. ann.", "5 yrs. ann.", "10 yrs. ann.",
    "Ansvarlig forvalter", "Siste", "Tid", "Startdato", "Min. kjøp", "Handles",
]


def nordnet_csv(path: Path, rader: list[dict], navdato: str) -> Path:
    """Skriv en fil i akkurat det formatet Nordnet eksporterer."""
    linjer = ["\t".join(HEADER)]
    for r in rader:
        felt = [
            f'"{r["navn"]}"', f'"{r["isin"]}"', f'"{r.get("valuta", "NOK")}"',
            f'"{r.get("kategori", "Global Equity Large Cap")}"',
            f'"{r.get("pris", "0,25")}"', f'"{r.get("rangering", "4")}"', '"4"',
            '"Artikkel 8"', '"85"', '"1000"', '"−0,10"', '"1,00"', '"2,00"',
            f'"{r.get("m3", "5,00")}"', '"10,00"', f'"{r.get("y1", "12,00")}"',
            f'"{r.get("y3", "16,00")}"', f'"{r.get("y5", "13,00")}"', '"11,00"',
            '"Forvalter AS"', f'"{r["nav"]}"', navdato, "1.1.2015", '"100"', '"Daglig"',
        ]
        linjer.append("\t".join(felt))
    # UTF-16LE med BOM og CRLF, slik Nordnet faktisk leverer den.
    path.write_bytes("\r\n".join(linjer).encode("utf-16"))
    return path


class TestTallOgDato(unittest.TestCase):
    def test_unicode_minus(self):
        """Nordnet bruker U+2212, ikke ASCII-bindestrek."""
        self.assertEqual(nordnet.parse_number("−1,16"), -1.16)
        self.assertEqual(nordnet.parse_number("-1,16"), -1.16)

    def test_desimalkomma_og_tusenskille(self):
        self.assertEqual(nordnet.parse_number("2098,49"), 2098.49)
        self.assertEqual(nordnet.parse_number("1 234,50"), 1234.50)
        self.assertEqual(nordnet.parse_number("1 234,50"), 1234.50)

    def test_tomme_felt(self):
        for tom in ("", "  ", '""', None):
            self.assertIsNone(nordnet.parse_number(tom))

    def test_dato(self):
        self.assertEqual(nordnet.parse_date("18.8.2026"), date(2026, 8, 18))
        self.assertEqual(nordnet.parse_date("1.10.2024"), date(2024, 10, 1))
        self.assertIsNone(nordnet.parse_date("32.1.2026"))
        self.assertIsNone(nordnet.parse_date(""))


class TestEksponering(unittest.TestCase):
    def test_sammensatte_norske_navn(self):
        """'AksjeNorge' har ingen ordgrense foran 'Norge' -- må likevel treffe."""
        for navn, ventet in [
            ("KLP AksjeNorge Indeks N", "Norge"),
            ("KLP AksjeUSA Indeks N", "USA"),
            ("KLP AksjeVerden Indeks N", "Global"),
            ("KLP AksjeEuropa Indeks N", "Europa"),
            ("KLP AksjeFremvoksende Markeder Indeks N", "Fremvoksende"),
        ]:
            with self.subTest(navn=navn):
                self.assertEqual(
                    nordnet.derive_exposure(navn, "Europe Equity Large Cap", "NOK")["marked"],
                    ventet,
                )

    def test_kategori_skiller_ikke_norden(self):
        """Morningstar legger Norge, Sverige og Europa i samme kategori."""
        kat = "Europe Equity Large Cap"
        marked = {
            nordnet.derive_exposure(n, kat, "NOK")["marked"]
            for n in ("Nordnet Norge Indeks", "Nordnet Sverige Index",
                      "Nordnet Danmark Indeks B", "DNB Norden Indeks A",
                      "Nordnet Europa Indeks")
        }
        self.assertEqual(marked, {"Norge", "Sverige", "Danmark", "Norden", "Europa"})

    def test_valutasikring_er_egen_gruppe(self):
        sikret = nordnet.derive_exposure("KLP AksjeUSA Indeks Valutasikret N", "US Equity Large Cap Blend", "NOK")
        usikret = nordnet.derive_exposure("KLP AksjeUSA Indeks N", "US Equity Large Cap Blend", "NOK")
        self.assertTrue(sikret["valutasikret"])
        self.assertFalse(usikret["valutasikret"])
        self.assertNotEqual(sikret["eksponering"], usikret["eksponering"])

    def test_sektor_kommer_fra_kategori_ikke_navn(self):
        """'Handelsbanken' og 'SpareBank 1' er ikke finanssektor-fond."""
        for navn in ("Handelsbanken Global Index (A1 NOK)", "SpareBank 1 Indeks Global N"):
            with self.subTest(navn=navn):
                e = nordnet.derive_exposure(navn, "Global Equity Large Cap", "NOK")
                self.assertEqual(e["sektor"], "")
                self.assertEqual(e["eksponering"], "Global")
        tek = nordnet.derive_exposure("KLP AksjeTeknologi Indeks N", "Technology Sector Equity", "NOK")
        self.assertEqual(tek["sektor"], "Teknologi")

    def test_ex_japan_gaar_ikke_i_japan_gruppa(self):
        e = nordnet.derive_exposure("Lannebo Marknad Pacific A", "Asia ex-Japan Equity", "SEK")
        self.assertNotEqual(e["marked"], "Japan")

    def test_tilt_i_sammentrukne_navn(self):
        """Amundi skriver 'EurSRIClmtPrsAlgd' i ett ord."""
        self.assertTrue(nordnet.derive_exposure("Amundi MSCI EurSRIClmtPrsAlgd AE C", "Europe Equity Large Cap", "EUR")["tilt"])
        self.assertTrue(nordnet.derive_exposure("KLP AksjeGlobal Mer Samfunnsansvar N", "Global Equity Large Cap", "NOK")["tilt"])

    def test_plus_som_forvalternavn_er_ikke_tilt(self):
        self.assertFalse(nordnet.derive_exposure("PLUS Fastigheter Sverige Index", "Real Estate Sector Equity", "SEK")["tilt"])
        self.assertTrue(nordnet.derive_exposure("Storebrand Sverige Småbolag Plus C NOK", "Europe Equity Mid/Small Cap", "NOK")["tilt"])

    def test_giring(self):
        self.assertTrue(nordnet.derive_exposure("Nordnet Global Indeks 125", "Global Equity Large Cap", "NOK")["giret"])

    def test_renter_skilles_fra_aksjer(self):
        e = nordnet.derive_exposure("KLP Obligasjon Global 3 år N", "Global Fixed Income", "NOK")
        self.assertEqual(e["aktivaklasse"], "Renter")
        self.assertTrue(e["eksponering"].startswith("Renter"))


class TestFillesing(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_leser_utf16_med_bom(self):
        f = nordnet_csv(self.tmp / "a.csv", [
            {"navn": "Fond A", "isin": "NO1", "nav": "100,00"},
        ], "18.8.2026")
        snap = nordnet.parse_file(f)
        self.assertEqual(snap.dato, date(2026, 8, 18))
        self.assertEqual(snap.rader[0]["nav"], 100.0)

    def test_snapshot_dato_er_hyppigste_navdato(self):
        """Noen få fond prises et døgn senere; de skal ikke flytte datoen."""
        rader = [{"navn": f"Fond {i}", "isin": f"NO{i}", "nav": "100,00"} for i in range(5)]
        f = nordnet_csv(self.tmp / "b.csv", rader, "18.8.2026")
        tekst = f.read_bytes().decode("utf-16").replace("18.8.2026", "19.8.2026", 1)
        f.write_bytes(tekst.encode("utf-16"))
        self.assertEqual(nordnet.parse_file(f).dato, date(2026, 8, 18))

    def test_overstyring(self):
        ov = self.tmp / "ov.csv"
        ov.write_text("# kommentar\nisin,marked,notat\nNO1,Norge,fordi\n", encoding="utf-8")
        f = nordnet_csv(self.tmp / "c.csv", [
            {"navn": "Ukjent Indeks R", "isin": "NO1", "nav": "100,00",
             "kategori": "Europe Equity Large Cap"},
        ], "18.8.2026")
        snap = nordnet.parse_file(f, nordnet.load_overrides(ov))
        self.assertEqual(snap.rader[0]["marked"], "Norge")
        self.assertEqual(snap.rader[0]["eksponering"], "Norge")

    def test_tom_fil_gir_feil(self):
        tom = self.tmp / "tom.csv"
        tom.write_bytes("".encode("utf-16"))
        with self.assertRaises(ValueError):
            nordnet.parse_file(tom)


class TestPanelOgRangering(unittest.TestCase):
    """Bygger et flermåneders panel og sjekker NAV-avledede mål."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.panel = self.tmp / "snapshots.csv"
        start = date(2026, 1, 15)
        # Tre globale indeksfond: A og B følger hverandre tett (A billigst),
        # C er dyrere og sakker etter -- akkurat mønsteret rangeringen skal se.
        baner = {
            "A": ("Billig Global Indeks", "0,19", [100, 102, 101, 104, 106, 108]),
            "B": ("Dyr Global Indeks", "0,45", [100, 102, 101, 104, 106, 108]),
            "C": ("Etternøler Global Indeks", "0,35", [100, 101, 99, 100, 101, 101]),
        }
        for m in range(6):
            d = start + timedelta(days=30 * m)
            rader = [
                {"navn": navn, "isin": f"NO{k}", "nav": f"{navs[m]:.2f}".replace(".", ","),
                 "pris": pris, "y1": "12,00" if k != "C" else "6,00",
                 "y3": "16,00" if k != "C" else "9,00"}
                for k, (navn, pris, navs) in baner.items()
            ]
            f = nordnet_csv(self.tmp / f"s{m}.csv", rader, f"{d.day}.{d.month}.{d.year}")
            snap = nordnet.parse_file(f)
            self._append(snap)

    def _append(self, snap):
        rows = list(csv.DictReader(self.panel.open(encoding="utf-8"))) if self.panel.exists() else []
        rows = [r for r in rows if r["snapshot_dato"] != snap.dato.isoformat()]
        rows.extend(nordnet.snapshot_records(snap))
        with self.panel.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=nordnet.FIELDNAMES, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    def test_nav_maal_beregnes(self):
        rows = analyze.load_panel(self.panel)
        m = analyze.nav_metrics(rows)
        self.assertEqual(len(m), 3)
        a = m["NOA"]
        self.assertEqual(a["nav_n"], 6)
        self.assertGreater(a["nav_ann_pct"], 0)      # 100 -> 108 er positivt
        self.assertGreater(a["nav_vol_pct"], 0)
        self.assertLess(a["nav_mdd_pct"], 0)         # hadde et fall underveis

    def test_maks_fall(self):
        """102 -> 101 er det største fallet i banen: -0,98 %."""
        rows = analyze.load_panel(self.panel)
        self.assertAlmostEqual(analyze.nav_metrics(rows)["NOA"]["nav_mdd_pct"], -100 / 102, places=6)

    def test_billigste_av_to_like_vinner(self):
        rows = analyze.load_panel(self.panel)
        _, grupper = analyze.rank(rows)
        gruppe = next(g for g in grupper if len(g) == 3)
        rangert = [x["navn"] for x in gruppe]
        self.assertEqual(rangert[0], "Billig Global Indeks")
        self.assertEqual(rangert[-1], "Etternøler Global Indeks")

    def test_overlapp_finner_duplikatet(self):
        """A og B har identiske NAV-baner -- paret skal flagges, billigst først."""
        par = analyze.overlap_pairs(analyze.load_panel(self.panel))
        self.assertTrue(any(
            b == "Billig Global Indeks" and d == "Dyr Global Indeks" for b, d, _ in par
        ), par)

    def test_endringer_mellom_snapshots(self):
        ch = analyze.changes(analyze.load_panel(self.panel))
        self.assertEqual(ch["nye"], [])
        self.assertEqual(ch["borte"], [])


class TestScoring(unittest.TestCase):
    def _fond(self, **kw):
        base = {"isin": "X", "navn": "F", "kostnad_pct": 0.25, "eksponering": "Global",
                "avk_3m_pct": 5.0, "avk_1y_pct": 12.0, "avk_3y_ann_pct": 16.0,
                "avk_5y_ann_pct": 13.0}
        base.update(kw)
        return base

    def test_lavere_pris_gir_hoyere_score_alt_annet_likt(self):
        gruppe = [
            self._fond(isin="A", navn="Billig", kostnad_pct=0.15),
            self._fond(isin="B", navn="Middels", kostnad_pct=0.30),
            self._fond(isin="C", navn="Dyr", kostnad_pct=0.60),
        ]
        rangert = analyze.score_group(gruppe, {})
        self.assertEqual([x["navn"] for x in rangert], ["Billig", "Middels", "Dyr"])

    def test_stort_avvik_flagges_som_feilklassifisering(self):
        gruppe = [
            self._fond(isin="A", navn="Normal 1"),
            self._fond(isin="B", navn="Normal 2", avk_1y_pct=12.5, avk_3y_ann_pct=16.2),
            self._fond(isin="C", navn="Normal 3", avk_1y_pct=11.5, avk_3y_ann_pct=15.8),
            self._fond(isin="D", navn="Feilplassert", avk_1y_pct=30.0,
                       avk_3y_ann_pct=25.0, avk_3m_pct=15.0, avk_5y_ann_pct=24.0),
        ]
        rangert = analyze.score_group(gruppe, {})
        avvik = next(x for x in rangert if x["navn"] == "Feilplassert")
        self.assertIn("avvik", avvik["merknad"])
        # Skal ikke toppe lista på grunn av avviket.
        self.assertNotEqual(rangert[0]["navn"], "Feilplassert")
        self.assertTrue(analyze.suspects([rangert]))

    def test_manglende_historikk_straffes(self):
        gruppe = [
            self._fond(isin="A", navn="Med historikk"),
            self._fond(isin="B", navn="Uten historikk", avk_3m_pct=None,
                       avk_1y_pct=None, avk_3y_ann_pct=None, avk_5y_ann_pct=None),
            self._fond(isin="C", navn="Med historikk 2", avk_1y_pct=12.2),
            self._fond(isin="D", navn="Med historikk 3", avk_1y_pct=11.8),
        ]
        rangert = analyze.score_group(gruppe, {})
        ung = next(x for x in rangert if x["navn"] == "Uten historikk")
        self.assertEqual(ung["merknad"], "for kort historikk")
        self.assertEqual(ung["perioder_maalt"], 0)


class TestEkteFil(unittest.TestCase):
    """Kjører mot den innsjekkede eksportfila hvis den finnes."""

    def setUp(self):
        self.panel = Path(__file__).resolve().parent / "data" / "snapshots.csv"
        if not self.panel.exists():
            self.skipTest("ingen data/snapshots.csv")

    def test_panelet_lar_seg_rangere(self):
        rows = analyze.load_panel(self.panel)
        stamp, grupper = analyze.rank(rows)
        self.assertTrue(stamp)
        self.assertTrue(grupper)
        for g in grupper:
            plasser = [x["plass"] for x in g]
            self.assertEqual(plasser, sorted(plasser))

    def test_hvert_fond_faar_enten_marked_eller_sektor(self):
        """Sektorfond har ingen geografi, men ingen fond skal mangle begge."""
        rows = analyze.load_panel(self.panel)
        uklassifiserte = sorted(
            {r["navn"] for r in rows if r["marked"] == "Ukjent" and not r["sektor"]}
        )
        self.assertEqual(uklassifiserte, [], f"uklassifiserte fond: {uklassifiserte}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
