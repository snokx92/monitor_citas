import os, re, time, random, json, traceback
from datetime import datetime
import requests

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ============ Config ============
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

ENTRY_MODE = os.getenv("ENTRY_MODE", "mixed").lower()  # mixed|ministry|direct
WIDGET_TIMEOUT_MS = int(os.getenv("WIDGET_TIMEOUT_MS", "120000"))  # 120s por defecto

HEADLESS = os.getenv("HEADLESS", "1") == "1"

ROUND_SLEEP_MIN_S = int(os.getenv("ROUND_SLEEP_MIN_S", "300"))
ROUND_SLEEP_MAX_S = int(os.getenv("ROUND_SLEEP_MAX_S", "420"))

SHOW_PUBLIC_IP = os.getenv("SHOW_PUBLIC_IP", "0") == "1"

# Proxy (opcional)
PROXY_HOST = os.getenv("PROXY_HOST")
PROXY_PORT = os.getenv("PROXY_PORT")
PROXY_USER = os.getenv("PROXY_USER")
PROXY_PASS = os.getenv("PROXY_PASS")

# Opcional: forzar URL directa al widget
DIRECT_URL_MTY  = os.getenv("DIRECT_URL_MTY", "").strip()
DIRECT_URL_CDMX = os.getenv("DIRECT_URL_CDMX", "").strip()

# Selectores/expresiones clave
BTN_CONTINUE_RX = re.compile(r"Continue\s*\/\s*Continuar", re.I)
NO_SLOTS_RX     = re.compile(r"No hay horas disponibles", re.I)
TIME_RX         = re.compile(r"\b\d{1,2}:\d{2}\b")  # hh:mm visible en celdas/slots
LOADING_RX      = re.compile(r"loading\.{0,3}", re.I)

# URLs de Ministerio (entrada “humana”)
MIN_URLS = {
    "Monterrey": "https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
    "Ciudad de México": "https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
}

# ============ Utilidades Telegram ============
def t_send_text(text: str):
    if not (BOT_TOKEN and CHAT_ID): 
        print(f"[TG] {text}")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=20)
    except Exception as e:
        print("[TG] sendMessage error:", e)

def t_send_file(path: str, caption: str = ""):
    if not os.path.exists(path): 
        print(f"[TG] archivo no existe: {path}")
        return
    if not (BOT_TOKEN and CHAT_ID):
        print(f"[TG-FILE] {path} :: {caption}")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    try:
        with open(path, "rb") as f:
            requests.post(url, data={"chat_id": CHAT_ID, "caption": caption}, files={"document": f}, timeout=60)
    except Exception as e:
        print("[TG] sendDocument error:", e)

def t_send_photo(path: str, caption: str = ""):
    if not os.path.exists(path):
        print(f"[TG] foto no existe: {path}")
        return
    if not (BOT_TOKEN and CHAT_ID):
        print(f"[TG-PHOTO] {path} :: {caption}")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    try:
        with open(path, "rb") as f:
            requests.post(url, data={"chat_id": CHAT_ID, "caption": caption}, files={"photo": f}, timeout=60)
    except Exception as e:
        print("[TG] sendPhoto error:", e)

# ============ Helpers ============
def human_sleep(a, b):
    time.sleep(random.uniform(a, b))

def now_ts():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def rand_user_agent():
    bases = [
        # Varias firmas Chromium
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{} Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{} Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{} Safari/537.36",
    ]
    ver = f"{random.randint(118,132)}.0.{random.randint(1000,6000)}.{random.randint(50,200)}"
    return random.choice(bases).format(ver)

def ip_publica():
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=10)
        return r.json().get("ip")
    except:
        return "desconocida"

# ============ Playwright helpers ============
STEALTH_JS = """
// Eliminar huella básica
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['es-ES', 'es', 'en'] });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
Object.defineProperty(navigator, 'language', { get: () => 'es-ES' });

// Plugins falsos
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });

// Hardware concurrency visible
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

// WebGL vendor/renderer
try {
  const getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(param){
    if (param === 37445) return 'Intel Inc.';       // UNMASKED_VENDOR_WEBGL
    if (param === 37446) return 'Intel Iris OpenGL';// UNMASKED_RENDERER_WEBGL
    return getParameter.apply(this, [param]);
  }
} catch(e) {}
"""

def launch_browser(p):
    launch_opts = {
        "headless": HEADLESS,
        "args": [
            "--lang=es-ES,es",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    }
    if PROXY_HOST and PROXY_PORT:
        if PROXY_USER and PROXY_PASS:
            proxy = f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"
        else:
            proxy = f"http://{PROXY_HOST}:{PROXY_PORT}"
        launch_opts["proxy"] = {"server": proxy}
        print(f"[INFO] Proxy: {proxy}")

    browser = p.chromium.launch(**launch_opts)
    context = browser.new_context(
        user_agent=rand_user_agent(),
        locale="es-ES",
        timezone_id="America/Mexico_City",
        viewport={"width": 1366, "height": 900},
        java_script_enabled=True,
    )
    page = context.new_page()
    page.add_init_script(STEALTH_JS)
    return browser, context, page

# ============ Flujo de entrada ============
def goto_ministry(page, cons_name):
    page.goto(MIN_URLS[cons_name], wait_until="domcontentloaded", timeout=60000)
    t_send_file(_save_html(page, f"{cons_name}_ministerio"), f"{cons_name}: HTML inicial (ministerio)")
    _snap(page, f"{cons_name}_ministerio.jpg", f"{cons_name}: evidencia ministerio")

    # Buscar el enlace “ELEGIR FECHA Y HORA”
    # (anchor con mayúsculas o contiene citaconsular.es)
    link = page.locator("a:has-text('ELEGIR FECHA Y HORA')")
    if link.count() == 0:
        # fallback: primer enlace que apunte a citaconsular/bookitit
        link = page.locator("a[href*='citaconsular'], a[href*='bookitit']")
    link.first.click(timeout=20000, force=True)
    human_sleep(1.0, 2.0)
    page.wait_for_load_state("domcontentloaded", timeout=45000)

def goto_direct(page, cons_name):
    forced = DIRECT_URL_MTY if cons_name == "Monterrey" else DIRECT_URL_CDMX
    if forced:
        page.goto(forced, wait_until="domcontentloaded", timeout=60000)
        return
    # Fallback: si estamos en ministerio, tomar enlace que apunte a citaconsular
    goto_ministry(page, cons_name)  # reaprovechamos
    # (goto_ministry ya hizo click; aquí no hacemos nada extra)

# ============ Evidencias ============

def _save_html(page, base_name):
    path = f"{_slug(base_name)}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(page.content())
    return path

def _snap(page, jpg_name, caption=None, full=True, quality=70):
    # nombre seguro + guardado
    if not jpg_name.lower().endswith(".jpg") and not jpg_name.lower().endswith(".jpeg"):
        jpg_name += ".jpg"
    page.screenshot(path=jpg_name, full_page=full, type="jpeg", quality=quality)
    if caption:
        t_send_photo(jpg_name, caption)

def _slug(s):
    return re.sub(r"[^a-z0-9_]+", "_", s.lower())

# ============ Widget & parsing ============

def list_frames(page):
    return [f.url for f in page.frames]

def _find_widget_frame(page):
    # Heurística: frame con “citaconsular” o “bookitit” o donde aparezca el botón/leyendas
    for fr in page.frames:
        url = (fr.url or "").lower()
        if any(k in url for k in ("citaconsular", "bookitit")):
            return fr
    # si no hay pista por URL, el primero que tenga "Continuar" o "No hay horas"
    for fr in page.frames:
        try:
            if fr.get_by_text(BTN_CONTINUE_RX).count() > 0 or fr.get_by_text(NO_SLOTS_RX).count() > 0:
                return fr
        except:
            pass
    return None

def wait_widget_ready(page, cons_name, timeout_ms: int):
    """
    Espera a que en la página o en alguno de los iframes aparezca:
     - Botón Continuar, o
     - Texto 'No hay horas disponibles'
    Devuelve la referencia del frame "activo" a revisar (puede ser None si está en main frame).
    """
    start = time.time()
    last_error = None
    while (time.time() - start) * 1000 < timeout_ms:
        try:
            # ¿aparece en main?
            if page.get_by_text(BTN_CONTINUE_RX).first.is_visible(timeout=1000):
                return page.main_frame
        except Exception as e: last_error = e

        try:
            if page.get_by_text(NO_SLOTS_RX).first.is_visible(timeout=1000):
                return page.main_frame
        except Exception as e: last_error = e

        fr = _find_widget_frame(page)
        if fr:
            try:
                if fr.get_by_text(BTN_CONTINUE_RX).first.is_visible(timeout=800):
                    return fr
            except Exception as e: last_error = e
            try:
                if fr.get_by_text(NO_SLOTS_RX).first.is_visible(timeout=800):
                    return fr
            except Exception as e: last_error = e

        human_sleep(0.6, 1.2)

    # Timeout → evidencias
    _snap(page, f"{cons_name}_timeout_full.jpg", f"{cons_name}: captura en timeout")
    t_send_file(_save_html(page, f"{cons_name}_error_state"), f"{cons_name}: HTML en error")
    t_send_text(f"⚠️ {cons_name}: timeout esperando widget.")
    t_send_text(f"Frames: {json.dumps(list_frames(page), ensure_ascii=False)}")
    if last_error:
        print("last error while waiting:", last_error)
    raise PWTimeout(f"Timeout esperando widget {cons_name}")

def open_panel_and_check(page_or_frame, cons_name):
    """
    Si existe botón 'Continuar', hacer click y esperar a panel.
    Luego, cuando el panel esté, revisar si hay horarios (regex hh:mm) o 'No hay horas...'.
    """
    ctx = page_or_frame

    # 1) Click Continuar (si está)
    try:
        btn = ctx.get_by_text(BTN_CONTINUE_RX).first
        if btn.is_visible(timeout=1500):
            btn.click(timeout=12000)
            human_sleep(0.8, 1.5)
    except:
        pass

    # 2) Esperar a que realmente cargue el panel (evitar foto del spinner)
    #    criterio: aparece “No hay horas…” o aparece un hh:mm, y además no está "Loading"
    end = time.time() + 45  # 45s to settle panel
    found_slots = None
    while time.time() < end:
        try:
            if ctx.get_by_text(NO_SLOTS_RX).first.is_visible(timeout=1000):
                found_slots = False
                break
        except: pass

        # buscar horario explícito
        try:
            html = ctx.content()
            if TIME_RX.search(html):
                found_slots = True
                # pre-cheque: evitar que esté aún mostrando Loading
                if not LOADING_RX.search(html):
                    break
        except: pass

        human_sleep(0.7, 1.2)

    # Evidencias finales
    _snap(ctx.page, f"{cons_name}_final.jpg", f"{cons_name}: captura final")
    t_send_file(_save_html(ctx.page, f"{cons_name}_final"), f"{cons_name}: HTML final")

    return found_slots is True

# ============ Flujos por consulado ============

def revisar_consulado(page, cons_name, entry_mode: str):
    """
    Devuelve True si encontró horarios, False si 'No hay horas...', lanza en bloqueos.
    """
    # Elegir ruta
    if entry_mode == "ministry":
        goto_ministry(page, cons_name)
    elif entry_mode == "direct":
        goto_direct(page, cons_name)
    else:  # mixed: mitad de las rondas por una y mitad por otra
        if random.random() < 0.5:
            goto_ministry(page, cons_name)
        else:
            goto_direct(page, cons_name)

    # evidencia antes de parsear
    t_send_file(_save_html(page, f"{cons_name}_before_check"), f"{cons_name}: HTML inicial (widget)")
    _snap(page, f"{cons_name}_before_check.jpg", f"{cons_name}: evidencia inicial (widget)")

    # Esperar a ver el widget en page o en alguno de los iframes
    ctx = wait_widget_ready(page, cons_name, WIDGET_TIMEOUT_MS)

    # Abrir panel y revisar
    has_slots = open_panel_and_check(ctx, cons_name)
    return has_slots

# ============ Main loop ============

CONSULADOS = [
    ("Monterrey", "Monterrey"),
    ("Ciudad de México", "Ciudad de México"),
]

def main():
    print("[start] Launching bot…")
    if SHOW_PUBLIC_IP:
        t_send_text(f"[INFO] IP pública: {ip_publica()}")

    with sync_playwright() as p:
        browser, context, page = launch_browser(p)
        try:
            while True:
                for key, name in CONSULADOS:
                    try:
                        t_send_text(f"[{now_ts()}] [{name}] goto…")
                        has = revisar_consulado(page, name, ENTRY_MODE)
                        if has:
                            t_send_text(f"[{now_ts()}] {name} → <b>HAY HORAS</b> ✅")
                        else:
                            t_send_text(f"[{now_ts()}] {name} → sin huecos por ahora.")
                    except PWTimeout as te:
                        t_send_text(f"⚠️ {name}: timeout esperando widget")
                        # ya se mandaron evidencias dentro de wait_widget_ready
                    except Exception as e:
                        t_send_text(f"⚠️ {name}: error: {type(e).__name__}")
                        print("Error:", traceback.format_exc())
                    finally:
                        human_sleep(1.0, 2.0)

                # pausa humana entre rondas
                wait_s = random.randint(ROUND_SLEEP_MIN_S, ROUND_SLEEP_MAX_S)
                t_send_text(f"[INFO] Esperando {wait_s}s antes de la siguiente ronda…")
                time.sleep(wait_s)

        finally:
            context.close()
            browser.close()

# ============ Entrypoint ============
if __name__ == "__main__":
    main()
