# -*- coding: utf-8 -*-
"""
Monitor de citas (Monterrey + Ciudad de México, sin Miami)
- CDMX: click extra sobre el panel de normas.
- Detección por HH:MM y por “Hueco libre” (fallback si no se ve hora).
- Modo PRUEBA (--probe "Ciudad de Mexico"): vídeo, HTML y capturas, en móvil.
- En producción: opción PROOF_ON_NO_SLOTS=1 para adjuntar prueba cuando diga “sin huecos”.
- Anti-bloqueo: si body/html muy cortos => “blank” (posible bloqueo) + reintentos con proxy (si hay).

Requisitos:
  pip install playwright requests
  python -m playwright install --with-deps chromium
"""

import os, sys, time, random, re, hashlib, json
from typing import List, Optional, Tuple, Dict
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout

# =========================
# Variables de entorno / ajustes
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "120"))
HUMAN_MIN = float(os.getenv("HUMAN_MIN", "0.7"))
HUMAN_MAX = float(os.getenv("HUMAN_MAX", "1.6"))

# Ahorro de datos (en pruebas RECOMIENDO poner 0)
BLOCK_IMAGES = os.getenv("BLOCK_IMAGES", "0") == "1"
BLOCK_FONTS  = os.getenv("BLOCK_FONTS", "1") == "1"

# Pruebas / evidencias
DEBUG_STEPS       = os.getenv("DEBUG_STEPS", "1") == "1"
TRACE_PLAYWRIGHT  = os.getenv("TRACE_PLAYWRIGHT", "0") == "1"
PROOF_ON_NO_SLOTS = os.getenv("PROOF_ON_NO_SLOTS", "1") == "1"  # en prod: adjunta prueba si “no_citas”

# Proxies (solo si blank)
PROXY_LIST = [s.strip() for s in os.getenv("PROXY_LIST", "").split(",") if s.strip()]
RETRIES_ON_BLOCK = int(os.getenv("RETRIES_ON_BLOCK", "2"))

# Modo móvil específico para CDMX
CDMX_MOBILE = os.getenv("CDMX_MOBILE", "1") == "1"

# Consulados (solo Mty + CDMX)
CONSUL_URLS = ",".join([
    "Monterrey|https://www.citaconsular.es/es/hosteds/widgetdefault/25b18886db70f7ec9fd6dfd1a85d1395f/|default",
    "Ciudad de Mexico|https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/|cdmx_panel",
])

# =========================
# Utilidades
# =========================
USER_AGENTS_DESKTOP = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]
UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"

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

def nodes_with_hueco_libre_anywhere(page) -> List[str]:
    """Devuelve lista de textos de botones/elementos que contengan Hueco libre (en main y iframes)."""
    textos = []
    css = 'text=/Hueco\\s+libre/i, button:has-text("Hueco libre"), .btn:has-text("Hueco libre")'
    def scan(scope):
        try:
            loc = scope.locator(css)
            n = loc.count()
            for i in range(min(n, 300)):
                try:
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    txt = (el.inner_text() or "").strip()
                    if txt:
                        textos.append(txt)
                except Exception:
                    continue
        except Exception:
            pass
    scan(page)
    try:
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            scan(fr)
    except Exception:
        pass
    return textos

def find_time_nodes_anywhere(page) -> List[str]:
    horas = set()
    # por regex HH:MM
    def scan(scope):
        try:
            times = scope.locator(r"text=/\b([01]?\d|2[0-3]):[0-5]\d\b/")
            n = times.count()
            for i in range(min(n, 600)):
                try:
                    el = times.nth(i)
                    if not el.is_visible():
                        continue
                    txt = (el.inner_text() or "").strip()
                    m = TIME_RE.search(txt)
                    if m:
                        horas.add(m.group(0))
                except Exception:
                    continue
        except Exception:
            pass
    scan(page)
    try:
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            scan(fr)
    except Exception:
        pass

    # si encontró Hueco libre pero no hora, intenta rescatar hora del mismo texto
    if not horas:
        huecos = nodes_with_hueco_libre_anywhere(page)
        for t in huecos:
            m = TIME_RE.search(t)
            if m:
                horas.add(m.group(0))
        # si sigue sin hora pero hay Hueco libre, agrega un placeholder
        if huecos and not horas:
            horas.add("Hueco libre (sin hora)")
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

def wait_calendar_ready(page, timeout_ms: int = 45000) -> str:
    """Devuelve: 'hours' | 'no_citas' | 'timeout'."""
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        hrs = find_time_nodes_anywhere(page)
        if hrs:
            return "hours"
        if page_has_no_citas_visible(page):
            return "no_citas"
        time.sleep(0.25)
    return "timeout"

def shoot_best_view(page, name: str, suffix: str) -> Optional[str]:
    try:
        ifr = page.locator("iframe")
        n = ifr.count()
        best_i = -1
        best_area = 0
        for i in range(min(n, 20)):
            try:
                el = ifr.nth(i)
                if not el.is_visible():
                    continue
                box = el.bounding_box()
                if not box:
                    continue
                area = box["width"] * box["height"]
                if area > best_area:
                    best_area = area
                    best_i = i
            except Exception:
                continue
        if best_i >= 0:
            el = ifr.nth(best_i)
            path = f"/tmp/{name.replace(' ', '_').lower()}_{suffix}_iframe.jpg"
            el.screenshot(path=path, type="jpeg", quality=70)
            return path
    except Exception:
        pass
    try:
        path = f"/tmp/{name.replace(' ', '_').lower()}_{suffix}_page.jpg"
        page.screenshot(path=path, type="jpeg", quality=70, full_page=True)
        return path
    except Exception:
        return None

def dump_html(page, name: str, suffix: str) -> Optional[str]:
    try:
        html = page.content() or ""
        path = f"/tmp/{name.replace(' ', '_').lower()}_{suffix}.html"
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        return path
    except Exception:
        return None

def slots_signature(hours: List[str]) -> str:
    return hashlib.sha256(",".join(sorted(hours)).encode("utf-8")).hexdigest()

# =========================
# Playwright context
# =========================
def _open_context(p, proxy_conf: Optional[dict], mobile: bool, record_video: bool):
    browser_args = [
        "--no-sandbox", "--disable-setuid-sandbox",
        "--disable-dev-shm-usage", "--hide-scrollbars", "--disable-gpu",
    ]
    context_kwargs = {}
    if record_video:
        context_kwargs["record_video_dir"] = "/tmp"

    browser = p.chromium.launch(headless=True, args=browser_args, proxy=proxy_conf)

    if mobile:
        # iPhone-ish
        context = browser.new_context(
            viewport={"width": 390, "height": 800},
            user_agent=UA_IPHONE,
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            locale="es-ES",
            extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"},
            **context_kwargs
        )
    else:
        ua = random.choice(USER_AGENTS_DESKTOP)
        vw = random.randint(1200, 1440)
        vh = random.randint(800, 960)
        context = browser.new_context(
            viewport={"width": vw, "height": vh},
            user_agent=ua,
            locale="es-ES",
            extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"},
            **context_kwargs
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
def revisar_once(name: str, url: str, mode: str, proxy_conf: Optional[dict], proof_mode: bool=False) -> Tuple[str, List[str], Optional[str], Optional[str]]:
    """
    return: (status, hours, fecha, shot)
      status: "hours" | "no_citas" | "timeout" | "blank"
      shot: ruta a screenshot (cuando hay huecos)
    """
    mobile = (mode == "cdmx_panel" and CDMX_MOBILE)
    record_video = proof_mode
    with sync_playwright() as p:
        browser, context, page = _open_context(p, proxy_conf, mobile=mobile, record_video=record_video)

        log_step(f"[{name}] goto…")
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        human_pause()

        body_txt = (visible_text(page) or "").strip()
        html_len = len(page.content() or "")
        log_step(f"[{name}] text_len={len(body_txt)} html_len={html_len}")

        if proof_mode:
            dump_html(page, name, "before")
            shoot_best_view(page, name, "before")

        # Página vacía => blank
        if len(body_txt) < 8 and html_len < 1500:
            status = "blank"
            if proof_mode:
                dump_html(page, name, "blank")
                shoot_best_view(page, name, "blank")
            if TRACE_PLAYWRIGHT:
                try:
                    tpath = f"/tmp/trace_{name.replace(' ', '_').lower()}_{int(time.time())}.zip"
                    context.tracing.stop(path=tpath)
                    send_document(tpath, f"{name}: trace (blank/bloqueo)")
                except Exception:
                    pass
            _finish_media_send(context, name, proof_mode)
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
                if proof_mode:
                    shoot_best_view(page, name, "after_continue")
                    dump_html(page, name, "after_continue")
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
                    if proof_mode:
                        shoot_best_view(page, name, "after_panel")
                        dump_html(page, name, "after_panel")
                else:
                    log_step(f"[{name}] CDMX: no se encontró panel (no click extra).")
            except Exception as e:
                log_step(f"[{name}] CDMX: error en click panel: {e}")

        status = wait_calendar_ready(page, timeout_ms=45000)
        fecha = None

        if status == "hours":
            hours = find_time_nodes_anywhere(page)
            shot = shoot_best_view(page, name, "citas")
            _finish_media_send(context, name, proof_mode)
            browser.close()
            return ("hours", hours, fecha, shot)

        if status == "no_citas":
            # pruebas opcionales en producción
            if PROOF_ON_NO_SLOTS or proof_mode:
                dump_html(page, name, "nocitas")
                shoot_best_view(page, name, "nocitas")
            _finish_media_send(context, name, proof_mode)
            browser.close()
            return ("no_citas", [], fecha, None)

        # timeout
        hours = find_time_nodes_anywhere(page)
        if hours:
            shot = shoot_best_view(page, name, "citas")
            _finish_media_send(context, name, proof_mode)
            browser.close()
            return ("hours", hours, fecha, shot)

        if proof_mode:
            dump_html(page, name, "timeout")
            shoot_best_view(page, name, "timeout")
        _finish_media_send(context, name, proof_mode)
        browser.close()
        return ("timeout", [], fecha, None)

def _finish_media_send(context, name: str, proof_mode: bool) -> None:
    """Adjunta vídeo y/o trace al terminar en modo prueba."""
    if proof_mode:
        try:
            for v in context.pages[0].video.path():
                pass  # solo para asegurar que existe
        except Exception:
            pass
        try:
            # Envío de video (si existe)
            for page in context.pages:
                try:
                    vpath = page.video.path()
                    if vpath and os.path.exists(vpath):
                        send_document(vpath, f"{name}: video prueba")
                except Exception:
                    continue
        except Exception:
            pass

    if TRACE_PLAYWRIGHT:
        try:
            context.tracing.stop()
        except Exception:
            pass

# =========================
# Orquestador con proxy si blank
# =========================
def revisar_un_consulado(name: str, url: str, mode: str, proof_mode: bool=False) -> Tuple[bool, List[str], Optional[str], Optional[str], Optional[str]]:
    status, hours, fecha, shot = revisar_once(name, url, mode, proxy_conf=None, proof_mode=proof_mode)
    if status == "blank" and PROXY_LIST and RETRIES_ON_BLOCK > 0 and not proof_mode:
        notify(f"⚠️ {name}: página vacía (posible bloqueo). Reintentando con proxy…")
        attempts = min(RETRIES_ON_BLOCK, len(PROXY_LIST))
        for _ in range(attempts):
            proxy = choose_proxy(PROXY_LIST)
            status, hours, fecha, shot = revisar_once(name, url, mode, proxy_conf=proxy, proof_mode=False)
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
# Bucle / Probe
# =========================
def main_loop():
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
                ok, hours, fecha, shot, block = revisar_un_consulado(name, url, mode, proof_mode=False)

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
                    if shot:
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

def probe_one(target_name: str):
    print(f"[PROBE] Arrancando modo prueba para: {target_name}", flush=True)
    consulados = parse_consul_list(CONSUL_URLS)
    info = [c for c in consulados if c[0].lower() == target_name.lower()]
    if not info:
        print(f"[PROBE] No encontré '{target_name}'. Opciones: " + ", ".join(n for n,_,_ in consulados), flush=True)
        sys.exit(1)
    name, url, mode = info[0]
    # En modo prueba NO usamos proxies; queremos ver qué pasa directo.
    ok, hours, fecha, shot, block = revisar_un_consulado(name, url, mode, proof_mode=True)
    if block == "blank":
        notify(f"⚠️ [PRUEBA] {name}: página vacía (bloqueo probable). Se adjuntó evidencia.")
    elif ok and hours:
        msg = f"✅ [PRUEBA] ¡HAY HUECOS en {name}! Horas: {', '.join(hours[:6])}\n{url}"
        notify(msg)
        if shot:
            send_photo(shot, msg)
    else:
        notify(f"ℹ️ [PRUEBA] {name}: no se detectaron huecos. Se adjuntó evidencia para verificar.")

if __name__ == "__main__":
    # Uso:
    #  - Producción (Railway): python monitor_citas_multiconsulados.py
    #  - Prueba 1 consulado (con video/html/capturas): python monitor_citas_multiconsulados.py --probe "Ciudad de Mexico"
    if len(sys.argv) >= 3 and sys.argv[1] == "--probe":
        probe_one(sys.argv[2])
    else:
        main_loop()
