# -*- coding: utf-8 -*-
"""
Monitor de citas – Monterrey + Ciudad de México

Lo que hace:
- Revisa múltiples consulados (por env CONSUL_URLS).
- Ciudad de México: hace el "click extra" sobre el panel antes de buscar el calendario.
- Detección por:
    * Horas HH:MM (regex) en página e iframes.
    * Fallback por texto "Hueco libre" (aunque no haya HH:MM).
    * Texto de "No hay horas disponibles".
- Evidencia:
    * PROOF_ON_NO_SLOTS=1 -> cuando no hay huecos, envía captura y HTML.
    * NUEVO: si la página parece "vacía/bloqueo", SIEMPRE adjunta HTML + captura.
    * --probe "Nombre" -> prueba guiada (1 vuelta) con video + capturas + HTML.
- Anti-bloqueo:
    * Si el HTML es muy corto o sin texto visible -> marca "blank" y adjunta evidencia.
    * Opcionalmente rota proxy si defines PROXY_LIST (formato http://user:pass@host:port).
- Emulación:
    * CDMX emula iPhone Safari por defecto (CDMX_MOBILE=1).
    * Monterrey usa desktop.

Variables útiles (Railway -> Variables):
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
CHECK_INTERVAL_SEC (default 120)
HUMAN_MIN (0.7) HUMAN_MAX (1.6)
BLOCK_IMAGES (0/1) BLOCK_FONTS (1/0)
DEBUG_STEPS (1/0)
PROOF_ON_NO_SLOTS (1/0)
CDMX_MOBILE (1/0)
PROXY_LIST (separado por coma)
RETRIES_ON_BLOCK (2 por defecto)

CONSUL_URLS ejemplo (default ya trae MTY y CDMX):
"Monterrey|https://.../25b18886db70f7ec9fd6dfd1a85d1395f/|default,
 Ciudad de Mexico|https://.../21b7c1aaf9fef2785deb64ccab5ceca06/|cdmx_panel"
"""

import os
import sys
import re
import time
import random
import hashlib
from typing import List, Tuple, Optional, Dict

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout


# =========================
# Entorno
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "120"))
HUMAN_MIN = float(os.getenv("HUMAN_MIN", "0.7"))
HUMAN_MAX = float(os.getenv("HUMAN_MAX", "1.6"))

BLOCK_IMAGES = os.getenv("BLOCK_IMAGES", "0") == "1"
BLOCK_FONTS  = os.getenv("BLOCK_FONTS", "1") == "1"

DEBUG_STEPS       = os.getenv("DEBUG_STEPS", "1") == "1"
TRACE_PLAYWRIGHT  = os.getenv("TRACE_PLAYWRIGHT", "0") == "1"
PROOF_ON_NO_SLOTS = os.getenv("PROOF_ON_NO_SLOTS", "1") == "1"

PROXY_LIST = [s.strip() for s in os.getenv("PROXY_LIST", "").split(",") if s.strip()]
RETRIES_ON_BLOCK = int(os.getenv("RETRIES_ON_BLOCK", "2"))
CDMX_MOBILE = os.getenv("CDMX_MOBILE", "1") == "1"

DEFAULT_CONSUL_URLS = ",".join([
    "Monterrey|https://www.citaconsular.es/es/hosteds/widgetdefault/25b18886db70f7ec9fd6dfd1a85d1395f/|default",
    "Ciudad de Mexico|https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/|cdmx_panel",
])
CONSUL_URLS = os.getenv("CONSUL_URLS", DEFAULT_CONSUL_URLS)

# =========================
# Constantes/regex
# =========================
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
NO_CITAS_PATTERNS = [
    "No hay horas disponibles",
    "No hay citas disponibles",
    "No hay disponibilidad",
    "Inténtelo de nuevo dentro de unos días",
]

USER_AGENTS_DESKTOP = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]
UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => false});
Object.defineProperty(navigator, 'languages', {get: () => ['es-MX','es','en-US','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
window.chrome = { runtime: {} };
"""

# =========================
# Telegram helpers
# =========================
def notify(msg: str) -> None:
    print(msg, flush=True)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
                timeout=30,
            )
        except Exception as e:
            print(f"[WARN] Telegram mensaje fallo: {e}", flush=True)

def send_photo(path: str, caption: str = "") -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"photo": f},
                timeout=60,
            )
    except Exception as e:
        print(f"[WARN] Telegram photo fallo: {e}", flush=True)

def send_document(path: str, caption: str = "") -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"document": f},
                timeout=60,
            )
    except Exception as e:
        print(f"[WARN] Telegram doc fallo: {e}", flush=True)


# =========================
# Utilidades varias
# =========================
def human_pause():
    time.sleep(random.uniform(HUMAN_MIN, HUMAN_MAX))

def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower())

def parse_consuls(env_string: str) -> List[Tuple[str,str,str]]:
    out = []
    for part in env_string.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            name, url, mode = [x.strip() for x in part.split("|", 2)]
            out.append((name, url, mode))
        except Exception:
            pass
    return out

def is_blank_like(html: str, text: str) -> bool:
    # Heurística conservadora: muy poco HTML o texto nulo
    if not html:
        return True
    if len(html) < 1200 and len(text.strip()) < 30:
        return True
    # HTML de Bookitit mínimo suele superar este umbral
    return False

def has_no_citas_text(text: str) -> bool:
    low = text.lower()
    for pat in NO_CITAS_PATTERNS:
        if pat.lower() in low:
            return True
    return False

def extract_slots_from_html(html: str) -> List[str]:
    slots = []
    for m in TIME_RE.finditer(html or ""):
        slots.append(m.group(0))
    # Fallback por "Hueco libre" si no vemos HH:MM
    if not slots and "hueco libre" in (html or "").lower():
        slots.append("Hueco libre (sin hora)")
    return sorted(set(slots))


# =========================
# Navegación Playwright
# =========================
def build_context(browser, name: str, mode: str, probe: bool = False):
    # Desktop por defecto; CDMX puede ir móvil
    use_mobile = (mode == "cdmx_panel" and CDMX_MOBILE)
    if use_mobile:
        user_agent = UA_IPHONE
        viewport = {"width": 390, "height": 844}
        device_scale = 3
        is_mobile = True
        has_touch = True
    else:
        user_agent = random.choice(USER_AGENTS_DESKTOP)
        viewport = {"width": random.randint(1200,1440), "height": random.randint(800,960)}
        device_scale = 1
        is_mobile = False
        has_touch = False

    ctx = browser.new_context(
        user_agent=user_agent,
        viewport=viewport,
        device_scale_factor=device_scale,
        is_mobile=is_mobile,
        has_touch=has_touch,
        locale="es-ES",
        accept_downloads=False,
        record_video_dir="/tmp/vid" if probe else None,
    )

    # Stealth
    try:
        ctx.add_init_script(STEALTH_JS)
    except Exception:
        pass

    # Bloqueo selectivo de recursos para ahorrar (opcional)
    if BLOCK_IMAGES or BLOCK_FONTS:
        def _route(route):
            req = route.request
            rtype = req.resource_type
            url = req.url
            if BLOCK_IMAGES and rtype in ("image", "media"):
                return route.abort()
            if BLOCK_FONTS and (rtype == "font" or url.endswith(".woff") or url.endswith(".woff2")):
                return route.abort()
            return route.continue_()
        try:
            ctx.route("**/*", _route)
        except Exception:
            pass

    return ctx

def attach_dialog_autoaccept(page):
    def _on_dialog(d):
        try:
            d.accept()
        except Exception:
            pass
    page.on("dialog", _on_dialog)

def click_extra_cdmx(page) -> None:
    """CDMX: un solo click en el panel de texto para desplegar el calendario."""
    # Intentamos click por selectores razonables; fallback: click al centro.
    tried = False
    locators = [
        "text=PRESENTACION DOCUMENTACION LEY MEMORIA DEMOCRATICA",
        "div.panel-body",
        "div.panel",
        "div.container",
    ]
    for sel in locators:
        try:
            page.locator(sel).first.click(timeout=2000)
            tried = True
            break
        except Exception:
            continue
    if not tried:
        try:
            box = page.viewport_size
            x = (box["width"] // 2) if box else 300
            y = (box["height"] // 3) if box else 300
            page.mouse.click(x, y)
        except Exception:
            pass
    human_pause()

def page_main_text(page) -> str:
    try:
        return (page.inner_text("body") or "").strip()
    except Exception:
        try:
            return (page.content() or "")
        except Exception:
            return ""

def collect_iframe_htmls(page) -> List[str]:
    htmls = []
    try:
        for fr in page.frames:
            try:
                c = fr.content() or ""
                htmls.append(c)
            except Exception:
                continue
    except Exception:
        pass
    return htmls

def take_best_screenshot(page, slug: str, tag: str) -> Optional[str]:
    png_path = f"/tmp/{slug}_{tag}.png"
    # Intentar del iframe más "grande" (por longitud de HTML); si no, de toda la página.
    best = None
    best_len = -1
    try:
        for fr in page.frames:
            try:
                c = fr.content() or ""
                if len(c) > best_len and fr != page.main_frame:
                    best = fr
                    best_len = len(c)
            except Exception:
                continue
    except Exception:
        best = None

    try:
        if best:
            try:
                best.page.screenshot(path=png_path, full_page=True)
            except Exception:
                page.screenshot(path=png_path, full_page=True)
        else:
            page.screenshot(path=png_path, full_page=True)
        return png_path
    except Exception:
        return None


# =========================
# Revisión de un consulado
# =========================
def revisar_consulado(p, name: str, url: str, mode: str, probe: bool = False) -> Tuple[bool, List[str]]:
    """
    Devuelve (hay_huecos, lista_horas_o_labels)
    En cualquier caso puede adjuntar evidencia según flags.
    """
    slug = slugify(name)

    # Proxy simple (opcional): escogemos uno si lo hay
    proxy_arg = None
    if PROXY_LIST:
        proxy_arg = random.choice(PROXY_LIST)

    # Lanzar Chromium
    launch_args = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
        "timeout": 120000,
    }
    if proxy_arg:
        launch_args["proxy"] = {"server": proxy_arg}

    browser = p.chromium.launch(**launch_args)
    ctx = build_context(browser, name, mode, probe=probe)
    page = ctx.new_page()
    attach_dialog_autoaccept(page)
    page.set_default_timeout(20000)

    if DEBUG_STEPS:
        print(f"[{name}] goto…", flush=True)

    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception:
        pass
    human_pause()

    # CDMX: click extra sobre panel
    if mode == "cdmx_panel":
        if DEBUG_STEPS:
            print(f"[{name}] CDMX: click panel…", flush=True)
        click_extra_cdmx(page)

    # Heurística de "página vacía"
    html = page.content() or ""
    txt = page_main_text(page)
    if is_blank_like(html, txt):
        # Reintentos suaves locales
        blank_confirmed = False
        for _ in range(RETRIES_ON_BLOCK):
            human_pause()
            try:
                page.reload(wait_until="domcontentloaded")
            except Exception:
                pass
            human_pause()
            html = page.content() or ""
            txt = page_main_text(page)
            if not is_blank_like(html, txt):
                blank_confirmed = False
                break
            blank_confirmed = True

        if blank_confirmed:
            # Evidencia de blank: SIEMPRE HTML + captura
            notify(f"⚠ {name}: página vacía tras reintentos (bloqueo probable).")
            try:
                html_path = f"/tmp/{slug}_blank.html"
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html)
                send_document(html_path, caption=f"{name}: HTML en blanco (posible bloqueo)")
            except Exception as e:
                print(f"[WARN] No se pudo guardar/enviar HTML blank: {e}", flush=True)

            try:
                snap = take_best_screenshot(page, slug, "blank")
                if snap:
                    send_photo(snap, caption=f"{name}: captura en blanco (posible bloqueo)")
            except Exception as e:
                print(f"[WARN] No se pudo capturar/enviar screenshot blank: {e}", flush=True)

            try:
                ctx.close()
                browser.close()
            except Exception:
                pass
            return (False, [])

    # Buscar texto de "no hay"
    text_lower = (txt or "").lower()
    any_no = has_no_citas_text(txt)

    # Extraer HH:MM en principal e iframes
    slots = extract_slots_from_html(html)
    for sub in collect_iframe_htmls(page):
        slots += extract_slots_from_html(sub)
    slots = sorted(set(slots))

    # Evidencia cuando no hay huecos (si se pidió)
    if PROOF_ON_NO_SLOTS and not slots:
        try:
            html_path = f"/tmp/{slug}_no_slots.html"
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)
            send_document(html_path, caption=f"{name}: HTML sin huecos")
        except Exception as e:
            print(f"[WARN] No se pudo adjuntar HTML sin huecos: {e}", flush=True)
        try:
            snap = take_best_screenshot(page, slug, "no_slots")
            if snap:
                send_photo(snap, caption=f"{name}: captura sin huecos")
        except Exception as e:
            print(f"[WARN] No se pudo adjuntar screenshot sin huecos: {e}", flush=True)

    # Cierre
    try:
        ctx.close()
        browser.close()
    except Exception:
        pass

    if slots:
        return (True, slots)
    else:
        return (False, [])


# =========================
# Probe (una vuelta con video)
# =========================
def run_probe(which: str):
    cons = parse_consuls(CONSUL_URLS)
    target = None
    for c in cons:
        if c[0].lower() == which.lower():
            target = c
            break
    if not target:
        print(f"[probe] No se encontró consulado: {which}", flush=True)
        return

    name, url, mode = target
    slug = slugify(name)

    with sync_playwright() as p:
        # Video activo en probe: record_video_dir se configura en build_context
        launch_args = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
            "timeout": 120000,
        }
        browser = p.chromium.launch(**launch_args)
        ctx = build_context(browser, name, mode, probe=True)
        page = ctx.new_page()
        attach_dialog_autoaccept(page)
        page.set_default_timeout(20000)

        print(f"[probe:{name}] goto…", flush=True)
        page.goto(url, wait_until="domcontentloaded")
        human_pause()
        if mode == "cdmx_panel":
            print(f"[probe:{name}] click panel…", flush=True)
            click_extra_cdmx(page)
            human_pause()

        # Capturas + HTML
        try:
            html_path = f"/tmp/{slug}_probe.html"
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(page.content() or "")
            send_document(html_path, caption=f"{name} (probe): HTML")
        except Exception as e:
            print(f"[probe] no se pudo enviar HTML: {e}", flush=True)

        try:
            shot = take_best_screenshot(page, slug, "probe")
            if shot:
                send_photo(shot, caption=f"{name} (probe): captura")
        except Exception as e:
            print(f"[probe] no se pudo enviar captura: {e}", flush=True)

        try:
            ctx.close()
            browser.close()
        except Exception:
            pass

        notify(f"[probe] {name} terminado.")


# =========================
# Bucle principal
# =========================
def main_loop():
    cons = parse_consuls(CONSUL_URLS)
    names = ", ".join([c[0] for c in cons])
    notify(f"[INFO] Consulados: {names}")

    while True:
        start = time.strftime("%Y-%m-%d %H:%M:%S")
        for name, url, mode in cons:
            try:
                with sync_playwright() as p:
                    ok, slots = revisar_consulado(p, name, url, mode, probe=False)
                if ok and slots:
                    primeras = ", ".join(sorted(slots)[:5])
                    notify(f"[{start}] {name} -> HAY HUECOS: {primeras}  |  {url}")
                    # Respiro para que te dé tiempo de entrar
                    time.sleep(300)
                else:
                    notify(f"[{start}] {name} -> sin huecos por ahora.")
            except Exception as e:
                print(f"[ERROR] {name}: {e}", flush=True)
                time.sleep(60)

        # Espera aleatoria humana
        min_wait = max(30, CHECK_INTERVAL_SEC - 20)
        max_wait = CHECK_INTERVAL_SEC + 40
        wait_time = random.randint(min_wait, max_wait)
        print(f"[INFO] Esperando {wait_time}s antes de la siguiente ronda…", flush=True)
        time.sleep(wait_time)


if __name__ == "__main__":
    # Modo prueba puntual:
    #   python monitor_citas_multiconsulados.py --probe "Ciudad de Mexico"
    if "--probe" in sys.argv:
        try:
            idx = sys.argv.index("--probe") + 1
            target = sys.argv[idx]
        except Exception:
            print("Uso: --probe \"Nombre del consulado\"", flush=True)
            sys.exit(1)
        run_probe(target)
        sys.exit(0)

    # Arranque normal
    print("[start] Launching bot…", flush=True)
    try:
        main_loop()
    except KeyboardInterrupt:
        print("Bye.", flush=True)
