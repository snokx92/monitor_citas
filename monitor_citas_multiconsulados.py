#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, io, time, random, re, textwrap, traceback
from datetime import datetime
import requests

# --- JPG helper (opcional) ---
try:
    from PIL import Image  # pip install pillow
    PIL_OK = True
except Exception:
    PIL_OK = False

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ==========================
# Configuración por entorno
# ==========================
TELE_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELE_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PROXY_HOST   = os.getenv("PROXY_HOST", "").strip()
PROXY_PORT   = os.getenv("PROXY_PORT", "").strip()
PROXY_USER   = os.getenv("PROXY_USER", "").strip()
PROXY_PASS   = os.getenv("PROXY_PASS", "").strip()
PROOF        = os.getenv("PROOF", "ON").upper() == "ON"
SHOW_PUBLIC_IP = os.getenv("SHOW_PUBLIC_IP", "ON").upper() == "ON"

# tiempos (ms)
WIDGET_TIMEOUT_MS   = int(os.getenv("WIDGET_TIMEOUT_MS", "70000"))
LANDING_TIMEOUT_MS  = int(os.getenv("LANDING_TIMEOUT_MS", "30000"))
GOTO_RETRIES        = int(os.getenv("GOTO_RETRIES", "2"))
ROTATE_AFTER_BLANK  = int(os.getenv("ROTATE_AFTER_BLANK", "2"))  # sin uso directo, mantenido por compat.

# Esperas humanas entre acciones (seg)
HUMAN_CLICK_MIN = 0.8
HUMAN_CLICK_MAX = 1.8

# URLs Ministerio (desde ahí “Elegir fecha y hora”)
MIN_MTY = "https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx"
MIN_CDMX = "https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx"

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
    """
    Hace screenshot. Si hay Pillow -> JPG; si no -> PNG (fallback).
    """
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

def log_info(msg):  tele_send_text(f"[INFO] {msg}")
def log_warn(msg):  tele_send_text(f"⚠️ {msg}")
def log_err(msg):   tele_send_text(f"❌ {msg}")

# ==========================
# Utilidad Playwright
# ==========================
BTN_CONTINUE     = r'text=/Continue\s*\/\s*Continuar/i'
NO_SLOTS_TEXT    = r'text=/No hay horas disponibles/i'
PANEL_HEADER     = r'text=/PRESENTACION DOCUMENTACION/i'  # CDMX
PANEL_HEADER_MTY = r'text=/PRESENTACION DOCUMENTACION/i'  # MTY (igual frase hoy)

def human_pause(a=HUMAN_CLICK_MIN, b=HUMAN_CLICK_MAX):
    time.sleep(random.uniform(a,b))

def safe_wait_network(page, state="domcontentloaded", t=LANDING_TIMEOUT_MS):
    try:
        page.wait_for_load_state(state=state, timeout=t)
    except Exception:
        pass

def close_overlays(page):
    selectors = [
        "button[aria-label*=accept], button:has-text('Aceptar')",
        "button:has-text('Entendido')",
        "div.cookie *:has-text('Aceptar')",
        "div[role=dialog] button:has-text('OK')",
        "button:has-text('Continuar')",  # a veces es overlay
        "div[aria-label*=close], button[aria-label*=close]"
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
        page.evaluate("""() => new Promise(resolve=>{
            let total = 0;
            const step = () => {
              window.scrollBy(0, 600);
              total += 600;
              if (total < document.body.scrollHeight + 600) requestAnimationFrame(step);
              else resolve();
            };
            step();
        })""")
    except Exception:
        pass

def find_in_frames(page, patterns, timeout_ms) -> bool:
    """
    Busca cualquiera de los selectores en la página y en todos los iframes.
    Devuelve True si alguno aparece visible antes del timeout.
    """
    end = time.time() + (timeout_ms/1000.0)
    while time.time() < end:
        # página principal
        for pat in patterns:
            try:
                if page.locator(pat).first.is_visible():
                    return True
            except Exception:
                pass
        # frames
        try:
            for fr in page.frames:
                for pat in patterns:
                    try:
                        if fr.locator(pat).first.is_visible():
                            return True
                    except Exception:
                        pass
        except Exception:
            pass
        human_pause(0.35, 0.6)
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
# Flujo común hacia el widget
# ==========================
def goto_ministry_and_click(page, min_url: str, cons_name: str):
    """
    1) Ir al ministerio
    2) Capturar evidencia + HTML
    3) Click en “ELEGIR FECHA Y HORA” (target citaconsular)
    """
    for attempt in range(GOTO_RETRIES):
        try:
            page.goto(min_url, wait_until="domcontentloaded", timeout=LANDING_TIMEOUT_MS)
            safe_wait_network(page, "networkidle", LANDING_TIMEOUT_MS)
            close_overlays(page)
            if PROOF:
                tele_send_html(page, f"{cons_name.lower()}_ministerio", f"{cons_name}: HTML inicial (ministerio)")
                tele_send_jpg(page, f"{cons_name}: evidencia ministerio")

            # botón / enlace “ELEGIR FECHA Y HORA”
            # Variantes: mayúsculas, con icono, <a> o <button>, etc.
            found = (
                click_if_exists(page, "a:has-text('ELEGIR FECHA Y HORA')") or
                click_if_exists(page, "button:has-text('ELEGIR FECHA Y HORA')") or
                click_if_exists(page, "a:has-text('Elegir fecha y hora')") or
                click_if_exists(page, "a:has-text('ELIGIR FECHA Y HORA')")
            )
            if not found:
                # A veces el texto está metido en <strong> o similar
                links = page.locator("a")
                count = links.count()
                for i in range(min(200, count)):
                    try:
                        t = links.nth(i).inner_text().strip()
                        if re.search(r"elegir\s+fecha\s+y\s+hora", t, re.I):
                            links.nth(i).scroll_into_view_if_needed()
                            human_pause()
                            links.nth(i).click()
                            found = True
                            break
                    except Exception:
                        pass
            if found:
                return True
        except Exception:
            pass
    return False

def wait_widget_ready(page, entry_fallback=None) -> bool:
    """
    Espera a ver:
      - botón Continuar, o
      - texto “No hay horas…”
    Incluye búsqueda en iframes y scroll. Si no aparece en WIDGET_TIMEOUT_MS:
      - reintenta 1 vez (si entry_fallback se pasó) reingresando al enlace
    """
    patterns = [BTN_CONTINUE, NO_SLOTS_TEXT]
    start_try = _wait_widget_once(page, patterns, WIDGET_TIMEOUT_MS)
    if start_try:
        return True

    if entry_fallback:
        log_warn("Timeout al esperar widget; intento volver a entrada y re-checar…")
        try:
            page.goto(entry_fallback, wait_until="domcontentloaded", timeout=LANDING_TIMEOUT_MS)
            safe_wait_network(page, "networkidle", LANDING_TIMEOUT_MS)
            close_overlays(page)
        except Exception:
            pass
        return _wait_widget_once(page, patterns, WIDGET_TIMEOUT_MS)

    return False

def _wait_widget_once(page, patterns, timeout_ms) -> bool:
    # Hacer scroll y cerrar overlays mientras esperamos
    end = time.time() + (timeout_ms/1000.0)
    while time.time() < end:
        close_overlays(page)
        scroll_full(page)
        if find_in_frames(page, patterns, 1200):
            return True
        human_pause(0.4, 0.7)
    return False

def click_continue_anywhere(page) -> bool:
    # En página o iframes
    if click_if_exists(page, BTN_CONTINUE):
        return True
    try:
        for fr in page.frames:
            if fr.locator(BTN_CONTINUE).first.is_visible():
                fr.locator(BTN_CONTINUE).first.click(timeout=2000)
                human_pause()
                return True
    except Exception:
        pass
    return False

def open_panel(page, cons_name: str) -> bool:
    # Abrir el acordeón/panel “PRESENTACIÓN…”
    header_sel = PANEL_HEADER_MTY if "Monterrey" in cons_name else PANEL_HEADER
    # Intentar varias veces (algunos sitios mueven el DOM)
    for _ in range(3):
        if click_if_exists(page, header_sel):
            return True
        # Búsqueda flexible por texto
        try:
            anyh = page.get_by_text(re.compile(r"presentaci[oó]n\s+documentaci[oó]n", re.I)).first
            if anyh.is_visible():
                anyh.scroll_into_view_if_needed()
                human_pause()
                anyh.click(timeout=2000)
                return True
        except Exception:
            pass
        # también en iframes
        try:
            for fr in page.frames:
                h = fr.get_by_text(re.compile(r"presentaci[oó]n\s+documentaci[oó]n", re.I)).first
                if h.is_visible():
                    h.click(timeout=2000)
                    return True
        except Exception:
            pass
        human_pause()
    return False

def parse_has_slots(page) -> bool:
    """
    Heurística de citas: si NO aparece “No hay horas…”
    y sí aparece una celda clickable de horario/fecha,
    lo tratamos como “hay huecos”.
    """
    # Señal negativa:
    try:
        if find_in_frames(page, [NO_SLOTS_TEXT], 800):
            return False
    except Exception:
        pass

    # Señales positivas típicas (bookitit):
    positives = [
        "div.calendar-day.available",
        "button.time-slot, a.time-slot",
        "div#slots-container button, div#slots-container a",
        "a:has-text('Cambiar de día') ~ div button"  # layout visto
    ]
    # Página
    for sel in positives:
        try:
            if page.locator(sel).first.is_visible():
                return True
        except Exception:
            pass
    # Iframes
    try:
        for fr in page.frames:
            for sel in positives:
                try:
                    if fr.locator(sel).first.is_visible():
                        return True
                except Exception:
                    pass
    except Exception:
        pass
    return False

# ==========================
# Flujo genérico por consulado
# ==========================
def generic_flow(page, cons_name: str, ministry_url: str):
    # 1. Ministerio
    ok = goto_ministry_and_click(page, ministry_url, cons_name)
    if not ok:
        log_warn(f"{cons_name}: no pude abrir 'Elegir fecha y hora'.")
        return False

    # 2. Esperar widget (con reentrada)
    entry_fallback = ministry_url
    ctx_ready = wait_widget_ready(page, entry_fallback)
    if not ctx_ready:
        # evidencia de error
        if PROOF:
            tele_send_html(page, f"{cons_name.lower()}_error_state", f"{cons_name}: HTML en error")
            tele_send_jpg(page, f"{cons_name}: captura en error")
        raise PWTimeout("Widget no apareció")

    # 3. Evidencia inicial (antes de parsear)
    if PROOF:
        tele_send_html(page, f"{cons_name.lower()}_before_check", f"{cons_name}: HTML inicial (widget listo)")
        tele_send_jpg(page, f"{cons_name}: evidencia inicial (antes de parsear)")

    # 4. Si aparece “Continuar”, clic
    click_continue_anywhere(page)

    # 5. Abrir panel
    open_panel(page, cons_name)
    if PROOF:
        tele_send_html(page, f"{cons_name.lower()}_after_panel", f"{cons_name}: HTML tras abrir panel")
        tele_send_jpg(page, f"{cons_name}: pantalla tras abrir panel")

    # 6. Parsear si hay huecos
    has = parse_has_slots(page)

    # 7. Evidencia final
    if PROOF:
        name = f"{cons_name.lower()}_final"
        tele_send_html(page, name, f"{cons_name}: HTML final — {'SÍ' if has else 'NO'}")
        tele_send_jpg(page, f"{cons_name}: captura final — {'SÍ' if has else 'NO'}")

    return has

# ==========================
# Main loop
# ==========================
CONSULADOS = [
    {"name": "Monterrey",       "ministry": MIN_MTY},
    {"name": "Ciudad de México","ministry": MIN_CDMX},
]

def play_args_with_proxy():
    if not PROXY_HOST or not PROXY_PORT:
        return {}
    proxy = {"server": f"http://{PROXY_HOST}:{PROXY_PORT}"}
    if PROXY_USER and PROXY_PASS:
        proxy["username"] = PROXY_USER
        proxy["password"] = PROXY_PASS
    return {"proxy": proxy}

def print_public_ip(context):
    if not SHOW_PUBLIC_IP:
        return
    try:
        page = context.new_page()
        page.goto("https://api.ipify.org?format=json", timeout=15000)
        data = page.inner_text("pre, body")
        match = re.search(r'"ip"\s*:\s*"([^"]+)"', data)
        ip = match.group(1) if match else data.strip()
        log_info(f"IP pública: {ip}")
        page.close()
    except Exception:
        pass

def run_round(context):
    page = context.new_page()

    # Bloqueo de imágenes opcional para ahorrar datos (mantengo compatibilidad)
    if os.getenv("BLOCK_IMAGES", "ON").upper() == "ON":
        page.route("**/*", lambda route: route.abort() if route.request.resource_type in {"image","media","font"} else route.continue_())

    results = []
    for cons in CONSULADOS:
        name = cons["name"]
        min_url = cons["ministry"]
        try:
            has = generic_flow(page, name, min_url)
            results.append((name, has))
        except PWTimeout:
            log_warn(f"{name}: timeout esperando widget.")
            # evidencia corta ya se mandó en generic_flow si estaba PROOF
            results.append((name, False))
        except Exception as e:
            # Dump corto
            msg = f"{name}: error durante la revisión.\n" + str(e.__class__.__name__)
            log_warn(msg)
            try:
                tele_send_html(page, f"{name.lower()}_error_state", f"{name}: HTML en error")
                tele_send_jpg(page, f"{name}: captura en error")
            except Exception:
                pass
            results.append((name, False))
        # pausa humana entre consulados
        human_pause(1.6, 2.4)

    page.close()
    # resumen corto
    for name, has in results:
        tele_send_text(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] {name} → {'HAY huecos' if has else 'sin huecos por ahora.'}")
    return results

def main():
    tele_send_text("[start] Launching bot…")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage","--no-sandbox"])
        context = browser.new_context(**play_args_with_proxy())
        print_public_ip(context)

        # bucle infinito con espera aleatoria 5–7 min
        while True:
            try:
                run_round(context)
            except Exception as e:
                log_err("Fallo de ronda: " + str(e))
            # 5–7 minutos aleatorio
            wait_s = random.randint(300, 420)
            log_info(f"Esperando {wait_s}s antes de la siguiente ronda…")
            time.sleep(wait_s)

if __name__ == "__main__":
    main()
