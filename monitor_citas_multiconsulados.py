# -*- coding: utf-8 -*-
"""
Monitor de citas (múltiples consulados con paso intermedio por Exteriores)

- Abre la página oficial de Exteriores del consulado
- Hace clic en "ELEGIR FECHA Y HORA"
- Sigue la nueva pestaña (citaconsular.es)
- Acepta alertas/diálogos
- Click en Continue / Continuar (si aplica)
- Detecta huecos reales (botones con "Hueco libre" y hora)
- Envía evidencias a Telegram: capturas + HTML cuando hay bloqueos/página vacía
- Intervalos humanizados entre rondas

Requiere: playwright (sync), requests
"""

import os
import sys
import time
import random
import re
import io
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout, Browser, Page

# =========================
# CONFIGURACIÓN GENERAL (por variables de entorno)
# =========================

@dataclass
class Cfg:
    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Pausas humanizadas
    HUMAN_MIN: float = float(os.getenv("HUMAN_MIN", "0.7"))
    HUMAN_MAX: float = float(os.getenv("HUMAN_MAX", "1.8"))

    # Intervalos entre rondas (segundos)
    CHECK_MIN_SEC: int = int(os.getenv("CHECK_MIN_SEC", "300"))  # 5 min
    CHECK_MAX_SEC: int = int(os.getenv("CHECK_MAX_SEC", "420"))  # 7 min

    # Timeouts (ms)
    NAV_TIMEOUT_MS: int = int(os.getenv("NAV_TIMEOUT_MS", "20000"))
    SEL_TIMEOUT_MS: int = int(os.getenv("SEL_TIMEOUT_MS", "8000"))

    # Test: enviar ping y salir
    FORCE_TEST: str = os.getenv("FORCE_TEST", "0")

    # Proxy (opcional)
    PROXY_HOST: str = os.getenv("PROXY_HOST", "").strip()
    PROXY_PORT: str = os.getenv("PROXY_PORT", "").strip()
    PROXY_USER: str = os.getenv("PROXY_USER", "").strip()
    PROXY_PASS: str = os.getenv("PROXY_PASS", "").strip()

    # Mostrar IP pública (1 = sí)
    SHOW_PUBLIC_IP: str = os.getenv("SHOW_PUBLIC_IP", "1")


cfg = Cfg()

# =========================
# CONSULADOS (Exteriores → Citaconsular widget)
# =========================
# Puedes agregar más entradas siguiendo este formato.
CONSULADOS: List[Dict] = [
    {
        "id": "monterrey",
        "nombre": "Monterrey",
        "exteriores_url": "https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
        "widget_hosted": "citaconsular.es",
        # Texto/cadena en el enlace amarillo
        "landing_link_text": "ELEGIR FECHA Y HORA",
    },
    {
        "id": "cdmx_panel",
        "nombre": "Ciudad de México",
        "exteriores_url": "https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
        "widget_hosted": "citaconsular.es",
        "landing_link_text": "ELEGIR FECHA Y HORA",
    },
]

# =========================
# Selectores y patrones comunes del widget
# =========================

SELECTOR_CONTINUE = 'button.btn.btn-success, button:has-text("Continue"), button:has-text("Continuar")'
TEXT_NO_CITAS = "No hay horas disponibles"
BUTTON_CANDIDATES = "button, .btn, [role=button]"
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
DIA_REGEX = r"(Lunes|Martes|Miércoles|Jueves|Viernes|Sábado|Domingo).*?\b\d{4}\b"


# =========================
# Utilidades
# =========================

def human_pause(min_s: Optional[float] = None, max_s: Optional[float] = None):
    a = min_s if min_s is not None else cfg.HUMAN_MIN
    b = max_s if max_s is not None else cfg.HUMAN_MAX
    time.sleep(random.uniform(a, b))


def log(s: str):
    print(s, flush=True)


def telegram_send_text(msg: str):
    print(msg, flush=True)
    if not (cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": cfg.TELEGRAM_CHAT_ID, "text": msg},
            timeout=15,
        )
    except Exception as e:
        print(f"[WARN] Telegram text err: {e}", file=sys.stderr)


def telegram_send_photo(caption: str, image_bytes: bytes):
    if not (cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID):
        return
    files = {"photo": ("screenshot.jpg", image_bytes)}
    data = {"chat_id": cfg.TELEGRAM_CHAT_ID, "caption": caption}
    try:
        requests.post(
            f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendPhoto",
            data=data, files=files, timeout=20
        )
    except Exception as e:
        print(f"[WARN] Telegram photo err: {e}", file=sys.stderr)


def telegram_send_document(caption: str, filename: str, file_bytes: bytes):
    if not (cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID):
        return
    files = {"document": (filename, file_bytes)}
    data = {"chat_id": cfg.TELEGRAM_CHAT_ID, "caption": caption}
    try:
        requests.post(
            f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendDocument",
            data=data, files=files, timeout=20
        )
    except Exception as e:
        print(f"[WARN] Telegram doc err: {e}", file=sys.stderr)


def public_ip() -> Optional[str]:
    if cfg.SHOW_PUBLIC_IP != "1":
        return None
    try:
        r = requests.get("https://api.ipify.org?format=text", timeout=10)
        if r.ok:
            return r.text.strip()
    except Exception:
        pass
    return None


def make_proxy_for_playwright() -> Optional[dict]:
    if not cfg.PROXY_HOST or not cfg.PROXY_PORT:
        return None
    proxy = {"server": f"http://{cfg.PROXY_HOST}:{cfg.PROXY_PORT}"}
    if cfg.PROXY_USER and cfg.PROXY_PASS:
        proxy["username"] = cfg.PROXY_USER
        proxy["password"] = cfg.PROXY_PASS
    return proxy


def accept_dialogs(page: Page):
    def _on_dialog(dlg):
        try:
            dlg.accept()
        except Exception:
            pass
    page.context.on("dialog", _on_dialog)


def extract_date_text(html: str) -> Optional[str]:
    m = re.search(DIA_REGEX, html, re.IGNORECASE | re.DOTALL)
    return m.group(0) if m else None


def find_real_slots(page: Page) -> List[Tuple[str, str]]:
    slots: List[Tuple[str, str]] = []
    try:
        cands = page.locator(BUTTON_CANDIDATES)
        count = cands.count()
    except Exception:
        count = 0

    for i in range(min(count, 400)):
        try:
            el = cands.nth(i)
            if not el.is_visible():
                continue
            txt = el.inner_text().strip()
            if "hueco libre" not in txt.lower():
                continue
            m = TIME_RE.search(txt)
            if m:
                slots.append((m.group(0), txt))
        except Exception:
            continue
    return slots


# =========================
# Flujo por consulado
# =========================

def open_exteriores_and_click(page: Page, cons: Dict) -> Page:
    """
    Abre la página de Exteriores y hace clic en "ELEGIR FECHA Y HORA".
    Captura la nueva pestaña si se abre; si no, usa la misma.
    """
    page.set_default_timeout(cfg.NAV_TIMEOUT_MS)
    page.set_default_navigation_timeout(cfg.NAV_TIMEOUT_MS)

    # 1) Abrir página de Exteriores
    page.goto(cons["exteriores_url"], wait_until="domcontentloaded")
    human_pause()

    accept_dialogs(page)
    # 2) Buscar el enlace (case-insensitive)
    link_txt = cons["landing_link_text"]
    link = page.get_by_text(link_txt, exact=False)
    link.wait_for(timeout=cfg.SEL_TIMEOUT_MS)

    # 3) Preparar captura de nueva pestaña
    new_page = None
    with page.context.expect_event("page", timeout=cfg.NAV_TIMEOUT_MS) as newp:
        link.click(force=True)
    try:
        new_page = newp.value
    except Exception:
        # Si no abrió nueva pestaña, seguimos en la misma
        new_page = page

    # Asegurar carga inicial
    try:
        new_page.wait_for_load_state("domcontentloaded", timeout=cfg.NAV_TIMEOUT_MS)
    except PTimeout:
        pass

    human_pause()
    return new_page


def handle_widget_and_detect(page: Page, cons: Dict) -> Tuple[bool, List[Tuple[str, str]], Optional[str], str]:
    """
    En la página del widget (citaconsular), aceptar diálogos,
    hacer click en Continue/Continuar si existe,
    y detectar huecos reales o estado de "no hay horas".
    Devuelve: (hay_citas, slots, fecha, status_msg)
    """
    accept_dialogs(page)
    # Click en Continue si aparece
    try:
        page.wait_for_selector(SELECTOR_CONTINUE, timeout=cfg.SEL_TIMEOUT_MS)
        page.click(SELECTOR_CONTINUE, force=True)
        human_pause()
    except PTimeout:
        pass

    # Caso explícito: "No hay horas disponibles"
    try:
        page.get_by_text(TEXT_NO_CITAS, exact=False).wait_for(timeout=3000)
        html = (page.content() or "")
        fecha = extract_date_text(html)
        return (False, [], fecha, "no_hay_horas")
    except PTimeout:
        pass

    # Buscar huecos reales
    slots = find_real_slots(page)
    html = (page.content() or "")
    fecha = extract_date_text(html)
    if slots:
        return (True, slots, fecha, "hay_huecos")

    # Si no hay huecos y tampoco el texto de no-disponible:
    # Podría estar en blanco/bloqueo. Revisamos longitud.
    html_len = len(html)
    if html_len < 60:
        return (False, [], None, "html_blanco")
    return (False, [], fecha, "sin_huecos")


def screenshot_bytes(page: Page, full: bool = True) -> bytes:
    try:
        return page.screenshot(full_page=full)
    except Exception:
        # fallback a viewport
        try:
            return page.screenshot(full_page=False)
        except Exception:
            return b""


def revisar_consulado(context, cons: Dict) -> None:
    """Flujo completo por consulado con evidencias"""
    nombre = cons["nombre"]
    log(f"[{nombre}] goto…")

    page = context.new_page()
    try:
        # Abrir exteriores y click al widget
        widget_page = open_exteriores_and_click(page, cons)

        # Verificar dominio correcto o al menos host esperado
        url = widget_page.url
        if cons["widget_hosted"] not in url:
            # esperar una navegación/redirect más
            try:
                widget_page.wait_for_url(lambda u: cons["widget_hosted"] in u, timeout=7000)
            except Exception:
                pass

        # “Trabajar” el widget
        ok, slots, fecha, status = handle_widget_and_detect(widget_page, cons)

        # Mensajes / evidencias
        marca = time.strftime("%Y-%m-%d %H:%M:%S")
        if status == "hay_huecos" and slots:
            horarios = ", ".join(sorted({h for h, _ in slots})[:6])
            ftxt = f" ({fecha})" if fecha else ""
            cap = screenshot_bytes(widget_page, full=True)
            telegram_send_photo(
                f"✅ {nombre}: ¡HAY HUECOS!{ftxt}\nHoras: {horarios}\nURL: {widget_page.url}",
                cap or b""
            )
            telegram_send_text(f"[{marca}] {nombre} → ¡HAY HUECOS!{ftxt} Horas: {horarios}")
        elif status == "no_hay_horas":
            telegram_send_text(f"[{marca}] {nombre} → Sin huecos por ahora.")
        elif status == "html_blanco":
            cap = screenshot_bytes(widget_page, full=True)
            telegram_send_photo(f"⚠️ {nombre}: captura en blanco (posible bloqueo).", cap or b"")
            try:
                html = (widget_page.content() or "").encode("utf-8", errors="ignore")
                telegram_send_document(f"{nombre}: HTML en blanco (posible bloqueo).", f"{cons['id']}_blank.html", html)
            except Exception:
                pass
        else:
            telegram_send_text(f"[{marca}] {nombre} → sin huecos por ahora.")

    except PTimeout:
        telegram_send_text(f"⚠️ {nombre}: Timeout durante la navegación (posible bloqueo).")
    except Exception as e:
        telegram_send_text(f"⚠️ {nombre}: Error inesperado: {e}")
    finally:
        try:
            page.close()
        except Exception:
            pass


# =========================
# Runner
# =========================

def run_once():
    proxy = make_proxy_for_playwright()
    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            viewport={"width": random.randint(1200, 1440), "height": random.randint(800, 960)},
            user_agent=random.choice([
                # Windows / Chrome / Edge / Firefox / Safari (desktop)
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
            ]),
            locale="es-ES",
            extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"},
            proxy=proxy if proxy else None,
        )

        # Mostrar config/IP
        log("[start] Launching bot…")
        log(f"[INFO] Config: proof=OFF debug=ON block_images=OFF")
        cons_names = ", ".join([c["nombre"] for c in CONSULADOS])
        log(f"[INFO] Consulados: {cons_names}")
        if proxy:
            log(f"[INFO] Proxy: {proxy.get('server')}")
        ip = public_ip()
        if ip:
            log(f"[INFO] IP pública: {ip}")

        if cfg.FORCE_TEST == "1":
            telegram_send_text("🚀 Test OK: el bot está listo y puede enviarte evidencias e IP.")
            # captura mini ping (pantalla en blanco con texto)
            try:
                tmp = context.new_page()
                tmp.set_content("<h3>Test de evidencia OK</h3>")
                ph = screenshot_bytes(tmp, full=False)
                telegram_send_photo("Test: evidencia de captura", ph or b"")
                tmp.close()
            except Exception:
                pass
            browser.close()
            return

        for cons in CONSULADOS:
            revisar_consulado(context, cons)
            human_pause(1.0, 2.2)

        browser.close()


def main_loop():
    while True:
        try:
            run_once()
        except Exception as e:
            log(f"[ERROR] {e}")
        # Intervalo humanizado entre rondas
        wait_s = random.randint(cfg.CHECK_MIN_SEC, cfg.CHECK_MAX_SEC)
        log(f"[INFO] Esperando {wait_s}s antes de la siguiente ronda…")
        time.sleep(wait_s)


if __name__ == "__main__":
    # Si pasas "headed" como argumento, abre navegador con UI (debug local)
    headed = len(sys.argv) > 1 and sys.argv[1].lower().startswith("head")
    if headed:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=False)
            c = b.new_context()
            page = c.new_page()
            page.goto("about:blank")
            input("Ventana de depuración abierta. Pulsa ENTER para salir…")
            b.close()
    else:
        main_loop()
