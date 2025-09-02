# -*- coding: utf-8 -*-
import os, sys, time, random, re, io, traceback, json
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout, Page, Browser, BrowserContext
import requests

# ------------------------------------------------------------
# Config
# ------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Envío de evidencias (activado por defecto)
SEND_PROOF = os.getenv("PROOF", "ON").strip().upper() != "OFF"

# Bloquear imágenes (ahorra datos)
BLOCK_IMAGES = os.getenv("BLOCK_IMAGES", "ON").strip().upper() == "ON"

# Retrasos "humanos"
HUMAN_MIN = float(os.getenv("HUMAN_MIN", "0.7"))
HUMAN_MAX = float(os.getenv("HUMAN_MAX", "1.4"))

# Reintentos básicos de goto / esperas
GOTO_RETRIES = int(os.getenv("GOTO_RETRIES", "2"))
LANDING_TIMEOUT_MS = int(os.getenv("LANDING_TIMEOUT_MS", "30000"))
WIDGET_TIMEOUT_MS  = int(os.getenv("WIDGET_TIMEOUT_MS",  "25000"))

# Patrón de hora visible en botón/slot
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")  # 0:00–23:59

@dataclass
class Consulado:
    name: str
    # URL externa (exteriores.gob.es) desde donde se abre el widget (t.me → abre en pestaña nueva)
    landing_url: str
    # Texto/enlace que abre el widget (ancla amarillo "ELEGIR FECHA Y HORA")
    landing_link_text: str = "ELEGIR FECHA Y HORA"
    # Estrategia: "default" (Monterrey) o "cdmx_panel" (flujo especial CDMX)
    strategy: str = "default"

CONSULADOS: List[Consulado] = [
    Consulado(
        name="Monterrey",
        landing_url="https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
        strategy="default",
    ),
    Consulado(
        name="Ciudad de México",
        landing_url="https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
        strategy="cdmx_panel",
    ),
]

# ------------------------------------------------------------
# Notificaciones
# ------------------------------------------------------------

def tg_send_text(text: str):
    print(text, flush=True)
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=20,
        )
    except Exception as e:
        print(f"[WARN] Telegram text fail: {e}", file=sys.stderr)

def tg_send_document(filepath: str, caption: str):
    print(f"[proof] file → {filepath} / {caption}", flush=True)
    if not SEND_PROOF or not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        with open(filepath, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"document": (os.path.basename(filepath), f, "application/octet-stream")},
                timeout=30,
            )
    except Exception as e:
        print(f"[WARN] Telegram document fail: {e}", file=sys.stderr)

def tg_send_photo(filepath: str, caption: str):
    print(f"[proof] image → {filepath} / {caption}", flush=True)
    if not SEND_PROOF or not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        with open(filepath, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"photo": (os.path.basename(filepath), f, "image/png")},
                timeout=30,
            )
    except Exception as e:
        print(f"[WARN] Telegram photo fail: {e}", file=sys.stderr)

# ------------------------------------------------------------
# Utilidades
# ------------------------------------------------------------

def human_pause(a: float = None, b: float = None):
    lo = HUMAN_MIN if a is None else a
    hi = HUMAN_MAX if b is None else b
    time.sleep(random.uniform(lo, hi))

def save_html(page: Page, path: str):
    try:
        html = page.content()
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass

def save_screenshot(page: Page, path: str):
    try:
        page.screenshot(path=path, full_page=True)
    except Exception:
        pass

def accept_dialogs(context: BrowserContext):
    def _on_dialog(d):
        try:
            d.accept()
        except Exception:
            pass
    context.on("dialog", _on_dialog)

def set_request_blocking(context: BrowserContext):
    if not BLOCK_IMAGES:
        return
    def _route(route, request):
        if request.resource_type in ("image", "media", "font"):
            return route.abort()
        return route.continue_()
    context.route("**/*", _route)

# ------------------------------------------------------------
# Apertura de widget desde exteriores (abre nueva pestaña)
# ------------------------------------------------------------

def open_widget_from_landing(context: BrowserContext, landing_url: str, link_text: str) -> Page:
    page = context.new_page()
    page.set_default_timeout(LANDING_TIMEOUT_MS)
    page.goto(landing_url, wait_until="domcontentloaded")
    human_pause()

    # clic en ancla "ELEGIR FECHA Y HORA" — abre en nueva pestaña
    with context.expect_page() as p_ev:
        # intenta con el texto exacto / en mayúsculas
        link = page.get_by_text(link_text, exact=False)
        link.first.click(force=True, timeout=LANDING_TIMEOUT_MS)
    widget_page = p_ev.value
    widget_page.set_default_timeout(WIDGET_TIMEOUT_MS)

    # espera a que el widget cargue algo (HTML no vacío)
    widget_page.wait_for_load_state("domcontentloaded")
    human_pause(0.6, 1.4)
    return widget_page

# ------------------------------------------------------------
# Obtención de slots/huecos
# ------------------------------------------------------------

def extract_real_slots(page: Page) -> List[Tuple[str, str]]:
    """Devuelve (hora, texto_boton) para botones con 'Hueco libre'"""
    slots = []
    try:
        cands = page.locator("button, .btn, [role=button]")
        n = cands.count()
    except Exception:
        n = 0

    for i in range(min(n, 300)):
        try:
            el = cands.nth(i)
            if not el.is_visible():
                continue
            txt = el.inner_text().strip()
            if not txt or "hueco libre" not in txt.lower():
                continue
            m = TIME_RE.search(txt)
            if m:
                slots.append((m.group(0), txt))
        except Exception:
            continue
    return slots

# ------------------------------------------------------------
# Flujo genérico (Monterrey / default)
# ------------------------------------------------------------

def revisar_default(context: BrowserContext, cons: Consulado) -> Tuple[bool, List[Tuple[str, str]], Optional[str]]:
    """Abrimos desde exteriores, aceptamos bienvenida si aparece y revisamos directamente."""
    page = open_widget_from_landing(context, cons.landing_url, cons.landing_link_text)
    human_pause()

    # evidencia inicial
    if SEND_PROOF:
        pfx = cons.name.replace(" ", "_").lower()
        save_html(page, f"{pfx}_init_widget.html")
        tg_send_document(f"{pfx}_init_widget.html", f"{cons.name}: HTML inicial (widget listo)")
        save_screenshot(page, f"{pfx}_init_widget.png")
        tg_send_photo(f"{pfx}_init_widget.png", f"{cons.name}: evidencia inicial (widget listo)")

    # botón Continuar si existe
    try:
        btn = page.locator("button:has-text('Continuar'), button:has-text('Continue')")
        if btn.first.is_visible():
            btn.first.click(force=True, timeout=6000)
            human_pause()
    except Exception:
        pass

    # chequeo de "no hay horas"
    try:
        page.get_by_text("No hay horas disponibles", exact=False).wait_for(timeout=2500)
        fecha = None
        return (False, [], fecha)
    except PTimeout:
        pass

    # buscar huecos
    slots = extract_real_slots(page)
    fecha = None
    ok = len(slots) > 0

    if SEND_PROOF:
        pfx = cons.name.replace(" ", "_").lower()
        save_html(page, f"{pfx}_final.html")
        tg_send_document(f"{pfx}_final.html", f"{cons.name}: HTML final — {'OK' if ok else 'NO'}")
        save_screenshot(page, f"{pfx}_final.png")
        tg_send_photo(f"{pfx}_final.png", f"{cons.name}: captura final — {'OK' if ok else 'NO'}")

    return (ok, slots, fecha)

# ------------------------------------------------------------
# Flujo especial Ciudad de México (bienvenida → continuar → panel)
# ------------------------------------------------------------

def revisar_cdmx_panel(context: BrowserContext, cons: Consulado) -> Tuple[bool, List[Tuple[str, str]], Optional[str]]:
    """
    Pasos específicos:
      1) Bienvenida (se acepta automáticamente por handler de dialogs)
      2) Click en “Continuar / Continue”
      3) Esperar y click en el recuadro “PRESENTACION DOCUMENTACION LEY MEMORIA DEMOCRATICA”
      4) Chequeo final de huecos (“No hay horas…” o botones con Hueco libre)
    """
    page = open_widget_from_landing(context, cons.landing_url, cons.landing_link_text)
    human_pause()

    # Evidencia 0: antes de parsear
    if SEND_PROOF:
        pfx = "ciudad_de_mexico"
        save_html(page, f"{pfx}_before_check_widget.html")
        tg_send_document(f"{pfx}_before_check_widget.html", "Ciudad de México: HTML inicial (widget listo)")
        save_screenshot(page, f"{pfx}_before_check_widget.png")
        tg_send_photo(f"{pfx}_before_check_widget.png", "Ciudad de México: evidencia inicial (antes de parsear)")

    # Paso 2: boton Continuar
    try:
        btn = page.locator("button:has-text('Continuar'), button:has-text('Continue')")
        if btn.first.is_visible():
            btn.first.click(force=True, timeout=7000)
            human_pause(0.9, 1.6)
    except Exception:
        pass

    # Evidencia 1: tras continuar
    if SEND_PROOF:
        save_html(page, "ciudad_de_mexico_after_continue.html")
        tg_send_document("ciudad_de_mexico_after_continue.html", "Ciudad de México: HTML tras 'Continuar'")
        save_screenshot(page, "ciudad_de_mexico_after_continue.png")
        tg_send_photo("ciudad_de_mexico_after_continue.png", "Ciudad de México: pantalla tras 'Continuar'")

    # Paso 3: click en el recuadro (panel) — intentamos varios selectores de respaldo
    panel_clicked = False
    panel_text = "PRESENTACION DOCUMENTACION LEY MEMORIA DEMOCRATICA"

    # a) por texto exacto (insensible a mayúsculas)
    try:
        loc = page.get_by_text(panel_text, exact=False)
        if loc.first.is_visible():
            loc.first.click(force=True, timeout=7000)
            panel_clicked = True
    except Exception:
        pass

    # b) por posibles contenedores
    if not panel_clicked:
        try:
            cands = page.locator(".panel, .panel-default, .panel-body, .well, .list-group-item, .list-group a")
            n = cands.count()
            for i in range(min(n, 12)):
                el = cands.nth(i)
                if not el.is_visible():
                    continue
                txt = el.inner_text().strip()
                if "memoria" in txt.lower() or "presentacion" in txt.lower():
                    el.click(force=True, timeout=6000)
                    panel_clicked = True
                    break
        except Exception:
            pass

    # c) último esfuerzo: clic centrado de la zona de contenido
    if not panel_clicked:
        try:
            page.mouse.click(600, 340)  # heurístico
            panel_clicked = True
        except Exception:
            pass

    human_pause(0.9, 1.6)

    # Evidencia 2: tras intentar abrir el panel
    if SEND_PROOF:
        save_html(page, "ciudad_de_mexico_after_panel.html")
        tg_send_document("ciudad_de_mexico_after_panel.html", "Ciudad de México: HTML tras abrir panel")
        save_screenshot(page, "ciudad_de_mexico_after_panel.png")
        tg_send_photo("ciudad_de_mexico_after_panel.png", "Ciudad de México: pantalla tras abrir panel")

    # Paso 4: chequeo final (no hay horas vs slots)
    try:
        page.get_by_text("No hay horas disponibles", exact=False).wait_for(timeout=2500)
        return (False, [], None)
    except PTimeout:
        pass

    slots = extract_real_slots(page)
    ok = len(slots) > 0

    # Evidencia 3: final
    if SEND_PROOF:
        save_html(page, "ciudad_de_mexico_final.html")
        tg_send_document("ciudad_de_mexico_final.html", f"Ciudad de México: HTML final — {'OK' if ok else 'NO'}")
        save_screenshot(page, "ciudad_de_mexico_final.png")
        tg_send_photo("ciudad_de_mexico_final.png", f"Ciudad de México: captura final — {'OK' if ok else 'NO'}")

    return (ok, slots, None)

# ------------------------------------------------------------
# Bucle de monitoreo
# ------------------------------------------------------------

def revisar_consulado(context: BrowserContext, cons: Consulado) -> Tuple[bool, List[Tuple[str, str]], Optional[str]]:
    if cons.strategy == "cdmx_panel":
        return revisar_cdmx_panel(context, cons)
    return revisar_default(context, cons)

def main():
    # Mensaje de prueba si FORCED TEST
    if os.getenv("FORCE_TEST") == "1":
        tg_send_text("🚀 Test OK: evidencias e IP activadas.")
        time.sleep(2)
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-gpu", "--no-sandbox"])
        context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="es-ES")
        accept_dialogs(context)
        set_request_blocking(context)

        while True:
            try:
                tg_send_text(f"[INFO] Consulados: {', '.join(c.name for c in CONSULADOS)}")

                for cons in CONSULADOS:
                    human_pause(0.9, 1.6)
                    try:
                        ok, slots, _ = revisar_consulado(context, cons)
                        if ok and slots:
                            primeras = ", ".join(sorted({h for h, _ in slots})[:5])
                            tg_send_text(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {cons.name} → ¡HAY HUECOS! Horas: {primeras}")
                        else:
                            tg_send_text(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {cons.name} → sin huecos por ahora.")
                    except Exception as e:
                        tg_send_text(f"⚠️ {cons.name}: error durante la revisión. {e}")
                        traceback.print_exc()

                # Espera humana entre rondas (pide ~5–7 min)
                wait_sec = random.randint(300, 420)
                tg_send_text(f"[INFO] Esperando {wait_sec}s antes de la siguiente ronda…")
                time.sleep(wait_sec)

            except Exception as e:
                tg_send_text(f"[ERROR] ciclo principal: {e}")
                time.sleep(90)

        # Nunca llega aquí en modo loop
        # context.close(); browser.close()

if __name__ == "__main__":
    main()
