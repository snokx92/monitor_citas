import os
import sys
import time
import random
import re
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# =========================
# CONFIGURACIÓN
# =========================
URLS = {
    "Monterrey": "https://www.citaconsular.es/es/hosteds/widgetdefault/25b18886db70f7ec9fd6dfd1a85d1395f/",
    "Ciudad de México": "https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/",
    "Miami": "https://www.citaconsular.es/es/hosteds/widgetdefault/2533f04b1d3e818b66f175afc9c24cf63/",
}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "60"))
HUMAN_MIN = float(os.getenv("HUMAN_MIN", "0.7"))
HUMAN_MAX = float(os.getenv("HUMAN_MAX", "1.5"))

# Detectar citas disponibles
HUECO_REGEX = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
NO_HORAS_TEXT = "No hay horas disponibles"
LOCALE = "es-ES"

# =========================
# FUNCIONES AUXILIARES
# =========================
def notify(msg: str):
    print(msg, flush=True)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
                timeout=15
            )
        except Exception as e:
            print(f"[WARN] Error al enviar a Telegram: {e}", file=sys.stderr)

def human_pause():
    time.sleep(random.uniform(HUMAN_MIN, HUMAN_MAX))

def analizar_slots(page):
    """Busca botones con huecos libres"""
    slots = []
    try:
        buttons = page.locator("button")
        for i in range(buttons.count()):
            try:
                b = buttons.nth(i)
                if not b.is_visible():
                    continue
                text = b.inner_text().strip()
                if "hueco libre" in text.lower():
                    m = HUECO_REGEX.search(text)
                    if m:
                        slots.append((m.group(0), text))
            except:
                continue
    except:
        pass
    return slots

def revisar_un_consulado(nombre, url, headless=True):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(
                viewport={"width": random.randint(1280, 1440), "height": random.randint(800, 960)},
                user_agent=random.choice([
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
                ]),
                locale=LOCALE,
                extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"}
            )

            page = context.new_page()
            page.set_default_timeout(25000)

            # Capturar pop-ups automáticos
            def on_dialog(dialog):
                try:
                    dialog.accept()
                except:
                    pass
            context.on("dialog", on_dialog)

            page.goto(url, wait_until="domcontentloaded")
            human_pause()

            # Detectar página vacía → posible bloqueo por IP
            content = page.content().strip()
            if len(content) < 1500:
                notify(f"⚠️ {nombre}: la página parece vacía (posible bloqueo por IP/anti-bot).")
                browser.close()
                return False, [], None

            # Buscar aviso de "no hay horas"
            try:
                page.get_by_text(NO_HORAS_TEXT, exact=False).wait_for(timeout=4000)
                browser.close()
                return False, [], None
            except PlaywrightTimeout:
                pass

            # Buscar huecos
            slots = analizar_slots(page)
            fecha = None
            if slots:
                try:
                    page.screenshot(path=f"{nombre}_disponibles.png", full_page=True)
                except:
                    pass
                browser.close()
                return True, slots, fecha

            # Sin huecos, pero página cargada
            try:
                page.screenshot(path=f"{nombre}_sin_huecos.png", full_page=True)
            except:
                pass
            browser.close()
            return False, [], fecha

    except PlaywrightTimeout:
        notify(f"⏳ {nombre}: timeout al cargar la página.")
        return False, [], None
    except Exception as e:
        notify(f"❌ {nombre}: error inesperado → {e}")
        return False, [], None

# =========================
# LOOP PRINCIPAL
# =========================
def main():
    while True:
        for nombre, url in URLS.items():
            ok, slots, fecha = revisar_un_consulado(nombre, url, headless=True)
            if ok and slots:
                primeras = ", ".join(sorted({h for h, _ in slots})[:5])
                f = f" ({fecha})" if fecha else ""
                notify(f"✅ {nombre}: ¡HAY HUECOS!{f} → Horas: {primeras}\nEntra ya: {url}")
            elif not ok and not slots:
                print(f"[INFO] {nombre} → Sin huecos reales por ahora.", flush=True)
            else:
                print(f"[WARN] {nombre} → No se pudo verificar bien.", flush=True)

        wait = random.randint(CHECK_INTERVAL_SEC, CHECK_INTERVAL_SEC + 30)
        print(f"[INFO] Esperando {wait} segundos antes de la siguiente ronda…")
        time.sleep(wait)

if __name__ == "__main__":
    main()
