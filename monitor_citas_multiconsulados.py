# -*- coding: utf-8 -*-
# monitor_citas_multiconsulados.py
#
# Bot de monitoreo para citas (Bookitit / citaconsular.es)
# - Telegram: envía alertas, capturas y HTML de evidencia
# - Proxy residencial: toma PROXY_LIST (1+ proxies separados por coma)
# - Muestra la IP pública usada en cada vuelta cuando SHOW_PUBLIC_IP=1
# - CDMX: hace el click adicional en el panel de aviso
#
# ENV requeridas (Railway → Variables):
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
#
# Opcionales:
#   TELEGRAM_FILE_CHAT_ID (si no, usa TELEGRAM_CHAT_ID)
#   PROXY_LIST="http://user:pass@host:port[, http://user:pass@host2:port2]"
#   CHECK_INTERVAL_SEC=120        (default 120)
#   PROOF_ON_NO_SLOTS=1           (envía HTML/captura si no hay huecos)
#   DEBUG_STEPS=1                 (más logs y evidencias de cada paso)
#   BLOCK_IMAGES=1                (1= no cargar imágenes, 0= sí cargar)
#   SHOW_PUBLIC_IP=1              (imprime IP pública)
#   IP_ENDPOINT="https://api.ipify.org?format=json"
#   HUMAN_MIN=0.7  HUMAN_MAX=1.5  (pausas “humanas”)
#   FORCE_TEST=1                  (envía mensaje de prueba al iniciar)
#
# Consulados configurados:
#   Monterrey (flujo estándar)
#   Ciudad de México (requiere click panel)
#
# Ejecutar local:
#   pip install playwright requests
#   playwright install --with-deps chromium
#   python monitor_citas_multiconsulados.py
#
# Probe (una sola visita y evidencias):
#   python monitor_citas_multiconsulados.py --probe "Ciudad de México"

import os, sys, time, json, random, re, pathlib, urllib.parse
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout

# ------------------------ Config & helpers ------------------------

@dataclass
class Cfg:
    # URLs
    URLS: Dict[str, Dict] = None

    # Selectores / textos frecuentes
    SELECTOR_CONTINUE: str = 'button.btn.btn-success, button:has-text("Continue"), button:has-text("Continuar")'
    TEXT_NO_CITAS: str = "No hay horas disponibles"
    BUTTON_CANDIDATES: str = "button, .btn, [role=button]"
    DIA_REGEX: str = r"(Lunes|Martes|Miércoles|Jueves|Viernes|Sábado|Domingo).*?\b\d{4}\b"

    # Control
    CHECK_INTERVAL_SEC: int = int(os.getenv("CHECK_INTERVAL_SEC", "120"))
    PROOF_ON_NO_SLOTS: bool = os.getenv("PROOF_ON_NO_SLOTS", "0") == "1"
    DEBUG_STEPS: bool = os.getenv("DEBUG_STEPS", "0") == "1"
    BLOCK_IMAGES: bool = os.getenv("BLOCK_IMAGES", "1") == "1"
    SHOW_PUBLIC_IP: bool = os.getenv("SHOW_PUBLIC_IP", "1") == "1"
    IP_ENDPOINT: str = os.getenv("IP_ENDPOINT", "https://api.ipify.org?format=json")

    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_FILE_CHAT_ID: str = os.getenv("TELEGRAM_FILE_CHAT_ID", "")

    HUMAN_MIN: float = float(os.getenv("HUMAN_MIN", "0.7"))
    HUMAN_MAX: float = float(os.getenv("HUMAN_MAX", "1.5"))

    PROXY_LIST_RAW: str = os.getenv("PROXY_LIST", "").strip()

    def __post_init__(self):
        if not self.URLS:
            self.URLS = {
                # Monterrey (flujo estándar)
                "Monterrey": {
                    "url": "https://www.citaconsular.es/es/hosteds/widgetdefault/25b18886db70f7ec9fd6dfd1a85d1395f/",
                    "cdmx_panel": False,
                    "mobile_like": False,
                },
                # Ciudad de México (click panel)
                "Ciudad de México": {
                    "url": "https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/",
                    "cdmx_panel": True,
                    "mobile_like": True,   # emular iPhone para mejorar score anti-bot
                },
            }

cfg = Cfg()

CHAT_FILES = cfg.TELEGRAM_FILE_CHAT_ID or cfg.TELEGRAM_CHAT_ID

UA_DESKTOPS = [
    # Chrome / Edge / Firefox recientes
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4; rv:127.0) Gecko/20100101 Firefox/127.0",
]
UA_MOBILE = [
    # iPhone Safari
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")

# ------------------------ Telegram ------------------------

def notify(text: str):
    print(text, flush=True)
    if not (cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": cfg.TELEGRAM_CHAT_ID, "text": text},
            timeout=15
        )
    except Exception as e:
        print(f"[WARN] Telegram sendMessage: {e}", flush=True)

def send_photo(path: str, caption: str = ""):
    if not (cfg.TELEGRAM_BOT_TOKEN and CHAT_FILES and pathlib.Path(path).exists()):
        return
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": CHAT_FILES, "caption": caption},
                files={"photo": f},
                timeout=30
            )
    except Exception as e:
        print(f"[WARN] Telegram sendPhoto: {e}", flush=True)

def send_document(path: str, caption: str = ""):
    if not (cfg.TELEGRAM_BOT_TOKEN and CHAT_FILES and pathlib.Path(path).exists()):
        return
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendDocument",
                data={"chat_id": CHAT_FILES, "caption": caption},
                files={"document": f},
                timeout=30
            )
    except Exception as e:
        print(f"[WARN] Telegram sendDocument: {e}", flush=True)

# ------------------------ Proxy & IP ------------------------

def parse_proxy(url: str) -> Optional[dict]:
    """Convierte una URL de proxy en dict para Playwright: {'server', 'username', 'password'}."""
    try:
        u = urllib.parse.urlparse(url.strip())
        if not u.scheme or not u.hostname or not u.port:
            return None
        pw = None
        user = None
        if u.username:
            user = urllib.parse.unquote(u.username)
        if u.password:
            pw = urllib.parse.unquote(u.password)
        return {
            "server": f"{u.scheme}://{u.hostname}:{u.port}",
            "username": user,
            "password": pw
        }
    except Exception:
        return None

def pick_proxy() -> Tuple[Optional[dict], Optional[str]]:
    """Elige un proxy de PROXY_LIST (al azar) y devuelve (dict_playwright, url_plain)."""
    if not cfg.PROXY_LIST_RAW:
        return None, None
    options = [p.strip() for p in cfg.PROXY_LIST_RAW.split(",") if p.strip()]
    if not options:
        return None, None
    chosen = random.choice(options)
    pd = parse_proxy(chosen)
    return pd, chosen

def get_public_ip(proxy_url: Optional[str]) -> Optional[str]:
    """Obtiene IP pública usando la misma ruta (con o sin proxy)."""
    try:
        kw = {}
        if proxy_url:
            kw["proxies"] = {"http": proxy_url, "https": proxy_url}
        r = requests.get(cfg.IP_ENDPOINT, timeout=10, **kw)
        if r.ok:
            j = r.json() if r.headers.get("content-type","").startswith("application/json") else {"ip": r.text.strip()}
            return j.get("ip")
    except Exception as e:
        print(f"[WARN] get_public_ip: {e}", flush=True)
    return None

# ------------------------ Utils ------------------------

def human_pause():
    time.sleep(random.uniform(cfg.HUMAN_MIN, cfg.HUMAN_MAX))

def now_stamp():
    return time.strftime("%Y-%m-%d_%H-%M-%S")

def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", s.lower())

def find_date_text(page) -> Optional[str]:
    try:
        content = (page.content() or "").strip()
    except Exception:
        return None
    m = re.search(cfg.DIA_REGEX, content, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(0)
    return None

def extract_real_slots(page) -> List[Tuple[str, str]]:
    slots = []
    try:
        candidates = page.locator(cfg.BUTTON_CANDIDATES)
        count = candidates.count()
    except Exception:
        count = 0
    for i in range(min(count, 400)):
        try:
            el = candidates.nth(i)
            if not el.is_visible():
                continue
            text = el.inner_text().strip()
            if not text:
                continue
            if "hueco libre" not in text.lower():
                continue
            m = TIME_RE.search(text)
            if m:
                slots.append((m.group(0), text))
        except Exception:
            continue
    return slots

# ------------------------ Core visit ------------------------

def revisar_una_vez(nombre: str, url: str, cdmx_panel: bool, mobile_like: bool,
                    p, browser, debug_prefix: str, proxy_url: Optional[str]) -> Tuple[bool, List[Tuple[str,str]], Optional[str], bool, int]:
    """
    Devuelve: (ok_slots, slots, fecha, blank_html, html_len)
    """
    # Contexto
    ua = random.choice(UA_MOBILE if mobile_like else UA_DESKTOPS)
    vw = random.randint(1200, 1440)
    vh = random.randint(800, 960)

    ctx_args = dict(
        viewport={"width": vw, "height": vh},
        user_agent=ua,
        locale="es-ES",
        extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"},
    )
    if cfg.BLOCK_IMAGES:
        ctx_args["permissions"] = []
        ctx_args["bypass_csp"] = True

    context = browser.new_context(**ctx_args)
    page = context.new_page()
    page.set_default_timeout(20000)

    # Bloqueo de imágenes si así lo definen (request interception simple)
    if cfg.BLOCK_IMAGES:
        try:
            page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"] else route.continue_())
        except Exception:
            pass

    # Primer aviso JS alert — aceptar automáticamente
    def on_dialog(dialog):
        try:
            dialog.accept()
        except Exception:
            pass
    context.on("dialog", on_dialog)

    # Navegar
    if cfg.DEBUG_STEPS:
        print(f"[{nombre}] goto…", flush=True)
    page.goto(url, wait_until="domcontentloaded")
    human_pause()

    # Botón Continue si existe (flujo estándar)
    try:
        page.wait_for_selector(cfg.SELECTOR_CONTINUE, timeout=7000)
        page.click(cfg.SELECTOR_CONTINUE, force=True)
        human_pause()
    except PTimeout:
        pass

    # Para CDMX – click en panel grande de aviso
    if cdmx_panel:
        if cfg.DEBUG_STEPS:
            print(f"[{nombre}] CDMX: click panel…", flush=True)
        clicked = False
        # 1) Por texto visible
        try:
            panel = page.get_by_text("PRESENTACION DOCUMENTACION", exact=False)
            panel.first.click(timeout=4000)
            clicked = True
            human_pause()
        except Exception:
            pass
        # 2) Fallback: click en panel/container grande
        if not clicked:
            try:
                page.click("css=.panel, .panel-body, .container, .content, .box", timeout=3000)
                clicked = True
                human_pause()
            except Exception:
                pass

    # ¿Mensaje “No hay horas disponibles”?
    try:
        page.get_by_text(cfg.TEXT_NO_CITAS, exact=False).wait_for(timeout=3000)
        fecha = find_date_text(page)
        # Evidencia si corresponde
        if cfg.PROOF_ON_NO_SLOTS or cfg.DEBUG_STEPS:
            save_evidencias(nombre, page, debug_prefix, "no_slots")
        context.close()
        return (False, [], fecha, False, 0)
    except PTimeout:
        pass

    # Capturar HTML para comprobar “blanco”
    html = ""
    try:
        html = page.content() or ""
    except Exception:
        html = ""
    html_len = len(html.strip())
    blank = html_len < 120  # umbral bajo; típico "en blanco" ~39B

    # Buscar huecos
    slots = extract_real_slots(page)
    fecha = find_date_text(page)

    # Evidencias: si hay huecos, siempre; si no, según flags
    need_proof = bool(slots) or cfg.PROOF_ON_NO_SLOTS or cfg.DEBUG_STEPS or blank
    if need_proof:
        save_evidencias(nombre, page, debug_prefix, "slots" if slots else ("blank" if blank else "page"))

    context.close()
    return (bool(slots), slots, fecha, blank, html_len)

def save_evidencias(nombre: str, page, prefix: str, tag: str):
    """Guarda y envía HTML + captura."""
    stamp = now_stamp()
    base = f"/tmp/{prefix}_{slug(nombre)}_{tag}_{stamp}"
    html_path = base + ".html"
    png_path = base + ".png"

    # HTML
    try:
        content = page.content() or ""
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(content)
        send_document(html_path, caption=f"{nombre}: HTML ({tag})")
    except Exception as e:
        print(f"[WARN] save_evidencias(html): {e}", flush=True)

    # PNG
    try:
        page.screenshot(path=png_path, full_page=True)
        send_photo(png_path, caption=f"{nombre}: captura ({tag})")
    except Exception as e:
        print(f"[WARN] save_evidencias(png): {e}", flush=True)

# ------------------------ Main loop ------------------------

def main_loop():
    # Msg de prueba
    if os.getenv("FORCE_TEST") == "1":
        notify("🚀 Test OK: el bot está listo y puede enviarte evidencias e IP.")
        time.sleep(3)

    # Proxy
    pw_proxy, raw_proxy = pick_proxy()
    proxy_info = pw_proxy["server"] if pw_proxy else "SIN PROXY"
    print(f"[INFO] Proxy: {proxy_info}", flush=True)

    # IP pública
    if cfg.SHOW_PUBLIC_IP:
        ip = get_public_ip(raw_proxy)
        if ip:
            print(f"[INFO] IP pública: {ip}", flush=True)
        else:
            print("[INFO] IP pública: (no disponible)", flush=True)

    # Playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy=pw_proxy if pw_proxy else None,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )

        while True:
            try:
                for nombre, meta in cfg.URLS.items():
                    url = meta["url"]
                    cdmx_panel = bool(meta.get("cdmx_panel"))
                    mobile_like = bool(meta.get("mobile_like"))

                    prefix = "debug" if cfg.DEBUG_STEPS else "proof" if cfg.PROOF_ON_NO_SLOTS else "run"

                    ok, slots, fecha, blank, html_len = revisar_una_vez(
                        nombre, url, cdmx_panel, mobile_like, p, browser, prefix, raw_proxy
                    )

                    if blank:
                        notify(f"⚠️ {nombre}: página vacía tras reintentos (bloqueo probable). [html_len={html_len}]")

                    if ok and slots:
                        primeras = ", ".join(sorted({h for h, _ in slots})[:5])
                        suf = f" ({fecha})" if fecha else ""
                        notify(f"✅ ¡HAY HUECOS! {nombre}{suf} → Horas: {primeras}\nEntra ya: {url}")
                        # Espera anti-doble notificación
                        time.sleep(300)
                    else:
                        marca = time.strftime("%Y-%m-%d %H:%M:%S")
                        notify(f"[{marca}] {nombre} -> sin huecos por ahora.")

                # Espera “humana” entre rondas
                wait_min = max(60, cfg.CHECK_INTERVAL_SEC - 20)
                wait_max = cfg.CHECK_INTERVAL_SEC + 40
                espera = random.randint(wait_min, wait_max)
                print(f"[INFO] Esperando {espera}s antes de la siguiente ronda…", flush=True)
                time.sleep(espera)

            except Exception as e:
                print(f"[ERROR] loop: {e}", flush=True)
                time.sleep(90)

        # browser.close()  # (no se alcanza)

# ------------------------ Probe ------------------------

def run_probe(target_name: str):
    pw_proxy, raw_proxy = pick_proxy()
    proxy_info = pw_proxy["server"] if pw_proxy else "SIN PROXY"
    print(f"[probe] Proxy: {proxy_info}", flush=True)
    if cfg.SHOW_PUBLIC_IP:
        ip = get_public_ip(raw_proxy)
        print(f"[probe] IP pública: {ip}", flush=True)

    meta = cfg.URLS.get(target_name)
    if not meta:
        print(f"[probe] Desconocido: {target_name}", flush=True)
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy=pw_proxy if pw_proxy else None,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        ok, slots, fecha, blank, html_len = revisar_una_vez(
            target_name, meta["url"], bool(meta.get("cdmx_panel")), bool(meta.get("mobile_like")),
            p, browser, "probe", raw_proxy
        )
        print(f"[probe] {target_name}: ok={ok}, slots={slots}, fecha={fecha}, blank={blank}, html_len={html_len}", flush=True)
        browser.close()

# ------------------------ Entrypoint ------------------------

def parse_args():
    # Muy simple: --probe "<nombre exacto>"
    if len(sys.argv) >= 3 and sys.argv[1] == "--probe":
        return ("probe", sys.argv[2])
    return ("run", "")

if __name__ == "__main__":
    mode, param = parse_args()
    print("[start] Launching bot…", flush=True)
    print(f"[INFO] Config: proof={'ON' if cfg.PROOF_ON_NO_SLOTS else 'OFF'} "
          f"debug={'ON' if cfg.DEBUG_STEPS else 'OFF'} "
          f"block_images={'ON' if cfg.BLOCK_IMAGES else 'OFF'}", flush=True)
    if mode == "probe":
        run_probe(param)
    else:
        names = ", ".join(cfg.URLS.keys())
        print(f"[INFO] Consulados: {names}", flush=True)
        main_loop()
