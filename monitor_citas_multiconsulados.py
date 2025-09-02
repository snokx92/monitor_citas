# -*- coding: utf-8 -*-
"""
monitor_citas_multiconsulados.py
Bot Playwright + Telegram para monitorear huecos en citaconsular.es
- Flujos robustos para Monterrey y Ciudad de México.
- Entra desde páginas del Ministerio -> "ELEGIR FECHA Y HORA" -> citaconsular.es
- Evidencias JPG + HTML.
- Errores compactos (y detallados si VERBOSE_ERRORS=1).
"""

import os
import io
import re
import time
import json
import random
import traceback
from datetime import datetime

import requests
from PIL import Image

from playwright.sync_api import (
    sync_playwright, TimeoutError as PWTimeout, Error as PWError, expect
)

# --------------------------
# Config desde variables env
# --------------------------
ENV = os.getenv

TELE_TOKEN = ENV("TELEGRAM_BOT_TOKEN", "").strip()
TELE_CHAT  = ENV("TELEGRAM_CHAT_ID", "").strip()

PROXY_HOST = ENV("PROXY_HOST", "").strip()
PROXY_PORT = ENV("PROXY_PORT", "").strip()
PROXY_USER = ENV("PROXY_USER", "").strip()
PROXY_PASS = ENV("PROXY_PASS", "").strip()

SHOW_PUBLIC_IP = ENV("SHOW_PUBLIC_IP", "1") == "1"
BLOCK_IMAGES    = ENV("BLOCK_IMAGES", "1") == "1"
DEBUG_STEPS     = ENV("DEBUG_STEPS", "0") == "1"
PROOF           = ENV("PROOF", "1") == "1"
VERBOSE_ERRORS  = ENV("VERBOSE_ERRORS", "0") == "1"

ROUND_MIN_SEC   = int(ENV("ROUND_MIN_SEC", "300"))
ROUND_MAX_SEC   = int(ENV("ROUND_MAX_SEC", "420"))

LANDING_TIMEOUT_MS = int(ENV("LANDING_TIMEOUT_MS", "30000"))
WIDGET_TIMEOUT_MS  = int(ENV("WIDGET_TIMEOUT_MS", "70000"))

# Selectores / patrones
BTN_CONTINUE      = "text=/Continue\\s*\\/\\s*Continuar/i"
NO_SLOTS_TEXT     = "text=/No hay horas disponibles/i"
CDMX_PANEL_CARD   = "text=/PRESENTACION\\s+DOCUMENTACION\\s+LEY\\s+MEMORIA\\s+DEMOCRATICA/i"
SPINNER_SEL       = "css=div[role=progressbar], css=.spinner, text=/Loading/i"

# Entradas del Ministerio (botón "ELEGIR FECHA Y HORA")
MIN_CDMX = "https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx"
MIN_MTY  = "https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx"

# -----------------------------------------------------------------------------------
# Utilidades Telegram
# -----------------------------------------------------------------------------------
def tele_send_text(msg: str):
    if not TELE_TOKEN or not TELE_CHAT:
        print(msg)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage",
            data={"chat_id": TELE_CHAT, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15
        )
    except Exception as e:
        print(f"[TELE] sendMessage error: {e}")

def tele_send_doc(bytes_data: bytes, filename: str, caption: str = ""):
    if not TELE_TOKEN or not TELE_CHAT:
        return
    files = {"document": (filename, bytes_data)}
    data = {"chat_id": TELE_CHAT, "caption": caption}
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELE_TOKEN}/sendDocument",
            data=data, files=files, timeout=30
        )
    except Exception as e:
        print(f"[TELE] sendDocument error: {e}")

def tele_send_jpg(page, caption: str, quality: int = 82):
    """Screenshot a JPG (full page) and send."""
    # Playwright genera PNG; convertimos a JPG en memoria
    png_bytes = page.screenshot(full_page=True)
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    tele_send_doc(out.getvalue(), "capture.jpg", caption)

def save_html_and_send(page, name: str, caption: str):
    html = page.content().encode("utf-8")
    tele_send_doc(html, f"{name}.html", caption)

# -----------------------------------------------------------------------------------
# Auxiliares Playwright
# -----------------------------------------------------------------------------------
def human_sleep(min_ms=300, max_ms=900):
    time.sleep(random.uniform(min_ms/1000.0, max_ms/1000.0))

def wait_network_quiet(page, idle_ms=800, timeout_ms=10000):
    """Espera momento de red ociosa."""
    with page.expect_event("requestfinished", timeout=timeout_ms) as _:
        pass
    time.sleep(idle_ms/1000.0)

def any_frame_has(page, selector: str):
    """Devuelve primer frame/locator visible con selector (incluye main frame)."""
    try:
        if page.locator(selector).first.is_visible():
            return page.locator(selector).first
    except Exception:
        pass
    for fr in page.frames:
        try:
            loc = fr.locator(selector).first
            if loc.is_visible():
                return loc
        except Exception:
            continue
    return None

def click_if_visible(page_or_frame, selector: str, name_for_log: str = "", delay_ms=(150, 400)):
    try:
        loc = page_or_frame.locator(selector).first
        if loc.is_visible():
            human_sleep(*delay_ms)
            loc.click()
            return True
    except Exception:
        pass
    return False

def block_images_route(page):
    if not BLOCK_IMAGES:
        return
    page.route("**/*", lambda route: route.abort() if route.request.resource_type == "image" else route.continue_())

# -----------------------------------------------------------------------------------
# Esperas del widget
# -----------------------------------------------------------------------------------
def wait_widget_ready(page, entry_fallback: str | None = None) -> bool:
    """
    Espera a que aparezca el botón Continuar O el texto 'No hay horas...'
    Busca en página y en iframes. Si no aparece, intenta un 'rescate' opcional:
    volver al entry (entry_fallback).
    """
    start = time.time()
    deadline = start + (WIDGET_TIMEOUT_MS / 1000.0)

    while time.time() < deadline:
        # ¿Ya está alguno?
        loc = any_frame_has(page, f"{BTN_CONTINUE}, {NO_SLOTS_TEXT}")
        if loc:
            return True

        # A veces hay overlays/banners: intentamos cerrarlos si existen
        # (no hay selectores fijos, probamos algunos patrones comunes)
        for sel in [
            "text=/Aceptar/i", "text=/Entiendo/i", "text=/Close/i", "role=button[name=/Aceptar|Close|Cerrar/i]"
        ]:
            try:
                if click_if_visible(page, sel, "overlay-close"):
                    human_sleep(200, 500)
            except Exception:
                pass

        # pequeños scrolls para disparar carga perezosa
        try:
            page.mouse.wheel(0, random.randint(150, 350))
        except Exception:
            pass

        human_sleep(320, 640)

    # Si llega aquí: timeout
    if entry_fallback:
        tele_send_text("⚠️ Timeout al esperar widget; intento volver a entrada y re-checar…")
        try:
            page.goto(entry_fallback, timeout=LANDING_TIMEOUT_MS, wait_until="domcontentloaded")
            human_sleep(600, 1000)
            # Reintento corto
            loc = any_frame_has(page, f"{BTN_CONTINUE}, {NO_SLOTS_TEXT}")
            if loc:
                return True
        except Exception:
            pass

    raise PWTimeout(
        f"No apareció Continuar ni 'No hay horas...' tras {WIDGET_TIMEOUT_MS}ms (incluyendo iframes/banners)."
    )

def wait_spinner_gone(page, max_ms=15000):
    """Intenta esperar a que desaparezca un spinner típico antes de la foto final."""
    end = time.time() + (max_ms/1000.0)
    while time.time() < end:
        try:
            sp = any_frame_has(page, SPINNER_SEL)
            if not sp:
                return True
        except Exception:
            return True
        human_sleep(150, 300)
    return False

# -----------------------------------------------------------------------------------
# Flujos por consulado
# -----------------------------------------------------------------------------------
def flow_ministry_to_widget(page, ministry_url: str, cons_name: str, panel_selector: str | None) -> str:
    """
    Entra a la página del Ministerio y hace click en 'ELEGIR FECHA Y HORA'.
    Devuelve la URL actual (ya en citaconsular) para usar como entry_fallback.
    Si panel_selector (CDMX), hace click en el panel específico antes del widget.
    """
    page.goto(ministry_url, timeout=LANDING_TIMEOUT_MS, wait_until="domcontentloaded")
    block_images_route(page)
    human_sleep(700, 1200)
    if PROOF or DEBUG_STEPS:
        save_html_and_send(page, f"{cons_name.lower()}_ministerio", f"{cons_name}: HTML inicial (ministerio)")
        tele_send_jpg(page, f"{cons_name}: evidencia ministerio")

    # Click en enlace 'ELEGIR FECHA Y HORA'
    # evitamos anchors movibles: buscamos por texto
    link = page.get_by_text(re.compile(r"ELEGIR\s+FECHA\s+Y\s+HORA", re.I)).first
    link.wait_for(state="visible", timeout=LANDING_TIMEOUT_MS)
    human_sleep(250, 550)
    link.click()
    # Debe abrir la agenda (misma pestaña o nueva); esperamos navegar
    page.wait_for_load_state("domcontentloaded", timeout=LANDING_TIMEOUT_MS)
    human_sleep(600, 1200)
    entry_url = page.url

    # En CDMX hay tarjeta intermedia: “PRESENTACION DOCUMENTACION…”
    if panel_selector:
        try:
            # a veces tarda en pintar; esperamos a que exista y se pueda clickar
            page.get_by_text(re.compile(r"PRESENTACION\s+DOCUMENTACION", re.I)).first.wait_for(
                state="visible", timeout=LANDING_TIMEOUT_MS
            )
            human_sleep(400, 800)
            page.locator(panel_selector).first.click()
            page.wait_for_load_state("domcontentloaded", timeout=LANDING_TIMEOUT_MS)
            human_sleep(600, 1000)
            if PROOF or DEBUG_STEPS:
                save_html_and_send(page, f"{cons_name.lower()}_after_panel", f"{cons_name}: HTML tras abrir panel")
                tele_send_jpg(page, f"{cons_name}: pantalla tras abrir panel")
        except Exception as e:
            # No es fatal: puede no existir en MTY
            if DEBUG_STEPS:
                tele_send_text(f"ℹ️ {cons_name}: sin panel intermedio o click omitido ({e}).")

    return entry_url

def generic_flow(page, cons_name: str, needs_panel: bool, ministry_url: str):
    """
    Flujo general:
    1) Ministerio -> Elegir fecha/hora (+ panel en CDMX).
    2) Espera 'Continuar' o 'No hay horas...'.
    3) Si hay 'Continuar', click + abrir panel de horarios del día.
    4) Revisar si hay bloques (no mostrar 'No hay horas…').
    5) Evidencias y respuesta.
    """
    panel_selector = CDMX_PANEL_CARD if needs_panel else None
    entry = flow_ministry_to_widget(page, ministry_url, cons_name, panel_selector)

    # Paso 2: widget listo
    ctx_ready = wait_widget_ready(page, entry_fallback=entry)

    # Evidencia pre-continuar
    if PROOF or DEBUG_STEPS:
        save_html_and_send(page, f"{cons_name.lower()}_before_check", f"{cons_name}: HTML inicial (widget listo)")
        tele_send_jpg(page, f"{cons_name}: evidencia inicial (widget)")

    # Si se ve “No hay horas…”, reportamos de una
    no_text = any_frame_has(page, NO_SLOTS_TEXT)
    if no_text:
        if PROOF:
            save_html_and_send(page, f"{cons_name.lower()}_no_final", f"{cons_name}: HTML final — NO")
            tele_send_jpg(page, f"{cons_name}: captura final — NO")
        return False

    # Click en Continuar
    btn = any_frame_has(page, BTN_CONTINUE)
    if btn:
        human_sleep(250, 600)
        btn.click()
        page.wait_for_load_state("domcontentloaded", timeout=LANDING_TIMEOUT_MS)
        human_sleep(600, 1100)
        if PROOF or DEBUG_STEPS:
            save_html_and_send(page, f"{cons_name.lower()}_after_continue", f"{cons_name}: HTML tras 'Continuar'")
            tele_send_jpg(page, f"{cons_name}: pantalla tras 'Continuar'")

    # Intentar abrir panel de horarios del día (bloque grande con hora)
    # Si existe “Cambiar de día” y debajo bloques/hora -> intentamos click panel
    opened_panel = False
    try:
        # un click al bloque de hora si aparece
        blk = page.locator("css=[role=button], text=/\\d{1,2}:\\d{2}/").first
        if blk.is_visible():
            human_sleep(250, 500)
            blk.click()
            human_sleep(500, 900)
            opened_panel = True
    except Exception:
        pass

    # Captura final (esperando que se vaya spinner si lo hay)
    wait_spinner_gone(page, max_ms=12000)
    if PROOF:
        save_html_and_send(page, f"{cons_name.lower()}_final", f"{cons_name}: HTML final")
        tele_send_jpg(page, f"{cons_name}: captura final — {'OK' if opened_panel else 'NO'}")

    # Heurística de presencia de huecos:
    # Si NO aparece el texto 'No hay horas...', asumimos que hay panel/horarios.
    no_text = any_frame_has(page, NO_SLOTS_TEXT)
    return not bool(no_text)

# -----------------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------------
CONSULADOS = [
    {
        "name": "Monterrey",
        "needs_panel": False,
        "entry": MIN_MTY,
    },
    {
        "name": "Ciudad de México",
        "needs_panel": True,
        "entry": MIN_CDMX,
    },
]

def build_launch_args():
    args = [
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--force-color-profile=srgb",
        "--hide-scrollbars",
        "--mute-audio",
    ]
    return args

def build_proxy():
    if not PROXY_HOST or not PROXY_PORT:
        return None
    if PROXY_USER and PROXY_PASS:
        return {
            "server": f"http://{PROXY_HOST}:{PROXY_PORT}",
            "username": PROXY_USER,
            "password": PROXY_PASS,
        }
    return {"server": f"http://{PROXY_HOST}:{PROXY_PORT}"}

def public_ip() -> str:
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=8)
        return r.json().get("ip", "?")
    except Exception:
        return "?"

def run_round(browser):
    msg_hdr = "[INFO]"
    if SHOW_PUBLIC_IP:
        ip = public_ip()
        tele_send_text(f"{msg_hdr} IP pública: <code>{ip}</code>")

    for cons in CONSULADOS:
        name = cons["name"]
        needs_panel = cons["needs_panel"]
        entry = cons["entry"]

        ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1366, "height": 850})
        page = ctx.new_page()
        try:
            ok = generic_flow(page, name, needs_panel, entry)
            stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            if ok:
                tele_send_text(f"[{stamp}] {name} → <b>POSIBLES HUECOS</b> (no se detectó 'No hay horas...').")
            else:
                tele_send_text(f"[{stamp}] {name} → sin huecos por ahora.")
        except (PWTimeout, PWError) as e:
            # Error compacto
            short = str(e)
            if not VERBOSE_ERRORS:
                # recortamos a una línea breve
                short = re.sub(r"\s+", " ", short)
                short = (short[:400] + "…") if len(short) > 400 else short
                tele_send_text(f"⚠️ {name}: error durante la revisión.\n<code>{short}</code>")
            else:
                # Detalle extendido
                tb = traceback.format_exc(limit=8)
                tele_send_text(f"⚠️ {name}: error durante la revisión.\n<code>{short}</code>\n<pre>{tb}</pre>")
            # Enviar HTML/captura actual para diagnóstico
            try:
                if PROOF or DEBUG_STEPS:
                    save_html_and_send(page, f"{name.lower()}_error_state", f"{name}: HTML en error")
                    tele_send_jpg(page, f"{name}: captura en error")
            except Exception:
                pass
        finally:
            try:
                ctx.close()
            except Exception:
                pass

def main():
    tele_send_text("🚀 Bot iniciado. Config: proof="
                   f"{'ON' if PROOF else 'OFF'} debug={'ON' if DEBUG_STEPS else 'OFF'} block_images={'ON' if BLOCK_IMAGES else 'OFF'}")
    proxy = build_proxy()

    with sync_playwright() as p:
        launch_opts = {
            "headless": True,
            "args": build_launch_args(),
        }
        if proxy:
            launch_opts["proxy"] = proxy

        browser = p.chromium.launch(**launch_opts)

        # Bucle principal
        while True:
            run_round(browser)
            # espera humanizada entre rondas
            wait_s = random.randint(ROUND_MIN_SEC, ROUND_MAX_SEC)
            tele_send_text(f"[INFO] Esperando {wait_s}s antes de la siguiente ronda…")
            time.sleep(wait_s)

if __name__ == "__main__":
    main()
