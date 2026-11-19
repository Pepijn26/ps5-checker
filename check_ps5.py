"""
Controleert Sony's certified refurbished PS5-pagina's en stuurt een pushmelding
via ntfy.sh zodra een console koopbaar wordt.

Werkt met een echte (headless) Chromium-browser, zodat de voorraadknop
("Niet beschikbaar voor aankoop" / "Toevoegen aan wagentje") net zo wordt
ingeladen als in je eigen browser.
"""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

COLLECTION_URL = "https://www.playstation.com/nl-nl/collections/certified-refurbished/"
# Vaste lijst als vangnet, voor het geval de overzichtspagina niet laadt.
FALLBACK_URLS = [
    "https://www.playstation.com/nl-nl/ps5/certified-refurbished-ps5-slim/",
]

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
TEST_MODE = os.environ.get("TEST_NOTIFICATION", "false").lower() == "true"
# Na zoveel mislukte checks op rij krijg je één waarschuwing (~30 min bij 5-min interval)
ERROR_THRESHOLD = 6

# Alleen de specifieke teksten van de koopknop van dít product, niet algemene
# "niet beschikbaar"-teksten die ook bij andere producten onderaan de pagina staan.
NOT_AVAILABLE_PATTERNS = [
    r"niet beschikbaar voor aankoop",
    r"op dit moment uitverkocht",
]
BUY_BUTTON = re.compile(r"(toevoegen aan (winkel)?wagentje|in winkelwagen|nu kopen|doorgaan om te kopen)", re.I)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def notify(title: str, message: str, url: str | None = None, priority: str = "default", tags: str = "") -> None:
    print(f"[MELDING] {title}: {message}")
    if not NTFY_TOPIC:
        print("  (NTFY_TOPIC niet ingesteld, melding niet verstuurd)")
        return
    headers = {"Title": title.encode("utf-8"), "Priority": priority}
    if url:
        headers["Click"] = url
    if tags:
        headers["Tags"] = tags
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(f"  ntfy-status: {resp.status}")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"products": {}, "consecutive_errors": 0, "error_notified": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def accept_cookies(page) -> None:
    for label in ["Alle cookies accepteren", "Accepteren", "Alles accepteren", "Accept All"]:
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=3000)
                return
        except Exception:
            pass


def find_product_urls(page) -> list[str]:
    urls: list[str] = []
    try:
        page.goto(COLLECTION_URL, wait_until="domcontentloaded", timeout=45000)
        accept_cookies(page)
        page.wait_for_timeout(6000)
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        for h in hrefs:
            h = h.split("?")[0].split("#")[0]
            hl = h.lower()
            if "refurbished" in hl and "ps5" in hl and "/collections/" not in hl and "playstation.com" in hl:
                if h not in urls:
                    urls.append(h)
    except Exception as e:
        print(f"Overzichtspagina kon niet geladen worden: {e}")
    for u in FALLBACK_URLS:
        if u not in urls:
            urls.append(u)
    return urls


def check_product(page, url: str) -> tuple[str, str]:
    """Geeft (status, naam) terug. status = 'available' | 'unavailable' | 'error'."""
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    accept_cookies(page)
    # Wacht tot de voorraadknop/-tekst is ingeladen
    try:
        page.wait_for_function(
            """() => {
                const t = document.body.innerText.toLowerCase();
                return t.includes('niet beschikbaar') || t.includes('uitverkocht')
                    || t.includes('wagentje') || t.includes('winkelwagen');
            }""",
            timeout=25000,
        )
    except PWTimeout:
        pass
    page.wait_for_timeout(3000)

    name = (page.title() or url).split("|")[0].strip()
    body = page.inner_text("body").lower()

    if "access denied" in body or "request unsuccessful" in body or len(body) < 300:
        return "error", name

    if any(re.search(p, body) for p in NOT_AVAILABLE_PATTERNS):
        return "unavailable", name

    # Geen "niet beschikbaar"-tekst: alleen 'beschikbaar' als er ook echt een zichtbare, klikbare koopknop is.
    buttons = page.get_by_role("button", name=BUY_BUTTON)
    for i in range(buttons.count()):
        b = buttons.nth(i)
        try:
            if b.is_visible() and b.is_enabled():
                return "available", name
        except Exception:
            continue
    return "error", name


def main() -> int:
    if TEST_MODE:
        notify("PS5-checker test", "Het werkt! Je krijgt zo een melding als er een refurbished PS5 beschikbaar komt.",
               url=COLLECTION_URL, tags="white_check_mark")
        return 0

    state = load_state()
    results: dict[str, tuple[str, str]] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=USER_AGENT, locale="nl-NL", viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        urls = find_product_urls(page)
        print(f"Te controleren pagina's: {len(urls)}")
        for url in urls:
            try:
                status, name = check_product(page, url)
            except Exception as e:
                print(f"Fout bij {url}: {e}")
                status, name = "error", url
            results[url] = (status, name)
            print(f"  {status:12s} {name}  ({url})")
        browser.close()

    any_ok = False
    for url, (status, name) in results.items():
        prev = state["products"].get(url, {}).get("status")
        if status != "error":
            any_ok = True
        if status == "available" and prev != "available":
            notify("PS5 refurbished BESCHIKBAAR!", f"{name} is nu te koop. Tik om de pagina te openen.",
                   url=url, priority="urgent", tags="rotating_light,video_game")
        if status != "error":
            state["products"][url] = {"status": status, "name": name}

    if any_ok:
        if state.get("error_notified"):
            notify("PS5-checker werkt weer", "De pagina's zijn weer bereikbaar, de controle loopt weer.", tags="ok")
        state["consecutive_errors"] = 0
        state["error_notified"] = False
    else:
        state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
        if state["consecutive_errors"] >= ERROR_THRESHOLD and not state.get("error_notified"):
            notify("PS5-checker kan Sony niet lezen",
                   "Al een tijdje lukt het niet om de voorraad te controleren (mogelijk blokkeert Sony de checker). "
                   "Kijk zelf even op de site; ik meld het als het weer werkt.",
                   url=COLLECTION_URL, priority="high", tags="warning")
            state["error_notified"] = True

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
