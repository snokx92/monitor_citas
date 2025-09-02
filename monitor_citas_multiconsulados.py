# monitor_citas_multiconsulados.py
# -*- coding: utf-8 -*-

import os, sys, time, random, re, io, json
from dataclasses import dataclass
from typing import List, Tuple, Optional
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout, Error as PError

# =========================
#  CONFIG
# =========================

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")  # 0:00–23:59

@dataclass
class Consulado:
    name: str
    # Página “externa” del MAEC con el enlace ELEGIR FECHA Y HORA
    landing_url: str
    # Texto del enlace que abre el widget (abre nueva pestaña)
    landing_link_text: str = "ELEGIR FECHA Y HORA"
    # ¿Hay panel intermedio que abrir dentro del widget (CDMX)?
    needs_panel_click: bool = False
    panel_text: str = "PRESENTACION DOCUMENTACION LEY MEMORIA DEMOCRATICA"

# ---- Consulados (respetando tu base; Monterrey + CDMX) ----
CONSULADOS: List[Consulado] = [
    Consulado(
        name="Monterrey",
        landing_url="https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
        needs_panel_click=False,
    ),
    Consulado(
        name="Ciudad de México",
        landing_url="https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
        needs_panel_click=True,
        panel_text="PRESENTACION DOCUMENTACION LEY MEMORIA DEMOCRATICA",
    ),
]

# ---- Variables entorno (mismas claves que ya usas) ----
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

PROOF              = os.getenv("PROOF", "1") == "1"          # enviar evidencias a Telegram
SHOW_PUBLIC_IP     = os.getenv("SHOW_PUBLIC_IP", "1") == "1"
BLOCK_IMAGES       = os.getenv("BLOCK_IMAGES", "1") == "1"
DEBUG_STEPS        = os.getenv("DEBUG_STEPS", "1") == "1"

# tiempo máximo al esperar que cargue “algo” en el widget
WIDGET_TIMEOUT_MS  = int(os.getenv("WIDGET_TIMEOUT_MS", "18000"))
LANDING_TIMEOUT_MS = int(os.getenv("LANDING_TIMEOUT_MS", "20000"))

# reintentos de goto cuando algo tarda
GOTO_RETRIES       = int(os.getenv("GOTO_RETRIES", "2"))

# espera humanizada entre rondas
WAIT_MIN_SEC       = int(os.getenv("WAIT_MIN_SEC", "300"))   # 5 min
WAIT_MAX_SEC       = int(os.getenv("WAIT_MAX_SEC", "420"))   # 7 min

# rotación tras detectar HTML en blanco
ROTATE_AFTER_BLANK = os.getenv("ROTATE_AFTER_BLANK", "1") == "1"
ROTATE_COOLDOWN_SEC= int(os.getenv("ROTATE_COOLDOWN_SEC", "20"))

# Proxy (HTTP) – DataImpulse
PROXY_HOST         = os.getenv("PROXY_HOST", "").strip()
PROXY_PORT         = os.getenv("PROXY_PORT", "").strip() or "823"
PROXY_USER         = os.getenv("PROXY_USER", "").strip()
PROXY_PASS         = os.getenv("PROXY_PASS", "").strip()
PROXY_SESSION_IN_USER = os.getenv("PROXY_SESSION_IN_USER", "0") == "1"  # si agregas _cr.xx a user

# =========================
#  Telegram helpers
# =========================

def tg_send_message(text: str):
    print(text, flush=True)
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
    except Exception as e:
        print(f"[WARN] Telegram MSG failed: {e}", file=sys.stderr)

def tg_send_file(caption: str, filename: str, data: bytes, mime: str = "text/html"):
    print(f"[proof] file -> {filename}", flush=True)
    if not PROOF or not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    files = {"document": (filename, data, mime)}
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
            files=files,
            timeout=20,
        )
    except Exception as e:
        print(f"[WARN] Telegram FILE failed: {e}", file=sys.stderr)

def tg_send_png(caption: str, image_bytes: bytes):
    print(f"[proof] photo -> {caption}", flush=True)
    if not PROOF or not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    files = {"photo": ("evidence.png", image_bytes, "image/png")}
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
            files=files,
            timeout=20,
        )
    except Exception as e:
        print(f"[WARN] Telegram PHOTO failed: {e}", file=sys.stderr)

# =========================
#  Utilidades
# =========================

def human_pause(a=0.7, b=1.6):
    time.sleep(random.uniform(a, b))

def public_ip_text() -> Optional[str]:
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=12)
        return r.json().get("ip")
    except Exception:
        return None

def mk_session_user(base: str) -> str:
    if PROXY_SESSION_IN_USER:
        # agrega un sufijo de sesión (“sticky” por unos minutos, DataImpulse)
        suf = random.randint(11, 99)
        return f"{base}_cr.us,mx:{suf}"
    return base

def mk_proxy_kwargs():
    if not PROXY_HOST or not PROXY_PORT:
        return {}
    server = f"http://{PROXY_HOST}:{PROXY_PORT}"
    proxy = {"server": server}
    if PROXY_USER and PROXY_PASS:
        proxy["username"] = mk_session_user(PROXY_USER)
        proxy["password"] = PROXY_PASS
    return {"proxy": proxy}

def save_html(page, caption_prefix: str, tag: str, cons_name: str):
    html = page.content().encode("utf-8", "ignore")
    fname = f"{cons_name.lower().replace(' ', '_')}_{tag}.html"
    tg_send_file(f"{cons_name}: {caption_prefix}", fname, html)
    return html

def save_screenshot(page, caption: str):
    try:
        png = page.screenshot(full_page=True)
        tg_send_png(caption, png)
    except Exception:
        pass

def any_visible(page, locators: List):
    """Espera a que cualquiera de los locators sea visible (o agota timeout)."""
    deadline = time.time() + (WIDGET_TIMEOUT_MS / 1000)
    last_err = None
    while time.time() < deadline:
        for loc in locators:
            try:
                if loc.count() > 0:
                    if loc.first.is_visible():
                        return loc
            except Exception as e:
                last_err = e
        human_pause(0.3, 0.6)
    if last_err:
        raise last_err
    raise PTimeout("any_visible timeout")

# =========================
#  Parser de huecos (robusto)
# =========================

def detect_no_hay_horas(page) -> bool:
    # Evita strict mode: usa count() y no wait_for
    try:
        c = page.get_by_text("No hay horas disponibles", exact=False).count()
        return c > 0
    except Exception:
        return False

def extract_slots_robusto(page) -> List[str]:
    """
    Heurística robusta:
      1) Si aparece “No hay horas disponibles” -> NO huecos, []
      2) Si hay “Hueco libre” en el HTML -> huecos (extrae horas si existen)
      3) Si no, pero aparecen horas (HH:MM) -> asume huecos (Bookitit muestra horas solo cuando hay slots)
    Devuelve lista de horas únicas (strings).
    """
    try:
        html = page.content()
    except Exception:
        return []

    # 1) negación explícita
    if re.search(r"No\s+hay\s+horas\s+disponibles", html, re.I):
        return []

    horas = re.findall(TIME_RE, html)
    horas_norm = [h if isinstance(h, str) else h[0] for h in horas]
    horas_norm = list(dict.fromkeys(horas_norm))  # únicas, preserva orden

    # 2) pista fuerte “Hueco libre”
    if re.search(r"Hueco\s+libre", html, re.I):
        # si no detectamos horas por alguna razón, lo marcamos igualmente
        return horas_norm if horas_norm else ["(sin hora visible)"]

    # 3) no hay negación y hay horas en la página -> marcar huecos
    if horas_norm:
        return horas_norm

    # fallback: buscar “disponible(s)”
    if re.search(r"disponible", html, re.I):
        return ["(disponible)"]

    return []

# =========================
#  Flujo del widget
# =========================

def goto_with_retries(page, url: str, timeout_ms: int):
    last = None
    for i in range(GOTO_RETRIES):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return
        except Exception as e:
            last = e
            human_pause(1.0, 2.0)
    raise last or PTimeout(f"goto retries exhausted for {url}")

def open_widget_from_landing(context, cons: Consulado):
    """
    Abre la página del MAEC y hace click en “ELEGIR FECHA Y HORA”, capturando el popup (widget).
    Devuelve la página del widget.
    """
    page = context.new_page()
    goto_with_retries(page, cons.landing_url, LANDING_TIMEOUT_MS)

    # Captura evidencia inicial (landing) solo si debug
    if DEBUG_STEPS and PROOF:
        save_screenshot(page, f"{cons.name}: evidencia inicial (landing)")

    link = page.get_by_text(cons.landing_link_text, exact=False)
    with page.expect_popup() as pop:
        link.first.click(force=True)
    widget_page = pop.value

    # Aceptar dialog de “Welcome / Bienvenido”
    def _on_dialog(d):
        try:
            d.accept()
        except Exception:
            pass
    widget_page.on("dialog", _on_dialog)

    # Dejar que cargue
    goto_with_retries(widget_page, widget_page.url, WIDGET_TIMEOUT_MS)

    return page, widget_page

def revisar_consulado(context, cons: Consulado) -> Tuple[bool, List[str]]:
    """
    Core: entra al widget, pulsa “Continuar”, (CDMX) abre panel, y decide huecos.
    Devuelve (ok, horas[])
    """
    landing, page = open_widget_from_landing(context, cons)
    human_pause(0.8, 1.6)

    # Bloquea imágenes para ahorrar ancho de banda si se pidió
    if BLOCK_IMAGES:
        page.route("**/*", lambda route: route.abort() if route.request.resource_type == "image" else route.continue_())

    # Evidencia “widget listo”
    if PROOF:
        save_html(page, "HTML inicial (widget listo)", "check_widget", cons.name)
        save_screenshot(page, f"{cons.name}: evidencia inicial (widget listo)")

    # 1) Click en “Continuar”
    try:
        any_visible(page, [
            page.get_by_text("Continue / Continuar", exact=False),
            page.get_by_text("Continuar", exact=False),
            page.locator("text=Continuar"),
        ]).first.click(force=True)
    except Exception:
        # Si no aparece el botón, en algunos flujos ya estás “dentro”
        pass

    human_pause(0.6, 1.1)

    # 2) (CDMX) abrir panel grande
    if cons.needs_panel_click:
        try:
            # espera que aparezca la tarjeta/panel con ese texto y haz click amplio
            any_visible(page, [
                page.get_by_text(cons.panel_text, exact=False),
                page.locator(f"text={cons.panel_text}")
            ]).first.click(position={"x": 30, "y": 30})
            human_pause(0.8, 1.4)
        except Exception:
            # si no aparece, seguimos (a veces se abre directo)
            pass

    # Esperar a que aparezca algo “determinante”: o el texto de no-disponibilidad o alguna hora
    try:
        any_visible(page, [
            page.get_by_text("No hay horas disponibles", exact=False),
            page.locator(":text-matches('\\b([01]?\\d|2[0-3]):[0-5]\\d\\b')"),
        ])
    except Exception:
        # como evidencia, por si quedó “Loading…”
        if PROOF:
            save_screenshot(page, f"{cons.name}: pantalla tras intentar abrir panel")
            save_html(page, "HTML tras intentar abrir panel", "after_panel_fail", cons.name)

    # Evidencia final
    if PROOF:
        save_screenshot(page, f"{cons.name}: captura final")
        save_html(page, "HTML final", "final", cons.name)

    # ---- Decisión de huecos ----
    if detect_no_hay_horas(page):
        return False, []

    horas = extract_slots_robusto(page)
    return (len(horas) > 0), horas

# =========================
#  MAIN LOOP
# =========================

def build_launch_args():
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    if BLOCK_IMAGES:
        # (ya bloqueamos por route, pero este flag ahorra algo en chromium)
        args.append("--blink-settings=imagesEnabled=false")
    return args

def main():
    if SHOW_PUBLIC_IP:
        ip = public_ip_text()
        if ip:
            tg_send_message(f"[INFO] IP pública: {ip}")

    proxy_kwargs = mk_proxy_kwargs()
    if proxy_kwargs:
        s = proxy_kwargs["proxy"]["server"]
        tg_send_message(f"[INFO] Proxy: {s}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=build_launch_args(),
            **proxy_kwargs
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="es-ES",
            user_agent=random.choice([
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
            ])
        )

        tg_send_message(f"[INFO] Consulados: {', '.join([c.name for c in CONSULADOS])}")

        while True:
            try:
                for cons in CONSULADOS:
                    print(f"[{cons.name}] goto…", flush=True)
                    ok, horas = revisar_consulado(context, cons)

                    if ok and horas:
                        primeras = ", ".join(horas[:5])
                        tg_send_message(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {cons.name} → ¡HUECOS! Horas: {primeras}")
                        # enfría 5 min para darte tiempo real de entrar
                        time.sleep(300)
                    else:
                        tg_send_message(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {cons.name} → sin huecos por ahora.")

                    human_pause(0.8, 1.5)

            except Exception as e:
                # errores amplios (incluye strict mode, timeouts, etc.)
                tg_send_message(f"⚠️ Error general: {e}")
                time.sleep(15)

            # espera humanizada entre rondas
            wt = random.randint(WAIT_MIN_SEC, WAIT_MAX_SEC)
            tg_send_message(f"[INFO] Esperando {wt}s antes de la siguiente ronda…")
            time.sleep(wt)

if __name__ == "__main__":
    main()
