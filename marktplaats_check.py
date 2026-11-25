"""
Marktplaats PS5-zoeker
Zoekt elke run naar nieuwe PlayStation 5-advertenties op Marktplaats en stuurt een
Telegram-/ntfy-melding als een advertentie aan ALLE eisen hieronder voldoet.

Pas de instellingen in het blok INSTELLINGEN aan als je iets wilt veranderen.
"""

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ============================ INSTELLINGEN ============================
ZOEKTERMEN = ["playstation 5", "ps5"]
CATEGORIE_ID = 356               # Marktplaats-rubriek "Spelcomputers en Games"
PRIJS_MIN = 250                  # euro (vraagprijs)
PRIJS_MAX = 500                  # euro (vraagprijs; biedingen erboven maken niet uit)
MIN_STERREN = 4.0                # gemiddelde beoordeling van de verkoper (op 5)
MIN_REVIEWS = 3                  # verkopers met minder (of geen) reviews worden overgeslagen
MIN_JAAR_ACTIEF = 1              # account moet minstens zo lang bestaan
MIN_FOTOS = 2
MELD_ZONDER_PRIJS = False        # advertenties met alleen "Bieden"/"Zie omschrijving" ook melden?
MAX_MELDINGEN_EERSTE_RUN = 0     # eerste run: bestaande advertenties alleen onthouden, niet melden
MAX_LEEFTIJD_UUR = 6             # advertenties die langer dan dit online staan worden nooit gemeld
# =====================================================================

STATE_FILE = Path(os.environ.get("MP_STATE_FILE", "marktplaats_state.json"))
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

BASE = "https://www.marktplaats.nl"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept-Language": "nl-NL,nl;q=0.9",
}

# ---------- tekstfilters ----------
# Titel moet over een PS5 gaan
PS5_RE = re.compile(r"\b(ps\s?5|playstation\s?5|play\s?station\s?5)\b", re.I)

# "Gezocht"-advertenties
GEZOCHT_RE = re.compile(
    r"\b(gezocht|gevraagd|zoek(e|ende)?|op zoek|ik koop|wij kopen|inkoop|te koop gevraagd|"
    r"zoekt|wanted|koop je|verkoop je)\b", re.I)

# Defect / gebanned / onderdelen
PROBLEEM_RE = re.compile(
    r"\b(defect|defecte|kapot|kapotte|stuk|werkt niet|doet het niet|geen beeld|geen geluid|"
    r"start niet|gaat niet aan|blod|blue light|blauw licht|for parts|voor onderdelen|"
    r"reparatie|repair|hdmi[- ]?(poort )?(kapot|defect|stuk)|"
    r"ban|banned|gebanned|gebant|verbannen|geblokkeerd|console ban|permanent ban|"
    r"oververhit|overheat|opknapper|niet werkend|beschadigd moederbord)\b", re.I)

# Ontkenningen eruit halen vóór de probleemcheck ("niet defect", "geen ban", "nooit kapot geweest")
ONTKENNING_RE = re.compile(
    r"\b(niet|geen|nooit|nergens|zonder|not|no|never)\b(\s+\S+){0,3}?\s+"
    r"(defect|defecte|kapot|kapotte|stuk|ban|banned|gebanned|gebant|verbannen|geblokkeerd|"
    r"oververhit|beschadigd|problemen|reparatie)\b", re.I)

# Alleen accessoires / games / andere producten (zonder console)
ACCESSOIRE_RE = re.compile(
    r"\b(controller|dualsense|game|games|spel|spellen|fifa|fc ?2\d|cod|call of duty|gta|"
    r"hoes|case|skin|sticker|faceplate|cover|cover plate|stand|standaard|oplaad|laadstation|"
    r"headset|koptelefoon|vr2?|psvr|portal|kabel|ssd|harde schijf|fan|koeler|"
    r"disc ?drive|discdrive|camera|afstandsbediening|remote|doos|lege doos|alleen doos)\b", re.I)
CONSOLE_RE = re.compile(
    r"\b(console|spelcomputer|disc|digital|digitale|slim|edition|editie|1 ?tb|825 ?gb|"
    r"met|incl|inclusief|\+)\b", re.I)


def strip_ontkenningen(tekst: str) -> str:
    return ONTKENNING_RE.sub(" ", tekst)


def is_console_advertentie(titel: str) -> bool:
    if not PS5_RE.search(titel):
        return False
    if ACCESSOIRE_RE.search(titel) and not CONSOLE_RE.search(titel):
        return False
    t = titel.lower()
    # "Controller voor PS5", "Game PS5 ..." -> titel begint met accessoire
    if re.match(r"^\s*(ps5\s+)?(controller|dualsense|game|spel|fifa|headset|hoes|vr)", t) and "console" not in t:
        return False
    return True


# ---------- http ----------
def http_get(url: str, accept: str = "text/html") -> str:
    req = urllib.request.Request(url, headers={**HEADERS, "Accept": accept})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="replace")


def zoek(query: str, met_categorie: bool = True) -> list[dict]:
    params = {
        "query": query,
        "limit": "100",
        "offset": "0",
        "sortBy": "SORT_INDEX",
        "sortOrder": "DECREASING",
        "viewOptions": "list-view",
    }
    if met_categorie and CATEGORIE_ID:
        params["l1CategoryId"] = str(CATEGORIE_ID)
    url = f"{BASE}/lrp/api/search?{urllib.parse.urlencode(params)}"
    data = json.loads(http_get(url, accept="application/json"))
    return data.get("listings", []) or []


# ---------- leeftijd ----------
def leeftijd_uur(l: dict) -> float | None:
    """Hoeveel uur de advertentie online staat (None als onbekend)."""
    for key in ("date", "listingDate", "createdAt", "startDate"):
        v = l.get(key)
        if v is None or v == "":
            continue
        try:
            if isinstance(v, (int, float)) or str(v).isdigit():
                ts = float(v)
                ts = ts / 1000 if ts > 1e11 else ts
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except Exception:
            continue
    return None


# ---------- prijs ----------
def prijs_info(l: dict) -> tuple[str, float | None, bool]:
    """Geeft (prijstype, prijs in euro of None, gereserveerd?)."""
    pi = l.get("priceInfo") or {}
    ptype = (pi.get("priceType") or "").upper()
    cents = pi.get("priceCents")
    prijs = (cents / 100) if isinstance(cents, (int, float)) and cents > 0 else None
    gereserveerd = (ptype == "RESERVED" or bool(l.get("reserved"))
                    or "gereserveerd" in (l.get("title") or "").lower())
    return ptype, prijs, gereserveerd


def prijs_ok(ptype: str, prijs: float | None, gereserveerd: bool) -> tuple[bool, str]:
    if ptype in ("EXCHANGE", "FREE"):
        return False, f"prijstype {ptype}"
    if prijs is not None:
        if PRIJS_MIN <= prijs <= PRIJS_MAX:
            return True, ""
        return False, f"prijs €{prijs:.0f} buiten range"
    # geen prijs bekend
    if gereserveerd:
        return True, ""          # gereserveerd zonder zichtbare prijs: wel tonen
    if MELD_ZONDER_PRIJS:
        return True, ""
    return False, f"geen prijs ({ptype or 'onbekend'})"


# ---------- details van advertentie + verkoper ----------
def _getal(s: str) -> float:
    return float(s.replace(",", "."))


def haal_details(l: dict) -> dict:
    """Leest de advertentiepagina: foto's, volledige omschrijving en verkopersgegevens."""
    vip = l.get("vipUrl") or ""
    url = vip if vip.startswith("http") else BASE + vip
    page = http_get(url)
    info: dict = {"url": url}

    # --- foto's ---
    ids = set(re.findall(r"images\.marktplaats\.com/api/v1/listing-mp-p/images/([0-9a-f]{2}/[0-9a-f-]{20,})", page))
    n_zoek = max(len(l.get("pictures") or []), len(l.get("imageUrls") or []))
    info["fotos"] = max(len(ids), n_zoek)

    # --- omschrijving ---
    m = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', page)
    omschrijving = html.unescape(m.group(1)) if m else ""
    m2 = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.){20,})"', page)
    if m2:
        try:
            omschrijving += " " + json.loads(f'"{m2.group(1)}"')
        except Exception:
            pass
    info["omschrijving"] = omschrijving

    # --- verkoper: beoordeling ---
    score = None
    for pat in [r'"averageScore"\s*:\s*([\d.]+)', r'"averageRating"\s*:\s*([\d.]+)',
                r'"score"\s*:\s*([\d.]+)\s*,\s*"(?:numberOfReviews|count|reviewCount)"',
                r'"ratingValue"\s*:\s*"?([\d.,]+)']:
        m = re.search(pat, page)
        if m:
            score = _getal(m.group(1))
            break
    if score is not None and score > 5:
        score = score / 2  # schaal 0-10 -> 0-5
    info["sterren"] = score

    # --- verkoper: aantal reviews ---
    reviews = None
    for pat in [r'"numberOfReviews"\s*:\s*(\d+)', r'"reviewCount"\s*:\s*"?(\d+)', r'"totalReviews"\s*:\s*(\d+)',
                r'"reviewsCount"\s*:\s*(\d+)', r'(\d+)\s+(?:reviews|beoordelingen)\b']:
        m = re.search(pat, page, re.I)
        if m:
            reviews = int(m.group(1))
            break
    info["reviews"] = reviews

    # --- verkoper: hoe lang actief ---
    jaren = None
    m = re.search(r'"activeYears"\s*:\s*(\d+)', page)
    if m:
        jaren = int(m.group(1))
    if jaren is None:
        m = re.search(r"(\d+)\+?\s+jaar\s+(?:actief\s+)?op\s+Marktplaats", page, re.I)
        if m:
            jaren = int(m.group(1))
    if jaren is None and re.search(r"\d+\s+maand(?:en)?\s+(?:actief\s+)?op\s+Marktplaats", page, re.I):
        jaren = 0
    if jaren is None:
        m = re.search(r'"(?:registrationDate|memberSince|registeredSince|sinceDate)"\s*:\s*"(\d{4})-(\d{2})', page)
        if m:
            start = datetime(int(m.group(1)), int(m.group(2)), 1, tzinfo=timezone.utc)
            jaren = int((datetime.now(timezone.utc) - start).days // 365)
    if jaren is None:
        m = re.search(r"(?:sinds|lid sinds|actief sinds)\s+(?:\w+\s+)?(\d{4})", page, re.I)
        if m:
            jaren = datetime.now().year - int(m.group(1))
    info["jaren"] = jaren

    # Seller zonder reviews: als er expliciet "geen reviews" staat
    if reviews is None and re.search(r"(nog )?geen (reviews|beoordelingen)", page, re.I):
        info["reviews"] = 0
    return info


# ---------- meldingen ----------
def send_telegram(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    data = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": False}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                 data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"  telegram-status: {r.status}")
    except Exception as e:
        print(f"  Telegram mislukt: {e}")


def send_ntfy(title: str, msg: str, url: str | None) -> None:
    if not NTFY_TOPIC:
        return
    h = {"Title": title.encode("utf-8"), "Priority": "high", "Tags": "video_game"}
    if url:
        h["Click"] = url
    req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"  ntfy-status: {r.status}")
    except Exception as e:
        print(f"  ntfy mislukt: {e}")


def meld(l: dict, prijs: float | None, ptype: str, gereserveerd: bool, d: dict) -> None:
    titel = l.get("title") or "PS5"
    stad = ((l.get("location") or {}).get("cityName")) or "?"
    prijs_txt = f"€{prijs:.0f}" if prijs is not None else "geen prijs"
    if ptype == "MIN_BID":
        prijs_txt = f"bieden vanaf {prijs_txt}"
    status = " · 🔒 GERESERVEERD" if gereserveerd else ""
    tekst = (f"🎮 <b>{html.escape(titel)}</b>\n"
             f"💶 {prijs_txt}{status}\n"
             f"⭐ {d['sterren']:.1f} ({d['reviews']} reviews) · 📅 {d['jaren']} jaar op Marktplaats\n"
             f"📷 {d['fotos']} foto's · 📍 {html.escape(stad)}\n\n{d['url']}")
    print(f"[MELDING] {titel} – {prijs_txt}{status}")
    send_telegram(tekst)
    send_ntfy(f"Marktplaats: {titel}", f"{prijs_txt}{status} · ⭐{d['sterren']:.1f} ({d['reviews']}) · {d['jaren']} jr · {stad}", d["url"])


# ---------- state ----------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen": {}, "pogingen": {}, "eerste_run": True, "fout_runs": 0, "fout_gemeld": False}


def save_state(s: dict) -> None:
    # oude items (ouder dan 30 dagen) opruimen
    grens = time.time() - 30 * 86400
    s["seen"] = {k: v for k, v in s["seen"].items() if v > grens}
    STATE_FILE.write_text(json.dumps(s))


# ---------- hoofdprogramma ----------
def main() -> int:
    state = load_state()
    eerste_run = state.get("eerste_run", True)
    nu = time.time()

    # 1. Zoeken
    listings: dict[str, dict] = {}
    zoek_fout = False
    for q in ZOEKTERMEN:
        try:
            res = zoek(q)
            if not res:
                res = zoek(q, met_categorie=False)
            for l in res:
                iid = l.get("itemId")
                if iid:
                    listings[iid] = l
        except Exception as e:
            print(f"Zoeken naar '{q}' mislukt: {e}")
            zoek_fout = True
    print(f"Gevonden advertenties: {len(listings)}")

    if not listings and zoek_fout:
        state["fout_runs"] = state.get("fout_runs", 0) + 1
        if state["fout_runs"] >= 6 and not state.get("fout_gemeld"):
            send_telegram("⚠️ <b>Marktplaats-zoeker</b> kan Marktplaats al een tijdje niet bereiken.")
            state["fout_gemeld"] = True
        save_state(state)
        return 0
    if state.get("fout_gemeld"):
        send_telegram("✅ <b>Marktplaats-zoeker</b> werkt weer.")
    state["fout_runs"], state["fout_gemeld"] = 0, False

    # 2. Filteren
    meldingen = 0
    onbekend = 0
    for iid, l in listings.items():
        if iid in state["seen"]:
            continue
        titel = l.get("title") or ""
        korte_omschr = l.get("description") or l.get("categorySpecificDescription") or ""

        def afwijzen(reden: str) -> None:
            print(f"  ✗ {titel[:60]!r}: {reden}")
            state["seen"][iid] = nu

        if eerste_run:
            # Allereerste run: alles wat er nu al staat alleen onthouden
            state["seen"][iid] = nu
            continue
        uur = leeftijd_uur(l)
        if uur is not None and uur > MAX_LEEFTIJD_UUR:
            afwijzen(f"al {uur:.0f} uur online")
            continue
        if not is_console_advertentie(titel):
            afwijzen("geen PS5-console")
            continue
        if GEZOCHT_RE.search(titel) or GEZOCHT_RE.search(korte_omschr[:120]):
            afwijzen("gezocht-advertentie")
            continue
        ptype, prijs, gereserveerd = prijs_info(l)
        ok, reden = prijs_ok(ptype, prijs, gereserveerd)
        if not ok:
            afwijzen(reden)
            continue
        if PROBLEEM_RE.search(strip_ontkenningen(f"{titel} {korte_omschr}")):
            afwijzen("defect/ban genoemd")
            continue

        # Details ophalen (foto's, volledige omschrijving, verkoper)
        try:
            d = haal_details(l)
            time.sleep(1.5)
        except Exception as e:
            print(f"  ? {titel[:60]!r}: details niet op te halen ({e})")
            onbekend += 1
            continue

        if PROBLEEM_RE.search(strip_ontkenningen(d["omschrijving"])):
            afwijzen("defect/ban in omschrijving")
            continue
        if GEZOCHT_RE.search(d["omschrijving"][:200]):
            afwijzen("gezocht-advertentie (omschrijving)")
            continue
        if d["fotos"] < MIN_FOTOS:
            afwijzen(f"{d['fotos']} foto('s)")
            continue
        if d["reviews"] is None or d["sterren"] is None or d["jaren"] is None:
            # Gegevens niet gevonden: een paar keer opnieuw proberen, daarna overslaan
            p = state["pogingen"].get(iid, 0) + 1
            state["pogingen"][iid] = p
            print(f"  ? {titel[:60]!r}: verkopersgegevens onvolledig "
                  f"(sterren={d['sterren']}, reviews={d['reviews']}, jaren={d['jaren']}) poging {p}")
            onbekend += 1
            if p >= 3:
                afwijzen("verkopersgegevens niet te lezen")
            continue
        if d["reviews"] < MIN_REVIEWS:
            afwijzen(f"{d['reviews']} reviews")
            continue
        if d["sterren"] < MIN_STERREN:
            afwijzen(f"{d['sterren']:.1f} sterren")
            continue
        if d["jaren"] < MIN_JAAR_ACTIEF:
            afwijzen(f"{d['jaren']} jaar actief")
            continue

        # Voldoet aan alles!
        state["seen"][iid] = nu
        if eerste_run and meldingen >= MAX_MELDINGEN_EERSTE_RUN:
            print(f"  ✓ {titel[:60]!r} (niet gemeld: limiet eerste run)")
            continue
        print(f"  ✓ {titel[:60]!r}")
        meld(l, prijs, ptype, gereserveerd, d)
        meldingen += 1

    if eerste_run:
        print(f"Eerste run: {len(state['seen'])} bestaande advertenties onthouden, geen meldingen verstuurd.")
    print(f"Meldingen verstuurd: {meldingen} · onvolledig: {onbekend}")
    state["eerste_run"] = False
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
