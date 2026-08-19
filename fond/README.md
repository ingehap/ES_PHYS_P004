# Fondsvalg fra månedlige Nordnet-eksporter

Verktøy for å gjøre en serie nedlastede fondslister om til et grunnlag for
å velge fond. Ren Python 3.10+, ingen avhengigheter.

```
python3 fond/ingest.py  ~/Nedlastinger/fond_*.csv   # legg filene i panelet
python3 fond/analyze.py                             # rangering
python3 fond/analyze.py --endringer                 # hva endret seg?
python3 fond/analyze.py --overlapp                  # hvilke fond er duplikater?
python3 fond/test_fond.py                           # tester
```

---

## Hovedpoenget

Én nedlastet fil er et øyeblikksbilde. Rangerer du hele fila etter «1 år %»,
måler du hvilket **marked** som gikk best det siste året – ikke hvilket **fond**
som er best. Det er en helt annen beslutning enn den du prøver å ta.

I fila fra 18.8.2026 topper hedgede Asia- og Norge-fond ettårslista. Det er
kronekurs og ett godt børsår, ikke forvaltning. Velger du derfra, kjøper du det
som nettopp har steget.

Verktøyet gjør to ting med det:

1. **Sammenligner bare fond som er sammenlignbare** – samme marked, samme
   valutasikring, samme størrelsessegment, samme (fravær av) tilt.
2. **Bygger en NAV-historikk** av de månedlige filene, som lar deg regne ut
   ting fila ikke inneholder: volatilitet, maksimalt fall, og hvilke fond som
   i praksis er samme fond.

---

## Hvorfor Morningstar-kategorien ikke holder

Kategorikolonnen i fila legger disse fem i samme bøtte, «Europe Equity Large Cap»:

| Fond | 1 år |
|---|---|
| Nordnet Norge Indeks | 29,6 % |
| Nordnet Sverige Index | 13,7 % |
| DNB Norden Indeks A | 11,8 % |
| Nordnet Europa Indeks | 6,7 % |
| Nordnet Danmark Indeks B | 4,8 % |

Spennet på 25 prosentpoeng er ren geografi. Rangerer du innenfor kategorien,
vinner Norge-fondet – ikke fordi det er godt forvaltet, men fordi Oslo Børs
gikk bra. `nordnet.py` utleder derfor et eget marked fra fondsnavnet, og faller
tilbake på kategorien bare når navnet ikke sier noe.

Det krever litt varsomhet med norske sammensatte ord: «KLP AksjeNorge Indeks»
har ingen ordgrense foran «Norge», så et vanlig `\bnorge`-søk bommer og fondet
havner i Europa-gruppa. Det samme gjelder «AksjeUSA», «AksjeVerden» og Amundis
«EurSRIClmtPrsAlgd».

---

## Valutasikring er en egen gruppe

Samme indeks, samme forvalter, ett års forskjell i resultat:

| Fond | 1 år | 3 år ann. |
|---|---|---|
| KLP AksjeUSA Indeks **Valutasikret** N | 20,3 % | 21,0 % |
| KLP AksjeUSA Indeks N | 11,7 % | 17,7 % |

De 8,6 prosentpoengene er kronekursen, ikke fondet. Sikret og usikret variant
rangeres derfor aldri mot hverandre – valget mellom dem er en beslutning om
valutarisiko, som du tar før du velger fond.

Merk også at avkastningstallene for SEK-, EUR- og USD-fond er i fondets egen
valuta. En norsk sparer får ikke det tallet.

---

## Hva som faktisk scores

Innenfor hver gruppe, z-score mot gruppa:

| Ledd | Vekt | Hvorfor |
|---|---|---|
| **Kostnad** | 0,45 | Det eneste feltet i fila som er kjent på forhånd og gjelder framover. |
| **Etterslep** | 0,35 | Avkastning minus gruppemedianen. To indeksfond på samme marked skal ligge likt; avviket er forvaltningskvalitet og kostnader som ikke står i prisfeltet. |
| **Konsistens** | 0,20 | Andel perioder (3m/1år/3år/5år) fondet slår medianen. Skiller vedvarende kvalitet fra ett heldig år. |

Kostnad veier tyngst med vilje. Historisk avkastning er et svakt signal for
framtidig avkastning; kostnaden er det ikke – den påløper hvert år uansett hva
markedet gjør.

**Felter som bevisst ikke inngår i scoren:**

- *Rangering* (Morningstar-stjerner) – bakoverskuende, og mangler for 22 av 84 fond.
- *Risiko* – 74 av 84 fond har verdien 4. Ingen informasjon.
- *Antall eiere* – popularitet, ikke kvalitet.
- *Bærekraft* – artikkel 6/8/9 er en juridisk klassifisering, ikke en kvalitetsrangering. Den hører hjemme i valget av *gruppe*, ikke i rangeringen innenfor den.

De ligger fortsatt i panelet og i `--csv`-uttrekket, så du kan bruke dem som
filter om du vil.

---

## Feilklassifisering flagges

Navnebasert klassifisering bommer noen ganger. Rapporten flagger derfor fond
som avviker mer enn 3 prosentpoeng fra gruppa si, og sorterer dem sist i stedet
for å la dem toppe lista:

```
Mulig feilklassifisering -- avviket mot gruppa er for stort til å være
forvaltningskvalitet:
   -3,0 pp mot «Europa»  Nordnet Europa Indeks  (IE00BMTD2N07)
```

To fond som følger samme marked skal ikke ligge tre prosentpoeng fra hverandre.
Enten følger fondet en annen indeks enn du tror, eller så er det plassert feil.

Er det plassert feil, retter du det i `overrides.csv`:

```csv
isin,marked,sektor,eksponering,tilt,smallcap,valutasikret,notat
NO0010700891,Norge,,,,,,"Alfred Berg Indeks R følger Oslo Børs"
```

Den linjen ligger der allerede, og er et ekte tilfelle: fondet følger Oslo Børs,
men navnet sier ikke «Norge» og Morningstar kaller det «Europe Equity Large Cap».
Uten overstyringen så det ut til å slå Europa-gruppa med seks prosentpoeng.

---

## Det de månedlige filene gir deg som én fil ikke gjør

`Siste`-kolonnen er andelskursen. Stabler du filene, får du en NAV-serie per
ISIN – og NAV for norske og nordiske verdipapirfond er totalavkastning, så
prisendringen *er* avkastningen. Fra fire snapshots og oppover regner
`analyze.py` ut:

- **Volatilitet** og **maksimalt fall** – målt på dine egne data, likt for alle fond og over samme vindu. Fila har ingen risikotall utover et 1–7-nummer som er 4 for nesten alle.
- **Overlapp** (`--overlapp`) – fondspar som beveger seg tilnærmet likt. Korrelasjon over 0,98 betyr samme eksponering, og da er det billigste av dem det åpenbare valget. Dette er det praktisk viktigste: det fanger at du eier tre fond som er samme fond.
- **Endringer** (`--endringer`) – nye fond, fond som forsvinner, og prisendringer. Prisendringer varsler ingen deg om.

At fond forsvinner fra lista er verdt å merke seg for seg selv. Sammenligner du
bare dagens liste med dagens liste, ser du aldri de som ble lagt ned – og de
gikk sjelden ned fordi de gjorde det bra. Panelet husker dem.

**Kadensen bestemmer hva du kan måle.** 12 månedlige snapshots gir brukbar
volatilitet og korrelasjon. Årlig nedlasting gir det ikke. Vil du ha
korrelasjonene raskt, last ned ukentlig det første halvåret.

---

## Foreslått arbeidsflyt

**Hver måned** – samme dag i måneden, `ingest.py`, så `--endringer`. Tar et
minutt. Ikke handle på det.

**Hvert kvartal** – `analyze.py`, se på gruppene du faktisk eier eller vurderer.
Bytt bare hvis noe er varig: fondet har blitt dyrere, eller ligger under
gruppemedianen over flere perioder, ikke bare siste.

**Hvert år** – `--overlapp` for å luke ut duplikater, og en gjennomgang av
fordelingen mellom grupper. Fordelingen mellom Global / USA / Norge / Fremvoksende
avgjør mesteparten av resultatet ditt; hvilket globalt indeksfond du valgte
avgjør nesten ingenting.

---

## Hva verktøyet ikke gjør

Det velger ikke **hvilke** markeder du skal eie, og det er den beslutningen som
faktisk betyr noe. Rangeringen svarer bare på: «gitt at jeg vil eie globale
aksjer, hvilket av disse tolv fondene?» Der er forskjellene små – som de skal
være – og da er billigst med lavt etterslep riktig svar.

Det korrigerer heller ikke for valuta, og det sier ingenting om framtidig
avkastning. Ingenting i denne fila gjør det.

Dette er verktøystøtte for egne beslutninger, ikke investeringsrådgivning.

---

## Filer

| Fil | |
|---|---|
| `nordnet.py` | Parsing (UTF-16, desimalkomma, U+2212-minus) og klassifisering |
| `ingest.py` | Legger eksporter inn i `data/snapshots.csv`, idempotent |
| `analyze.py` | Rangering, overlapp, endringer |
| `overrides.csv` | Manuell omklassifisering per ISIN |
| `test_fond.py` | 27 tester, stdlib `unittest` |
| `data/snapshots.csv` | Panelet – én rad per (snapshot-dato, ISIN) |
