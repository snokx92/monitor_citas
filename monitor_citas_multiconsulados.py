import os, sys, time, random, traceback
from datetime import datetime
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page, Frame

# GIF opcional
try:
    from PIL import Image
    PIL_OK = True
except Exception:
    PIL_OK = False

# ================== ENV ==================
def env_flag(name, default="0"):
    return os.getenv(name, default).strip().lower() in ("1","true","yes","on")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID","").strip()

BLOCK_IMAGES       = env_flag("BLOCK_IMAGES","0")
DEBUG_STEPS        = env_flag("DEBUG_STEPS","1")

# subimos un poco el timeout por latencias / iframes
LANDING_TIMEOUT_MS = int(os.getenv("LANDING_TIMEOUT_MS","50000"))
WIDGET_TIMEOUT_MS  = int(os.getenv("WIDGET_TIMEOUT_MS","60000"))
PAGE_OP_TIMEOUT_MS = int(os.getenv("PAGE_OP_TIMEOUT_MS","9000"))
GOTO_RETRIES       = int(os.getenv("GOTO_RETRIES","2"))

ROUND_MIN_WAIT     = int(os.getenv("ROUND_MIN_WAIT","300"))
ROUND_MAX_WAIT     = int(os.getenv("ROUND_MAX_WAIT","420"))

GIF_ENABLE  = env_flag("GIF_ENABLE","1")
GIF_SECONDS = int(os.getenv("GIF_SECONDS","6"))
GIF_FPS     = int(os.getenv("GIF_FPS","2"))

OUTDIR = Path("/tmp/evidencias"); OUTDIR.mkdir(parents=True, exist_ok=True)
def now(): return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

# ================== URLs / Selectores ==================
WIDGET_MTY  = "https://www.citaconsular.es/es/hosteds/widgetdefault/2533f04b1d3e818b66f175afc9c24cf63/"
WIDGET_CDMX = "https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/"

# Botón principal (más permisivo) y variantes
BTN_CONTINUE_WIDE = (
    "role=button[name=/continuar|continue|aceptar|accept|entrar/i], "
    "text=/\\bcontinuar\\b|\\bcontinue\\b|\\baceptar\\b|\\baccept\\b|\\bentrar\\b/i"
)
NO_SLOTS_TEXT   = "text=/No hay horas disponibles/i"
CDMX_PANEL      = "text=/PRESENTACION\\s+DOCUMENTACION\\s+LEY\\s+MEMORIA/i"
CAL_CHANGE_DAY  = "text=/Cambiar de d[ií]a/i"
SLOT_BUTTONS    = "button:has-text('Hueco'), button:has-text('libre')"
SPINNER_LOADING = "text=/Loading|Cargando/i"

# ================== Telegram ==================
def tg_text(msg: str):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=15)
        except Exception:
            pass

def tg_file(path: Path, caption=""):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and path.exists():
        try:
            with open(path,"rb") as f:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                              data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                              files={"document": (path.name, f)}, timeout=30)
        except Exception:
            pass

def shot_jpg(page_or_frame, path: Path, full=True):
    page = page_or_frame.page if hasattr(page_or_frame, "page") else page_or_frame
    page.screenshot(path=str(path), type="jpeg", quality=70, full_page=full)

def save_html(ctx, path: Path):
    try:
        html = ctx.content()
    except Exception:
        html = (ctx.page.content() if hasattr(ctx,"page") else "") or ""
    path.write_text(html or "", encoding="utf-8")

def human(a,b): time.sleep(random.uniform(a,b))

# ================== Context helpers ==================
def all_contexts(page: Page):
    # priorizamos frames del dominio del widget
    frames = list(page.frames)
    frames.sort(key=lambda fr: ("citaconsular.es" in (fr.url or "") or "bookitit" in (fr.url or ""), ), reverse=True)
    return [page] + frames

def any_count(page: Page, selector: str) -> int:
    total = 0
    for ctx in all_contexts(page):
        try:
            total += ctx.locator(selector).count()
        except Exception:
            continue
    return total

def wait_spinner_gone(page: Page, timeout_ms: int):
    deadline = time.time() + timeout_ms/1000
    while time.time() < deadline:
        found = False
        for ctx in all_contexts(page):
            try:
                loc = ctx.locator(SPINNER_LOADING).first
                if loc.count() and loc.is_visible():
                    found = True
                    break
            except Exception:
                continue
        if not found:
            return
        time.sleep(0.25)

def click_cookies_if_any(page: Page):
    # banners típicos de cookies
    COOKIE_BTNS = (
        "role=button[name=/acept(ar|o)|accept|de acuerdo|entendido|ok/i], "
        "text=/acept(ar|o)|accept|de acuerdo|entendido|ok/i"
    )
    for ctx in all_contexts(page):
        try:
            loc = ctx.locator(COOKIE_BTNS).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=3000)
                human(0.4, 0.8)
        except Exception:
            continue

def wait_widget_ready(page: Page):
    # 1) quitar overlays (spinner + cookies)
    click_cookies_if_any(page)
    wait_spinner_gone(page, int(WIDGET_TIMEOUT_MS/2))

    # 2) esperar “Continuar/Accept/Entrar” o “No hay horas…” en page o iframes
    deadline = time.time() + WIDGET_TIMEOUT_MS/1000
    while time.time() < deadline:
        for ctx in all_contexts(page):
            try:
                loc = ctx.locator(f"{BTN_CONTINUE_WIDE}, {NO_SLOTS_TEXT}").first
                if loc.count() and loc.is_visible():
                    return ctx
            except Exception:
                continue
        time.sleep(0.25)

    # 3) reintento único: reload suave y nuevo barrido
    page.reload(wait_until="domcontentloaded")
    human(1.0, 1.6)
    click_cookies_if_any(page)
    wait_spinner_gone(page, int(WIDGET_TIMEOUT_MS/2))

    deadline = time.time() + WIDGET_TIMEOUT_MS/2000  # medio timeout en el segundo intento
    while time.time() < deadline:
        for ctx in all_contexts(page):
            try:
                loc = ctx.locator(f"{BTN_CONTINUE_WIDE}, {NO_SLOTS_TEXT}").first
                if loc.count() and loc.is_visible():
                    return ctx
            except Exception:
                continue
        time.sleep(0.25)

    raise PWTimeout(f"No apareció Continuar ni 'No hay horas...' tras {WIDGET_TIMEOUT_MS}ms (incluyendo iframes/banners).")

def click_continue(page: Page):
    for ctx in all_contexts(page):
        try:
            loc = ctx.locator(BTN_CONTINUE_WIDE).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=PAGE_OP_TIMEOUT_MS)
                return True
        except Exception:
            continue
    return False

def click_cdmx_panel(page: Page):
    for ctx in all_contexts(page):
        try:
            loc = ctx.locator(CDMX_PANEL).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=PAGE_OP_TIMEOUT_MS)
                return True
        except Exception:
            continue
    return False

def wait_calendar_ready(page: Page):
    # quitar spinners residuales
    wait_spinner_gone(page, WIDGET_TIMEOUT_MS)
    # algo del calendario o “no hay…”
    deadline = time.time() + WIDGET_TIMEOUT_MS/1000
    while time.time() < deadline:
        if any_count(page, f"{CAL_CHANGE_DAY}, {SLOT_BUTTONS}, {NO_SLOTS_TEXT}") > 0:
            return True
        time.sleep(0.25)
    # intento extra con scroll
    try:
        page.mouse.wheel(0, 900)
        time.sleep(1.0)
    except Exception:
        pass
    return any_count(page, f"{CAL_CHANGE_DAY}, {SLOT_BUTTONS}, {NO_SLOTS_TEXT}") > 0

def detect_slots(page: Page) -> bool:
    if any_count(page, NO_SLOTS_TEXT) > 0:
        return False
    if any_count(page, SLOT_BUTTONS) > 0:
        return True
    # fallback: botones con horas HH:MM
    for ctx in all_contexts(page):
        try:
            if ctx.locator("button >> text=/\\b([01]\\d|2[0-3]):[0-5]\\d\\b/").count() > 0:
                return True
        except Exception:
            continue
    return False

# ================== GIF ==================
def record_gif_until_ready(page: Page, base_name: str, seconds: int, fps: int):
    if not (GIF_ENABLE and PIL_OK and seconds > 0 and fps > 0): return
    frames = []; interval = 1.0/float(fps)
    end_t = time.time() + seconds
    tmp = OUTDIR / f"{base_name}_frames"; tmp.mkdir(exist_ok=True)
    idx = 0
    while time.time() < end_t:
        ready = (any_count(page, CAL_CHANGE_DAY) > 0 or
                 any_count(page, SLOT_BUTTONS) > 0 or
                 any_count(page, NO_SLOTS_TEXT) > 0)
        p = tmp / f"f_{idx:03d}.jpg"
        try:
            shot_jpg(page, p, full=True); frames.append(Image.open(p))
        except Exception:
            pass
        idx += 1
        if ready and idx >= max(2, fps): break
        time.sleep(interval)
    if frames:
        gif = OUTDIR / f"{base_name}.gif"
        try:
            frames[0].save(gif, save_all=True, append_images=frames[1:], duration=int(1000/fps), loop=0)
            tg_file(gif, f"{base_name.replace('_',' ').title()}: GIF de carga")
        except Exception:
            pass
    try:
        for f in tmp.glob("*.jpg"): f.unlink(missing_ok=True)
        tmp.rmdir()
    except Exception:
        pass

# ================== Flujos ==================
def generic_flow(page: Page, nombre: str, needs_panel: bool):
    # 1) Widget listo en main o iframe
    ctx_ready = wait_widget_ready(page)

    # 2) Evidencia inicial (HTML del contexto donde apareció algo + captura de página)
    base = nombre.lower().replace(" ", "_")
    img0 = OUTDIR / f"{base}_before.jpg"; shot_jpg(page, img0, full=True); tg_file(img0, f"{nombre}: evidencia inicial")
    html0 = OUTDIR / f"{base}_before.html"; save_html(ctx_ready, html0); tg_file(html0, f"{nombre}: HTML inicial (ctx)")

    # 3) Continuar (o “Aceptar/Entrar” si esa es la variante)
    click_continue(page)
    page.wait_for_timeout(700)

    # 4) CDMX: click panel
    if needs_panel:
        click_cdmx_panel(page)
        page.wait_for_timeout(600)

    # 5) GIF durante la carga
    record_gif_until_ready(page, f"{base}_loading", GIF_SECONDS, GIF_FPS)

    # 6) Esperar calendario listo (sin spinner)
    ok_ready = False
    for _ in range(3):
        if wait_calendar_ready(page):
            ok_ready = True
            break
        try:
            page.mouse.wheel(0, 800)
        except Exception:
            pass
        time.sleep(1.0)

    # 7) Evidencias finales
    htmlf = OUTDIR / f"{base}_final.html"; save_html(page, htmlf); tg_file(htmlf, f"{nombre}: HTML final — {'OK' if ok_ready else 'pendiente'}")
    capf  = OUTDIR / f"{base}_final.jpg"; shot_jpg(page, capf, full=True); tg_file(capf, f"{nombre}: captura final — {'OK' if ok_ready else 'pendiente'}")

    return detect_slots(page)

CONSULADOS = [
    {"name": "Monterrey",        "widget": WIDGET_MTY,  "needs_panel": False},
    {"name": "Ciudad de México", "widget": WIDGET_CDMX, "needs_panel": True},
]

# ================== Navegación & Loop ==================
def goto_with_retries(page: Page, url: str, timeout_ms: int):
    err=None
    for _ in range(GOTO_RETRIES+1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            return True
        except Exception as e:
            err=e; time.sleep(1.0)
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

        # aceptar cualquier diálogo nativo (Welcome/Bienvenido)
        def on_dialog(dlg):
            try: dlg.accept()
            except Exception: pass
        context.on("dialog", on_dialog)

        # opcion: bloquear imágenes (recomendación: OFF mientras afinamos)
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
