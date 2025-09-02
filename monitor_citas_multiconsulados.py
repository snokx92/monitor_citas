# -*- coding: utf-8 -*-
"""
Monitor de citas Bookitit para Mty/CDMX con evidencias a Telegram
- Flujo CDMX: Bienvenida → Continuar → Panel "PRESENTACION..." → calendario
- Flujo Mty: Bienvenida → Continuar → calendario
- Evidencias: HTML y screenshot en cada paso clave
- Detección de huecos: botones con 'Hueco libre' y una HORA (HH:MM)
- Proxy residencial (DataImpulse) opcional
"""

import os, re, sys, io, time, random, json
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict
import requests

from playwright.sync_api import sync_playwright, TimeoutError as PTimeout, Page, BrowserContext

# ======================
# CONFIG & ENV
# ======================

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")  # 0:00–23:59

@dataclass
class Consulado:
    nombre: str
    # Página ministerio (donde está el enlace “ELEGIR FECHA Y HORA”)
    ministerio_url: str
    # Texto del panel (solo CDMX necesita este paso intermedio)
    panel_texto: Optional[str] = None

@dataclass
class Cfg:
    # ====== Consulados a monitorear ======
    CONSULADOS: List[Consulado] = (
        # Monterrey
        Consulado(
            nombre="Monterrey",
            ministerio_url="https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
            panel_texto=None,  # Mty no requiere panel intermedio
        ),
        # Ciudad de México (con panel intermedio)
        Consulado(
            nombre="Ciudad de México",
            ministerio_url="https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
            panel_texto="PRESENTACION DOCUMENTACION LEY MEMORIA DEMOCRATICA",
        ),
    )

    # ========= Telegram =========
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ========= Anti-bloqueos / humano =========
    HUMAN_MIN: float = float(os.getenv("HUMAN_MIN", "0.8"))
    HUMAN_MAX: float = float(os.getenv("HUMAN_MAX", "1.8"))

    # ========= Intervalos =========
    # Entre rondas completas
    ROUND_MIN_S: int = int(os.getenv("ROUND_MIN_S", "300"))   # 5 min
    ROUND_MAX_S: int = int(os.getenv("ROUND_MAX_S", "420"))   # 7 min
    # Esperas de carga
    LANDING_TIMEOUT_MS: int = int(os.getenv("LANDING_TIMEOUT_MS", "25000"))
    WIDGET_TIMEOUT_MS:  int = int(os.getenv("WIDGET_TIMEOUT_MS",  "25000"))

    # ========= Comportamiento =========
    BLOCK_IMAGES: str = os.getenv("BLOCK_IMAGES", "ON")  # ON/OFF
    DEBUG_STEPS: str = os.getenv("DEBUG_STEPS", "ON")    # ON/OFF (envía evidencias)
    SHOW_PUBLIC_IP: str = os.getenv("SHOW_PUBLIC_IP", "ON")

    # ========= Proxy (DataImpulse) =========
    PROXY_HOST: str = os.getenv("PROXY_HOST", "").strip()
    PROXY_PORT: str = os.getenv("PROXY_PORT", "").strip()  # EJ: 823
    PROXY_USER: str = os.getenv("PROXY_USER", "").strip()
    PROXY_PASS: str = os.getenv("PROXY_PASS", "").strip()

cfg = Cfg()

# ======================
# Telegram helpers
# ======================

def t_send_message(text: str):
    print(text, flush=True)
    if not (cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": cfg.TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
    except Exception as e:
        print(f"[WARN] Telegram message failed: {e}", file=sys.stderr, flush=True)

def t_send_file(caption: str, filename: str, data: bytes):
    print(f"[proof] file -> {filename} / {caption}", flush=True)
    if not (cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID):
        return
    url = f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendDocument"
    files = {"document": (filename, data)}
    try:
        requests.post(url, data={"chat_id": cfg.TELEGRAM_CHAT_ID, "caption": caption}, files=files, timeout=30)
    except Exception as e:
        print(f"[WARN] Telegram doc failed: {e}", file=sys.stderr, flush=True)

def send_html(name: str, page: Page, caption: str):
    try:
        html = (page.content() or "").encode("utf-8", "ignore")
        t_send_file(caption, f"{name}.html", html)
    except Exception as e:
        print(f"[WARN] dump html failed: {e}", flush=True)

def send_screenshot(name: str, page: Page, caption: str):
    try:
        png = page.screenshot(full_page=True)
        t_send_file(caption, f"{name}.png", png)
    except Exception as e:
        print(f"[WARN] screenshot failed: {e}", flush=True)

def hpause(a: Optional[float]=None, b: Optional[float]=None):
    lo = cfg.HUMAN_MIN if a is None else a
    hi = cfg.HUMAN_MAX if b is None else b
    time.sleep(random.uniform(lo, hi))

# ======================
# Navegación helpers
# ======================

def context_args_for_proxy() -> Dict:
    if not cfg.PROXY_HOST or not cfg.PROXY_PORT:
        return {}
    proxy = {
        "server": f"http://{cfg.PROXY_HOST}:{cfg.PROXY_PORT}"
    }
    if cfg.PROXY_USER and cfg.PROXY_PASS:
        proxy["username"] = cfg.PROXY_USER
        proxy["password"] = cfg.PROXY_PASS
    return {"proxy": proxy}

def safe_wait_text(page: Page, text: str, timeout: int) -> bool:
    """
    Espera a que aparezca un texto evitando el strict-mode error de Playwright.
    Devuelve True si lo ve; False si no.
    """
    try:
        loc = page.get_by_text(text, exact=False).nth(0)
        loc.wait_for(state="visible", timeout=timeout)
        return True
    except PTimeout:
        return False
    except Exception as e:
        print(f"[WARN] safe_wait_text('{text}') -> {e}", flush=True)
        return False

def wait_widget_ready(page: Page) -> None:
    # aquí el widget de Bookitit suele estar ya “vivo” (aparece botón Continuar)
    safe_wait_text(page, "Continuar", timeout=cfg.WIDGET_TIMEOUT_MS)

def click_continue_if_present(page: Page) -> None:
    # botón “Continue / Continuar”
    try:
        btn = page.get_by_role("button", name=re.compile(r"continuar|continue", re.I)).nth(0)
        if btn.is_visible():
            btn.click(force=True)
            hpause()
    except Exception:
        # fallback por texto
        try:
            page.get_by_text("Continuar", exact=False).nth(0).click(force=True)
            hpause()
        except Exception:
            pass

def click_panel_if_needed(page: Page, texto_panel: Optional[str]) -> None:
    if not texto_panel:
        return
    # Para CDMX: click al recuadro grande que contiene ese texto
    try:
        # Primero asegurarnos de que la caja cargó (a veces tarda)
        safe_wait_text(page, "PRESENTACION", timeout=cfg.WIDGET_TIMEOUT_MS)
        panel = page.get_by_text(texto_panel, exact=False).nth(0)
        panel.click(force=True)
        hpause()
    except Exception as e:
        print(f"[WARN] click_panel_if_needed: {e}", flush=True)

def open_widget_from_ministerio(context: BrowserContext, cons: Consulado) -> Page:
    """
    Abre ministerio → click 'ELEGIR FECHA Y HORA' (abre Bookitit en nueva pestaña)
    Devuelve la página del widget.
    """
    page = context.new_page()
    page.set_default_timeout(cfg.LANDING_TIMEOUT_MS)
    # 1) ministerio
    page.goto(cons.ministerio_url, wait_until="domcontentloaded")
    hpause(1.2, 2.2)
    if cfg.DEBUG_STEPS.upper() == "ON":
        send_screenshot(f"{cons.nombre.lower()}_ministerio", page, f"{cons.nombre}: HTML inicial (ministerio)")
        send_html(f"{cons.nombre.lower()}_ministerio", page, f"{cons.nombre}: HTML ministerio")

    # 2) click enlace “ELEGIR FECHA Y HORA”
    # puede decir “ELEGIR FECHA Y HORA” o variaciones
    link = None
    try:
        link = page.get_by_role("link", name=re.compile(r"ELEGIR\s+FECHA\s+Y\s+HORA", re.I)).nth(0)
    except Exception:
        pass
    if not link:
        # fallback por texto
        link = page.get_by_text("ELEGIR FECHA Y HORA", exact=False).nth(0)

    # Capturamos nueva pestaña
    with page.expect_popup() as newp:
        link.click(force=True)
    widget = newp.value
    widget.set_default_timeout(cfg.WIDGET_TIMEOUT_MS)

    # algunas webs muestran un alert("Welcome / Bienvenido")
    def on_dialog(dialog):
        try:
            dialog.accept()
        except Exception:
            pass
    widget.on("dialog", on_dialog)

    wait_widget_ready(widget)
    return widget

# ======================
# Parsing de huecos
# ======================

def find_slots(page: Page) -> List[Tuple[str, str]]:
    """
    Devuelve lista de (hora, texto_boton) si el botón contiene una hora
    y el mismo elemento/bloque incluye 'Hueco libre'
    """
    slots: List[Tuple[str,str]] = []

    # 1) ¿está el mensaje de “No hay horas disponibles”?
    if safe_wait_text(page, "No hay horas disponibles", timeout=1500):
        return slots

    # 2) buscar botones candidatos
    # Preferimos botones que contengan “Hueco libre”
    cand = page.locator("button:has-text('Hueco libre')")
    try:
        count = cand.count()
    except Exception:
        count = 0

    if count == 0:
        # fallback: cualquier botón visible con hora y alguna pista
        cand = page.locator("button")
        try:
            count = cand.count()
        except Exception:
            count = 0

    for i in range(min(count, 300)):
        try:
            el = cand.nth(i)
            if not el.is_visible():
                continue
            text = el.inner_text().strip()
            # si no venía “Hueco libre” en la query, filtramos aquí
            if "hueco" not in text.lower():
                # revisar contenedor cercano por 'Hueco libre'
                try:
                    near = el.locator("xpath=..").inner_text().lower()
                    if "hueco" not in near:
                        continue
                except Exception:
                    continue
            m = TIME_RE.search(text)
            if m:
                slots.append((m.group(0), text))
        except Exception:
            continue
    return slots

# ======================
# Revisión por consulado
# ======================

def revisar_consulado(context: BrowserContext, cons: Consulado) -> Tuple[bool, List[Tuple[str,str]]]:
    """
    Devuelve (hay_huecos, slots)
    """
    name_slug = cons.nombre.lower().replace(" ", "_")

    widget = open_widget_from_ministerio(context, cons)
    hpause()

    # Evidencia inicial de widget
    if cfg.DEBUG_STEPS.upper() == "ON":
        send_screenshot(f"{name_slug}_before_check", widget, f"{cons.nombre}: evidencia inicial (antes de parsear)")
        send_html(f"{name_slug}_before_check", widget, f"{cons.nombre}: HTML inicial")

    # Paso 1: botón Continuar
    click_continue_if_present(widget)
    if cfg.DEBUG_STEPS.upper() == "ON":
        send_screenshot(f"{name_slug}_after_continue", widget, f"{cons.nombre}: pantalla tras 'Continuar'")
        send_html(f"{name_slug}_after_continue", widget, f"{cons.nombre}: HTML tras 'Continuar'")

    # Paso 2 (solo CDMX): click panel
    click_panel_if_needed(widget, cons.panel_texto)
    if cfg.DEBUG_STEPS.upper() == "ON":
        send_screenshot(f"{name_slug}_after_panel", widget, f"{cons.nombre}: pantalla tras abrir panel")
        send_html(f"{name_slug}_after_panel", widget, f"{cons.nombre}: HTML tras abrir panel")

    # Buscar huecos
    slots = find_slots(widget)

    # Evidencia final
    if cfg.DEBUG_STEPS.upper() == "ON":
        send_screenshot(f"{name_slug}_final", widget, f"{cons.nombre}: captura final — {'OK' if slots else 'NO'}")
        send_html(f"{name_slug}_final", widget, f"{cons.nombre}: HTML final — {'OK' if slots else 'NO'}")

    try:
        widget.close()
    except Exception:
        pass

    return (len(slots) > 0, slots)

# ======================
# Main loop
# ======================

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]

def new_context(play, headless=True) -> BrowserContext:
    args = context_args_for_proxy()
    browser = play.chromium.launch(headless=headless)
    viewport = {"width": random.randint(1200, 1440), "height": random.randint(850, 980)}
    context = browser.new_context(
        viewport=viewport,
        user_agent=random.choice(USER_AGENTS),
        locale="es-ES",
        **args
    )
    # Opcional: bloquear imágenes
    if cfg.BLOCK_IMAGES.upper() == "ON":
        context.route("**/*", lambda route: route.abort() if route.request.resource_type in ("image","media","font") else route.continue_())
    return context

def show_public_ip_once():
    if cfg.SHOW_PUBLIC_IP.upper() != "ON":
        return
    try:
        ip = requests.get("https://api.ipify.org?format=json", timeout=12).json().get("ip","")
        t_send_message(f"[INFO] IP pública: {ip}")
    except Exception:
        pass

def main():
    t_send_message("[start] Launching bot…")
    t_send_message(f"[INFO] Config: proof={'ON' if cfg.DEBUG_STEPS=='ON' else 'OFF'} debug=ON block_images={cfg.BLOCK_IMAGES}")
    if cfg.PROXY_HOST:
        t_send_message(f"[INFO] Proxy: http://{cfg.PROXY_HOST}:{cfg.PROXY_PORT}")

    show_public_ip_once()

    with sync_playwright() as p:
        while True:
            try:
                ctx = new_context(p, headless=True)
                t_send_message(f"[INFO] Consulados: {', '.join(c.nombre for c in cfg.CONSULADOS)}")

                for cons in cfg.CONSULADOS:
                    t_send_message(f"[{cons.nombre}] goto…")
                    ok, slots = revisar_consulado(ctx, cons)
                    if ok and slots:
                        horas = ", ".join(sorted({h for h, _ in slots})[:6])
                        msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {cons.nombre} → ¡HAY HUECOS! Horas: {horas}"
                        t_send_message(msg)
                        # Enfriar 5 min si hay huecos, para no “machacar”
                        time.sleep(300)
                    else:
                        t_send_message(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {cons.nombre} → sin huecos por ahora.")

                    hpause(1.2, 2.0)

                try:
                    ctx.close()
                except Exception:
                    pass

                # Espera humana entre rondas
                wait_s = random.randint(cfg.ROUND_MIN_S, cfg.ROUND_MAX_S)
                t_send_message(f"[INFO] Esperando {wait_s}s antes de la siguiente ronda…")
                time.sleep(wait_s)

            except Exception as e:
                t_send_message(f"[ERROR] {e}")
                time.sleep(90)

if __name__ == "__main__":
    main()
