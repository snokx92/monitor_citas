import os, sys, time, random, traceback
from datetime import datetime
from pathlib import Path
import requests

# Importación opcional para GIF (no rompe si no está disponible)
try:
    from PIL import Image
    PIL_OK = True
except Exception:
    PIL_OK = False

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ========= Configuración =========
def env_flag(name, default="0"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

PROXY_HOST = os.getenv("PROXY_HOST", "").strip()
PROXY_PORT = os.getenv("PROXY_PORT", "").strip()
PROXY_USER = os.getenv("PROXY_USER", "").strip()
PROXY_PASS = os.getenv("PROXY_PASS", "").strip()
PROXY_SESSION_IN_USER = env_flag("PROXY_SESSION_IN_USER", "0")

BLOCK_IMAGES = env_flag("BLOCK_IMAGES", "0")
DEBUG_STEPS = env_flag("DEBUG_STEPS", "1")
GOTO_RETRIES = int(os.getenv("GOTO_RETRIES", "2"))
LANDING_TIMEOUT_MS = int(os.getenv("LANDING_TIMEOUT_MS", "45000"))
WIDGET_TIMEOUT_MS = int(os.getenv("WIDGET_TIMEOUT_MS", "50000"))
PAGE_OP_TIMEOUT_MS = int(os.getenv("PAGE_OP_TIMEOUT_MS", "8000"))

ROUND_MIN_WAIT = 300  # 5 min
ROUND_MAX_WAIT = 420  # 7 min

# GIF opcional
GIF_ENABLE  = env_flag("GIF_ENABLE", "1")
GIF_SECONDS = int(os.getenv("GIF_SECONDS", "6"))
GIF_FPS     = int(os.getenv("GIF_FPS", "2"))  # 2 fps ~ un frame cada 500 ms

OUTDIR = Path("/tmp/evidencias"); OUTDIR.mkdir(parents=True, exist_ok=True)
def now(): return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

# ========= Telegram =========
def tg_text(msg):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
        except: pass

def tg_file(path: Path, caption=""):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and path.exists():
        try:
            with open(path, "rb") as f:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                              data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                              files={"document": (path.name, f)})
        except: pass

def save_html(page, path: Path): path.write_text(page.content(), encoding="utf-8")
def shot_jpg(page, path: Path, full=False): page.screenshot(path=str(path), type="jpeg", quality=70, full_page=full)
def human(a, b): time.sleep(random.uniform(a, b))

# ========= Proxy =========
def build_proxy():
    if not PROXY_HOST or not PROXY_PORT: return None
    if PROXY_USER and PROXY_PASS:
        return {"server": f"http://{PROXY_HOST}:{PROXY_PORT}",
                "username": PROXY_USER, "password": PROXY_PASS}
    return {"server": f"http://{PROXY_HOST}:{PROXY_PORT}"}

# ========= URLs / selectores =========
MIN_MTY  = "https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx"
MIN_CDMX = "https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx"

WIDGET_MTY  = "https://www.citaconsular.es/es/hosteds/widgetdefault/2533f04b1d3e818b66f175afc9c24cf63/"
WIDGET_CDMX = "https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/"

# Enlaces amarillos del ministerio (los mantenemos por si los usas como “capa humana”)
MIN_YELLOW_LINK = "text=/ELEGIR FECHA Y HORA/i"

BTN_CONTINUE    = "text=/Continue\\s*\\/\\s*Continuar/i"
CDMX_PANEL      = "text=/PRESENTACION DOCUMENTACION LEY MEMORIA/i"
NO_SLOTS_TEXT   = "text=/No hay horas disponibles/i"
CAL_CHANGE_DAY  = "text=/Cambiar de d[ií]a/i"
SLOT_BUTTONS    = "button:has-text('Hueco'), button:has-text('libre')"
SPINNER_LOADING = "text=/Loading|Cargando/i"

# ========= Utilidades de espera =========
def wait_for_loading_to_finish(page):
    """Espera a que desaparezca un spinner si aparece."""
    try:
        if page.locator(SPINNER_LOADING).count() > 0:
            page.locator(SPINNER_LOADING).first.wait_for(state="detached", timeout=WIDGET_TIMEOUT_MS)
    except PWTimeout:
        pass

def wait_widget_ready(page):
    """Widget listo para pulsar Continuar o mostrar 'No hay horas...'."""
    for attempt in range(2):
        try:
            wait_for_loading_to_finish(page)
            page.wait_for_selector(f"{BTN_CONTINUE}, {NO_SLOTS_TEXT}", timeout=WIDGET_TIMEOUT_MS)
            return
        except PWTimeout:
            if attempt == 0:
                page.reload(wait_until="domcontentloaded")
                human(2, 3)
            else:
                raise

def wait_calendar_ready(page):
    """Esperar fin de carga del calendario o mensaje 'No hay horas'."""
    wait_for_loading_to_finish(page)
    try:
        page.wait_for_selector(f"{CAL_CHANGE_DAY}, {SLOT_BUTTONS}, {NO_SLOTS_TEXT}", timeout=WIDGET_TIMEOUT_MS)
    except PWTimeout:
        page.wait_for_timeout(1500)

def detect_slots(page):
    if page.locator(NO_SLOTS_TEXT).count(): return False
    if page.locator(SLOT_BUTTONS).count() > 0: return True
    # Algunos widgets muestran la hora como botón grande:
    if page.locator("button >> text=/\\d{1,2}:\\d{2}/").count() > 0: return True
    return False

# ========= GIF de depuración =========
def record_gif_until_ready(page, base_name: str, max_seconds: int, fps: int):
    """
    Graba una serie de capturas mientras se carga el calendario.
    Solo guarda si Pillow está disponible y GIF_ENABLE=1.
    """
    if not (GIF_ENABLE and PIL_OK and max_seconds > 0 and fps > 0):
        return

    frames = []
    interval = 1.0 / float(fps)  # segundos entre frames
    start = time.time()
    end_time = start + max_seconds

    tmp_dir = OUTDIR / f"{base_name}_gif_frames"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    idx = 0
    while time.time() < end_time:
        # Si ya está listo, tomamos un par de frames extra y salimos
        ready = (page.locator(CAL_CHANGE_DAY).count() > 0 or
                 page.locator(SLOT_BUTTONS).count() > 0 or
                 page.locator(NO_SLOTS_TEXT).count() > 0)
        # Captura
        frame_path = tmp_dir / f"f_{idx:03d}.jpg"
        try:
            shot_jpg(page, frame_path, full=True)
            frames.append(Image.open(frame_path))
        except Exception:
            pass

        idx += 1
        if ready and idx >= max(2, fps):  # dejamos 1 seg extra para contexto
            break
        time.sleep(interval)

    # Guardar GIF si hay frames
    if frames:
        gif_path = OUTDIR / f"{base_name}.gif"
        try:
            frames[0].save(
                gif_path,
                save_all=True,
                append_images=frames[1:],
                duration=int(1000 / fps),
                loop=0,
                optimize=False,
                quality=70,
                format="GIF"
            )
            tg_file(gif_path, f"{base_name.replace('_',' ').title()}: GIF de carga")
        except Exception:
            pass

    # Limpieza (best effort)
    try:
        for p in tmp_dir.glob("*.jpg"):
            p.unlink(missing_ok=True)
        tmp_dir.rmdir()
    except Exception:
        pass

# ========= Flujos =========
def generic_flow(page, name, btn_panel=False):
    # 1) Esperar widget
    wait_widget_ready(page)

    # 2) Evidencia inicial
    img0 = OUTDIR / f"{name.lower().replace(' ','_')}_before.jpg"
    shot_jpg(page, img0, full=True); tg_file(img0, f"{name}: evidencia inicial")
    html0 = OUTDIR / f"{name.lower().replace(' ','_')}_before_check.html"
    save_html(page, html0); tg_file(html0, f"{name}: HTML inicial")

    # 3) Continuar
    try:
        page.locator(BTN_CONTINUE).first.click(timeout=PAGE_OP_TIMEOUT_MS)
    except Exception:
        pass

    page.wait_for_timeout(700)  # respirito humano

    # 4) CDMX: abrir panel LMD si aplica
    if btn_panel:
        try:
            page.locator(CDMX_PANEL).first.click(timeout=PAGE_OP_TIMEOUT_MS)
        except Exception:
            pass

    # 5) Grabar GIF de la carga del calendario (opcional) + esperar calendario
    base_name = f"{name.lower().replace(' ','_')}_loading"
    record_gif_until_ready(page, base_name, max_seconds=GIF_SECONDS, fps=GIF_FPS)

    # 6) Esperas reforzadas a que desaparezca el spinner y se vea algo “útil”
    for _ in range(3):
        try:
            wait_calendar_ready(page)
            break
        except Exception:
            page.mouse.wheel(0, 600)
            page.wait_for_timeout(1000)

    # 7) Evidencia final (ya con carga terminada)
    htmlf = OUTDIR / f"{name.lower().replace(' ','_')}_final.html"
    save_html(page, htmlf)
    capf  = OUTDIR / f"{name.lower().replace(' ','_')}_final.jpg"
    shot_jpg(page, capf, full=True)
    has = detect_slots(page)
    tg_file(htmlf, f"{name}: HTML final — {'HUECOS' if has else 'NO'}")
    tg_file(capf,  f"{name}: captura final — {'HUECOS' if has else 'NO'}")

    return has

CONSULADOS = [
    # Monterrey: mismo flujo que CDMX pero sin click de panel
    {"name": "Monterrey", "min": MIN_MTY, "widget": WIDGET_MTY, "btn_panel": False},
    # CDMX: abre el panel “PRESENTACION DOCUMENTACION LMD”
    {"name": "Ciudad de México", "min": MIN_CDMX, "widget": WIDGET_CDMX, "btn_panel": True},
]

def run_round(context):
    for cons in CONSULADOS:
        page = context.new_page()
        try:
            # Carga del widget con retries básicos
            for attempt in range(max(1, GOTO_RETRIES)):
                try:
                    page.goto(cons["widget"], wait_until="domcontentloaded", timeout=LANDING_TIMEOUT_MS)
                    break
                except PWTimeout:
                    if attempt + 1 >= GOTO_RETRIES:
                        raise
            has = generic_flow(page, cons["name"], btn_panel=cons["btn_panel"])
            tg_text(f"[{cons['name']}] {now()} {'→ HUECOS detectados' if has else '→ sin huecos por ahora.'}")
        except Exception as e:
            tg_text(f"⚠️ {cons['name']}: error durante la revisión.\n{repr(e)}\n{traceback.format_exc()[:1000]}")
        finally:
            try: page.close()
            except: pass

# ========= Main loop =========
def main():
    with sync_playwright() as pw:
        # Lanzamos el browser (headless) – mantenemos la base estable
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width":1280,"height":900})
        while True:
            run_round(context)
            wait_s = random.randint(ROUND_MIN_WAIT, ROUND_MAX_WAIT)
            tg_text(f"[INFO] Esperando {wait_s}s antes de la siguiente ronda…")
            time.sleep(wait_s)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
