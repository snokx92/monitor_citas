import os, sys, time, random, traceback
from datetime import datetime
from pathlib import Path
import requests

# GIF opcional
try:
    from PIL import Image
    PIL_OK = True
except Exception:
    PIL_OK = False

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page, Frame

# ============ ENV ============
def env_flag(name, default="0"):
    return os.getenv(name, default).strip().lower() in ("1","true","yes","on")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID","").strip()

BLOCK_IMAGES       = env_flag("BLOCK_IMAGES","0")
DEBUG_STEPS        = env_flag("DEBUG_STEPS","1")
GOTO_RETRIES       = int(os.getenv("GOTO_RETRIES","2"))
LANDING_TIMEOUT_MS = int(os.getenv("LANDING_TIMEOUT_MS","45000"))
WIDGET_TIMEOUT_MS  = int(os.getenv("WIDGET_TIMEOUT_MS","50000"))
PAGE_OP_TIMEOUT_MS = int(os.getenv("PAGE_OP_TIMEOUT_MS","8000"))

ROUND_MIN_WAIT     = int(os.getenv("ROUND_MIN_WAIT","300"))
ROUND_MAX_WAIT     = int(os.getenv("ROUND_MAX_WAIT","420"))

GIF_ENABLE  = env_flag("GIF_ENABLE","1")
GIF_SECONDS = int(os.getenv("GIF_SECONDS","6"))
GIF_FPS     = int(os.getenv("GIF_FPS","2"))

OUTDIR = Path("/tmp/evidencias"); OUTDIR.mkdir(parents=True, exist_ok=True)
def now(): return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

# ============ URLs / Selectores ============
MIN_MTY  = "https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx"
MIN_CDMX = "https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx"

WIDGET_MTY  = "https://www.citaconsular.es/es/hosteds/widgetdefault/2533f04b1d3e818b66f175afc9c24cf63/"
WIDGET_CDMX = "https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/"

BTN_CONTINUE    = "text=/Continue\\s*\\/\\s*Continuar/i"
NO_SLOTS_TEXT   = "text=/No hay horas disponibles/i"
CDMX_PANEL      = "text=/PRESENTACION DOCUMENTACION LEY MEMORIA/i"
CAL_CHANGE_DAY  = "text=/Cambiar de d[ií]a/i"
SLOT_BUTTONS    = "button:has-text('Hueco'), button:has-text('libre')"
SPINNER_LOADING = "text=/Loading|Cargando/i"

# ============ Telegram ============
def tg_text(msg: str):
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

def shot_jpg(page_or_frame, path: Path, full=True):
    # page_or_frame debe tener .page
    page = page_or_frame.page if hasattr(page_or_frame, "page") else page_or_frame
    page.screenshot(path=str(path), type="jpeg", quality=70, full_page=full)

def save_html(ctx, path: Path):
    try:
        html = ctx.content()
    except Exception:
        html = (ctx.page.content() if hasattr(ctx, "page") else "") or ""
    path.write_text(html or "", encoding="utf-8")

def human(a,b): time.sleep(random.uniform(a,b))

# ============ Helper: buscar en página + iframes ============
def all_contexts(page: Page):
    return [page] + list(page.frames)

def any_count(page: Page, selector: str) -> int:
    total = 0
    for ctx in all_contexts(page):
        try:
            total += ctx.locator(selector).count()
        except Exception:
            continue
    return total

def wait_any(page: Page, selector: str, timeout_ms: int) -> Frame | Page | None:
    """Espera hasta que 'selector' sea visible en page o en cualquier iframe."""
    deadline = time.time() + timeout_ms/1000
    while time.time() < deadline:
        for ctx in all_contexts(page):
            try:
                loc = ctx.locator(selector).first
                if loc.count() and loc.is_visible():
                    return ctx
            except Exception:
                continue
        time.sleep(0.25)
    return None

def wait_spinner_gone(page: Page, timeout_ms: int):
    """Si hay spinner en algún contexto, espera a que desaparezca."""
    deadline = time.time() + timeout_ms/1000
    while time.time() < deadline:
        found = False
        for ctx in all_contexts(page):
            try:
                loc = ctx.locator(SPINNER_LOADING).first
                if loc.count():
                    found = True
                    # si existe y es visible, esperamos un poco
                    break
            except Exception:
                continue
        if not found:
            return
        time.sleep(0.3)
    # no levantamos excepción; seguimos el flujo

# ============ Esperas específicas ============
def wait_widget_ready(page: Page):
    # Primero dejar que se vaya cualquier "Loading"
    wait_spinner_gone(page, WIDGET_TIMEOUT_MS)
    # Luego esperar "Continuar" o "No hay horas..." en page o iframes
    ctx = wait_any(page, f"{BTN_CONTINUE}, {NO_SLOTS_TEXT}", WIDGET_TIMEOUT_MS)
    if ctx is None:
        # Último intento: recargar y esperar otro poco
        page.reload(wait_until="domcontentloaded")
        human(1.0, 1.5)
        wait_spinner_gone(page, int(WIDGET_TIMEOUT_MS/2))
        ctx = wait_any(page, f"{BTN_CONTINUE}, {NO_SLOTS_TEXT}", int(WIDGET_TIMEOUT_MS/2))
        if ctx is None:
            raise PWTimeout(f"No apareció Continuar ni 'No hay horas...' tras {WIDGET_TIMEOUT_MS}ms (incluyendo iframes).")
    return ctx  # devolvemos el contexto donde apareció

def wait_calendar_ready(page: Page):
    # spinner out
    wait_spinner_gone(page, WIDGET_TIMEOUT_MS)
    # algo del calendario o no-horas
    ctx = wait_any(page, f"{CAL_CHANGE_DAY}, {SLOT_BUTTONS}, {NO_SLOTS_TEXT}", WIDGET_TIMEOUT_MS)
    if ctx is None:
        # scroll suave y un último intento
        try: page.mouse.wheel(0, 800)
        except Exception: pass
        ctx = wait_any(page, f"{CAL_CHANGE_DAY}, {SLOT_BUTTONS}, {NO_SLOTS_TEXT}", int(WIDGET_TIMEOUT_MS/2))
    return ctx

def click_continue(page: Page):
    # Continuar puede estar en main o en iframe
    for ctx in all_contexts(page):
        try:
            loc = ctx.locator(BTN_CONTINUE).first
            if loc.count():
                if loc.is_enabled():
                    loc.click(timeout=PAGE_OP_TIMEOUT_MS)
                    return True
        except Exception:
            continue
    return False

def click_cdmx_panel(page: Page):
    for ctx in all_contexts(page):
        try:
            loc = ctx.locator(CDMX_PANEL).first
            if loc.count():
                loc.click(timeout=PAGE_OP_TIMEOUT_MS)
                return True
        except Exception:
            continue
    return False

def detect_slots(page: Page) -> bool:
    # Si hay "No hay horas..." en cualquier contexto → NO
    if any_count(page, NO_SLOTS_TEXT) > 0:
        return False
    # Si hay botones de hueco en cualquier contexto → SÍ
    if any_count(page, SLOT_BUTTONS) > 0:
        return True
    # fallback: botones que contienen HH:MM
    for ctx in all_contexts(page):
        try:
            if ctx.locator("button >> text=/\\b([01]\\d|2[0-3]):[0-5]\\d\\b/").count() > 0:
                return True
        except Exception:
            continue
    return False

# ============ GIF ============
def record_gif_until_ready(page: Page, base_name: str, seconds: int, fps: int):
    if not (GIF_ENABLE and PIL_OK and seconds > 0 and fps > 0): return
    frames = []
    interval = 1.0 / float(fps)
    end_time = time.time() + seconds
    tmp = OUTDIR / f"{base_name}_frames"; tmp.mkdir(exist_ok=True)
    idx = 0
    while time.time() < end_time:
        ready = (any_count(page, CAL_CHANGE_DAY) > 0 or
                 any_count(page, SLOT_BUTTONS) > 0 or
                 any_count(page, NO_SLOTS_TEXT) > 0)
        # screenshot frame
        p = tmp / f"f_{idx:03d}.jpg"
        try:
            shot_jpg(page, p, full=True)
            frames.append(Image.open(p))
        except Exception:
            pass
        idx += 1
        if ready and idx >= max(2, fps):  # 1 seg extra
            break
        time.sleep(interval)
    if frames:
        gif = OUTDIR / f"{base_name}.gif"
        try:
            frames[0].save(
                gif, save_all=True, append_images=frames[1:],
                duration=int(1000/fps), loop=0, optimize=False
            )
            tg_file(gif, f"{base_name.replace('_',' ').title()}: GIF de carga")
        except Exception:
            pass
    # cleanup
    try:
        for f in tmp.glob("*.jpg"): f.unlink(missing_ok=True)
        tmp.rmdir()
    except Exception:
        pass

# ============ Flujos ============
def generic_flow(page: Page, nombre: str, needs_panel: bool):
    # 1) Widget listo en page o iframe
    ctx_ready = wait_widget_ready(page)

    # 2) Evidencia inicial (HTML del contexto real + captura de página)
    base = nombre.lower().replace(" ", "_")
    img0 = OUTDIR / f"{base}_before.jpg"; shot_jpg(page, img0, full=True); tg_file(img0, f"{nombre}: evidencia inicial")
    html0 = OUTDIR / f"{base}_before.html"; save_html(ctx_ready, html0); tg_file(html0, f"{nombre}: HTML inicial (ctx)")

    # 3) Continuar (en el contexto correcto)
    click_continue(page)
    page.wait_for_timeout(700)

    if needs_panel:
        click_cdmx_panel(page)
        page.wait_for_timeout(600)

    # 4) GIF opcional durante la carga real
    record_gif_until_ready(page, f"{base}_loading", GIF_SECONDS, GIF_FPS)

    # 5) Esperar calendario listo (ctx_cal puede ser main o frame)
    ctx_cal = None
    for _ in range(3):
        ctx_cal = wait_calendar_ready(page)
        if ctx_cal is not None: break
        try: page.mouse.wheel(0, 600)
        except Exception: pass
        page.wait_for_timeout(900)

    # 6) Evidencia final (HTML del contexto de calendario + captura final)
    htmlf = OUTDIR / f"{base}_final.html"; save_html(ctx_cal or page, htmlf); tg_file(htmlf, f"{nombre}: HTML final")
    capf  = OUTDIR / f"{base}_final.jpg"; shot_jpg(page, capf, full=True); tg_file(capf, f"{nombre}: captura final")

    return detect_slots(page)

CONSULADOS = [
    {"name": "Monterrey",        "widget": WIDGET_MTY,  "needs_panel": False},
    {"name": "Ciudad de México", "widget": WIDGET_CDMX, "needs_panel": True},
]

# ============ Navegación/Loop ============
def goto_with_retries(page: Page, url: str, timeout_ms: int):
    err = None
    for _ in range(GOTO_RETRIES+1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            return True
        except Exception as e:
            err = e; time.sleep(1.0)
    tg_text(f"[warn] GOTO falló: {url}\n{repr(err)}")
    return False

def run_round(context):
    for cons in CONSULADOS:
        name, widget, needs_panel = cons["name"], cons["widget"], cons["needs_panel"]
        page = context.new_page()
        try:
            if not goto_with_retries(page, widget, LANDING_TIMEOUT_MS):
                tg_text(f"[{name}] {now()} → error al abrir widget.")
                page.close(); continue
            has = generic_flow(page, name, needs_panel)
            tg_text(f"[{name}] {now()} {'→ HUECOS detectados' if has else '→ sin huecos por ahora.'}")
        except Exception as e:
            tg_text(f"⚠️ {name}: error durante la revisión.\n{repr(e)}\n{traceback.format_exc()[:1200]}")
        finally:
            try: page.close()
            except Exception: pass

def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width":1280,"height":900})
        if BLOCK_IMAGES:
            context.route("**/*", lambda route: route.abort() if route.request.resource_type=="image" else route.continue_())
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
