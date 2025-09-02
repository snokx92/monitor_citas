#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import io
import sys
import time
import random
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

import requests
from PIL import Image
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PWTimeout,
    Page,
    BrowserContext,
)

# =========================
# Config vía variables env
# =========================

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BLOCK_IMAGES = os.getenv("BLOCK_IMAGES", "1") in ("1", "true", "True")
DEBUG_STEPS = os.getenv("DEBUG_STEPS", "0") in ("1", "true", "True")
SHOW_PUBLIC_IP = os.getenv("SHOW_PUBLIC_IP", "1") in ("1", "true", "True")

# Esperas y tiempos
WIDGET_TIMEOUT_MS = int(os.getenv("WIDGET_TIMEOUT_MS", "70000"))
PANEL_TIMEOUT_MS = int(os.getenv("PANEL_TIMEOUT_MS", "25000"))
LANDING_TIMEOUT_MS = int(os.getenv("LANDING_TIMEOUT_MS", "25000"))

ROUND_MIN_SEC = int(os.getenv("ROUND_MIN_SEC", "300"))  # 5 min
ROUND_MAX_SEC = int(os.getenv("ROUND_MAX_SEC", "420"))  # 7 min

# Calidad de JPEG
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "70"))

# Proxy (opcional)
PROXY_HOST = os.getenv("PROXY_HOST", "").strip()
PROXY_PORT = os.getenv("PROXY_PORT", "").strip()
PROXY_USER = os.getenv("PROXY_USER", "").strip()
PROXY_PASS = os.getenv("PROXY_PASS", "").strip()
PROXY_SESSION_IN_USER = os.getenv("PROXY_SESSION_IN_USER", "0") in ("1","true","True")

# Reaccionar ante “página vacía”
ROTATE_AFTER_BLANK = os.getenv("ROTATE_AFTER_BLANK", "0") in ("1","true","True")
ROTATE_COOLDOWN_SEC = int(os.getenv("ROTATE_COOLDOWN_SEC", "30"))

# Rutas de entrada del Ministerio
URL_MONTERREY_MIN = "https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx"
URL_CDMX_MIN     = "https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx"

# Selectores y textos comunes
BTN_ELEGIR = re.compile(r"ELEGIR\s+FECHA\s+Y\s+HORA", re.I)
BTN_CONTINUE = re.compile(r"Continue\s*/\s*Continuar", re.I)
NO_SLOTS_TEXT = re.compile(r"No\s+hay\s+horas\s+disponibles", re.I)
CARD_LMD = re.compile(r"PRESENTACI[ÓO]N\s+DOCUMENTACI[ÓO]N.*(LEY|LMD)", re.I)
PANEL_OPEN_HINTS = [re.compile(r"Cambiar\s+de\s+d[ií]a", re.I),
                    re.compile(r"Change\s+day", re.I),
                    re.compile(r"Seleccionar\s+fecha", re.I)]

SPINNER_HINTS = [
    re.compile(r"Loading", re.I),
]

# =========================
# Utilidades
# =========================

def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def log(msg: str):
    print(msg, flush=True)

def tg_send_text(text: str):
    if not TG_TOKEN or not TG_CHAT:
        log(f"[TG] {text}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as e:
        log(f"[TG] error sendMessage: {e}")

def _jpeg_bytes_from_png_bytes(png_bytes: bytes, quality: int = 70) -> bytes:
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    bio = io.BytesIO()
    im.save(bio, format="JPEG", quality=quality, optimize=True)
    return bio.getvalue()

def tg_send_bytes_as_file(bts: bytes, filename: str, caption: str = ""):
    if not TG_TOKEN or not TG_CHAT:
        log(f"[TG FILE] {filename} ({len(bts)} bytes). {caption}")
        return
    files = {"document": (filename, bts)}
    data = {"chat_id": TG_CHAT, "caption": caption}
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
            data=data, files=files, timeout=30
        )
    except Exception as e:
        log(f"[TG] error sendDocument: {e}")

def send_html(page: Page, name: str, tag: str, caption: str):
    html = page.content()
    tg_send_bytes_as_file(html.encode("utf-8"),
                          f"{name}_{tag}.html",
                          f"{name}: {caption}")

def send_jpeg(page: Page, name: str, tag: str, caption: str, full: bool = True):
    # Playwright no exporta directamente JPEG: tiramos a PNG y convertimos a JPEG
    png_bytes = page.screenshot(full_page=full)
    jpg = _jpeg_bytes_from_png_bytes(png_bytes, JPEG_QUALITY)
    tg_send_bytes_as_file(jpg, f"{name}_{tag}.jpg", f"{name}: {caption}")

def sleep_human(a: float, b: float):
    t = random.uniform(a, b)
    time.sleep(t)

def short_err(name: str, txt: str):
    tg_send_text(f"⚠️ <b>{name}</b>: {txt}")

def show_public_ip_through_proxy():
    if not SHOW_PUBLIC_IP:
        return
    try:
        ip = requests.get("https://api.ipify.org", timeout=10).text
        tg_send_text(f"[INFO] IP pública: <code>{ip}</code>")
    except Exception as e:
        log(f"[IP] {e}")

def build_proxy_setting():
    if not PROXY_HOST or not PROXY_PORT:
        return None
    creds = ""
    if PROXY_USER:
        user = PROXY_USER
        if PROXY_SESSION_IN_USER and "{rnd}" in user:
            user = user.replace("{rnd}", f"{random.randint(100000,999999)}")
        creds = f"{user}:{PROXY_PASS}@"
    return f"http://{creds}{PROXY_HOST}:{PROXY_PORT}"

# =========================
# Playwright helpers
# =========================

def block_assets(route, request):
    if request.resource_type in ("image", "media", "font"):
        return route.abort()
    return route.continue_()

def wait_for_network_and_idle(page: Page, hard_ms: int = 8000):
    try:
        page.wait_for_load_state("networkidle", timeout=hard_ms)
    except PWTimeout:
        pass

def close_banners(page: Page):
    # Intentos suaves por textos comunes
    texts = [r"Aceptar", r"Entendido", r"Continuar", r"Rechazar", r"Cerrar"]
    for t in texts:
        try:
            el = page.get_by_text(re.compile(t, re.I)).first
            if el.is_visible():
                el.click(timeout=2000)
                sleep_human(0.2, 0.6)
        except Exception:
            pass

def any_spinner_visible(page: Page) -> bool:
    try:
        for rx in SPINNER_HINTS:
            if page.get_by_text(rx).first.is_visible():
                return True
    except Exception:
        pass
    # fallback: elementos con clase loading
    try:
        if page.locator("[class*=load],[class*=spinner]").first.is_visible():
            return True
    except Exception:
        pass
    return False

def wait_spinner_gone(page: Page, timeout_ms: int):
    end = time.time() + (timeout_ms / 1000)
    while time.time() < end:
        if not any_spinner_visible(page):
            return True
        time.sleep(0.25)
    return False

def expect_new_tab_click(context: BrowserContext, page: Page, locator_text_regex):
    # Click que abre nueva pestaña; hacemos expect_page con timeout corto
    with context.expect_page(timeout=10000) as pinfo:
        page.get_by_text(locator_text_regex).first.click(timeout=8000)
    newp = pinfo.value
    newp.bring_to_front()
    return newp

def click_if_visible(page: Page, text_rx, timeout_ms=6000) -> bool:
    try:
        el = page.get_by_text(text_rx).first
        el.wait_for(state="visible", timeout=timeout_ms)
        el.click()
        return True
    except Exception:
        return False

def find_continue_or_nohours(page: Page) -> Optional[str]:
    try:
        if page.get_by_text(NO_SLOTS_TEXT).first.is_visible():
            return "nohours"
    except Exception:
        pass
    try:
        if page.get_by_text(BTN_CONTINUE).first.is_visible():
            return "continue"
    except Exception:
        pass
    return None

def open_day_panel(page: Page) -> bool:
    # Tratamos múltiples textos posibles
    for rx in PANEL_OPEN_HINTS:
        if click_if_visible(page, rx, timeout_ms=3000):
            return True
    # Fallback: botón dentro de la franja superior
    try:
        page.locator("button, a").filter(has_text=re.compile(r"(día|day)", re.I)).first.click(timeout=2000)
        return True
    except Exception:
        return False

def detect_slots_by_text(page: Page) -> bool:
    html = page.content()
    # Si aparece el “no hay horas”, directamente NO
    if re.search(NO_SLOTS_TEXT, html):
        return False
    # Detectar horas como 9:00 / 12:30 en celdas
    if re.search(r'\b([01]?\d|2[0-3]):[0-5]\d\b', html):
        return True
    # Detectar botones “Reservar”/“Siguiente” típicos de disponibilidad
    if re.search(r"Reservar|Seleccione hora|Seleccionar hora", html, re.I):
        return True
    return False

# =========================
# Flujos por consulado
# =========================

def go_via_ministry(context: BrowserContext, entry_url: str, cons_name: str) -> Page:
    page = context.new_page()
    page.set_default_timeout(LANDING_TIMEOUT_MS)
    if BLOCK_IMAGES:
        page.route("**/*", lambda route, req: block_assets(route, req))
    log(f"[{cons_name}] goto ministerio…")
    page.goto(entry_url, wait_until="domcontentloaded")
    wait_for_network_and_idle(page)
    close_banners(page)
    send_html(page, cons_name, "ministerio", "HTML inicial (ministerio)")
    send_jpeg(page, cons_name, "ministerio", "evidencia ministerio", full=True)

    # Click “ELEGIR FECHA Y HORA” -> nueva pestaña (citaconsular/bookitit)
    try:
        newp = expect_new_tab_click(context, page, BTN_ELEGIR)
    except PWTimeout:
        # A veces abre en MISMO tab si bloquea popups
        if click_if_visible(page, BTN_ELEGIR, timeout_ms=8000):
            newp = page
        else:
            raise
    return newp

def wait_widget_ready(page: Page, cons_name: str) -> Optional[str]:
    # Espera compuesta:
    # 1) networkidle
    # 2) cerrar banners
    # 3) esperar que aparezca “Continuar” o “No hay horas…”
    # 4) tolerar iframes
    wait_for_network_and_idle(page, hard_ms=12000)
    close_banners(page)

    # Tolerar spinner
    wait_spinner_gone(page, min(6000, WIDGET_TIMEOUT_MS))

    outcome = find_continue_or_nohours(page)
    if outcome:
        return outcome

    # Escaneo de iframes simples
    try:
        for f in page.frames:
            try:
                if f.get_by_text(NO_SLOTS_TEXT).first.is_visible():
                    return "nohours"
            except Exception:
                pass
            try:
                if f.get_by_text(BTN_CONTINUE).first.is_visible():
                    # Hacemos click desde el frame
                    f.get_by_text(BTN_CONTINUE).first.click()
                    return "continue_clicked"
            except Exception:
                pass
    except Exception:
        pass

    # Espera final dura
    end = time.time() + (WIDGET_TIMEOUT_MS / 1000)
    while time.time() < end:
        outcome = find_continue_or_nohours(page)
        if outcome:
            return outcome
        time.sleep(0.3)

    return None

def cdmx_extra_after_continue(page: Page, cons_name: str):
    # En CDMX aparece la tarjeta con el texto largo, damos click.
    clicked = click_if_visible(page, CARD_LMD, timeout_ms=6000)
    if clicked:
        wait_for_network_and_idle(page, hard_ms=12000)
        wait_spinner_gone(page, 8000)
        send_html(page, cons_name, "after_panel", "HTML tras abrir panel")
        send_jpeg(page, cons_name, "after_panel", "pantalla tras abrir panel")
    else:
        # no pasa nada si no aparece (a veces entra directo)
        pass

def generic_flow(context: BrowserContext,
                 entry_url: str,
                 cons_name: str,
                 needs_panel: bool,
                 do_cdmx_extra: bool = False) -> Tuple[bool, str]:
    """
    Devuelve (hay_huecos, resumen)
    """
    # 1) Ministerio -> citaconsular
    page = go_via_ministry(context, entry_url, cons_name)
    page.set_default_timeout(WIDGET_TIMEOUT_MS)

    # 2) Estado inicial del widget (antes de parsear)
    send_html(page, cons_name, "before_check", "HTML inicial (widget)")
    send_jpeg(page, cons_name, "before_check", "evidencia inicial (antes de parsear)")

    # 3) Esperar “Continuar” o “No hay horas…”
    outcome = wait_widget_ready(page, cons_name)
    if not outcome:
        short_err(cons_name, "Timeout esperando widget; intento volver a entrada y re-checar…")
        # Reintento 1: volver a Ministerio para refrescar cookie/banner
        page.close()
        page = go_via_ministry(context, entry_url, cons_name)
        send_html(page, cons_name, "error_state", "HTML en error")
        send_jpeg(page, cons_name, "error_state", "captura en error")
        outcome = wait_widget_ready(page, cons_name)
        if not outcome:
            raise PWTimeout("No apareció Continuar ni 'No hay horas...'")

    # 4) Si ya dice NO HAY desde el inicio:
    if outcome == "nohours":
        send_html(page, cons_name, "no_final", "HTML final — NO")
        send_jpeg(page, cons_name, "no_final", "captura final — NO")
        return (False, "sin huecos por ahora.")

    # 5) Click en Continuar (si no se clicó ya en frame)
    if outcome in ("continue",):
        page.get_by_text(BTN_CONTINUE).first.click()
        sleep_human(0.6, 1.2)
        wait_for_network_and_idle(page, hard_ms=12000)

    # 6) Caso CDMX: tarjeta LMD
    if do_cdmx_extra:
        cdmx_extra_after_continue(page, cons_name)

    # 7) Abrir panel de día (para forzar render del calendario)
    if needs_panel:
        opened = open_day_panel(page)
        # captura “después de abrir panel” aunque no lo encuentre, por evidencia
        send_html(page, cons_name, "after_panel", "HTML tras abrir panel")
        send_jpeg(page, cons_name, "after_panel", "pantalla tras abrir panel")
        if opened:
            # Esperar que el contenido deje de “cargar”
            wait_spinner_gone(page, PANEL_TIMEOUT_MS)
            wait_for_network_and_idle(page, hard_ms=8000)

    # 8) Detección de huecos (texto)
    has = detect_slots_by_text(page)
    # Evidencia final
    if has:
        send_html(page, cons_name, "final_yes", "HTML final — ¡POSIBLE DISPONIBLE!")
        send_jpeg(page, cons_name, "final_yes", "captura final — ¡POSIBLE DISPONIBLE!")
        return (True, "¡posible disponibilidad!")
    else:
        send_html(page, cons_name, "final", "HTML final — NO")
        # Evitar captura “en blanco”: esperamos hasta 2s si ve spinner
        if any_spinner_visible(page):
            wait_spinner_gone(page, 2000)
            wait_for_network_and_idle(page, hard_ms=2000)
        send_jpeg(page, cons_name, "final", "captura final — NO")
        return (False, "sin huecos por ahora.")

# =========================
# Bucle principal
# =========================

CONSULADOS = [
    {
        "name": "Monterrey",
        "entry": URL_MONTERREY_MIN,
        "needs_panel": True,
        "cdmx_extra": False,
    },
    {
        "name": "Ciudad de México",
        "entry": URL_CDMX_MIN,
        "needs_panel": True,
        "cdmx_extra": True,
    },
]

def run_round(context: BrowserContext):
    for cons in CONSULADOS:
        name = cons["name"]
        try:
            has, summary = generic_flow(
                context=context,
                entry_url=cons["entry"],
                cons_name=name,
                needs_panel=cons["needs_panel"],
                do_cdmx_extra=cons["cdmx_extra"],
            )
            tg_send_text(f"[{now_ts()}] <b>{name}</b> → {summary}")
        except PWTimeout as e:
            short_err(name, "timeout esperando widget")
            # evidencia de la última página si existe
        except Exception as e:
            short_err(name, "error durante la revisión.")
            if DEBUG_STEPS:
                tb = traceback.format_exc()
                tg_send_text(f"<code>{tb[:3900]}</code>")

def main():
    # Mensaje de arranque
    log("[start] Launching bot…")
    tg_send_text("🟣 Bot de citas listo. Enviando evidencias e IP públicas cuando aplique.")
    show_public_ip_through_proxy()

    proxy_url = build_proxy_setting()

    with sync_playwright() as pw:
        args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-features=Translate",
            "--lang=es-ES",
        ]
        browser = pw.chromium.launch(headless=True, args=args, proxy={"server": proxy_url} if proxy_url else None)
        context = browser.new_context(locale="es-ES", user_agent=None)
        try:
            while True:
                log("[INFO] Iniciando ronda…")
                run_round(context)
                wait_s = random.randint(ROUND_MIN_SEC, ROUND_MAX_SEC)
                tg_send_text(f"[INFO] Esperando {wait_s}s antes de la siguiente ronda…")
                time.sleep(wait_s)
        finally:
            context.close()
            browser.close()

if __name__ == "__main__":
    main()
