# monitor_citas_multiconsulados.py
import os, sys, re, time, random, json
from dataclasses import dataclass
from typing import List, Tuple, Optional
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout

# ============== CONFIG ==============

@dataclass
class Consulado:
    name: str
    landing_url: str  # página pública del consulado o directamente el widget
    use_panel: bool   # si debe hacer click al panel grande (CDMX sí)

CONS_MTY  = Consulado(
    name="Monterrey",
    # Puedes poner aquí la pública o el widget directo. La pública hace menos ruido:
    landing_url="https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
    use_panel=False
)

CONS_CDMX = Consulado(
    name="Ciudad de México",
    landing_url="https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
    use_panel=True
)

CONSULADOS = [CONS_MTY, CONS_CDMX]

# Env
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

BLOCK_IMAGES = os.getenv("BLOCK_IMAGES", "1") == "1"
GOTO_RETRIES = int(os.getenv("GOTO_RETRIES", "2"))
WIDGET_TIMEOUT_MS = int(os.getenv("WIDGET_TIMEOUT_MS", "25000"))

CHECK_INTERVAL_CENTER = int(os.getenv("CHECK_INTERVAL_SEC", "360"))  # 6 min
SHOW_PUBLIC_IP = os.getenv("SHOW_PUBLIC_IP", "1") == "1"

# Proxy (opcional)
PROXY_HOST = os.getenv("PROXY_HOST", "")
PROXY_PORT = os.getenv("PROXY_PORT", "")
PROXY_USER = os.getenv("PROXY_USER", "")
PROXY_PASS = os.getenv("PROXY_PASS", "")
PROXY_SESSION_IN_USER = os.getenv("PROXY_SESSION_IN_USER", "0") == "1"

# UA pool
USER_AGENTS = [
    # Win/Chrome/Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    # macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4; rv:127.0) Gecko/20100101 Firefox/127.0",
    # iPhone
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    # Android
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
]

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")

# ============== Notificaciones ==============

def notify(msg: str):
    print(msg, flush=True)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
                timeout=20
            )
        except Exception as e:
            print(f"[WARN] Telegram sendMessage fallo: {e}", flush=True)

def send_photo(path: str, caption: str = ""):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"photo": f},
                timeout=30
            )
    except Exception as e:
        print(f"[WARN] Telegram sendPhoto fallo: {e}", flush=True)

def send_document(path: str, caption: str = ""):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"document": f},
                timeout=30
            )
    except Exception as e:
        print(f"[WARN] Telegram sendDocument fallo: {e}", flush=True)

# ============== Helpers ==============

def tiny_pause(a=120, b=220):
    time.sleep(random.uniform(a/1000, b/1000))

def human_pause():
    time.sleep(random.uniform(0.6, 1.4))

def is_blank_html(html: str) -> bool:
    return html is None or len(html.strip()) < 200

def wait_any_text(frame, phrases: List[str], timeout_ms: int = 15000) -> Optional[str]:
    deadline = time.time() + timeout_ms/1000
    while time.time() < deadline:
        body = (frame.content() or "")
        for p in phrases:
            if p.lower() in body.lower():
                return p
        tiny_pause(80, 140)
    return None

def resolve_proxy() -> Optional[dict]:
    if not PROXY_HOST or not PROXY_PORT:
        return None
    server = f"http://{PROXY_HOST}:{PROXY_PORT}"
    username = PROXY_USER or None
    password = PROXY_PASS or None
    return {"server": server, "username": username, "password": password}

def log_public_ip_through_proxy(proxy_cfg: Optional[dict]):
    if not SHOW_PUBLIC_IP:
        return
    try:
        proxies = None
        if proxy_cfg and proxy_cfg.get("server", "").startswith("http"):
            url = proxy_cfg["server"].replace("http://", "")
            auth = ""
            if proxy_cfg.get("username"):
                auth = f"{proxy_cfg['username']}:{proxy_cfg.get('password','')}@"
            proxies = {"http": f"http://{auth}{url}", "https": f"http://{auth}{url}"}
        ip = requests.get("https://api.ipify.org", timeout=12, proxies=proxies).text
        print(f"[INFO] IP pública: {ip}", flush=True)
        notify(f"[INFO] IP pública: {ip}")
    except Exception as e:
        print(f"[WARN] ipify fallo: {e}", flush=True)

def block_images_route(route):
    if BLOCK_IMAGES:
        req = route.request
        if req.resource_type in ("image", "media", "font"):
            return route.abort()
    return route.continue_()

def new_context(p):
    proxy_cfg = resolve_proxy()
    browser = p.chromium.launch(headless=True, proxy=proxy_cfg or None, args=["--disable-dev-shm-usage"])
    ua = random.choice(USER_AGENTS)
    vw = random.randint(1200, 1440)
    vh = random.randint(800, 960)
    context = browser.new_context(
        user_agent=ua,
        viewport={"width": vw, "height": vh},
        locale="es-ES",
        extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"}
    )
    context.route("**/*", block_images_route)
    return browser, context

# ============== Parsers / Revisores ==============

def _goto_with_retries(page, url: str, desc: str):
    last = None
    for i in range(GOTO_RETRIES):
        try:
            page.goto(url, wait_until="domcontentloaded")
            return
        except Exception as e:
            last = e
            tiny_pause(250, 450)
    raise last or RuntimeError(f"goto failed: {desc}")

def _resolve_widget_frame(page):
    # intenta localizar el iframe del widget
    try:
        # primero, por frame_locator (más rápido si hay uno)
        try:
            fr = page.frame_locator("iframe").first.frame
            if fr:
                return fr
        except Exception:
            pass

        # fallback: buscar por URL conocida del widget
        for fr in page.frames:
            if "citaconsular.es/es/hosteds/widgetdefault" in (fr.url or ""):
                return fr
    except Exception:
        pass
    return None

def _close_welcome_dialog(page):
    def _on_dialog(dlg):
        try:
            dlg.accept()
        except Exception:
            pass
    page.on("dialog", _on_dialog)

# -------- Monterrey --------
def revisar_monterrey(context) -> Tuple[bool, List[Tuple[str,str]], Optional[str]]:
    name = "Monterrey"
    page = context.new_page()
    page.set_default_timeout(20000)

    try:
        _goto_with_retries(page, CONS_MTY.landing_url, name)
        tiny_pause()
        _close_welcome_dialog(page)
        # Click Continuar si aparece
        try:
            page.get_by_role("button", name=re.compile("Continu", re.I)).click(timeout=7000)
            tiny_pause()
        except Exception:
            pass

        frame = _resolve_widget_frame(page)

        # evidencia (widget listo)
        try:
            page.screenshot(path="mty_widget_ready.png", full_page=True)
            send_photo("mty_widget_ready.png", f"{name}: HTML inicial (widget listo)")
            with open("mty_widget_ready.html","w",encoding="utf-8") as f:
                f.write(page.content() or "")
        except Exception:
            pass

        if not frame:
            # si no detectamos iframe, concluye sin huecos
            return (False, [], None)

        html0 = frame.content() or ""
        if "No hay horas disponibles" in html0:
            # captura final
            try:
                page.screenshot(path="mty_final.png", full_page=True)
                send_photo("mty_final.png", f"{name}: captura final — NO")
            except Exception:
                pass
            return (False, [], None)

        # Parseo de huecos
        slots: List[Tuple[str,str]] = []

        # a) botones con “Hueco libre”
        try:
            btns = frame.locator("button:has-text('Hueco libre')")
            cnt = min(btns.count(), 100)
            for i in range(cnt):
                t = (btns.nth(i).inner_text() or "").strip()
                m = TIME_RE.search(t)
                if m:
                    slots.append((m.group(0), t))
        except Exception:
            pass

        # b) si no hay "Hueco libre" pero sí calendario (“Cambiar de día”), rascar horas
        try:
            if not slots and "Cambiar de día" in (frame.content() or ""):
                allbtn = frame.locator("button")
                cnt = min(allbtn.count(), 180)
                for i in range(cnt):
                    t = (allbtn.nth(i).inner_text() or "").strip()
                    m = TIME_RE.search(t)
                    if m and len(t) < 40:
                        slots.append((m.group(0), t))
        except Exception:
            pass

        try:
            page.screenshot(path="mty_final.png", full_page=True)
            send_photo("mty_final.png", f"{name}: captura final — {'SI' if slots else 'NO'}")
        except Exception:
            pass

        # Checar “No hay horas…” (no estricto)
        if not slots:
            try:
                frame.get_by_text("No hay horas disponibles").first.wait_for(timeout=2500)
                return (False, [], None)
            except Exception:
                pass

        return (len(slots) > 0, slots, None)

    except Exception as e:
        # Evidencia de fallo
        try:
            page.screenshot(path="mty_after_panel_fail.png", full_page=True)
            send_photo("mty_after_panel_fail.png", f"{name}: pantalla tras intentar abrir panel")
            with open("monterrey_after_panel_fail.html","w",encoding="utf-8") as f:
                f.write(page.content() or "")
            send_document("monterrey_after_panel_fail.html", f"{name}: HTML tras intentar abrir panel")
        except Exception:
            pass
        notify(f"⚠️ {name}: error durante la revisión. {e}")
        return (False, [], None)
    finally:
        page.close()

# -------- CDMX --------
def revisar_cdmx(context) -> Tuple[bool, List[Tuple[str,str]], Optional[str]]:
    name = "Ciudad de México"
    page = context.new_page()
    page.set_default_timeout(25000)

    try:
        _goto_with_retries(page, CONS_CDMX.landing_url, name)
        tiny_pause(180, 280)
        _close_welcome_dialog(page)

        # Click "Continuar"
        try:
            page.get_by_role("button", name=re.compile("Continu", re.I)).click(timeout=8000)
            tiny_pause(250, 420)
        except Exception:
            pass

        # Evidencia inicial antes de parsear
        try:
            page.screenshot(path="cdmx_before_check.png", full_page=True)
            send_photo("cdmx_before_check.png", f"{name}: evidencia inicial (antes de parsear)")
            with open("cdmx_before_check.html","w",encoding="utf-8") as f:
                f.write(page.content() or "")
            send_document("cdmx_before_check.html", f"{name}: HTML inicial")
        except Exception:
            pass

        frame = _resolve_widget_frame(page)
        if not frame:
            return (False, [], None)

        # Click al panel grande “PRESENTACION DOCUMENTACION LEY MEMORIA DEMOCRATICA”
        opened = False
        panel_candidates = [
            frame.get_by_text("PRESENTACION DOCUMENTACION LEY MEMORIA DEMOCRATICA", exact=False),
            frame.locator("div,section,article").filter(
                has_text=re.compile("PRESENTACION DOCUMENTACION", re.I)
            ).first
        ]
        for loc in panel_candidates:
            try:
                loc.wait_for(state="visible", timeout=6000)
                loc.click(force=True, timeout=6000)
                opened = True
                break
            except Exception:
                continue
        tiny_pause(260, 420)

        # Esperar señales del calendario
        ready = wait_any_text(frame, ["Cambiar de día", "Hueco libre", "No hay horas disponibles"], 14000)
        if not ready:
            try:
                frame.evaluate("() => window.scrollBy(0, 400)")
                tiny_pause(180, 260)
                ready = wait_any_text(frame, ["Cambiar de día", "Hueco libre", "No hay horas disponibles"], 7000)
            except Exception:
                pass

        # Evidencia tras abrir panel
        try:
            page.screenshot(path="cdmx_after_panel.png", full_page=True)
            send_photo("cdmx_after_panel.png", f"{name}: pantalla tras abrir panel")
            with open("cdmx_final.html","w",encoding="utf-8") as f:
                f.write(frame.content() or "")
            send_document("cdmx_final.html", f"{name}: HTML final — {'SI' if ready and 'Hueco' in ready else 'NO'}")
        except Exception:
            pass

        # Parseo de huecos
        slots: List[Tuple[str,str]] = []
        try:
            btns = frame.locator("button:has-text('Hueco libre')")
            cnt = min(btns.count(), 140)
            for i in range(cnt):
                t = (btns.nth(i).inner_text() or "").strip()
                m = TIME_RE.search(t)
                if m:
                    slots.append((m.group(0), t))
        except Exception:
            pass

        if not slots and "Cambiar de día" in (frame.content() or ""):
            try:
                allbtn = frame.locator("button")
                cnt = min(allbtn.count(), 180)
                for i in range(cnt):
                    t = (allbtn.nth(i).inner_text() or "").strip()
                    m = TIME_RE.search(t)
                    if m and len(t) < 40:
                        slots.append((m.group(0), t))
            except Exception:
                pass

        # Captura final de calendario
        try:
            page.screenshot(path="cdmx_final.png", full_page=True)
            send_photo("cdmx_final.png", f"{name}: captura final — {'SI' if slots else 'NO'}")
        except Exception:
            pass

        if not slots:
            try:
                frame.get_by_text("No hay horas disponibles").first.wait_for(timeout=2500)
                return (False, [], None)
            except Exception:
                pass

        return (len(slots) > 0, slots, None)

    except Exception as e:
        # Evidencia de fallo
        try:
            page.screenshot(path="cdmx_after_panel_fail.png", full_page=True)
            send_photo("cdmx_after_panel_fail.png", f"{name}: pantalla tras intentar abrir panel")
            with open("cdmx_after_panel_fail.html","w",encoding="utf-8") as f:
                f.write(page.content() or "")
            send_document("cdmx_after_panel_fail.html", f"{name}: HTML tras intentar abrir panel")
        except Exception:
            pass
        notify(f"⚠️ {name}: error durante la revisión. {e}")
        return (False, [], None)
    finally:
        page.close()

# ============== Motor principal ==============

def revisar_consulado(context, cons: Consulado) -> Tuple[bool, List[Tuple[str,str]], Optional[str]]:
    if cons.name == "Monterrey":
        return revisar_monterrey(context)
    elif cons.name == "Ciudad de México":
        return revisar_cdmx(context)
    else:
        return (False, [], None)

def main():
    print("[start] Launching bot…", flush=True)
    with sync_playwright() as p:
        proxy_cfg = resolve_proxy()
        if proxy_cfg:
            print(f"[INFO] Proxy: {proxy_cfg['server']}", flush=True)
        browser, context = new_context(p)
        if SHOW_PUBLIC_IP:
            log_public_ip_through_proxy(proxy_cfg)

        try:
            while True:
                print(f"[INFO] Consulados: {', '.join(c.name for c in CONSULADOS)}", flush=True)
                for cons in CONSULADOS:
                    print(f"[{cons.name}] goto…", flush=True)
                    ok, slots, fecha = revisar_consulado(context, cons)
                    marca = time.strftime("%Y-%m-%d %H:%M:%S")
                    if ok and slots:
                        primeras = ", ".join(sorted({h for h, _ in slots})[:5])
                        f = f" ({fecha})" if fecha else ""
                        notify(f"[{marca}] {cons.name} → ¡HAY HUECOS!{f} → Horas: {primeras}")
                        # breve espera para evitar spam
                        time.sleep(5)
                    else:
                        notify(f"[{marca}] {cons.name} → sin huecos por ahora.")

                    # Pausa humana entre consulados
                    tiny_pause(400, 900)

                # Espera humanizada entre rondas (5–7 min aleatorio aprox)
                jitter = random.randint(-45, 45)
                wait_s = max(180, CHECK_INTERVAL_CENTER + jitter)
                notify(f"[INFO] Esperando {wait_s}s antes de la siguiente ronda…")
                time.sleep(wait_s)

        finally:
            context.close()
            browser.close()

if __name__ == "__main__":
    main()
