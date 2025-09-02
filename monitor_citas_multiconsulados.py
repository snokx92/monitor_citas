# monitor_citas_multiconsulados.py
# -*- coding: utf-8 -*-

import os, sys, time, random, re, json, traceback
from dataclasses import dataclass
from typing import List, Optional, Tuple
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout
import requests

# ====== helpers entorno ======
def env_bool(n, d="0"): return (os.getenv(n, d) or "").strip() in ("1","true","TRUE","yes","on")
def env_int(n, d): 
    try: return int(os.getenv(n, d))
    except: return int(d)

DEBUG_STEPS        = env_bool("DEBUG_STEPS","1")
BLOCK_IMAGES       = env_bool("BLOCK_IMAGES","0")
SHOW_PUBLIC_IP     = env_bool("SHOW_PUBLIC_IP","1")
GOTO_RETRIES       = env_int("GOTO_RETRIES","3")
LANDING_TIMEOUT_MS = env_int("LANDING_TIMEOUT_MS","25000")
WIDGET_TIMEOUT_MS  = env_int("WIDGET_TIMEOUT_MS","25000")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID","")

PROXY_HOST  = os.getenv("PROXY_HOST","")
PROXY_PORT  = os.getenv("PROXY_PORT","")
PROXY_USER  = os.getenv("PROXY_USER","")
PROXY_PASS  = os.getenv("PROXY_PASS","")
PROXY_SESS  = os.getenv("PROXY_SESSION_IN_USER","")  # p.ej "__cr.us,mx"

IP_ENDPOINT = os.getenv("IP_ENDPOINT","https://api.ipify.org?format=json")

# chequeo humanizado entre 5–7 min
CHECK_MIN_SEC, CHECK_MAX_SEC = 300, 420

# ====== Telegram ======
def notify(msg:str):
    print(msg, flush=True)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=15)
        except Exception as e:
            print(f"[WARN] sendMessage: {e}", flush=True)

def send_photo(path:str, caption:str=""):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID): return
    try:
        with open(path,"rb") as f:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                          data={"chat_id": TELEGRAM_CHAT_ID,"caption":caption},
                          files={"photo":f}, timeout=30)
    except Exception as e:
        print(f"[WARN] sendPhoto: {e}", flush=True)

def send_document(path:str, caption:str=""):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID): return
    try:
        with open(path,"rb") as f:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                          data={"chat_id": TELEGRAM_CHAT_ID,"caption":caption},
                          files={"document":f}, timeout=30)
    except Exception as e:
        print(f"[WARN] sendDocument: {e}", flush=True)

# ====== UA + pausas ======
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]
def human_pause(a=0.7,b=1.5): time.sleep(random.uniform(a,b))

# ====== selectores / regex ======
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
CONTINUE_SELECTORS = [
    "button.btn.btn-success",
    "button:has-text('Continue')",
    "button:has-text('Continuar')",
    "text=/\\bContinue\\b/i","text=/\\bContinuar\\b/i"
]
NO_TXT = ["No hay horas disponibles","There are no available hours"]
CALENDAR_HINTS = ["Cambiar de día","Hueco libre"]

# ====== modelo ======
from dataclasses import dataclass
@dataclass
class Consulado:
    nombre: str
    landing_url: str
    landing_link_text: str
    widget_host: str

CONSULADOS = [
    Consulado(
        "Monterrey",
        "https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
        "ELEGIR FECHA Y HORA",
        "www.citaconsular.es",
    ),
    Consulado(
        "Ciudad de México",
        "https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
        "ELEGIR FECHA Y HORA",
        "www.citaconsular.es",
    ),
]

# ====== detección ======
def page_has_continue(ctx)->bool:
    try:
        for s in CONTINUE_SELECTORS:
            loc = ctx.locator(s)
            if loc.count() and loc.first.is_visible(): return True
    except: pass
    return False

def page_has_no_hours(ctx)->bool:
    try:
        for t in NO_TXT:
            if ctx.get_by_text(t, exact=False).count(): return True
    except: pass
    return False

def page_has_calendar_hints(ctx)->bool:
    try:
        for t in CALENDAR_HINTS:
            if ctx.get_by_text(t, exact=False).count(): return True
    except: pass
    return False

def extract_slots(ctx)->List[Tuple[str,str]]:
    out=[]
    try:
        c = ctx.locator("button, .btn, [role=button]")
        n = min(c.count(),300)
        for i in range(n):
            try:
                el=c.nth(i)
                if not el.is_visible(): continue
                txt=(el.inner_text() or "").strip()
                if not txt or "hueco libre" not in txt.lower(): continue
                m=TIME_RE.search(txt)
                if m: out.append((m.group(0),txt))
            except: continue
    except: pass
    return out

# ====== red / proxy ======
def set_request_interception(context):
    if not BLOCK_IMAGES: return
    def route_intercept(route):
        if route.request.resource_type in ("image","media","font"):
            return route.abort()
        return route.continue_()
    try: context.route("**/*", route_intercept)
    except: pass

def apply_proxy(play):
    if not PROXY_HOST or not PROXY_PORT: return {}
    user = PROXY_USER or ""
    if PROXY_SESS: user = f"{user}{PROXY_SESS}"
    p={"server":f"http://{PROXY_HOST}:{PROXY_PORT}"}
    if user or PROXY_PASS:
        p["username"]=user; p["password"]=PROXY_PASS or ""
    return {"proxy":p}

def show_ip(page):
    if not SHOW_PUBLIC_IP: return
    try:
        r=page.context.request.get(IP_ENDPOINT, timeout=15000)
        if r.ok: notify(f"[INFO] IP pública: {r.json().get('ip','?')}")
    except: pass

def goto(page,url,wait="domcontentloaded",timeout=LANDING_TIMEOUT_MS)->bool:
    for i in range(GOTO_RETRIES+1):
        try:
            if DEBUG_STEPS: print(f"[goto] {url}", flush=True)
            page.goto(url, wait_until=wait, timeout=timeout)
            return True
        except Exception:
            if i==GOTO_RETRIES: return False
            human_pause(0.8,1.5)
    return False

# ====== evidencias ======
def snapshot(page, base, caption):
    try:
        img=f"{base}.png"; html=f"{base}.html"
        page.screenshot(path=img, full_page=True)
        with open(html,"w",encoding="utf-8") as f: f.write(page.content() or "")
        send_photo(img, caption=caption); send_document(html, caption=caption.replace("captura","HTML"))
    except Exception as e:
        print(f"[WARN] snapshot: {e}", flush=True)

# ====== nuevas esperas del widget ======
def find_widget_frame(page):
    # el iframe suele vivir en el mismo host o con /hosteds/
    for fr in page.frames:
        try:
            u = fr.url or ""
            if "citaconsular" in u or "/hosteds/" in u: 
                return fr
        except: 
            continue
    return None

def wait_widget_ready(page, timeout_ms=WIDGET_TIMEOUT_MS)->Optional["Frame"]:
    """Espera a que el iframe tenga DOM real (no 'Loading')."""
    t0 = time.time()
    # espera a que aparezca un frame plausible
    while (time.time()-t0)*1000 < timeout_ms:
        fr = find_widget_frame(page)
        if fr:
            try:
                # body con algo y que no sea overlay de loading
                fr.wait_for_load_state("domcontentloaded", timeout=3000)
                ok = fr.wait_for_function(
                    """() => {
                        const b = document.body;
                        if(!b) return false;
                        const txt = (b.innerText||'').trim();
                        return txt.length > 20; // algo de contenido
                    }""",
                    timeout=4000
                )
                if ok:
                    # si literalmente vemos 'Loading' arriba, esperamos un poco más a que desaparezca
                    try:
                        fr.wait_for_selector("text='Loading'", timeout=1000)
                        # si lo encontró, espera a que se vaya
                        fr.wait_for_selector("text='Loading'", state="detached", timeout=5000)
                    except:
                        pass
                    return fr
            except:
                pass
        human_pause(0.4,0.9)

    return None

def click_continue_and_wait(page_or_frame, timeout_ms=WIDGET_TIMEOUT_MS)->bool:
    """Click robusto a Continuar dentro del contexto que ya está 'ready'."""
    try: 
        page_or_frame.page.on("dialog", lambda d: d.accept())
    except: 
        pass

    # reintentos suaves para encontrar y clicar
    t0 = time.time()
    while (time.time()-t0)*1000 < timeout_ms:
        try:
            tgt=None
            for sel in CONTINUE_SELECTORS:
                loc = page_or_frame.locator(sel)
                if loc.count() and loc.first.is_visible():
                    tgt = loc.first; break
            if tgt:
                tgt.scroll_into_view_if_needed()
                human_pause(0.2,0.5)
                tgt.click(timeout=3000, force=True)
                # ahora esperar señales de agenda
                t1=time.time()
                while (time.time()-t1)*1000 < 8000:
                    human_pause(0.3,0.8)
                    if page_has_no_hours(page_or_frame) or page_has_calendar_hints(page_or_frame) or extract_slots(page_or_frame):
                        return True
                # si no vimos señales, seguimos reintentando por si aparece overlay
        except Exception:
            pass
        human_pause(0.5,1.0)

    return False

# ====== abrir widget desde Exteriores ======
def open_widget_from_landing(page, cons:Consulado):
    try: page.wait_for_load_state("domcontentloaded", timeout=LANDING_TIMEOUT_MS)
    except: pass

    link=None
    try:
        link = page.get_by_text(cons.landing_link_text, exact=False).first
        if not link.count(): link = page.get_by_text("Elegir fecha y hora", exact=False).first
    except: link=None

    if not link or not link.count():
        if DEBUG_STEPS: print("[landing] enlace no encontrado", flush=True)
        return None

    # ver si abre pestaña nueva
    new_page=None
    try:
        with page.context.expect_page(timeout=6000) as pw:
            link.scroll_into_view_if_needed(); human_pause(0.2,0.5); link.click(timeout=5000)
        new_page=pw.value
    except:
        new_page=page  # misma pestaña

    try: new_page.wait_for_load_state("domcontentloaded", timeout=WIDGET_TIMEOUT_MS)
    except: pass
    return new_page

# ====== flujo por consulado ======
def revisar_consulado(play, cons:Consulado)->Tuple[bool,List[Tuple[str,str]],Optional[str]]:
    launch = {"headless":True}; launch.update(apply_proxy(play))
    browser = play.chromium.launch(**launch)
    ua=random.choice(USER_AGENTS)
    context = browser.new_context(
        viewport={"width":random.randint(1200,1440),"height":random.randint(800,960)},
        user_agent=ua, locale="es-ES",
        extra_http_headers={"Accept-Language":"es-MX,es;q=0.9,en;q=0.8"}
    )
    set_request_interception(context)
    page=context.new_page(); page.set_default_timeout(20000)

    try:
        # IP
        if SHOW_PUBLIC_IP: show_ip(page)

        # 1) Exteriores
        if not goto(page, cons.landing_url, "domcontentloaded", LANDING_TIMEOUT_MS):
            snapshot(page, f"{cons.nombre.lower()}_landing_timeout", f"{cons.nombre}: landing timeout")
            browser.close(); return (False,[],None)

        # 2) Click al enlace amarillo
        widget_page = open_widget_from_landing(page, cons)
        if not widget_page:
            snapshot(page, f"{cons.nombre.lower()}_no_widget", f"{cons.nombre}: no se abrió el widget")
            browser.close(); return (False,[],None)

        # 3) Esperar que el iframe del widget esté listo (evita HTML en blanco / 'Loading')
        fr = wait_widget_ready(widget_page, timeout_ms=WIDGET_TIMEOUT_MS)
        if not fr:
            snapshot(widget_page, f"{cons.nombre.lower()}_widget_not_ready", f"{cons.nombre}: widget no listo (posible bloqueo/slow)")
            browser.close(); return (False,[],None)

        # 4) Evidencia ya con iframe listo
        try:
            with open(f"{cons.nombre.lower()}_before_check_widget.html","w",encoding="utf-8") as f:
                f.write(fr.content() or "")
            send_document(f"{cons.nombre.lower()}_before_check_widget.html", caption=f"{cons.nombre}: HTML inicial (widget listo)")
        except: pass

        # 5) Click Continuar ahora sí
        advanced = click_continue_and_wait(fr, timeout_ms=WIDGET_TIMEOUT_MS)
        if not advanced:
            snapshot(widget_page, f"{cons.nombre.lower()}_no_avanzo_continuar", f"{cons.nombre}: se quedó en Continuar (posible overlay)")
            browser.close(); return (False,[],None)

        # 6) "no hay horas" claro
        fecha=None
        if page_has_no_hours(fr):
            snapshot(widget_page, f"{cons.nombre.lower()}_no_final", f"{cons.nombre}: HTML final — NO")
            browser.close(); return (True,[],fecha)

        # 7) slots en iframe o subframes
        slots = extract_slots(fr)
        if not slots:
            for sub in fr.child_frames:
                slots = extract_slots(sub)
                if slots: break

        if slots:
            try:
                widget_page.screenshot(path=f"{cons.nombre.lower()}_yes.png", full_page=True)
                send_photo(f"{cons.nombre.lower()}_yes.png", caption=f"{cons.nombre}: HAY HUECOS")
            except: pass
            browser.close(); return (True, slots, fecha)

        # 8) sin señales claras, pero avanzó
        snapshot(widget_page, f"{cons.nombre.lower()}_after_no_clear", f"{cons.nombre}: sin huecos claros (no apareció 'No hay horas…')")
        browser.close(); return (True,[],fecha)

    except Exception:
        snapshot(page, f"{cons.nombre.lower()}_fatal", f"{cons.nombre}: error inesperado")
        print("[ERROR] " + "".join(traceback.format_exc()), flush=True)
        browser.close(); return (False,[],None)

# ====== main loop ======
def main():
    notify("🚀 Start: Launching bot…")
    if PROXY_HOST and PROXY_PORT: notify(f"[INFO] Proxy: http://{PROXY_HOST}:{PROXY_PORT}")

    with sync_playwright() as p:
        while True:
            try:
                for cons in CONSULADOS:
                    if DEBUG_STEPS: print(f"[{cons.nombre}] goto…", flush=True)
                    ok, slots, fecha = revisar_consulado(p, cons)
                    if not ok: continue
                    if slots:
                        horas = ", ".join(sorted({h for h,_ in slots})[:5])
                        notify(f"✅ [{cons.nombre}] HAY HUECOS{(' ('+fecha+')') if fecha else ''} -> {horas}")
                        time.sleep(300)
                    else:
                        notify(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {cons.nombre} -> sin huecos por ahora.")
                wait = random.randint(CHECK_MIN_SEC, CHECK_MAX_SEC)
                print(f"[INFO] Esperando {wait}s antes de la siguiente ronda…", flush=True)
                time.sleep(wait)
            except Exception:
                print("[LOOP ERROR] " + "".join(traceback.format_exc()), flush=True)
                time.sleep(30)

if __name__ == "__main__":
    main()
