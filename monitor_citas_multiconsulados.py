import os, sys, time, random, traceback
from pathlib import Path
from datetime import datetime
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page, Frame

# ─────────────────────────────
# ENV / FLAGS
# ─────────────────────────────
def env_flag(name, default="0"):
    return os.getenv(name, default).strip().lower() in ("1","true","yes","on")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID","").strip()

BLOCK_IMAGES       = env_flag("BLOCK_IMAGES","0")
DEBUG_STEPS        = env_flag("DEBUG_STEPS","1")

LANDING_TIMEOUT_MS = int(os.getenv("LANDING_TIMEOUT_MS","60000"))
WIDGET_TIMEOUT_MS  = int(os.getenv("WIDGET_TIMEOUT_MS","70000"))
PAGE_OP_TIMEOUT_MS = int(os.getenv("PAGE_OP_TIMEOUT_MS","12000"))
GOTO_RETRIES       = int(os.getenv("GOTO_RETRIES","2"))

ROUND_MIN_WAIT     = int(os.getenv("ROUND_MIN_WAIT","300"))
ROUND_MAX_WAIT     = int(os.getenv("ROUND_MAX_WAIT","420"))

OUTDIR = Path("/tmp/evidencias")
OUTDIR.mkdir(parents=True, exist_ok=True)

def now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def human(a: float, b: float):
    time.sleep(random.uniform(a, b))

# ─────────────────────────────
# TELEGRAM
# ─────────────────────────────
def tg_text(text: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID): return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=20
        )
    except Exception:
        pass

def tg_file(path: Path, caption: str=""):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID): return
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"document": (path.name, f)},
                timeout=60
            )
    except Exception:
        pass

# ─────────────────────────────
# URLs / SELECTORES
# ─────────────────────────────
# Widget directo
WIDGET_MTY  = "https://www.citaconsular.es/es/hosteds/widgetdefault/2533f04b1d3e818b66f175afc9c24cf63/"
WIDGET_CDMX = "https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/"

# Entrada ministerio (fallback humanizado)
ENTRY_MTY  = "https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx"
ENTRY_CDMX = "https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx"

# Botón continuar + textos relevantes
BTN_CONTINUE   = (
    "role=button[name=/continuar|continue|aceptar|accept|entrar/i], "
    "text=/\\bcontinuar\\b|\\bcontinue\\b|\\baceptar\\b|\\baccept\\b|\\bentrar\\b/i"
)
TXT_CONTINUE_HINT = "text=/Para\\s+solicitar\\s+cita\\s+pulse\\s+en\\s+el\\s+bot[oó]n\\s+continuar/i"
NO_SLOTS_TEXT  = "text=/No hay horas disponibles/i"
SPINNER_TEXT   = "text=/Loading|Cargando/i"
CDMX_PANEL     = "text=/PRESENTACION\\s+DOCUMENTACION\\s+LEY\\s+MEMORIA/i"
CAL_CHANGE_DAY = "text=/Cambiar de d[ií]a/i"
SLOT_BUTTONS   = "button:has-text('Hueco'), button:has-text('libre'), button:has-text('Dispon')"

# ─────────────────────────────
# EVIDENCIAS
# ─────────────────────────────
def shot_jpg(ctx, path: Path, full=True):
    page = ctx.page if hasattr(ctx, "page") else ctx
    page.screenshot(type="jpeg", quality=72, full_page=full, path=str(path))

def save_html(ctx, path: Path):
    try:
        html = ctx.content()
    except Exception:
        try:
            html = ctx.page.content()
        except Exception:
            html = ""
    path.write_text(html or "", encoding="utf-8")

def dump_all_iframes(page: Page, base: str, send=True):
    try:
        p = OUTDIR / f"{base}_page.html"
        p.write_text(page.content(), encoding="utf-8")
        if send: tg_file(p, f"{base}: HTML page")
    except Exception:
        pass
    for idx, fr in enumerate(page.frames):
        try:
            p = OUTDIR / f"{base}_frame_{idx}.html"
            p.write_text(fr.content(), encoding="utf-8")
            if send and ("citaconsular" in (fr.url or "") or "bookitit" in (fr.url or "")):
                tg_file(p, f"{base}: HTML iframe[{idx}] ({fr.url})")
        except Exception:
            continue

# ─────────────────────────────
# CONTEXTOS / CLICK / WAITs
# ─────────────────────────────
def all_contexts(page: Page):
    frs = list(page.frames)
    frs.sort(key=lambda fr: ("citaconsular" in (fr.url or "") or "bookitit" in (fr.url or "")), reverse=True)
    return [page] + frs

def any_count(page: Page, selector: str) -> int:
    c = 0
    for ctx in all_contexts(page):
        try: c += ctx.locator(selector).count()
        except Exception: pass
    return c

def click_if_visible(page: Page, selector: str, timeout=3000) -> bool:
    for ctx in all_contexts(page):
        try:
            loc = ctx.locator(selector).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False

def close_cookie_bars(page: Page):
    cookie_btns = (
        "role=button[name=/acept(ar|o)|accept|ok|entendido|de acuerdo|permitir/i], "
        "text=/acept(ar|o)|accept|ok|entendido|de acuerdo|permitir/i"
    )
    # arriba y dentro de iframes
    click_if_visible(page, cookie_btns)

def wait_spinner_gone(page: Page, timeout_ms: int):
    deadline = time.time() + timeout_ms/1000
    while time.time() < deadline:
        visible = False
        for ctx in all_contexts(page):
            try:
                loc = ctx.locator(SPINNER_TEXT).first
                if loc.count() and loc.is_visible():
                    visible = True; break
            except Exception: pass
        if not visible: return
        time.sleep(0.25)

def goto_with_retries(page: Page, url: str, timeout_ms: int) -> bool:
    last = None
    for _ in range(GOTO_RETRIES + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            return True
        except Exception as e:
            last = e; time.sleep(1.2)
    tg_text(f"[warn] GOTO falló: {url}\n{repr(last)}")
    return False

# ─────────────────────────────
# FALLBACK: MINISTERIO → ELEGIR FECHA Y HORA
# ─────────────────────────────
def enter_via_ministerio(page: Page, entry_url: str) -> Page:
    """
    Abre la página del Ministerio y hace clic en 'ELEGIR FECHA Y HORA'.
    Devuelve la página resultante (misma pestaña o popup).
    """
    if not goto_with_retries(page, entry_url, LANDING_TIMEOUT_MS):
        return page

    close_cookie_bars(page)
    time.sleep(0.5)
    # Evidencia ministerio
    imgm = OUTDIR / ("ministerio.jpg")
    shot_jpg(page, imgm, full=True); tg_file(imgm, "HTML inicial (ministerio)")

    # Intentar popup o misma pestaña
    btn = page.locator("a, button, span, div").filter(has_text="ELEGIR FECHA Y HORA").first
    try:
        with page.expect_popup(timeout=5000) as pop:
            btn.click(timeout=PAGE_OP_TIMEOUT_MS)
        newp = pop.value
        newp.wait_for_load_state("domcontentloaded", timeout=LANDING_TIMEOUT_MS)
        try: newp.wait_for_load_state("networkidle", timeout=LANDING_TIMEOUT_MS)
        except Exception: pass
        return newp
    except Exception:
        # misma pestaña
        try:
            btn.click(timeout=PAGE_OP_TIMEOUT_MS)
            page.wait_for_load_state("domcontentloaded", timeout=LANDING_TIMEOUT_MS)
            try: page.wait_for_load_state("networkidle", timeout=LANDING_TIMEOUT_MS)
            except Exception: pass
        except Exception:
            pass
        return page

# ─────────────────────────────
# WIDGET READY / CALENDARIO
# ─────────────────────────────
def wait_widget_ready(page: Page, entry_url_fallback: str|None) -> Page|Frame:
    close_cookie_bars(page)
    wait_spinner_gone(page, int(WIDGET_TIMEOUT_MS/2))

    def _find_ctx() -> Page|Frame|None:
        for ctx in all_contexts(page):
            try:
                loc = ctx.locator(f"{BTN_CONTINUE}, {NO_SLOTS_TEXT}, {TXT_CONTINUE_HINT}").first
                if loc.count() and loc.is_visible():
                    return ctx
            except Exception: pass
        return None

    ctx = _find_ctx()
    if ctx: return ctx

    # recarga suave
    try:
        page.reload(wait_until="domcontentloaded", timeout=LANDING_TIMEOUT_MS)
        try: page.wait_for_load_state("networkidle", timeout=LANDING_TIMEOUT_MS)
        except Exception: pass
    except Exception: pass
    close_cookie_bars(page)
    wait_spinner_gone(page, int(WIDGET_TIMEOUT_MS/3))

    ctx = _find_ctx()
    if ctx: return ctx

    # FALLBACK: entrar por Ministerio si nos lo pasan
    if entry_url_fallback:
        page = enter_via_ministerio(page, entry_url_fallback)
        close_cookie_bars(page)
        wait_spinner_gone(page, int(WIDGET_TIMEOUT_MS/2))
        ctx = _find_ctx()
        if ctx: return ctx

    raise PWTimeout(f"No apareció Continuar ni 'No hay horas...' tras {WIDGET_TIMEOUT_MS}ms (incluyendo iframes/banners).")

def wait_calendar_ready(page: Page) -> bool:
    wait_spinner_gone(page, WIDGET_TIMEOUT_MS)
    deadline = time.time() + WIDGET_TIMEOUT_MS/1000
    while time.time() < deadline:
        if any_count(page, f"{CAL_CHANGE_DAY}, {SLOT_BUTTONS}, {NO_SLOTS_TEXT}") > 0:
            return True
        time.sleep(0.25)
    try:
        page.mouse.wheel(0, 1200); time.sleep(0.8)
    except Exception: pass
    return any_count(page, f"{CAL_CHANGE_DAY}, {SLOT_BUTTONS}, {NO_SLOTS_TEXT}") > 0

def detect_slots(page: Page) -> bool:
    if any_count(page, NO_SLOTS_TEXT) > 0:
        return False
    if any_count(page, SLOT_BUTTONS) > 0:
        return True
    for ctx in all_contexts(page):
        try:
            if ctx.locator("button >> text=/\\b([01]\\d|2[0-3]):[0-5]\\d\\b/").count() > 0:
                return True
        except Exception: pass
    return False

# ─────────────────────────────
# FLOWS
# ─────────────────────────────
def generic_flow(page: Page, nombre: str, needs_panel: bool, entry_fallback: str|None) -> bool:
    base = nombre.lower().replace(" ", "_")

    ctx_ready = wait_widget_ready(page, entry_fallback)

    # Evidencia inicial
    img0 = OUTDIR / f"{base}_initial.jpg"; shot_jpg(page, img0, full=True); tg_file(img0, f"{nombre}: evidencia inicial")
    html0 = OUTDIR / f"{base}_initial.html"; save_html(ctx_ready, html0); tg_file(html0, f"{nombre}: HTML inicial")

    # Continuar (si visible)
    click_if_visible(page, BTN_CONTINUE, timeout=PAGE_OP_TIMEOUT_MS)
    page.wait_for_timeout(900)

    # CDMX: abrir panel
    if needs_panel:
        click_if_visible(page, CDMX_PANEL, timeout=PAGE_OP_TIMEOUT_MS)
        page.wait_for_timeout(800)

    # Esperar calendario/estado final
    ok_ready = False
    for _ in range(2):
        if wait_calendar_ready(page):
            ok_ready = True; break
        page.wait_for_timeout(700)

    # Evidencia final
    htmlf = OUTDIR / f"{base}_final.html"; save_html(page, htmlf); tg_file(htmlf, f"{nombre}: HTML final — {'OK' if ok_ready else 'pendiente'}")
    capf  = OUTDIR / f"{base}_final.jpg"; shot_jpg(page, capf, full=True); tg_file(capf, f"{nombre}: captura final — {'OK' if ok_ready else 'pendiente'}")

    return detect_slots(page)

CONSULADOS = [
    {"name":"Monterrey",        "widget":WIDGET_MTY,  "needs_panel":False, "entry":ENTRY_MTY},
    {"name":"Ciudad de México", "widget":WIDGET_CDMX, "needs_panel":True,  "entry":ENTRY_CDMX},
]

# ─────────────────────────────
# RUN / LOOP
# ─────────────────────────────
def run_round(context):
    for cons in CONSULADOS:
        name, url, needs_panel, entry = cons["name"], cons["widget"], cons["needs_panel"], cons["entry"]
        page = context.new_page()
        try:
            if not goto_with_retries(page, url, LANDING_TIMEOUT_MS):
                tg_text(f"[{name}] {now()} → error abriendo widget.")
                page.close(); continue

            has = generic_flow(page, name, needs_panel, entry)
            tg_text(f"[{name}] {now()} {'→ HUECOS detectados' if has else '→ sin huecos por ahora.'}")

        except PWTimeout as e:
            base = name.lower().replace(" ","_")
            dump_all_iframes(page, base, send=True)
            try:
                img = OUTDIR / f"{base}_timeout.jpg"
                shot_jpg(page, img, full=True); tg_file(img, f"{name}: captura en timeout")
            except Exception: pass
            tg_text(f"⚠️ {name}: error durante la revisión.\n{repr(e)}\n{traceback.format_exc()[:1200]}")

        except Exception as e:
            tg_text(f"⚠️ {name}: error durante la revisión.\n{repr(e)}\n{traceback.format_exc()[:1200]}")
        finally:
            try: page.close()
            except Exception: pass

def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-renderer-backgrounding",
                "--force-color-profile=srgb",
            ],
        )
        context = browser.new_context(viewport={"width":1280,"height":900})

        def on_dialog(dlg):
            try: dlg.accept()
            except Exception: pass
        context.on("dialog", on_dialog)

        if BLOCK_IMAGES:
            def route_filter(route):
                if route.request.resource_type == "image":
                    return route.abort()
                return route.continue_()
            context.route("**/*", route_filter)

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
