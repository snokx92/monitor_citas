# -*- coding: utf-8 -*-
"""
Monitor de citas (Monterrey + Ciudad de México)
- CDMX usa "click extra" sobre el panel de normas para desplegar el calendario.
- Envío a Telegram solo cuando hay huecos (con captura).
- Anti-bloqueo: si la página llega "vacía", reintenta con proxy (si hay PROXY_LIST).
- Logs detallados para confirmar cada paso.

Requiere:
  pip install playwright requests
  python -m playwright install --with-deps chromium
"""

import os, sys, time, random, re, hashlib
from typing import List, Optional, Tuple, Dict
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout

# =========================
# Variables de entorno / ajustes
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Intervalo entre rondas (segundos) — usar >=120 para ahorrar datos
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "120"))

# Pausas humanas cortas entre acciones
HUMAN_MIN = float(os.getenv("HUMAN_MIN", "0.7"))
HUMAN_MAX = float(os.getenv("HUMAN_MAX", "1.5"))

# Ahorro de datos (si ves páginas “rotas”, pon BLOCK_IMAGES=0)
BLOCK_IMAGES = os.getenv("BLOCK_IMAGES", "1") == "1"
BLOCK_FONTS  = os.getenv("BLOCK_FONTS", "1") == "1"

# Debug opcional
DEBUG_STEPS       = os.getenv("DEBUG_STEPS", "1") == "1"   # logs paso a paso
TRACE_PLAYWRIGHT  = os.getenv("TRACE_PLAYWRIGHT", "0") == "1"  # genera trace.zip ante "blank"

# Proxies (solo se usan si detectamos bloqueo/“blank”)
PROXY_LIST = [s.strip() for s in os.getenv("PROXY_LIST", "").split(",") if s.strip()]
RETRIES_ON_BLOCK = int(os.getenv("RETRIES_ON_BLOCK", "2"))

# SOLO Monterrey + CDMX (sin Miami)
CONSUL_URLS = ",".join([
    "Monterrey|https://www.citaconsular.es/es/hosteds/widgetdefault/25b18886db70f7ec9fd6dfd1a85d1395f/|default",
    "Ciudad de Mexico|https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/|cdmx_panel",
])

# =========================
# Utilidades
# =========================
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
]
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => false});
Object.defineProperty(navigator, 'languages', {get: () => ['es-MX','es','en-US','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
window.chrome = { runtime: {} };
"""

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
NO_CITAS_PATTERNS = [
    "No hay horas disponibles",
    "No hay citas disponibles",
    "No hay disponibilidad",
    "Inténtelo de nuevo dentro de unos días",
]

def notify(msg: str) -> None:
    print(msg, flush=True)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
                timeout=25
            )
        except Exception as e:
            print(f"[WARN] Telegram fallo: {e}", file=sys.stderr)

def send_photo(path: str, caption: str = "") -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        with open(path, "rb") as f:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
            files = {"photo": (os.path.basename(path), f, "image/jpeg")}
            requests.post(url, data=data, files=files, timeout=60)
    except Exception as e:
        print(f"[WARN] send_photo: {e}", file=sys.stderr)

def send_document(path: str, caption: str = "") -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        with open(path, "rb") as f:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                          files={"document": (os.path.basename(path), f)}, timeout=90)
    except Exception as e:
        print(f"[WARN] send_document: {e}", file=sys.stderr)

def log_step(msg: str) -> None:
    if DEBUG_STEPS:
        print(msg, flush=True)

def human_pause() -> None:
    time.sleep(random.uniform(HUMAN_MIN, HUMAN_MAX))

def parse_consul_list(env_val: str) -> List[Tuple[str, str, str]]:
    out = []
    for item in [s.strip() for s in env_val.split(",") if s.strip()]:
        parts = [p.strip() for p in item.split("|")]
        if len(parts) >= 2:
            name, url = parts[0], parts[1]
            mode = parts[2].lower() if len(parts) >= 3 else "default"
            out.append((name, url, mode))
    return out

def choose_proxy(proxies: List[str]) -> Optional[dict]:
    if not proxies:
        return None
    raw = random.choice(proxies)
    try:
        if "://" not in raw:
            raw = "http://" + raw
        scheme, rest = raw.split("://", 1)
        if "@" in rest:
            creds, hostport = rest.split("@", 1)
            if ":" in creds:
                user, pwd = creds.split(":", 1)
                return {"server": f"{scheme}://{hostport}", "username": user, "password": pwd}
        return {"server": f"{scheme}://{rest}"}
    except Exception:
        return {"server": raw}

def visible_text(page) -> str:
    try:
        return (page.inner_text("body") or "")
    except Exception:
        return ""

def find_time_nodes_anywhere(page) -> List[str]:
    horas = set()
    # main
    try:
        times = page.locator(r"text=/\b([01]?\d|2[0-3]):[0-5]\d\b/")
        n = times.count()
        for i in range(min(n, 600)):
            try:
                node = times.nth(i)
                if not node.is_visible():
                    continue
                txt = (node.inner_text() or "").strip()
                m = TIME_RE.search(txt)
                if m:
                    horas.add(m.group(0))
            except Exception:
                continue
    except Exception:
        pass
    # iframes
    try:
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            times = fr.locator(r"text=/\b([01]?\d|2[0-3]):[0-5]\d\b/")
            n = times.count()
            for i in range(min(n, 600)):
                try:
                    node = times.nth(i)
                    if not node.is_visible():
                        continue
                    txt = (node.inner_text() or "").strip()
                    m = TIME_RE.search(txt)
                    if m:
                        horas.add(m.group(0))
                except Exception:
                    continue
    except Exception:
        pass
    return sorted(horas)

def page_has_no_citas_visible(page) -> bool:
    try:
        for pat in NO_CITAS_PATTERNS:
            loc = page.locator(f"text=/{re.escape(pat)}/i")
            n = loc.count()
            for i in range(min(n, 10)):
                try:
                    if loc.nth(i).is_visible():
                        return True
                except Exception:
                    continue
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            for pat in NO_CITAS_PATTERNS:
                try:
                    loc = fr.locator(f"text=/{re.escape(pat)}/i")
                    n = loc.count()
                    for i in range(min(n, 10)):
                        try:
                            if loc.nth(i).is_visible():
                                return True
                        except Exception:
                            continue
                except Exception:
                    continue
    except Exception:
        pass
    return False

def wait_calendar_ready(page, timeout_ms: int = 30000) -> str:
    """Devuelve: 'hours' | 'no_citas' | 'timeout'."""
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        if find_time_nodes_anywhere(page):
            return "hours"
        if page_has_no_citas_visible(page):
            return "no_citas"
        time.sleep(0.25)
    return "timeout"

def shoot_best_view(page, name: str, suffix: str) -> Optional[str]:
    # Prioriza el iframe más grande
    try:
        ifr = page.locator("iframe")
        n = ifr.count()
        biggest_i = -1
        biggest_area = 0
        for i in range(min(n, 20)):
            try:
                el = ifr.nth(i)
                if not el.is_visible():
                    continue
                box = el.bounding_box()
                if not box:
                    continue
                area = box["width"] * box["height"]
                if area > biggest_area:
                    biggest_area = area
                    biggest_i = i
            except Exception:
                continue
        if biggest_i >= 0:
            el = ifr.nth(biggest_i)
            path = f"/tmp/{name.replace(' ', '_').lower()}_{suffix}_iframe.jpg"
            el.screenshot(path=path, type="jpeg", quality=70)
            return path
    except Exception:
        pass
    # Página completa
    try:
        path = f"/tmp/{name.replace(' ', '_').lower()}_{suffix}_page.jpg"
        page.screenshot(path=path, type="jpeg", quality=70, full_page=True)
        return path
    except Exception:
        return None

def slots_signature(hours: List[str]) -> str:
    return hashlib.sha256(",".join(sorted(hours)).encode("utf-8")).hexdigest()

# =========================
# Playwright context
# =========================
def _open_context(p, proxy_conf: Optional[dict]):
    browser = p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--hide-scrollbars",
            "--disable-gpu",
        ],
        proxy=proxy_conf
    )
    ua = random.choice(USER_AGENTS)
    vw = random.randint(1200, 1440)
    vh = random.randint(800, 960)
    context = browser.new_context(
        viewport={"width": vw, "height": vh},
        user_agent=ua,
        locale="es-ES",
        extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"},
    )
    context.add_init_script(STEALTH_JS)

    if BLOCK_IMAGES or BLOCK_FONTS:
        def route_handler(route):
            try:
                rtype = route.request.resource_type
                if BLOCK_IMAGES and rtype in ("image", "media"):
                    return route.abort()
                if BLOCK_FONTS and rtype in ("font",):
                    return route.abort()
            except Exception:
                pass
            return route.continue_()
        context.route("**/*", route_handler)

    def on_dialog(dialog):
        try:
            dialog.accept()
        except Exception:
            pass
    context.on("dialog", on_dialog)

    if TRACE_PLAYWRIGHT:
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=False)
        except Exception:
            pass

    page = context.new_page()
    page.set_default_timeout(30000)
    return browser, context, page

# =========================
# Una pasada (posible proxy)
# =========================
def revisar_once(name: str, url: str, mode: str, proxy_conf: Optional[dict]) -> Tuple[str, List[str], Optional[str], Optional[str]]:
    """
    return: (status, hours, fecha, shot)
      status: "hours" | "no_citas" | "timeout" | "blank"
      shot: ruta a screenshot (solo se usa al notificar huecos)
    """
    with sync_playwright() as p:
        browser, context, page = _open_context(p, proxy_conf)

        log_step(f"[{name}] goto…")
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        human_pause()

        # Detección de "página vacía"
        body_txt = (visible_text(page) or "").strip()
        html_len = len(page.content() or "")
        log_step(f"[{name}] text_len={len(body_txt)} html_len={html_len}")
        if len(body_txt) < 8 and html_len < 1500:
            status = "blank"
            if TRACE_PLAYWRIGHT:
                try:
                    tpath = f"/tmp/trace_{name.replace(' ', '_').lower()}_{int(time.time())}.zip"
                    context.tracing.stop(path=tpath)
                    send_document(tpath, f"{name}: trace (blank/bloqueo)")
                except Exception:
                    pass
            browser.close()
            return (status, [], None, None)

        # Flujo por modo
        if mode == "default":
            try:
                log_step(f"[{name}] esperando botón Continue/Continuar…")
                page.wait_for_selector(
                    'button.btn.btn-success, button:has-text("Continue"), button:has-text("Continuar")',
                    timeout=8000
                )
                page.click('button.btn.btn-success, button:has-text("Continue"), button:has-text("Continuar")', force=True)
                human_pause()
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                log_step(f"[{name}] Continue OK → esperando calendario…")
            except PTimeout:
                log_step(f"[{name}] no apareció el botón Continue (posible ya adentro).")
        elif mode == "cdmx_panel":
            try:
                log_step(f"[{name}] CDMX: buscando panel…")
                panel = page.locator(
                    "text=/PRESENTACION|MEMORIA|CONTINUAR SUPONE/i, .panel, .well, .panel-body, .card"
                )
                cnt = panel.count()
                log_step(f"[{name}] CDMX: panel count = {cnt}")
                if cnt > 0:
                    panel.first.click(force=True)
                    human_pause()
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    log_step(f"[{name}] CDMX: click panel → esperando calendario…")
                else:
                    log_step(f"[{name}] CDMX: no se encontró panel (no click extra).")
            except Exception as e:
                log_step(f"[{name}] CDMX: error en click panel: {e}")

        status = wait_calendar_ready(page, timeout_ms=30000)
        fecha = None  # si luego quieres parsear fecha, colócala aquí

        if status == "hours":
            hours = find_time_nodes_anywhere(page)
            # Solo enviamos captura cuando hay huecos
            shot = shoot_best_view(page, name, "citas")
            if TRACE_PLAYWRIGHT:
                try:
                    context.tracing.stop()  # sin adjuntar
                except Exception:
                    pass
            browser.close()
            return ("hours", hours, fecha, shot)

        if status == "no_citas":
            if TRACE_PLAYWRIGHT:
                try:
                    context.tracing.stop()
                except Exception:
                    pass
            browser.close()
            return ("no_citas", [], fecha, None)

        # timeout
        hours = find_time_nodes_anywhere(page)
        if TRACE_PLAYWRIGHT:
            try:
                context.tracing.stop()
            except Exception:
                pass
        browser.close()
        if hours:
            # Si encontró horas justo al final, notifícalas
            return ("hours", hours, fecha, shoot_best_view(page, name, "citas"))
        return ("timeout", [], fecha, None)

# =========================
# Orquestador (usa proxy solo si hace falta)
# =========================
def revisar_un_consulado(name: str, url: str, mode: str) -> Tuple[bool, List[str], Optional[str], Optional[str], Optional[str]]:
    status, hours, fecha, shot = revisar_once(name, url, mode, proxy_conf=None)
    if status == "blank" and PROXY_LIST and RETRIES_ON_BLOCK > 0:
        notify(f"⚠️ {name}: página vacía (posible bloqueo). Reintentando con proxy…")
        attempts = min(RETRIES_ON_BLOCK, len(PROXY_LIST))
        for _ in range(attempts):
            proxy = choose_proxy(PROXY_LIST)
            status, hours, fecha, shot = revisar_once(name, url, mode, proxy_conf=proxy)
            if status != "blank":
                break

    if status == "hours":
        return (True, hours, fecha, shot, None)
    if status == "no_citas":
        return (False, [], fecha, None, None)
    if status == "timeout":
        return (False, [], fecha, None, None)
    return (False, [], None, None, "blank")

# =========================
# Bucle principal
# =========================
def main():
    consulados = parse_consul_list(CONSUL_URLS)
    if not consulados:
        print("[ERROR] No hay consulados configurados.", flush=True)
        sys.exit(1)

    print("[INFO] Consulados:", ", ".join(f"{n}({m})" for n,_,m in consulados), flush=True)
    if PROXY_LIST:
        print(f"[INFO] Proxies configurados: {len(PROXY_LIST)} (solo si hay bloqueo).", flush=True)

    last_sig: Dict[str, str] = {}

    while True:
        try:
            for (name, url, mode) in consulados:
                ok, hours, fecha, shot, block = revisar_un_consulado(name, url, mode)

                if block == "blank":
                    notify(f"⚠️ {name}: página vacía tras reintentos (bloqueo probable).")

                if ok and hours:
                    sig = slots_signature(hours)
                    if last_sig.get(name) == sig:
                        continue
                    last_sig[name] = sig
                    primeras = ", ".join(sorted(hours)[:6])
                    f = f" ({fecha})" if fecha else ""
                    msg = f"✅ ¡HAY HUECOS en {name}!{f}\nHoras: {primeras}\nEntra: {url}"
                    notify(msg)
                    if shot:  # captura SOLO cuando hay huecos
                        send_photo(shot, msg)
                else:
                    marca = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(f"[{marca}] {name} → sin huecos por ahora.", flush=True)

            wait = random.randint(max(60, CHECK_INTERVAL_SEC - 15), CHECK_INTERVAL_SEC + 40)
            print(f"[INFO] Esperando {wait}s antes de la siguiente ronda…", flush=True)
            time.sleep(wait)

        except Exception as e:
            print(f"[ERROR] loop: {e}", flush=True)
            time.sleep(120)

if __name__ == "__main__":
    main()
