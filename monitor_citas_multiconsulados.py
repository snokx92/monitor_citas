import os
import sys
import time
import random
import re
import hashlib
from typing import List, Optional, Tuple, Dict
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout

# -----------------------------
# Configuracion
# -----------------------------
class Cfg:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

    CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "60"))
    HUMAN_MIN = float(os.getenv("HUMAN_MIN", "0.7"))
    HUMAN_MAX = float(os.getenv("HUMAN_MAX", "1.5"))

    # Boton "Continue / Continuar" de Bookitit
    SELECTOR_CONTINUE = 'button.btn.btn-success, button:has-text("Continue"), button:has-text("Continuar")'

    # Para CDMX el calendario aparece al hacer click en el panel de texto
    # (solo un click al bloque de texto grande)
    # Modo "cdmx_panel" en CONSUL_URLS.
    CONSUL_URLS = os.getenv(
        "CONSUL_URLS",
        ",".join([
            "Monterrey|https://www.citaconsular.es/es/hosteds/widgetdefault/25b18886db70f7ec9fd6dfd1a85d1395f/|default",
            "Ciudad de Mexico|https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/|cdmx_panel",
            "Miami|https://www.citaconsular.es/es/hosteds/widgetdefault/2533f04b1d3e818b66f175afc9c24cf63/|default",
        ])
    )

    DEBUG_SHOT = os.getenv("DEBUG_SHOT", "0") == "1"
    SEND_ALL_SHOTS = os.getenv("SEND_ALL_SHOTS", "0") == "1"

    PROXY_LIST = os.getenv("PROXY_LIST", "").strip()
    ROTATE_PROXY_EACH_ROUND = os.getenv("ROTATE_PROXY_EACH_ROUND", "1") == "1"
    RETRIES_PER_SITE = int(os.getenv("RETRIES_PER_SITE", "2"))

cfg = Cfg()

# -----------------------------
# Telegram helpers
# -----------------------------
def notify(msg: str) -> None:
    print(msg, flush=True)
    if cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": cfg.TELEGRAM_CHAT_ID, "text": msg},
                timeout=25,
            )
        except Exception as e:
            print(f"[WARN] Telegram fallo: {e}", file=sys.stderr)

def send_photo(path: str, caption: str = "") -> None:
    if not (cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID):
        print("[WARN] Telegram no configurado; no se puede enviar foto.")
        return
    try:
        with open(path, "rb") as f:
            url = f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendPhoto"
            data = {"chat_id": cfg.TELEGRAM_CHAT_ID, "caption": caption}
            files = {"photo": (os.path.basename(path), f, "image/jpeg")}
            requests.post(url, data=data, files=files, timeout=45)
    except Exception as e:
        print(f"[WARN] send_photo fallo: {e}", file=sys.stderr)

# -----------------------------
# Human/stealth
# -----------------------------
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

def human_pause() -> None:
    time.sleep(random.uniform(cfg.HUMAN_MIN, cfg.HUMAN_MAX))

# -----------------------------
# Parsing util
# -----------------------------
TIME_RE = re.compile(r"\\b([01]?\\d|2[0-3]):[0-5]\\d\\b")

NO_CITAS_PATTERNS = [
    "No hay horas disponibles",
    "No hay citas disponibles",
    "No hay disponibilidad",
    "Inténtelo de nuevo dentro de unos días",
]

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
    return [s.strip() for s in env_val.split(",") if s.strip()]

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
        times = page.locator(r"text=/\\b([01]?\\d|2[0-3]):[0-5]\\d\\b/")
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
            times = fr.locator(r"text=/\\b([01]?\\d|2[0-3]):[0-5]\\d\\b/")
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

def wait_calendar_ready(page, timeout_ms: int = 22000) -> str:
    deadline = time.time() + (timeout_ms/1000.0)
    while time.time() < deadline:
        if find_time_nodes_anywhere(page):
            return "hours"
        if page_has_no_citas_visible(page):
            return "no_citas"
        time.sleep(0.25)
    return "timeout"

def visual_ready_for_photo(page) -> bool:
    try:
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        body_txt = (visible_text(page) or "").strip()
        if len(body_txt) > 600:
            return True
        ifr = page.locator("iframe")
        n = ifr.count()
        for i in range(min(n, 20)):
            try:
                el = ifr.nth(i)
                if not el.is_visible():
                    continue
                box = el.bounding_box()
                if box and box["width"] >= 300 and box["height"] >= 300:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False

def shoot_best_view(page, name: str, suffix: str) -> Optional[str]:
    # intenta iframe mas grande, si no la pagina completa
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
            el.screenshot(path=path, type="jpeg", quality=75)
            return path
    except Exception:
        pass
    try:
        path = f"/tmp/{name.replace(' ', '_').lower()}_{suffix}_page.jpg"
        page.screenshot(path=path, type="jpeg", quality=75, full_page=True)
        return path
    except Exception:
        return None

# -----------------------------
# Network diag
# -----------------------------
def _dump_network_on(context, bucket: list) -> None:
    def on_response(resp):
        try:
            bucket.append((resp.url, resp.status))
        except Exception:
            pass
    context.on("response", on_response)

def _diagnose_page(page) -> dict:
    info = {}
    try:
        info["url"] = page.url
        try:
            info["text_len"] = len((page.evaluate("document.body && document.body.innerText") or "").strip())
        except Exception:
            info["text_len"] = -1
        info["html_len"] = len(page.content() or "")
        ifr = page.locator("iframe")
        n = ifr.count()
        info["iframes_count"] = n
        srcs = []
        for i in range(min(n, 10)):
            try:
                el = ifr.nth(i)
                if el.is_visible():
                    srcs.append(el.get_attribute("src") or "(sin src)")
            except Exception:
                continue
        info["iframe_srcs"] = srcs
    except Exception as e:
        info["diag_error"] = str(e)
    return info

# -----------------------------
# Una pasada (posible proxy)
# -----------------------------
def _revisar_once(name: str, url: str, modo: str, headless: bool, proxy_conf: Optional[dict]) \
        -> Tuple[str, List[str], Optional[str], Optional[str], Dict]:
    """
    Retorna: (status: 'hours'/'no_citas'/'timeout'/'blank', horas, fecha, shot_path, diag)
    """
    diag: Dict = {"proxy": proxy_conf.get("server") if proxy_conf else None}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
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

        netlog = []
        _dump_network_on(context, netlog)

        def on_dialog(d):
            try:
                d.accept()
            except Exception:
                pass
        context.on("dialog", on_dialog)

        page = context.new_page()
        page.set_default_timeout(25000)

        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        human_pause()

        # pagina "blank" (bloqueo)
        body_txt = (visible_text(page) or "").strip()
        html_len = len(page.content() or "")
        if len(body_txt) < 8 and html_len < 1500:
            d = _diagnose_page(page)
            bad = [(u, s) for (u, s) in netlog if s and s >= 400]
            d["bad_responses"] = bad[:10]
            diag.update(d)
            browser.close()
            return ("blank", [], None, None, diag)

        # flujo
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
                panel = page.locator("text=/PRESENTACION|MEMORIA|CONTINUAR SUPONE/i, .panel, .well, .panel-body, .card")
                if panel.count() > 0:
                    panel.first.click(force=True)
                    human_pause()
            except Exception:
                pass

        # micro movimiento
        try:
            page.mouse.wheel(0, random.randint(100, 420))
        except Exception:
            pass

        status = wait_calendar_ready(page, timeout_ms=24000)
        fecha = None  # fecha es opcional

        if status == "hours":
            horas = find_time_nodes_anywhere(page)
            shot = shoot_best_view(page, name, "citas") if visual_ready_for_photo(page) else None
            browser.close()
            return ("hours", horas, fecha, shot, diag)

        if status == "no_citas":
            shot = shoot_best_view(page, name, "no_citas") if (cfg.SEND_ALL_SHOTS and visual_ready_for_photo(page)) else None
            browser.close()
            return ("no_citas", [], fecha, shot, diag)

        # timeout
        horas = find_time_nodes_anywhere(page)
        shot = shoot_best_view(page, name, "timeout") if (cfg.SEND_ALL_SHOTS and visual_ready_for_photo(page)) else None
        browser.close()
        if horas:
            return ("hours", horas, fecha, shot, diag)
        return ("timeout", [], fecha, shot, diag)

# -----------------------------
# Con reintentos y rotacion de proxy
# -----------------------------
def revisar_un_consulado(name: str, url: str, modo: str, headless: bool,
                         proxies: Optional[List[str]]) -> Tuple[bool, List[str], Optional[str], Optional[str], Optional[str]]:
    attempts: List[Optional[dict]] = []
    if proxies and cfg.ROTATE_PROXY_EACH_ROUND:
        attempts.append(choose_proxy(proxies))
    else:
        attempts.append(None)
    for _ in range(max(0, cfg.RETRIES_PER_SITE)):
        attempts.append(choose_proxy(proxies) if proxies else None)

    last_block_msg = None
    for idx, proxy_conf in enumerate(attempts, 1):
        status, horas, fecha, shot, diag = _revisar_once(name, url, modo, headless, proxy_conf)

        # log IP publica de vez en cuando
        if random.random() < 0.1:
            try:
                ip = requests.get("https://api.ipify.org", timeout=5).text
                print(f"[INFO] IP publica: {ip}", flush=True)
            except Exception:
                pass

        if status == "blank":
            bad = diag.get("bad_responses", [])
            msg = (
                f"⚠️ {name}: pagina parece vacia (posible bloqueo).\n"
                f"- Proxy: {diag.get('proxy')}\n"
                f"- iframes: {diag.get('iframes_count')} (ej: {', '.join(diag.get('iframe_srcs', [])[:3])})\n"
                f"- text_len: {diag.get('text_len')}  html_len: {diag.get('html_len')}\n"
                f"- respuestas >=400: {len(bad)}"
            )
            for (u, s) in bad[:5]:
                msg += f"\n  · {s} -> {u[:120]}"
            notify(msg)
            last_block_msg = msg
            continue

        if status == "hours":
            if cfg.SEND_ALL_SHOTS and shot:
                send_photo(shot, f"{name}: horas -> {', '.join(horas[:6])}")
            return (True, horas, fecha, shot, None)

        if status == "no_citas":
            if cfg.SEND_ALL_SHOTS and shot:
                send_photo(shot, f"{name}: sin huecos (mensaje visible).")
            return (False, [], fecha, None, None)

        if status == "timeout":
            if horas:
                return (True, horas, fecha, shot, None)
            notify(f"⏳ {name}: timeout. Sin horas.")
            return (False, [], fecha, None, None)

    return (False, [], None, None, last_block_msg or "blank")

# -----------------------------
# Anti-duplicado notificaciones
# -----------------------------
def sig_from_hours(hours: List[str]) -> str:
    return hashlib.sha256(",".join(sorted(hours)).encode("utf-8")).hexdigest()

# -----------------------------
# Main loop
# -----------------------------
def main() -> None:
    consulados = parse_consul_list(cfg.CONSUL_URLS)
    proxies = parse_proxies(cfg.PROXY_LIST)

    print("[INFO] Consulados:", ", ".join([f"{n}({m})" for (n, _, m) in consulados]), flush=True)
    if proxies:
        print(f"[INFO] Proxies cargados: {len(proxies)} (rotacion={'ON' if cfg.ROTATE_PROXY_EACH_ROUND else 'OFF'})", flush=True)

    if not consulados:
        print("[ERROR] CONSUL_URLS vacio.", flush=True)
        sys.exit(1)

    last_sig: Dict[str, str] = {}

    while True:
        try:
            for (name, url, modo) in consulados:
                ok, horas, fecha, shot, block_msg = revisar_un_consulado(name, url, modo, True, proxies)

                if block_msg:
                    # ya se notifico el detalle del bloqueo
                    continue

                if ok and horas:
                    sig = sig_from_hours(horas)
                    if last_sig.get(name) == sig:
                        continue
                    last_sig[name] = sig
                    primeras = ", ".join(horas[:6])
                    f = f" ({fecha})" if fecha else ""
                    msg = f"✅ {name}: HAY HUECOS{f} -> Horas: {primeras}\nEntra: {url}"
                    notify(msg)
                    if shot:
                        send_photo(shot, msg)
                    time.sleep(45)  # anti spam basico
                else:
                    marca = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(f"[{marca}] {name} -> sin huecos por ahora.", flush=True)

            wait = random.randint(max(35, cfg.CHECK_INTERVAL_SEC - 15), cfg.CHECK_INTERVAL_SEC + 40)
            print(f"[INFO] Esperando {wait}s antes de la proxima ronda...", flush=True)
            time.sleep(wait)

        except Exception as e:
            print(f"[ERROR] loop: {e}", flush=True)
            time.sleep(120)

if __name__ == "__main__":
    main()
