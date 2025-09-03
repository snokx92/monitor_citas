#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, io, re, time, random, textwrap
from datetime import datetime
import requests

# ===== JPG helper (opcional) =====
try:
    from PIL import Image                      # pip install pillow
    PIL_OK = True
except Exception:
    PIL_OK = False

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ==========================
# Variables de entorno
# ==========================
TELE_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELE_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
PROOF           = os.getenv("PROOF", "ON").upper() == "ON"
SHOW_PUBLIC_IP  = os.getenv("SHOW_PUBLIC_IP", "ON").upper() == "ON"

PROXY_HOST = os.getenv("PROXY_HOST", "").strip()
PROXY_PORT = os.getenv("PROXY_PORT", "").strip()
PROXY_USER = os.getenv("PROXY_USER", "").strip()
PROXY_PASS = os.getenv("PROXY_PASS", "").strip()

WIDGET_TIMEOUT_MS  = int(os.getenv("WIDGET_TIMEOUT_MS", "70000"))
LANDING_TIMEOUT_MS = int(os.getenv("LANDING_TIMEOUT_MS", "30000"))
GOTO_RETRIES       = int(os.getenv("GOTO_RETRIES", "2"))

# Esperas humanas (seg)
HUMAN_CLICK_MIN, HUMAN_CLICK_MAX = 0.8, 1.8

# URLs Ministerio
MIN_MTY  = "https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx"
MIN_CDMX = "https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx"

# Selectores/Patrones del widget
BTN_CONTINUE   = r'text=/Continue\s*\/\s*Continuar/i'
NO_SLOTS_TEXT  = r'text=/No hay horas disponibles/i'
PANEL_HEADER   = r'text=/PRESENTACION DOCUMENTACION/i'  # ambos consulados

# ==========================
# Telegram helpers
# ==========================
def tele_send_text(text: str):
    if not TELE_TOKEN or not TELE_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage",
            data={"chat_id": TELE_CHAT_ID, "text": text, "parse_mode":"HTML"},
            timeout=15
        )
    except Exception:
        pass

def tele_send_doc(bytes_, filename, caption=""):
    if not TELE_TOKEN or not TELE_CHAT_ID:
        return
    try:
        files = {"document": (filename, bytes_)}
        data  = {"chat_id": TELE_CHAT_ID, "caption": caption}
        requests.post(
            f"https://api.telegram.org/bot{TELE_TOKEN}/sendDocument",
            data=data, files=files, timeout=30
        )
    except Exception:
        pass

def tele_send_jpg(page, caption: str, quality: int = 82, full=True):
    png = page.screenshot(full_page=full)
    if not PIL_OK:
        tele_send_doc(png, "capture.png", caption)
        return
    try:
        img = Image.open(io.BytesIO(png)).convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        tele_send_doc(out.getvalue(), "capture.jpg", caption)
    except Exception:
        tele_send_doc(png, "capture.png", caption)

def tele_send_html(page, name, caption):
    try:
        html = page.content().encode("utf-8", "ignore")
        tele_send_doc(html, f"{name}.html", caption)
    except Exception:
        pass

def log_info(msg): tele_send_text(f"[INFO] {msg}")
def log_warn(msg): tele_send_text(f"⚠️ {msg}")
def log_err(msg):  tele_send_text(f"❌ {msg}")

# ==========================
# Utilidades Playwright
# ==========================
def human_pause(a=HUMAN_CLICK_MIN, b=HUMAN_CLICK_MAX):
    time.sleep(random.uniform(a,b))

def safe_wait(page, state="domcontentloaded", t=LANDING_TIMEOUT_MS):
    try:
        page.wait_for_load_state(state=state, timeout=t)
    except Exception:
        pass

def close_overlays(page):
    selectors = [
        "button[aria-label*=accept i], button:has-text('Aceptar')",
        "button:has-text('Entendido')",
        "div.cookie *:has-text('Aceptar')",
        "div[role=dialog] button:has-text('OK')",
        "div[aria-label*=close i], button[aria-label*=close i]"
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible():
                el.click()
                human_pause()
        except Exception:
            pass

def scroll_full(page):
    try:
        page.evaluate("""() => new Promise(r=>{
          let y=0; const h=document.body.scrollHeight+800;
          (function step(){ window.scrollBy(0,600); y+=600; 
            if(y<h) requestAnimationFrame(step); else r(); })();
        })""")
    except Exception:
        pass

def find_in_frames(page, patterns, timeout_ms) -> bool:
    end = time.time() + timeout_ms/1000.0
    while time.time() < end:
        for pat in patterns:
            try:
                if page.locator(pat).first.is_visible(): return True
            except Exception: pass
        try:
            for fr in page.frames:
                for pat in patterns:
                    try:
                        if fr.locator(pat).first.is_visible(): return True
                    except Exception: pass
        except Exception: pass
        human_pause(0.35,0.6)
    return False

def click_if_exists(page, selector) -> bool:
    try:
        loc = page.locator(selector).first
        if loc.is_visible():
            loc.scroll_into_view_if_needed(timeout=1500)
            human_pause()
            loc.click(timeout=2000)
            human_pause()
            return True
    except Exception:
        pass
    return False

# ==========================
# Salto Ministerio → citaconsular/bookitit (robusto)
# ==========================
def get_citaconsular_href(page) -> str:
    """Busca un <a> cuyo href contenga citaconsular/bookitit."""
    try:
        anchors = page.locator("a")
        n = min(400, anchors.count())
        for i in range(n):
            try:
                href = anchors.nth(i).get_attribute("href") or ""
                if re.search(r"(citaconsular|bookitit)", href, re.I):
                    return href
            except Exception:
                pass
    except Exception:
        pass
    return ""

def goto_ministry_and_open_widget(context, min_url: str, cons_name: str):
    """
    Abre el Ministerio y hace el salto al widget:
      - intenta click por texto
      - intenta detectar popup con expect_popup
      - si no hay popup, toma el href y hace page.goto(href)
      - último recurso: click por JS + href
    Devuelve la page ya posicionada en el widget (nueva o la misma).
    """
    page = context.new_page()
    page.set_default_timeout(LANDING_TIMEOUT_MS)

    # (opcional) bloquear imágenes para ahorrar datos
    if os.getenv("BLOCK_IMAGES", "ON").upper() == "ON":
        page.route("**/*", lambda r: r.abort() if r.request.resource_type in {"image","font","media"} else r.continue_())

    # Ministerio
    page.goto(min_url, wait_until="domcontentloaded", timeout=LANDING_TIMEOUT_MS)
    safe_wait(page, "networkidle", LANDING_TIMEOUT_MS)
    close_overlays(page)

    if PROOF:
        tele_send_html(page, f"{cons_name.lower()}_ministerio", f"{cons_name}: HTML inicial (ministerio)")
        tele_send_jpg(page, f"{cons_name}: evidencia ministerio")

    # Buscamos el enlace por texto visible
    link_sel_variants = [
        "a:has-text('ELEGIR FECHA Y HORA')",
        "button:has-text('ELEGIR FECHA Y HORA')",
        "a:has-text('Elegir fecha y hora')",
        "a:has-text('ELIGIR FECHA Y HORA')"
    ]
    link = None
    for sel in link_sel_variants:
        try:
            loc = page.locator(sel).first
            if loc.is_visible():
                link = loc
                break
        except Exception:
            pass

    # Si no lo hallamos por texto, intentamos por href
    if not link:
        href = get_citaconsular_href(page)
        if href:
            # ir directo
            page.goto(href, wait_until="domcontentloaded", timeout=LANDING_TIMEOUT_MS)
            safe_wait(page, "networkidle", LANDING_TIMEOUT_MS)
            return page

    # Tenemos un locator clickable → intentar popup
    if link:
        try:
            with page.expect_popup(timeout=5000) as p:
                link.scroll_into_view_if_needed()
                human_pause()
                link.click()
            new_page = p.value
            safe_wait(new_page, "domcontentloaded", LANDING_TIMEOUT_MS)
            safe_wait(new_page, "networkidle", LANDING_TIMEOUT_MS)
            return new_page
        except Exception:
            # Sin popup: ir por href
            try:
                href = link.get_attribute("href") or ""
                if href:
                    page.goto(href, wait_until="domcontentloaded", timeout=LANDING_TIMEOUT_MS)
                    safe_wait(page, "networkidle", LANDING_TIMEOUT_MS)
                    return page
            except Exception:
                pass
            # Último recurso: click por JS y si no, href global
            try:
                page.evaluate("(el)=>el.click()", link.element_handle())
                # pequeño chance a popup
                time.sleep(1.0)
                # si no cambió, usar href global
                href2 = get_citaconsular_href(page)
                if href2:
                    page.goto(href2, wait_until="domcontentloaded", timeout=LANDING_TIMEOUT_MS)
                    safe_wait(page, "networkidle", LANDING_TIMEOUT_MS)
                    return page
            except Exception:
                pass

    # si llegamos aquí, no pudimos saltar
    return page  # devolver ministerio (para evidenciar y fallar con timeout luego)

# ==========================
# Widget helpers
# ==========================
def wait_widget_ready(page, entry_fallback=None) -> bool:
    patterns = [BTN_CONTINUE, NO_SLOTS_TEXT]
    if _wait_widget_once(page, patterns, WIDGET_TIMEOUT_MS):
        return True
    if entry_fallback:
        log_warn("Timeout al esperar widget; intento volver a entrada y re–checar…")
        try:
            page.goto(entry_fallback, wait_until="domcontentloaded", timeout=LANDING_TIMEOUT_MS)
            safe_wait(page, "networkidle", LANDING_TIMEOUT_MS)
            close_overlays(page)
        except Exception:
            pass
        return _wait_widget_once(page, patterns, WIDGET_TIMEOUT_MS)
    return False

def _wait_widget_once(page, patterns, timeout_ms) -> bool:
    end = time.time() + timeout_ms/1000.0
    while time.time() < end:
        close_overlays(page)
        scroll_full(page)
        if find_in_frames(page, patterns, 1200):
            return True
        human_pause(0.4,0.7)
    return False

def click_continue_anywhere(page) -> bool:
    # página
    if click_if_exists(page, BTN_CONTINUE):
        return True
    # iframes
    try:
        for fr in page.frames:
            loc = fr.locator(BTN_CONTINUE).first
            if loc.is_visible():
                loc.click(timeout=2000)
                human_pause()
                return True
    except Exception:
        pass
    return False

def open_panel(page) -> bool:
    # abrir el acordeón/panel
    for _ in range(3):
        if click_if_exists(page, PANEL_HEADER): return True
        # flexible por texto en página
        try:
            loc = page.get_by_text(re.compile(r"presentaci[oó]n\s+documentaci[oó]n", re.I)).first
            if loc.is_visible():
                loc.scroll_into_view_if_needed()
                human_pause(); loc.click(timeout=2000); return True
        except Exception: pass
        # en iframes
        try:
            for fr in page.frames:
                loc = fr.get_by_text(re.compile(r"presentaci[oó]n\s+documentaci[oó]n", re.I)).first
                if loc.is_visible():
                    loc.click(timeout=2000); return True
        except Exception: pass
        human_pause()
    return False

def parse_has_slots(page) -> bool:
    # señal negativa
    if find_in_frames(page, [NO_SLOTS_TEXT], 900): return False
    # señales positivas
    positives = [
        "div.calendar-day.available",
        "button.time-slot, a.time-slot",
        "div#slots-container button, div#slots-container a",
        "a:has-text('Cambiar de día') ~ div button"
    ]
    for sel in positives:
        try:
            if page.locator(sel).first.is_visible(): return True
        except Exception: pass
    try:
        for fr in page.frames:
            for sel in positives:
                try:
                    if fr.locator(sel).first.is_visible(): return True
                except Exception: pass
    except Exception: pass
    return False

# ==========================
# Flujo por consulado
# ==========================
def flow_consulate(context, cons_name: str, ministry_url: str):
    # Abrir ministerio y saltar hacia widget (nueva o misma page)
    page = goto_ministry_and_open_widget(context, ministry_url, cons_name)

    # Esperar widget
    entry_fallback = ministry_url
    ready = wait_widget_ready(page, entry_fallback)
    if not ready:
        if PROOF:
            tele_send_html(page, f"{cons_name.lower()}_error_state", f"{cons_name}: HTML en error")
            tele_send_jpg(page, f"{cons_name}: captura en error")
        raise PWTimeout("timeout esperando widget")

    # Evidencia inicial
    if PROOF:
        tele_send_html(page, f"{cons_name.lower()}_before_check", f"{cons_name}: HTML inicial (widget)")
        tele_send_jpg(page, f"{cons_name}: evidencia inicial (antes de parsear)")

    # Continuar
    click_continue_anywhere(page)

    # Abrir panel
    open_panel(page)
    if PROOF:
        tele_send_html(page, f"{cons_name.lower()}_after_panel", f"{cons_name}: HTML tras abrir panel")
        tele_send_jpg(page, f"{cons_name}: pantalla tras abrir panel")

    # Parseo
    has = parse_has_slots(page)

    # Evidencia final
    if PROOF:
        tele_send_html(page, f"{cons_name.lower()}_final", f"{cons_name}: HTML final — {'SÍ' if has else 'NO'}")
        tele_send_jpg(page, f"{cons_name}: captura final — {'SÍ' if has else 'NO'}")

    # Cerrar pestaña de este consulado si fue popup
    try: page.close()
    except Exception: pass

    return has

# ==========================
# Main loop
# ==========================
CONSULADOS = [
    {"name": "Monterrey",        "ministry": MIN_MTY},
    {"name": "Ciudad de México", "ministry": MIN_CDMX},
]

def play_args_with_proxy():
    if not PROXY_HOST or not PROXY_PORT: return {}
    proxy = {"server": f"http://{PROXY_HOST}:{PROXY_PORT}"}
    if PROXY_USER and PROXY_PASS:
        proxy["username"] = PROXY_USER
        proxy["password"] = PROXY_PASS
    return {"proxy": proxy}

def print_public_ip(context):
    if not SHOW_PUBLIC_IP: return
    try:
        p = context.new_page()
        p.goto("https://api.ipify.org?format=json", timeout=15000)
        txt = p.inner_text("pre, body")
        m = re.search(r'"ip"\s*:\s*"([^"]+)"', txt); ip = m.group(1) if m else txt.strip()
        log_info(f"IP pública: {ip}")
        p.close()
    except Exception:
        pass

def run_round(context):
    results = []
    for cons in CONSULADOS:
        name, url = cons["name"], cons["ministry"]
        try:
            has = flow_consulate(context, name, url)
            results.append((name, has))
        except PWTimeout:
            log_warn(f"{name}: timeout esperando widget.")
            results.append((name, False))
        except Exception as e:
            log_warn(f"{name}: error durante la revisión. {e.__class__.__name__}")
            results.append((name, False))
        human_pause(1.6,2.4)

    for name, has in results:
        tele_send_text(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] {name} → {'HAY huecos' if has else 'sin huecos por ahora.'}")
    return results

def main():
    tele_send_text("[start] Launching bot…")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage","--no-sandbox"])
        context = browser.new_context(**play_args_with_proxy())

        print_public_ip(context)

        while True:
            try:
                run_round(context)
            except Exception as e:
                log_err(f"Fallo de ronda: {e.__class__.__name__}")
            wait_s = random.randint(300, 420)   # 5–7 min
            log_info(f"Esperando {wait_s}s antes de la siguiente ronda…")
            time.sleep(wait_s)

if __name__ == "__main__":
    main()
