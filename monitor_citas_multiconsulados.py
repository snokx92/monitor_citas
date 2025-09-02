# monitor_citas_multiconsulados.py
# -*- coding: utf-8 -*-

import os, sys, time, random, re, json, traceback
from dataclasses import dataclass
from typing import List, Optional, Tuple
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout
import requests

# =============================
# Config y helpers de entorno
# =============================

def env_bool(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or "").strip() in ("1", "true", "TRUE", "yes", "on")

def env_int(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except:
        return int(default)

DEBUG_STEPS       = env_bool("DEBUG_STEPS", "1")
BLOCK_IMAGES      = env_bool("BLOCK_IMAGES", "0")
SHOW_PUBLIC_IP    = env_bool("SHOW_PUBLIC_IP", "1")
GOTO_RETRIES      = env_int("GOTO_RETRIES", "2")
LANDING_TIMEOUT_MS= env_int("LANDING_TIMEOUT_MS", "20000")
WIDGET_TIMEOUT_MS = env_int("WIDGET_TIMEOUT_MS", "15000")
ROTATE_AFTER_BLANK= env_int("ROTATE_AFTER_BLANK", "3")
ROTATE_COOLDOWN_SEC= env_int("ROTATE_COOLDOWN_SEC", "60")

TELEGRAM_BOT_TOKEN= os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")

PROXY_HOST        = os.getenv("PROXY_HOST", "")
PROXY_PORT        = os.getenv("PROXY_PORT", "")
PROXY_USER        = os.getenv("PROXY_USER", "")
PROXY_PASS        = os.getenv("PROXY_PASS", "")
PROXY_SESSION     = os.getenv("PROXY_SESSION_IN_USER", "")  # ej: "__cr.us,mx" para DataImpulse

IP_ENDPOINT       = os.getenv("IP_ENDPOINT", "https://api.ipify.org?format=json")

# Ventana entre 5 y 7 minutos (humanizado)
CHECK_MIN_SEC     = 300
CHECK_MAX_SEC     = 420

# =============================
# Notificaciones Telegram
# =============================

def notify(msg: str):
    print(msg, flush=True)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
                timeout=15
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
                timeout=30,
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
                timeout=30,
            )
    except Exception as e:
        print(f"[WARN] Telegram sendDocument fallo: {e}", flush=True)

# =============================
# User agents y pausas humanas
# =============================

USER_AGENTS = [
    # Windows / Chrome / Edge / Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    # macOS / Safari / Firefox
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4; rv:127.0) Gecko/20100101 Firefox/127.0",
    # Mobile por si hace falta
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]

def human_pause(a=0.7, b=1.5):
    time.sleep(random.uniform(a, b))

# =============================
# Selectores y regex
# =============================

TIME_REGEX = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")  # 00:00–23:59

CONTINUE_SELECTORS = [
    "button.btn.btn-success",
    "button:has-text('Continue')",
    "button:has-text('Continuar')",
    "text=/\\bContinue\\b/i",
    "text=/\\bContinuar\\b/i",
]
NO_HOURS_TEXTS = ["No hay horas disponibles", "There are no available hours"]
CALENDAR_HINTS = ["Cambiar de día", "Hueco libre"]

# =============================
# Modelo de consulado
# =============================

@dataclass
class Consulado:
    nombre: str
    landing_url: str           # URL de exteriores (CitaNacionalidadLMD.aspx)
    landing_link_text: str     # texto "ELEGIR FECHA Y HORA" (ancla en exteriores)
    widget_host: str           # dominio esperado del widget (citaconsular.es)

# Monterrey y Ciudad de México con paso intermedio de Exteriores
CONSULADOS: List[Consulado] = [
    Consulado(
        nombre="Monterrey",
        landing_url="https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
        landing_link_text="ELEGIR FECHA Y HORA",
        widget_host="www.citaconsular.es",
    ),
    Consulado(
        nombre="Ciudad de México",
        landing_url="https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
        landing_link_text="ELEGIR FECHA Y HORA",
        widget_host="www.citaconsular.es",
    ),
]

# =============================
# Detección en página/frames
# =============================

def page_has_continue(ctx) -> bool:
    try:
        for sel in CONTINUE_SELECTORS:
            loc = ctx.locator(sel)
            if loc.count() and loc.first.is_visible():
                return True
    except Exception:
        pass
    return False

def page_has_no_hours(ctx) -> bool:
    try:
        for txt in NO_HOURS_TEXTS:
            if ctx.get_by_text(txt, exact=False).count():
                return True
    except Exception:
        pass
    return False

def page_has_calendar_hints(ctx) -> bool:
    try:
        for txt in CALENDAR_HINTS:
            if ctx.get_by_text(txt, exact=False).count():
                return True
    except Exception:
        pass
    return False

def extract_slots_in_ctx(ctx) -> List[Tuple[str, str]]:
    slots = []
    try:
        candidates = ctx.locator("button, .btn, [role=button]")
        n = min(candidates.count(), 300)
        for i in range(n):
            try:
                el = candidates.nth(i)
                if not el.is_visible():
                    continue
                txt = (el.inner_text() or "").strip()
                if not txt:
                    continue
                if "hueco libre" not in txt.lower():
                    continue
                m = TIME_REGEX.search(txt)
                if m:
                    slots.append((m.group(0), txt))
            except Exception:
                continue
    except Exception:
        pass
    return slots

# =============================
# Paso crítico: Click Continuar
# =============================

def click_continue_and_wait(page, timeout_ms=WIDGET_TIMEOUT_MS) -> bool:
    """Intenta pulsar 'Continue/Continuar' (también dentro de iframes) y espera cambio de vista."""
    try:
        page.on("dialog", lambda d: d.accept())
    except Exception:
        pass

    def all_contexts():
        ctxs = [page]
        try:
            ctxs += [fr for fr in page.frames]
        except Exception:
            pass
        return ctxs

    # click
    clicked = False
    for ctx in all_contexts():
        if not page_has_continue(ctx):
            continue
        try:
            target = None
            for sel in CONTINUE_SELECTORS:
                loc = ctx.locator(sel)
                if loc.count() and loc.first.is_visible():
                    target = loc.first
                    break
            if not target:
                continue
            target.scroll_into_view_if_needed()
            human_pause(0.3, 0.8)
            target.click(force=True, timeout=4000)
            clicked = True
            break
        except Exception:
            try:
                ctx.keyboard.press("Enter")
                clicked = True
                break
            except Exception:
                continue

    if not clicked:
        return False

    # esperar vista agenda / no-hours / slots
    t0 = time.time()
    while (time.time() - t0) * 1000 < timeout_ms:
        human_pause(0.4, 1.0)
        for ctx in all_contexts():
            if page_has_no_hours(ctx) or page_has_calendar_hints(ctx):
                return True
            if extract_slots_in_ctx(ctx):
                return True

        # si ya no está el botón, damos un respiro extra
        still_btn = any(page_has_continue(ctx) for ctx in all_contexts())
        if not still_btn:
            human_pause(0.5, 1.2)

    return False

# =============================
# Flujo por consulado
# =============================

def set_request_interception(context):
    if not BLOCK_IMAGES:
        return
    def route_intercept(route):
        req = route.request
        if req.resource_type in ("image", "media", "font"):
            return route.abort()
        return route.continue_()
    try:
        context.route("**/*", route_intercept)
    except Exception:
        pass

def apply_proxy(playwright):
    if not PROXY_HOST or not PROXY_PORT:
        return {}
    username = PROXY_USER
    if PROXY_SESSION:
        username = (username or "") + (PROXY_SESSION or "")
    proxy_opts = {
        "server": f"http://{PROXY_HOST}:{PROXY_PORT}",
    }
    if username or PROXY_PASS:
        proxy_opts["username"] = username or ""
        proxy_opts["password"] = PROXY_PASS or ""
    return {"proxy": proxy_opts}

def show_my_ip(page):
    if not SHOW_PUBLIC_IP:
        return
    try:
        resp = page.context.request.get(IP_ENDPOINT, timeout=15000)
        if resp.ok:
            notify(f"[INFO] IP pública: {resp.json().get('ip', 'desconocida')}")
    except Exception:
        pass

def goto_with_retries(page, url: str, wait_until="domcontentloaded", timeout_ms=LANDING_TIMEOUT_MS) -> bool:
    for i in range(GOTO_RETRIES + 1):
        try:
            if DEBUG_STEPS:
                print(f"[goto] {url}…", flush=True)
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            return True
        except PTimeout:
            if i == GOTO_RETRIES:
                return False
            human_pause(0.8, 1.5)
        except Exception:
            if i == GOTO_RETRIES:
                return False
            human_pause(0.8, 1.5)
    return False

def open_widget_from_landing(page, cons: Consulado) -> Optional["Page"]:
    """En exteriores: clic al enlace 'ELEGIR FECHA Y HORA'. Si abre nueva pestaña, la devuelve."""
    # scroll un poquito para hacer humano
    human_pause(0.6, 1.2)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=LANDING_TIMEOUT_MS)
    except Exception:
        pass

    link = None
    try:
        # buscador tolerante (mayúsculas con acentos)
        link = page.get_by_text(cons.landing_link_text, exact=False).first
        if not link.count():
            # fallback: buscar por 'Elegir fecha y hora' sin tildes
            link = page.get_by_text("Elegir fecha y hora", exact=False).first
    except Exception:
        link = None

    if not link or not link.count():
        if DEBUG_STEPS:
            print("[landing] No se encontró el enlace 'ELEGIR FECHA Y HORA'", flush=True)
        return None

    # Detectar si abrirá nueva pestaña
    new_page = None
    try:
        with page.context.expect_page(timeout=6000) as pw:
            link.scroll_into_view_if_needed()
            human_pause(0.2, 0.5)
            link.click(timeout=5000)
        new_page = pw.value
    except Exception:
        # si no abrió nueva pestaña, usamos la actual
        new_page = page

    # esperar a que cargue citaconsular
    try:
        new_page.wait_for_load_state("domcontentloaded", timeout=WIDGET_TIMEOUT_MS)
    except Exception:
        pass

    # en ocasiones abre un interstitial; comprobamos host
    try:
        if cons.widget_host not in (new_page.url or ""):
            # a veces redirige luego
            human_pause(1.0, 2.0)
    except Exception:
        pass

    return new_page

def snapshot(page, base: str, caption: str):
    try:
        img = f"{base}.png"
        html = f"{base}.html"
        page.screenshot(path=img, full_page=True)
        with open(html, "w", encoding="utf-8") as f:
            f.write(page.content() or "")
        send_photo(img, caption=caption)
        send_document(html, caption=caption.replace("captura", "HTML"))
    except Exception as e:
        print(f"[WARN] snapshot fallo: {e}", flush=True)

def revisar_consulado(play, cons: Consulado) -> Tuple[bool, List[Tuple[str, str]], Optional[str]]:
    """Devuelve (ok, slots, fecha_textual)"""
    # Navegador/contexto
    launch_opts = {"headless": True}
    launch_opts.update(apply_proxy(play))

    browser = play.chromium.launch(**launch_opts)
    ua = random.choice(USER_AGENTS)
    vw = random.randint(1200, 1440)
    vh = random.randint(800, 960)
    context = browser.new_context(
        viewport={"width": vw, "height": vh},
        user_agent=ua,
        locale="es-ES",
        extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"},
    )
    set_request_interception(context)

    page = context.new_page()
    page.set_default_timeout(20000)
    try:
        # IP pública (solo 1a vez recomendable, pero no hace daño)
        show_my_ip(page)
        # 1) Ir a Exteriores
        if not goto_with_retries(page, cons.landing_url, "domcontentloaded", LANDING_TIMEOUT_MS):
            snapshot(page, f"{cons.nombre.lower()}_landing_timeout", f"{cons.nombre}: landing timeout")
            browser.close()
            return (False, [], None)

        if DEBUG_STEPS:
            print(f"[{cons.nombre}] landing ok…", flush=True)

        # 2) Evidencia antes de parsear
        snapshot(page, f"{cons.nombre.lower()}_before_check", f"{cons.nombre}: evidencia inicial (antes de parsear)")

        # 3) Click en 'ELEGIR FECHA Y HORA' -> nueva pestaña o misma
        widget_page = open_widget_from_landing(page, cons)
        if widget_page is None:
            snapshot(page, f"{cons.nombre.lower()}_no_widget", f"{cons.nombre}: no se pudo abrir el widget")
            browser.close()
            return (False, [], None)

        # 4) Evidencia del HTML inicial del widget
        try:
            with open(f"{cons.nombre.lower()}_before_check_widget.html", "w", encoding="utf-8") as f:
                f.write(widget_page.content() or "")
            send_document(f"{cons.nombre.lower()}_before_check_widget.html", caption=f"{cons.nombre}: HTML inicial")
        except Exception:
            pass

        # 5) Click robusto en "Continuar"
        advanced = click_continue_and_wait(widget_page, timeout_ms=WIDGET_TIMEOUT_MS)
        if not advanced:
            # bloqueado en Continuar
            snapshot(widget_page, f"{cons.nombre.lower()}_no_avanzo_continuar", f"{cons.nombre}: captura en 'Continuar' (posible bloqueo)")
            browser.close()
            return (False, [], None)

        # 6) Chequeo de "no hay horas" claro
        fecha_text = None
        if page_has_no_hours(widget_page):
            # evidencia final sin huecos explícitos
            snapshot(widget_page, f"{cons.nombre.lower()}_no_final", f"{cons.nombre}: HTML final — NO")
            browser.close()
            return (True, [], fecha_text)

        # 7) Buscar huecos reales: en página y en frames
        slots = extract_slots_in_ctx(widget_page)
        if not slots:
            for fr in widget_page.frames:
                slots = extract_slots_in_ctx(fr)
                if slots:
                    break

        if slots:
            # evidencia positiva
            try:
                widget_page.screenshot(path=f"{cons.nombre.lower()}_yes.png", full_page=True)
                send_photo(f"{cons.nombre.lower()}_yes.png", caption=f"{cons.nombre}: HAY HUECOS")
            except Exception:
                pass
            browser.close()
            return (True, slots, fecha_text)

        # 8) Si no vimos texto NI huecos pero pasó "Continuar", avisamos sin falsos negativos
        snapshot(widget_page, f"{cons.nombre.lower()}_after_no_clear", f"{cons.nombre}: sin huecos claros (no se detectó 'No hay horas…')")
        browser.close()
        return (True, [], fecha_text)

    except Exception as e:
        snapshot(page, f"{cons.nombre.lower()}_fatal", f"{cons.nombre}: error inesperado")
        print("[ERROR] " + "".join(traceback.format_exc()), flush=True)
        browser.close()
        return (False, [], None)

# =============================
# Bucle principal
# =============================

def main():
    notify("🚀 Start: Launching bot…")

    # Proxy info visible (ayuda a confirmar config)
    if PROXY_HOST and PROXY_PORT:
        notify(f"[INFO] Proxy: http://{PROXY_HOST}:{PROXY_PORT}")

    with sync_playwright() as p:
        while True:
            try:
                names = ", ".join([c.nombre for c in CONSULADOS])
                if DEBUG_STEPS:
                    print(f"[INFO] Consulados: {names}", flush=True)

                for cons in CONSULADOS:
                    if DEBUG_STEPS:
                        print(f"[{cons.nombre}] goto…", flush=True)

                    ok, slots, fecha = revisar_consulado(p, cons)

                    if not ok:
                        # se envió evidencia dentro
                        continue

                    if slots:
                        primeras = ", ".join(sorted({h for h, _ in slots})[:5])
                        extra = f" ({fecha})" if fecha else ""
                        notify(f"✅ [{cons.nombre}] HAY HUECOS{extra} -> Horas: {primeras}")
                        # no dormir demasiado, pero damos margen para que el usuario entre
                        time.sleep(300)
                    else:
                        marca = time.strftime("%Y-%m-%d %H:%M:%S")
                        notify(f"[{marca}] {cons.nombre} -> sin huecos por ahora.")

                # Espera humanizada 5–7 min
                wait_time = random.randint(CHECK_MIN_SEC, CHECK_MAX_SEC)
                print(f"[INFO] Esperando {wait_time}s antes de la siguiente ronda…", flush=True)
                time.sleep(wait_time)

            except Exception:
                print("[LOOP ERROR] " + "".join(traceback.format_exc()), flush=True)
                time.sleep(30)

if __name__ == "__main__":
    main()
