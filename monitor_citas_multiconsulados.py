import os, sys, time, random, traceback, textwrap
from datetime import datetime
from pathlib import Path
import requests

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ========= util/env =========
def env_flag(name, default="0"):
    return os.getenv(name, default).strip().lower() in ("1","true","yes","on")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID","").strip()

PROXY_HOST  = os.getenv("PROXY_HOST","").strip()
PROXY_PORT  = os.getenv("PROXY_PORT","").strip()
PROXY_USER  = os.getenv("PROXY_USER","").strip()
PROXY_PASS  = os.getenv("PROXY_PASS","").strip()
PROXY_SESSION_IN_USER = env_flag("PROXY_SESSION_IN_USER","0")

BLOCK_IMAGES       = env_flag("BLOCK_IMAGES","0")
DEBUG_STEPS        = env_flag("DEBUG_STEPS","1")
GOTO_RETRIES       = int(os.getenv("GOTO_RETRIES","2"))
LANDING_TIMEOUT_MS = int(os.getenv("LANDING_TIMEOUT_MS","45000"))
WIDGET_TIMEOUT_MS  = int(os.getenv("WIDGET_TIMEOUT_MS","50000"))
PAGE_OP_TIMEOUT_MS = int(os.getenv("PAGE_OP_TIMEOUT_MS","8000"))

ROTATE_AFTER_BLANK = env_flag("ROTATE_AFTER_BLANK","1")
ROTATE_COOLDOWN    = int(os.getenv("ROTATE_COOLDOWN_SEC","90"))

SHOW_PUBLIC_IP     = env_flag("SHOW_PUBLIC_IP","1")
IP_ENDPOINT        = os.getenv("IP_ENDPOINT","https://api.ipify.org")

# nuevos endurecedores
ROUNDS_PER_RESTART      = int(os.getenv("ROUNDS_PER_RESTART","24"))  # reinicio preventivo
LAUNCH_RETRY_MAX        = int(os.getenv("LAUNCH_RETRY_MAX","4"))
LAUNCH_RETRY_BASE_DELAY = int(os.getenv("LAUNCH_RETRY_BASE_DELAY","5"))

# pausa humanizada entre rondas
ROUND_MIN_WAIT = 300   # 5 min
ROUND_MAX_WAIT = 420   # 7 min

OUTDIR = Path("/tmp/evidencias"); OUTDIR.mkdir(parents=True, exist_ok=True)
def now(): return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

# ========= telegram =========
def tg_text(msg: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID): return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except Exception: pass

def tg_file(path: Path, caption=""):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID): return
    if not path.exists(): return
    try:
        with open(path,"rb") as f:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                          data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                          files={"document":(path.name,f)})
    except Exception: pass

def save_html(page, path: Path): path.write_text(page.content(), encoding="utf-8")
def shot_jpg(page, path: Path, full=False): page.screenshot(path=str(path), type="jpeg", quality=70, full_page=full)

def human(a,b): time.sleep(random.uniform(a,b))

# ========= proxy =========
def build_proxy():
    if not PROXY_HOST or not PROXY_PORT: return None
    if PROXY_USER and PROXY_PASS:
        user = PROXY_USER
        if PROXY_SESSION_IN_USER:
            user = f"{PROXY_USER}_cr.us,mx"
        return {"server": f"http://{PROXY_HOST}:{PROXY_PORT}", "username": user, "password": PROXY_PASS}
    return {"server": f"http://{PROXY_HOST}:{PROXY_PORT}"}

# ========= URLs / selectores =========
MIN_MTY  = "https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx"
MIN_CDMX = "https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx"

WIDGET_MTY  = "https://www.citaconsular.es/es/hosteds/widgetdefault/2533f04b1d3e818b66f175afc9c24cf63/"
WIDGET_CDMX = "https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/"

MIN_YELLOW_LINK = "text=/ELEGIR FECHA Y HORA/i"
BTN_CONTINUE    = "text=/Continue\\s*\\/\\s*Continuar/i"
CDMX_PANEL      = "text=/PRESENTACION DOCUMENTACION LEY MEMORIA/i"
NO_SLOTS_TEXT   = "text=/No hay horas disponibles/i"
CAL_CHANGE_DAY  = "text=/Cambiar de d[ií]a/i"
SLOT_BUTTONS    = "button:has-text('Hueco'), button:has-text('Huecos'), button:has-text('libre')"
SPINNER_LOADING = "text=/Loading|Cargando/i"

# ========= navegación robusta =========
def goto(page, url, timeout_ms):
    last=None
    for _ in range(GOTO_RETRIES+1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            return True
        except Exception as e:
            last=e; human(0.6,1.3)
    if DEBUG_STEPS: tg_text(f"[warn] GOTO falló: {url}\n{last}")
    return False

def wait_widget_ready(page):
    # intenta detectar bienvenida/continuar o “no hay…”
    try:
        page.wait_for_timeout(400)
        page.wait_for_selector(f"{BTN_CONTINUE}, {NO_SLOTS_TEXT}", timeout=WIDGET_TIMEOUT_MS)
    except PWTimeout:
        # aceptar modal si sale
        try:
            page.get_by_text("Bienvenido").or_(page.get_by_text("Welcome")).get_by_text("Aceptar").click(timeout=2000)
        except Exception: pass
        page.wait_for_selector(f"{BTN_CONTINUE}, {NO_SLOTS_TEXT}", timeout=WIDGET_TIMEOUT_MS)

def wait_calendar_ready(page):
    # si existe “Loading”, esperar a que desaparezca
    try:
        if page.locator(SPINNER_LOADING).count() > 0:
            page.locator(SPINNER_LOADING).first.wait_for(state="detached", timeout=WIDGET_TIMEOUT_MS)
    except Exception: pass
    # luego algo relevante del calendario
    try:
        page.wait_for_selector(f"{CAL_CHANGE_DAY}, {SLOT_BUTTONS}, {NO_SLOTS_TEXT}", timeout=WIDGET_TIMEOUT_MS)
    except PWTimeout:
        page.wait_for_timeout(1200)

def detect_slots(page):
    if page.locator(NO_SLOTS_TEXT).count(): return False
    if page.locator(SLOT_BUTTONS).count() > 0: return True
    # heurística: HH:MM
    try:
        h = page.locator("text=/\\b([01]\\d|2[0-3]):[0-5]\\d\\b/")
        if h.count() > 0: return True
    except Exception: pass
    return False

# ========= flujos =========
def open_from_ministry(page, cons_name, min_url, widget_url):
    ok = goto(page, min_url, LANDING_TIMEOUT_MS)
    if not ok: 
        tg_text(f"{cons_name}: no abrió Ministerio"); return False

    # evidencia ministerio (html)
    html_min = OUTDIR / f"{cons_name.lower()}_ministerio.html"
    save_html(page, html_min); tg_file(html_min, f"{cons_name}: HTML inicial (ministerio)")

    # click amarillo (puede abrir nueva pestaña)
    try:
        with page.context.expect_page(timeout=4000) as maybe_new:
            page.locator(MIN_YELLOW_LINK).first.click(timeout=PAGE_OP_TIMEOUT_MS)
        return maybe_new.value
    except Exception:
        pass

    # si no abrió pestaña, vamos directo al widget
    if not goto(page, widget_url, LANDING_TIMEOUT_MS):
        tg_text(f"{cons_name}: no abrió Widget")
        return False
    return page

def cdmx_flow(page, name="Ciudad de México"):
    wait_widget_ready(page)

    img0 = OUTDIR / "cdmx_before.jpg"; shot_jpg(page, img0, full=True); tg_file(img0, f"{name}: evidencia inicial (antes de parsear)")
    html0 = OUTDIR / "cdmx_before_check.html"; save_html(page, html0); tg_file(html0, f"{name}: HTML inicial")

    try: page.locator(BTN_CONTINUE).first.click(timeout=PAGE_OP_TIMEOUT_MS)
    except Exception: pass
    page.wait_for_timeout(700)
    html1 = OUTDIR / "ciudad_de_méxico_after_continue.html"; save_html(page, html1); tg_file(html1, f"{name}: HTML tras 'Continuar'")

    page.wait_for_timeout(600)
    try: page.locator(CDMX_PANEL).first.click(timeout=PAGE_OP_TIMEOUT_MS)
    except Exception: pass

    # robustez extra
    for _ in range(3):
        try: wait_calendar_ready(page); break
        except Exception:
            page.mouse.wheel(0,800); page.wait_for_timeout(1000)

    htmlf = OUTDIR / "ciudad_de_méxico_final.html"; save_html(page, htmlf)
    capf  = OUTDIR / "ciudad_de_méxico_final.jpg"; shot_jpg(page, capf, full=True)
    tg_file(htmlf, f"{name}: HTML final — {'NO' if page.locator(NO_SLOTS_TEXT).count() else 'OK?'}")
    tg_file(capf,  f"{name}: captura final — {'NO' if page.locator(NO_SLOTS_TEXT).count() else '¿HUECOS?'}")
    return detect_slots(page)

def monterrey_flow(page, name="Monterrey"):
    wait_widget_ready(page)

    img0 = OUTDIR / "monterrey_before.jpg"; shot_jpg(page, img0, full=True); tg_file(img0, f"{name}: evidencia inicial (widget listo)")
    html0 = OUTDIR / "monterrey_before_check.html"; save_html(page, html0); tg_file(html0, f"{name}: HTML inicial (widget listo)")

    try: page.locator(BTN_CONTINUE).first.click(timeout=PAGE_OP_TIMEOUT_MS)
    except Exception: pass
    page.wait_for_timeout(700)
    html1 = OUTDIR / "monterrey_after_continue.html"; save_html(page, html1); tg_file(html1, f"{name}: HTML tras 'Continuar'")

    # si aparece el mismo panel que CDMX, clic
    try:
        if page.locator(CDMX_PANEL).count(): page.locator(CDMX_PANEL).first.click(timeout=PAGE_OP_TIMEOUT_MS)
    except Exception: pass

    # Esperas sólidas para evitar la “captura final” con spinner
    for _ in range(3):
        try:
            wait_calendar_ready(page)
            break
        except Exception:
            page.mouse.wheel(0, 800); page.wait_for_timeout(1200)

    htmlf = OUTDIR / "monterrey_final.html"; save_html(page, htmlf)
    capf  = OUTDIR / "monterrey_final.jpg"; shot_jpg(page, capf, full=True)
    tg_file(htmlf, f"{name}: HTML final — {'NO' if page.locator(NO_SLOTS_TEXT).count() else 'OK?'}")
    tg_file(capf,  f"{name}: captura final — {'NO' if page.locator(NO_SLOTS_TEXT).count() else '¿HUECOS?'}")
    return detect_slots(page)

CONSULADOS = [
    {"name":"Monterrey",       "min":MIN_MTY,  "widget":WIDGET_MTY,  "run":monterrey_flow},
    {"name":"Ciudad de México","min":MIN_CDMX, "widget":WIDGET_CDMX, "run":cdmx_flow},
]

# ========= lanzamiento robusto =========
def launch_browser(pw):
    proxy = build_proxy()
    # Nota: en Railway el sandbox está deshabilitado por defecto para Playwright build de runtime
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        proxy=proxy if proxy else None,
        viewport={"width":1280,"height":900},
        java_script_enabled=True,
        bypass_csp=True,
    )
    if BLOCK_IMAGES:
        context.route("**/*", lambda route: route.abort() if route.request.resource_type == "image" else route.continue_())
    return browser, context

def print_cfg():
    tg_text(f"[INFO] Config: debug={DEBUG_STEPS} block_images={'ON' if BLOCK_IMAGES else 'OFF'}")
    tg_text(f"[INFO] Consulados: Monterrey, Ciudad de México")
    prox = f"http://{PROXY_HOST}:{PROXY_PORT}" if PROXY_HOST else "sin proxy"
    tg_text(f"[INFO] Proxy: {prox}")
    if SHOW_PUBLIC_IP:
        try:
            ip = requests.get(IP_ENDPOINT, timeout=8).text.strip()
            tg_text(f"[INFO] IP pública: {ip}")
        except Exception: pass

def is_target_closed_error(e: Exception) -> bool:
    s = repr(e)
    return ("Target page, context or browser has been closed" in s) or ("Target closed" in s)

def run_round(context):
    for cons in CONSULADOS:
        name, min_url, widget_url, runner = cons["name"], cons["min"], cons["widget"], cons["run"]
        page = context.new_page()
        try:
            target = open_from_ministry(page, name, min_url, widget_url)
            if target is False:
                tg_text(f"[{name}] {now()} → error al abrir ministerio/widget.")
                try: page.close()
                except Exception: pass
                continue

            if target != page:
                # se abrió nueva pestaña
                try: page.close()
                except Exception: pass
                page = target

            has = runner(page, name)
            tg_text(f"[{name}] {now()} {'→ HUECOS detectados' if has else '→ sin huecos por ahora.'}")

        except Exception as e:
            # Evidencia del fallo y mensaje
            try:
                err_html = OUTDIR / f"{name.lower()}_after_panel_fail.html"
                save_html(page, err_html); tg_file(err_html, f"{name}: HTML tras intentar abrir panel")
                err_img  = OUTDIR / f"{name.lower()}_after_panel.jpg"
                shot_jpg(page, err_img, full=True); tg_file(err_img, f"{name}: pantalla tras intentar abrir panel")
            except Exception: pass
            tg_text(f"⚠️ {name}: error durante la revisión.\n{repr(e)}\n{traceback.format_exc()[:1200]}")
            if is_target_closed_error(e):
                raise   # que lo maneje el nivel superior para relanzar navegador
        finally:
            try: page.close()
            except Exception: pass

def main():
    print_cfg()
    rounds = 0
    with sync_playwright() as pw:
        browser = None
        context = None

        def open_session():
            nonlocal browser, context
            # backoff en launch
            for i in range(LAUNCH_RETRY_MAX):
                try:
                    browser, context = launch_browser(pw)
                    return True
                except Exception as e:
                    delay = LAUNCH_RETRY_BASE_DELAY * (2 ** i)
                    tg_text(f"[ERROR] launch falló (intento {i+1}/{LAUNCH_RETRY_MAX}). Esperando {delay}s.\n{repr(e)}")
                    time.sleep(delay)
            return False

        if not open_session():
            tg_text("[FATAL] No se pudo lanzar el navegador.")
            return

        while True:
            try:
                run_round(context)
                rounds += 1
            except Exception as e:
                if is_target_closed_error(e):
                    # relanzar navegador
                    try:
                        if context: context.close()
                    except Exception: pass
                    try:
                        if browser: browser.close()
                    except Exception: pass
                    tg_text("[warn] Sesión cerrada/crashed. Relanzando navegador…")
                    if not open_session():
                        tg_text("[FATAL] No se pudo relanzar el navegador.")
                        break
                else:
                    # error no fatal, continuamos
                    tg_text(f"[warn] Error no fatal, seguimos. {repr(e)}")

            # reinicio preventivo por salud
            if rounds > 0 and (rounds % ROUNDS_PER_RESTART == 0):
                tg_text("[INFO] Reinicio preventivo del navegador para liberar memoria.")
                try:
                    context.close(); browser.close()
                except Exception: pass
                if not open_session():
                    tg_text("[FATAL] No se pudo relanzar tras reinicio preventivo.")
                    break

            # espera humanizada
            wait_s = random.randint(ROUND_MIN_WAIT, ROUND_MAX_WAIT)
            tg_text(f"[INFO] Esperando {wait_s}s antes de la siguiente ronda…")
            time.sleep(wait_s)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
