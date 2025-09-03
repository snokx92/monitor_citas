import os, re, time, random, traceback, json
from datetime import datetime
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ================== ENV / CONFIG ==================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

ENTRY_MODE = os.getenv("ENTRY_MODE", "ministry").lower()  # ministry|direct|mixed
WIDGET_TIMEOUT_MS = int(os.getenv("WIDGET_TIMEOUT_MS", "90000"))  # 90s
ROUND_SLEEP_MIN_S = int(os.getenv("ROUND_SLEEP_MIN_S", "300"))
ROUND_SLEEP_MAX_S = int(os.getenv("ROUND_SLEEP_MAX_S", "420"))
HEADLESS = os.getenv("HEADLESS", "1") == "1"
SHOW_PUBLIC_IP = os.getenv("SHOW_PUBLIC_IP", "0") == "1"

# Proxy (opcional)
PROXY_HOST = os.getenv("PROXY_HOST")
PROXY_PORT = os.getenv("PROXY_PORT")
PROXY_USER = os.getenv("PROXY_USER")
PROXY_PASS = os.getenv("PROXY_PASS")

# Opcional: links directos al widget (si los tienes y están sanos)
DIRECT_URL_MTY  = os.getenv("DIRECT_URL_MTY", "").strip()
DIRECT_URL_CDMX = os.getenv("DIRECT_URL_CDMX", "").strip()

# Rutas de Ministerio (entrada humana)
MIN_URL = {
    "Monterrey": "https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
    "Ciudad de México": "https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
}

# Texto/regex clave
RX_CONTINUE  = re.compile(r"Continue\s*\/\s*Continuar", re.I)
RX_NO_HOURS  = re.compile(r"No hay horas disponibles", re.I)
RX_TIME_HHMM = re.compile(r"\b\d{1,2}:\d{2}\b")
RX_LOADING   = re.compile(r"loading\.{0,3}", re.I)
RX_PANEL_CDMX = re.compile(r"presentaci[oó]n\s+documentaci[oó]n\s+ley\s+memoria\s+democr[aá]tica", re.I)

# ================== Telegram ==================
def tg_text(msg: str):
    if not (BOT_TOKEN and CHAT_ID):
        print("[TG]", msg); return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg, "parse_mode":"HTML"},
            timeout=20
        )
    except Exception as e:
        print("[TG] sendMessage error:", e)

def tg_doc(path: str, caption: str=""):
    if not os.path.exists(path): return
    if not (BOT_TOKEN and CHAT_ID):
        print(f"[TG-DOC] {caption} -> {path}"); return
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": CHAT_ID, "caption": caption},
                files={"document": f}, timeout=60
            )
    except Exception as e:
        print("[TG] sendDocument error:", e)

def tg_photo(path: str, caption: str=""):
    if not os.path.exists(path): return
    if not (BOT_TOKEN and CHAT_ID):
        print(f"[TG-PHOTO] {caption} -> {path}"); return
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id": CHAT_ID, "caption": caption},
                files={"photo": f}, timeout=60
            )
    except Exception as e:
        print("[TG] sendPhoto error:", e)

# ================== Util ==================
import os
def _slug(s: str): return re.sub(r"[^a-z0-9_]+", "_", s.lower())

def snap(page, base, caption=None):
    name = f"{_slug(base)}.jpg"
    page.screenshot(path=name, type="jpeg", quality=70, full_page=True)
    if caption: tg_photo(name, caption)
    return name

def dump_html(page, base, caption=None):
    name = f"{_slug(base)}.html"
    with open(name, "w", encoding="utf-8") as f:
        f.write(page.content())
    if caption: tg_doc(name, caption)
    return name

def nowts(): return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def human(mins=0.8, maxs=1.6): time.sleep(random.uniform(mins, maxs))

def public_ip():
    try:
        return requests.get("https://api.ipify.org?format=json", timeout=10).json().get("ip")
    except: return "desconocida"

# ================== Playwright ==================
STEALTH_JS = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'languages',{get:()=>['es-ES','es','en']});
Object.defineProperty(navigator,'platform',{get:()=> 'Win32'});
Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8});
"""

def launch(p):
    la = {"headless": HEADLESS, "args": ["--lang=es-ES,es","--no-sandbox","--disable-dev-shm-usage"]}
    if PROXY_HOST and PROXY_PORT:
        if PROXY_USER and PROXY_PASS:
            la["proxy"] = {"server": f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"}
        else:
            la["proxy"] = {"server": f"http://{PROXY_HOST}:{PROXY_PORT}"}
        print("[INFO] Proxy ON")
    br = p.chromium.launch(**la)
    ctx = br.new_context(
        user_agent=f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(118,132)}.0 Safari/537.36",
        locale="es-ES",
        timezone_id="America/Mexico_City",
        viewport={"width":1366,"height":900},
        java_script_enabled=True
    )
    page = ctx.new_page()
    page.add_init_script(STEALTH_JS)
    return br, ctx, page

# ================== Navegación estable ==================
def goto_ministerio(page, cons):
    page.goto(MIN_URL[cons], wait_until="domcontentloaded", timeout=60000)
    dump_html(page, f"{cons}_ministerio", f"{cons}: HTML inicial (ministerio)")
    snap(page, f"{cons}_ministerio", f"{cons}: evidencia ministerio")
    # buscar “ELEGIR FECHA Y HORA”
    lnk = page.locator("a:has-text('ELEGIR FECHA Y HORA')")
    if lnk.count() == 0:
        lnk = page.locator("a[href*='citaconsular'], a[href*='bookitit']")
    lnk.first.click(timeout=20000, force=True)
    human(0.6,1.4)
    page.wait_for_load_state("domcontentloaded", timeout=45000)

def goto_direct(page, cons):
    url = DIRECT_URL_MTY if cons=="Monterrey" else DIRECT_URL_CDMX
    if url:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    else:
        # si no hay directa, usa ministerio
        goto_ministerio(page, cons)

def ensure_entry(page, cons):
    mode = ENTRY_MODE
    if mode == "ministry": goto_ministerio(page, cons)
    elif mode == "direct": goto_direct(page, cons)
    else:
        if random.random()<0.5: goto_ministerio(page, cons)
        else: goto_direct(page, cons)

    # pantallazo “antes de parsear”
    dump_html(page, f"{cons}_before_check", f"{cons}: HTML inicial (widget)")
    snap(page, f"{cons}_before_check", f"{cons}: evidencia inicial (widget)")

    # Intermedia: “Continuar”
    try:
        btn = page.get_by_text(RX_CONTINUE).first
        if btn.is_visible(timeout=2500):
            btn.click(timeout=12000)
            human(0.7, 1.5)
    except: pass

    # paso extra para CDMX: abrir panel “presentación documentación LMD”
    if cons == "Ciudad de México":
        # espera a que se dibuje la caja; luego clicar
        try:
            box = page.get_by_text(RX_PANEL_CDMX).first
            if box.is_visible(timeout=4000):
                box.click(timeout=15000, force=True)
                human(0.7,1.3)
        except: pass

def find_widget_frame(page):
    # por URL primero
    for fr in page.frames:
        u = (fr.url or "").lower()
        if "citaconsular" in u or "bookitit" in u: return fr
    # por contenido visible
    for fr in page.frames:
        try:
            if fr.get_by_text(RX_CONTINUE).count()>0 or fr.get_by_text(RX_NO_HOURS).count()>0:
                return fr
        except: pass
    return None

def wait_widget(page, cons, timeout_ms):
    start = time.time()
    last = None
    while (time.time()-start)*1000 < timeout_ms:
        # main
        try:
            if page.get_by_text(RX_CONTINUE).first.is_visible(timeout=700): return page.main_frame
        except Exception as e: last=e
        try:
            if page.get_by_text(RX_NO_HOURS).first.is_visible(timeout=700): return page.main_frame
        except Exception as e: last=e
        # iframe
        fr = find_widget_frame(page)
        if fr:
            try:
                if fr.get_by_text(RX_CONTINUE).first.is_visible(timeout=700): return fr
            except Exception as e: last=e
            try:
                if fr.get_by_text(RX_NO_HOURS).first.is_visible(timeout=700): return fr
            except Exception as e: last=e
        human(0.5, 1.0)

    # timeout -> evidencias
    snap(page, f"{cons}_timeout_full", f"{cons}: captura en timeout")
    dump_html(page, f"{cons}_error_state", f"{cons}: HTML en error")
    tg_text(f"⚠️ {cons}: timeout esperando widget")
    tg_text(f"Frames: {json.dumps([f.url for f in page.frames], ensure_ascii=False)}")
    raise PWTimeout("timeout widget")

def open_panel_and_read(ctx, cons):
    # click Continuar en el frame si aún existe
    try:
        b = ctx.get_by_text(RX_CONTINUE).first
        if b.is_visible(timeout=1500):
            b.click(timeout=12000)
            human(0.7, 1.4)
    except: pass

    # espera a que el panel esté “estable”
    end = time.time()+45
    has = None
    while time.time()<end:
        try:
            if ctx.get_by_text(RX_NO_HOURS).first.is_visible(timeout=800):
                has = False; break
        except: pass
        try:
            html = ctx.content()
            if RX_TIME_HHMM.search(html) and not RX_LOADING.search(html):
                has = True; break
        except: pass
        human(0.6,1.0)

    # evidencias finales
    snap(ctx.page, f"{cons}_final", f"{cons}: captura final")
    dump_html(ctx.page, f"{cons}_final", f"{cons}: HTML final")
    return bool(has)

# ================== Flujos ==================
CONSULADOS = ["Monterrey", "Ciudad de México"]

def revisar(page, cons):
    ensure_entry(page, cons)
    ctx = wait_widget(page, cons, WIDGET_TIMEOUT_MS)
    has = open_panel_and_read(ctx, cons)
    return has

# ================== Loop ==================
def main():
    print("[start] Lanzando bot…")
    if SHOW_PUBLIC_IP:
        tg_text(f"[INFO] IP: {public_ip()}")

    with sync_playwright() as p:
        br, ctx, page = launch(p)
        try:
            while True:
                for cons in CONSULADOS:
                    try:
                        tg_text(f"[{nowts()}] [{cons}] goto…")
                        ok = revisar(page, cons)
                        if ok:
                            tg_text(f"[{nowts()}] {cons} → <b>HAY HORAS</b> ✅")
                        else:
                            tg_text(f"[{nowts()}] {cons} → sin huecos por ahora.")
                    except PWTimeout:
                        # ya enviamos evidencias y frames
                        pass
                    except Exception as e:
                        tg_text(f"⚠️ {cons}: error de ejecución")
                        print(traceback.format_exc())
                    finally:
                        human(1.0, 2.0)

                wait_s = random.randint(ROUND_SLEEP_MIN_S, ROUND_SLEEP_MAX_S)
                tg_text(f"[INFO] Esperando {wait_s}s antes de la siguiente ronda…")
                time.sleep(wait_s)
        finally:
            ctx.close(); br.close()

if __name__ == "__main__":
    main()
