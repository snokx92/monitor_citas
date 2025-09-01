# monitor_citas_multiconsulados.py
# Bot para monitorear huecos en Bookitit (citaconsular.es) con:
# - Modo stealth (oculta señales de automatización)
# - Capturas correctas de iframes
# - Detección de HH:MM visible en todo el DOM (página + iframes)
# - Soporte de proxies rotativos
# - Aviso si la página está “en blanco” (posible bloqueo por IP/fingerprint)
# - Envío de capturas siempre (opcional) vía Telegram

import os, sys, time, random, re, hashlib
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout

# ─────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────
@dataclass
class Config:
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str   = os.getenv("TELEGRAM_CHAT_ID", "")

    CHECK_INTERVAL_SEC: int = int(os.getenv("CHECK_INTERVAL_SEC", "60"))
    HUMAN_MIN: float = float(os.getenv("HUMAN_MIN", "0.7"))
    HUMAN_MAX: float = float(os.getenv("HUMAN_MAX", "1.5"))

    # Selector general de “Continuar”
    SELECTOR_CONTINUE: str = 'button.btn.btn-success, button:has-text("Continue"), button:has-text("Continuar")'
    DIA_REGEX: str         = r"(Lunes|Martes|Miércoles|Jueves|Viernes|Sábado|Domingo).*?\b\d{4}\b"

    # Lista de consulados (Nombre|URL|modo)  modo: default / cdmx_panel
    CONSUL_URLS: str = os.getenv(
        "CONSUL_URLS",
        ",".join([
            "Monterrey|https://www.citaconsular.es/es/hosteds/widgetdefault/25b18886db70f7ec9fd6dfd1a85d1395f/|default",
            "Ciudad de México|https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/|cdmx_panel",
            "Miami|https://www.citaconsular.es/es/hosteds/widgetdefault/2533f04b1d3e818b66f175afc9c24cf63/|default",
        ])
    )

    # Capturas
    DEBUG_SHOT: bool = os.getenv("DEBUG_SHOT", "0") == "1"          # guardar en /tmp
    SEND_ALL_SHOTS: bool = os.getenv("SEND_ALL_SHOTS", "0") == "1"  # enviar siempre por Telegram

    # Proxies
    PROXY_LIST: str = os.getenv("PROXY_LIST", "").strip()
    ROTATE_PROXY_EACH_ROUND: bool = os.getenv("ROTATE_PROXY_EACH_ROUND", "1") == "1"

cfg = Config()

# ─────────────────────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────────────────────
def notify(msg: str):
    print(msg, flush=True)
    if cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": cfg.TELEGRAM_CHAT_ID, "text": msg},
                timeout=20,
            )
        except Exception as e:
            print(f"[WARN] Telegram fallo: {e}", file=sys.stderr)

def send_photo(path: str, caption: str = ""):
    if not (cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID):
        print("[WARN] Telegram no configurado; no se puede enviar foto.")
        return
    try:
        with open(path, "rb") as f:
            url = f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendPhoto"
            data = {"chat_id": cfg.TELEGRAM_CHAT_ID, "caption": caption}
            files = {"photo": (os.path.basename(path), f, "image/jpeg")}
            requests.post(url, data=data, files=files, timeout=40)
    except Exception as e:
        print(f"[WARN] send_photo fallo: {e}", file=sys.stderr)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
]

def human_pause():
    time.sleep(random.uniform(cfg.HUMAN_MIN, cfg.HUMAN_MAX))

def parse_consul_list(env_val: str) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    for item in [s.strip() for s in env_val.split(",") if s.strip()]:
        parts = [p.strip() for p in item.split("|")]
        if len(parts) >= 2:
            name, url = parts[0], parts[1]
            mode = parts[2].lower() if len(parts) >= 3 else "default"
            out.append((name, url, mode))
    return out

def parse_proxies(env_val: str) -> List[str]:
    vals = [s.strip() for s in env_val.split(",") if s.strip()]
    # soporta formatos: http://user:pass@host:port , http://host:port , socks5://host:port
    return vals

def choose_proxy(proxies: List[str]) -> Optional[dict]:
    if not proxies:
        return None
    raw = random.choice(proxies)
    # playwright acepta: {"server":"http://host:port","username":"user","password":"pass"}
    try:
        # simple: si viene con credenciales user:pass@
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

# ─────────────────────────────────────────────────────────────
# Stealth y detecciones
# ─────────────────────────────────────────────────────────────
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
NO_CITAS_PATTERNS = [
    "No hay horas disponibles",
    "No hay citas disponibles",
    "No hay disponibilidad",
    "Inténtelo de nuevo dentro de unos días",
]

STEALTH_JS = r"""
// Ocultar webdriver
Object.defineProperty(navigator, 'webdriver', {get: () => false});
// Idiomas plausibles
Object.defineProperty(navigator, 'languages', {get: () => ['es-MX','es','en-US','en']});
// Plugins fake
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
// Chrome object fake
window.chrome = { runtime: {} };
// Permissions siempre 'granted' a notifications (evita rarezas)
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
      ? Promise.resolve({ state: 'granted' })
      : origQuery(parameters)
  );
}
// WebGL vendor/renderer
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
  if (parameter === 37445) return 'Intel Inc.';         // UNMASKED_VENDOR_WEBGL
  if (parameter === 37446) return 'Intel Iris OpenGL';  // UNMASKED_RENDERER_WEBGL
  return getParameter.call(this, parameter);
};
"""

def visible_text(page) -> str:
    try:
        return (page.inner_text("body") or "")
    except Exception:
        return ""

def find_date_text(page) -> Optional[str]:
    try:
        content = (page.content() or "")
    except Exception:
        return None
    m = re.search(cfg.DIA_REGEX, content, re.IGNORECASE | re.DOTALL)
    return m.group(0) if m else None

def looks_blank(page) -> bool:
    try:
        txt = visible_text(page).strip()
        html = (page.content() or "")
        if len(txt) < 8 and len(html) < 1500:
            return True
    except Exception:
        pass
    return False

def page_has_no_citas_visible(page) -> bool:
    try:
        for pat in NO_CITAS_PATTERNS:
            loc = page.locator(f"text=/{re.escape(pat)}/i")
            n = loc.count()
            for i in range(min(n,10)):
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
                    for i in range(min(n,10)):
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

def find_time_nodes_anywhere(page) -> List[str]:
    horas = set()
    try:
        times = page.locator(r"text=/\b([01]?\d|2[0-3]):[0-5]\d\b/")
        n = times.count()
        for i in range(min(n,600)):
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
    try:
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            times = fr.locator(r"text=/\b([01]?\d|2[0-3]):[0-5]\d\b/")
            n = times.count()
            for i in range(min(n,600)):
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

def wait_calendar_ready(page, timeout_ms: int = 22000) -> str:
    deadline = time.time() + (timeout_ms/1000.0)
    while time.time() < deadline:
        if find_time_nodes_anywhere(page):
            return "hours"
        if page_has_no_citas_visible(page):
            return "no_citas"
        time.sleep(0.25)
    return "timeout"

# Captura: prioriza iframe visible más grande
def shoot_best_view(page, name: str, suffix: str) -> Optional[str]:
    # 1) Iframe más grande
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
            path = f"/tmp/{name.replace(' ', '').lower()}{suffix}_iframe.jpg"
            el.screenshot(path=path, type="jpeg", quality=75)
            return path
    except Exception:
        pass
    # 2) Página completa
    try:
        path = f"/tmp/{name.replace(' ', '').lower()}{suffix}_page.jpg"
        page.screenshot(path=path, type="jpeg", quality=75, full_page=True)
        return path
    except Exception:
        return None

# ─────────────────────────────────────────────────────────────
# Core de revisión por consulado
# ─────────────────────────────────────────────────────────────
def revisar_un_consulado(name: str, url: str, modo: str = "default", headless: bool = True,
                         proxy_conf: Optional[dict] = None
                         ) -> Tuple[bool, List[Tuple[str,str]], Optional[str], Optional[str], Optional[str]]:
    """
    Devuelve: (hay_huecos, slots, fecha, screenshot_path, bloqueo_msg)
    bloqueo_msg ≠ None cuando parece página en blanco/bloqueo.
    """
    shot_path = None
    block_msg = None
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
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
        # Stealth
        context.add_init_script(STEALTH_JS)

        page = context.new_page()
        page.set_default_timeout(25000)

        # Gesto humano inicial
        try:
            page.mouse.move(random.randint(50, vw-50), random.randint(50, vh-50), steps=8)
        except Exception:
            pass

        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        human_pause()

        # Si la página está en blanco ⇒ probable bloqueo (por IP o fingerprint)
        if looks_blank(page):
            block_msg = f"⚠️ {name}: la página parece vacía (posible bloqueo por IP/anti-bot)."
            blank_shot = shoot_best_view(page, name, "blank")
            if cfg.SEND_ALL_SHOTS and blank_shot:
                send_photo(blank_shot, block_msg)
            browser.close()
            return (False, [], None, None, block_msg)

        # Flujo por modo
        if modo == "default":
            try:
                page.wait_for_selector(cfg.SELECTOR_CONTINUE, timeout=8000)
                page.click(cfg.SELECTOR_CONTINUE, force=True)
                human_pause()
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
            except PTimeout:
                pass
        elif modo == "cdmx_panel":
            try:
                panel = page.locator(
                    "text=/PRESENTACION|LEY MEMORIA|CONTINUAR SUPONE/i, .panel, .well, .panel-body, .card"
                )
                if panel.count() > 0:
                    panel.first.click(force=True)
                    human_pause()
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    if not find_time_nodes_anywhere(page):
                        panel.first.click(force=True)
                        human_pause()
            except Exception:
                pass

        # Scroll y micro-movimientos (humano)
        try:
            page.mouse.wheel(0, random.randint(100, 400))
            human_pause()
            page.mouse.move(random.randint(40, vw-40), random.randint(40, vh-40), steps=6)
        except Exception:
            pass

        # Espera calendario/hours o “no hay”
        status = wait_calendar_ready(page, timeout_ms=22000)

        if status == "hours":
            time.sleep(0.5)
            horas = find_time_nodes_anywhere(page)
            slots = [(h, f"Hora {h}") for h in horas]
            fecha = find_date_text(page)
            time.sleep(0.4)
            shot_path = shoot_best_view(page, name, "citas")
            if cfg.SEND_ALL_SHOTS and shot_path:
                send_photo(shot_path, f"📸 {name}: horas detectadas → {', '.join(horas)}")
            browser.close()
            return (True, slots, fecha, shot_path, None)

        if status == "no_citas":
            fecha = find_date_text(page)
            time.sleep(0.4)
            no_shot = shoot_best_view(page, name, "no_citas")
            if cfg.SEND_ALL_SHOTS and no_shot:
                send_photo(no_shot, f"📸 {name}: sin huecos (mensaje visible).")
            browser.close()
            return (False, [], fecha, None, None)

        # timeout: último intento + captura
        fecha = find_date_text(page)
        horas = find_time_nodes_anywhere(page)
        slots = [(h, f"Hora {h}") for h in horas]
        time.sleep(0.4)
        timeout_shot = shoot_best_view(page, name, "timeout")
        if cfg.SEND_ALL_SHOTS and timeout_shot:
            send_photo(timeout_shot, f"⏳ {name}: timeout. Horas detectadas: {', '.join(horas) or 'ninguna'}")
        browser.close()
        return (bool(slots), slots, fecha, None, None)

# ─────────────────────────────────────────────────────────────
# Anti-spam de notificaciones repetidas
# ─────────────────────────────────────────────────────────────
def slots_signature(slots: List[Tuple[str, str]]) -> str:
    horas = sorted({h for h, _ in slots})
    return hashlib.sha256(",".join(horas).encode("utf-8")).hexdigest()

# ─────────────────────────────────────────────────────────────
def main():
    if os.getenv("FORCE_TEST") == "1":
        notify("🚀 Test OK: bot listo para enviar alertas.")
        print("[TEST] Notificación de prueba enviada.")
        time.sleep(2)
        sys.exit(0)

    consulados = parse_consul_list(cfg.CONSUL_URLS)
    proxies = parse_proxies(cfg.PROXY_LIST)
    print("[INFO] Consulados:", ", ".join(f"{n}({m})" for n,_,m in consulados), flush=True)
    if proxies:
        print(f"[INFO] Proxies cargados: {len(proxies)} (rotación={'ON' if cfg.ROTATE_PROXY_EACH_ROUND else 'OFF'})", flush=True)

    if not consulados:
        print("[ERROR] CONSUL_URLS vacío o mal formateado.", flush=True)
        sys.exit(1)

    last_sig: Dict[str, str] = {}

    while True:
        try:
            for (name, url, modo) in consulados:
                proxy_conf = choose_proxy(proxies) if cfg.ROTATE_PROXY_EACH_ROUND else (choose_proxy(proxies) if random.random()<0.33 else None)

                if proxy_conf:
                    print(f"[INFO] {name}: usando proxy {proxy_conf.get('server')}", flush=True)

                ok, slots, fecha, shot, block_msg = revisar_un_consulado(
                    name, url, modo, headless=True, proxy_conf=proxy_conf
                )

                if block_msg:
                    notify(block_msg)
                    continue

                if ok and slots:
                    sig = slots_signature(slots)
                    if last_sig.get(name) == sig:
                        continue
                    last_sig[name] = sig
                    primeras = ", ".join(sorted({h for h,_ in slots})[:6])
                    suf_fecha = f" ({fecha})" if fecha else ""
                    caption = f"✅ ¡HAY HUECOS en {name}!{suf_fecha}\nHoras: {primeras}\nEntra ya: {url}"
                    notify(caption)
                    if shot and os.path.exists(shot):
                        send_photo(shot, caption)
                    time.sleep(45)  # mini-antispam
                else:
                    marca = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(f"[{marca}] {name} → Sin huecos reales por ahora.", flush=True)

            # Espera aleatoria entre rondas
            min_wait = max(35, cfg.CHECK_INTERVAL_SEC - 15)
            max_wait = cfg.CHECK_INTERVAL_SEC + 40
            wait_time = random.randint(min_wait, max_wait)
            print(f"[INFO] Esperando {wait_time}s antes de la siguiente ronda...", flush=True)
            time.sleep(wait_time)

        except Exception as e:
            print(f"[ERROR] {e}", flush=True)
            time.sleep(120)

# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    headed = len(sys.argv) > 1 and sys.argv[1].lower().startswith("head")
    if headed:
        print("Headed demo de CDMX...")
        from typing import Optional
        def _choose():
            from random import random
            return None
        print(revisar_un_consulado(
            "Ciudad de México",
            "https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/",
            "cdmx_panel",
            headless=False,
            proxy_conf=None
        ))
    else:
        main()
