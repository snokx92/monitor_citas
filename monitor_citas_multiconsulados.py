# -*- coding: utf-8 -*-
"""
Monitor de citas (Bookitit / citaconsular) con:
- Entrada por Exteriores (Enlace: ELEGIR FECHA Y HORA)
- Simulación humana (UA/viewport/scroll/mouse/pausas)
- Proxy con rotación de sesión (DataImpulse, user:pass + -session-XXXXX)
- Evidencias a Telegram (PNG + HTML)
- Rotación automática ante "página vacía" repetida
- Intervalos humanos (5-7 min por defecto)
"""

import os, sys, time, random, re, io, zipfile, json
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, List

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout, Error as PWError

# -----------------------------
#   Configuración por entorno
# -----------------------------
@dataclass
class Config:
    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Proxy (DataImpulse)
    PROXY_HOST: str = os.getenv("PROXY_HOST", "")
    PROXY_PORT: str = os.getenv("PROXY_PORT", "")
    PROXY_USER: str = os.getenv("PROXY_USER", "")
    PROXY_PASS: str = os.getenv("PROXY_PASS", "")
    PROXY_SESSION_IN_USER: bool = os.getenv("PROXY_SESSION_IN_USER", "1") == "1"
    SHOW_PUBLIC_IP: bool = os.getenv("SHOW_PUBLIC_IP", "1") == "1"

    # Comportamiento / anti-bloqueo
    PROOF: bool = os.getenv("PROOF", "1") == "1"
    BLOCK_IMAGES: bool = os.getenv("BLOCK_IMAGES", "0") == "1"
    DEBUG_STEPS: bool = os.getenv("DEBUG_STEPS", "0") == "1"

    # Tiempo de espera (ms)
    LANDING_TIMEOUT_MS: int = int(os.getenv("LANDING_TIMEOUT_MS", "60000"))
    WIDGET_TIMEOUT_MS: int = int(os.getenv("WIDGET_TIMEOUT_MS", "45000"))
    GOTO_RETRIES: int = int(os.getenv("GOTO_RETRIES", "2"))

    # Rotación por página en blanco
    ROTATE_AFTER_BLANK: int = int(os.getenv("ROTATE_AFTER_BLANK", "2"))
    ROTATE_COOLDOWN_SEC: int = int(os.getenv("ROTATE_COOLDOWN_SEC", "20"))
    ROTATE_URL: str = os.getenv("ROTATE_URL", "")  # opcional (no necesario con DataImpulse session)

    # Intervalo entre rondas
    CHECK_INTERVAL_SEC_env = os.getenv("CHECK_INTERVAL_SEC", "").strip()

cfg = Config()

def notify(text: str):
    print(text, flush=True)
    if not (cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": cfg.TELEGRAM_CHAT_ID, "text": text},
            timeout=20,
        )
    except Exception as e:
        print(f"[WARN] Telegram error: {e}", flush=True)

def send_photo(path: str, caption: str = ""):
    if not (cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID):
        return
    try:
        with open(path, "rb") as f:
            files = {"photo": f}
            data = {"chat_id": cfg.TELEGRAM_CHAT_ID, "caption": caption}
            requests.post(
                f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendPhoto",
                data=data, files=files, timeout=30,
            )
    except Exception as e:
        print(f"[WARN] send_photo fallo: {e}", flush=True)

def send_document(path: str, caption: str = ""):
    if not (cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID):
        return
    try:
        with open(path, "rb") as f:
            files = {"document": f}
            data = {"chat_id": cfg.TELEGRAM_CHAT_ID, "caption": caption}
            requests.post(
                f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendDocument",
                data=data, files=files, timeout=30,
            )
    except Exception as e:
        print(f"[WARN] send_document fallo: {e}", flush=True)

# ---------------------------------
#   Datos de consulados/entradas
# ---------------------------------
CONSULADOS: Dict[str, Dict] = {
    # Monterrey
    "Monterrey": {
        "landing": "https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
        "widget_hint": "citaconsular.es",  # validación de que llegamos al widget
        "needs_panel_click": False,        # desde widget, normal
    },
    # Ciudad de México
    "Ciudad de México": {
        "landing": "https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
        "widget_hint": "citaconsular.es",
        # CDMX a veces requiere click sobre el panel/área para desplegar horas:
        "needs_panel_click": True,
    },
}

# user-agents frescos
USER_AGENTS = [
    # Windows: Chrome, Edge, Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.142 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    # macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.142 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    # móviles (por si cae en vista móvil)
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
]

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")  # 0:00–23:59

def rnd(a: float, b: float) -> float:
    return random.uniform(a, b)

def human_sleep(a: float, b: float):
    time.sleep(rnd(a, b))

def random_session_id() -> str:
    return str(random.randint(100000, 999999))

def build_proxy() -> Optional[Dict]:
    """Devuelve dict proxy para Playwright o None."""
    if not (cfg.PROXY_HOST and cfg.PROXY_PORT and cfg.PROXY_USER and cfg.PROXY_PASS):
        return None
    user = cfg.PROXY_USER
    if cfg.PROXY_SESSION_IN_USER:
        user = f"{cfg.PROXY_USER}-session-{random_session_id()}"
    return {
        "server": f"http://{cfg.PROXY_HOST}:{cfg.PROXY_PORT}",
        "username": user,
        "password": cfg.PROXY_PASS,
    }

def get_public_ip_through_requests(proxy: Optional[Dict]) -> Optional[str]:
    try:
        proxies = None
        if proxy:
            # form requests format
            auth = ""
            if proxy.get("username") and proxy.get("password"):
                auth = f"{proxy['username']}:{proxy['password']}@"
            proxies = {
                "http": f"http://{auth}{cfg.PROXY_HOST}:{cfg.PROXY_PORT}",
                "https": f"http://{auth}{cfg.PROXY_HOST}:{cfg.PROXY_PORT}",
            }
        r = requests.get("http://api.ipify.org?format=json", proxies=proxies, timeout=15)
        return r.json().get("ip")
    except Exception:
        return None

def page_blank_like(page_html: str) -> bool:
    """Heurística de 'página vacía / bloqueada'."""
    if not page_html:
        return True
    text = re.sub(r"\s+", " ", page_html).strip()
    # muchos bloques devuelven un body mínimo (30-80 chars) o nada relevante
    return len(text) < 200

def save_evidence(page, basename: str, note: str):
    """Guarda PNG + HTML y los envía a Telegram (si PROOF=1)."""
    try:
        png = f"{basename}.png"
        html = f"{basename}.html"
        page.screenshot(path=png, full_page=True)
        with open(html, "w", encoding="utf-8") as f:
            f.write(page.content())
        if cfg.PROOF:
            send_photo(png, caption=note)
            send_document(html, caption=note)
    except Exception as e:
        print(f"[WARN] save_evidence fallo: {e}", flush=True)

def simulate_human_actions(page):
    """Movimientos y scrolls suaves."""
    try:
        # viewport aleatorio
        vw = random.randint(1200, 1440)
        vh = random.randint(800, 960)
        page.set_viewport_size({"width": vw, "height": vh})
        # movimiento de mouse random
        x1, y1 = random.randint(50, vw-50), random.randint(50, vh-50)
        page.mouse.move(x1, y1, steps=random.randint(8, 18))
        human_sleep(0.2, 0.6)
        # scroll suave
        for _ in range(random.randint(1, 3)):
            page.mouse.wheel(0, random.randint(250, 600))
            human_sleep(0.2, 0.5)
        if random.random() < 0.4:
            page.mouse.wheel(0, -random.randint(150, 350))
            human_sleep(0.2, 0.4)
    except Exception:
        pass

def open_context(pw, proxy_cfg: Optional[Dict]):
    """Crea browser/context con UA/headers/imagenes on|off."""
    ua = random.choice(USER_AGENTS)
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=ua,
        locale="es-ES",
        viewport={"width": random.randint(1200, 1440), "height": random.randint(800, 960)},
        extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.7"},
        proxy=proxy_cfg,
    )
    if cfg.BLOCK_IMAGES:
        context.route(
            "**/*",
            lambda route: route.abort() if route.request.resource_type == "image" else route.continue_(),
        )
    # aceptar diálogos JS
    context.on("dialog", lambda d: (d.accept()))
    return browser, context

def goto_with_retries(page, url: str, timeout_ms: int):
    for i in range(cfg.GOTO_RETRIES + 1):
        try:
            if cfg.DEBUG_STEPS:
                print(f"[goto] {url} (try {i+1})", flush=True)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            human_sleep(0.6, 1.1)
            return True
        except (PTimeout, PWError) as e:
            print(f"[WARN] goto error: {e}", flush=True)
            human_sleep(1.0, 2.0)
    return False

def click_if_exists(page, selectors: List[str], timeout_ms: int = 4000) -> bool:
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=timeout_ms)
            page.locator(sel).first.click()
            human_sleep(0.4, 0.9)
            return True
        except Exception:
            continue
    return False

def find_slots_in_frame(frame) -> List[Tuple[str, str]]:
    slots = []
    try:
        buttons = frame.locator("button, .btn, [role=button]")
        count = min(buttons.count(), 300)
        for i in range(count):
            el = buttons.nth(i)
            if not el.is_visible():
                continue
            tx = el.inner_text().strip()
            if "hueco libre" not in tx.lower():
                continue
            m = TIME_RE.search(tx)
            if m:
                slots.append((m.group(0), tx))
    except Exception:
        pass
    return slots

def revisar_consulado(pw, cons_name: str, state: Dict) -> Tuple[bool, List[Tuple[str, str]], Optional[str], bool]:
    """
    Retorna: (hay_huecos, lista_slots, fecha_texto, blank_detected)
    blank_detected=True si creemos que hubo un bloqueo/página vacía.
    """
    data = CONSULADOS[cons_name]
    proxy_cfg = build_proxy()
    browser, context = open_context(pw, proxy_cfg)
    page = context.new_page()
    page.set_default_timeout(20000)

    # evidencia básica de IP
    if cfg.SHOW_PUBLIC_IP:
        ip = get_public_ip_through_requests(proxy_cfg)
        if ip:
            print(f"[INFO] IP pública: {ip}", flush=True)
            notify(f"[INFO] {cons_name} IP: {ip}")

    blank_detected = False
    try:
        # 1) Exteriores
        if not goto_with_retries(page, data["landing"], cfg.LANDING_TIMEOUT_MS):
            raise RuntimeError("Timeout en página de Exteriores")

        simulate_human_actions(page)

        # Click “ELEGIR FECHA Y HORA” (mayúsculas/versión acentos, varias variantes)
        clicked = click_if_exists(
            page,
            [
                "text=/ELEGIR FECHA Y HORA/i",
                "a:has-text('ELEGIR FECHA Y HORA')",
                "text=/ELEGIR FECHA/i",
            ],
            timeout_ms=6000,
        )
        if not clicked:
            # fallback: buscar anchors con texto “hora”
            anchors = page.locator("a")
            n = min(anchors.count(), 200)
            for i in range(n):
                tx = anchors.nth(i).inner_text().strip().lower()
                if "hora" in tx and "elegir" in tx:
                    anchors.nth(i).click()
                    clicked = True
                    break
        human_sleep(0.5, 1.2)
        if not clicked:
            # evidencia y salida
            save_evidence(page, f"{cons_name.lower().replace(' ', '_')}_exteriores", f"{cons_name}: no encontré enlace ELEGIR FECHA Y HORA")
            return (False, [], None, True)

        # 2) Esperar a que redirija al widget (citaconsular)
        # damos tiempo a que abra nueva pestaña o redirija
        human_sleep(1.0, 2.0)
        # Si abrió nueva page, cambiar a ella
        if len(context.pages) > 1:
            page = context.pages[-1]

        # esperamos que la URL contenga el host del widget
        ok = False
        for _ in range(12):  # ~12 * 1s = 12s
            if data["widget_hint"] in (page.url or ""):
                ok = True
                break
            human_sleep(1.0, 1.2)
        if not ok:
            # a veces redirige con retardo, forzamos wait
            goto_with_retries(page, page.url, cfg.WIDGET_TIMEOUT_MS)

        simulate_human_actions(page)

        # 3) Interacciones del widget
        # 3a) Botón continuar
        click_if_exists(page, [
            "button:has-text('Continuar')",
            "button:has-text('Continue')",
            "button.btn.btn-success",
        ], timeout_ms=5000)

        human_sleep(0.6, 1.0)

        # 3b) CDMX a veces requiere click en el panel para mostrar citas
        if data.get("needs_panel_click", False):
            # tratamos de clicar contenedor grande si existen
            click_if_exists(page, [
                "div:has-text('Cambiar de día')",
                "div.calendar, div.panel",    # heurísticos
                "div[class*=panel]",
            ], timeout_ms=3000)
            human_sleep(0.4, 0.8)

        # 4) detección de “no hay horas”
        no_slots_texts = [
            "No hay horas disponibles",
            "Sin horas disponibles",
        ]
        found_no_slots = False
        for tx in no_slots_texts:
            try:
                page.get_by_text(tx, exact=False).wait_for(timeout=2500)
                found_no_slots = True
                break
            except Exception:
                pass

        # 5) buscar slots con “Hueco libre” + hora
        slots: List[Tuple[str, str]] = []
        # buscar en main frame
        slots = find_slots_in_frame(page)
        # buscar en iframes
        if not slots:
            for fr in page.frames:
                if fr == page.main_frame:
                    continue
                s2 = find_slots_in_frame(fr)
                if s2:
                    slots = s2
                    break

        # 6) Heurística de bloqueo/página vacía
        html = page.content()
        if page_blank_like(html):
            blank_detected = True
            save_evidence(page, f"{cons_name.lower().replace(' ', '_')}_blank", f"{cons_name}: HTML vacío (posible bloqueo)")
        elif cfg.PROOF:
            # guardar una evidencia de navegación correcta cada X rondas aleatoriamente (bajo ruido)
            if random.random() < 0.15:
                save_evidence(page, f"{cons_name.lower().replace(' ', '_')}_ok", f"{cons_name}: widget cargado")

        # 7) Resultado
        if slots:
            # ordenar horas (por HH:MM)
            horas = sorted({h for h, _ in slots})
            return (True, [(h, t) for h, t in slots if h in horas], None, False)
        if found_no_slots:
            return (False, [], None, False)

        # si no vimos NO HAY y tampoco slots, devolvemos “sin huecos” pero con posible bloqueo si html era vacío
        return (False, [], None, blank_detected)

    finally:
        try:
            context.close()
            browser.close()
        except Exception:
            pass

# -----------------------------
#         LOOP principal
# -----------------------------
def main():
    # intervalos humanos: si no fijas CHECK_INTERVAL_SEC, usamos [300..420]s
    if cfg.CHECK_INTERVAL_SEC_env:
        base_wait = max(60, int(cfg.CHECK_INTERVAL_SEC_env))
        rnd_wait_fn = lambda: base_wait
    else:
        rnd_wait_fn = lambda: random.randint(300, 420)  # 5-7 min

    blank_counts: Dict[str, int] = {k: 0 for k in CONSULADOS.keys()}

    notify("[start] Launching bot…")
    print("[start] Launching bot…", flush=True)

    while True:
        try:
            with sync_playwright() as pw:
                for cons in CONSULADOS.keys():
                    print(f"[INFO] Consultado: {cons}", flush=True)
                    ok, slots, fecha, blanked = revisar_consulado(pw, cons, blank_counts)

                    marca = time.strftime("%Y-%m-%d %H:%M:%S")
                    if blanked:
                        blank_counts[cons] += 1
                        notify(f"⚠️ {cons}: página vacía tras reintentos (bloqueo probable).")
                        print(f"[{marca}] {cons} -> blank_count={blank_counts[cons]}", flush=True)
                    else:
                        blank_counts[cons] = 0

                    if ok and slots:
                        horas = ", ".join(sorted({h for h, _ in slots})[:6])
                        notify(f"✅ [{marca}] {cons} → ¡HUECOS! Horas: {horas}")
                        # pausa 5 min para que te dé tiempo a entrar manualmente
                        time.sleep(300)
                    else:
                        notify(f"[{marca}] {cons} → sin huecos por ahora.")

                    # ¿rotación de IP por bloqueos repetidos?
                    if blank_counts[cons] >= cfg.ROTATE_AFTER_BLANK:
                        blank_counts[cons] = 0  # reseteamos para evitar bucles
                        if cfg.ROTATE_URL:
                            try:
                                requests.get(cfg.ROTATE_URL, timeout=10)
                                notify(f"♻️ Roté IP vía endpoint para {cons}.")
                            except Exception:
                                notify(f"♻️ Rotación solicitada (endpoint falló) para {cons}.")
                        else:
                            # con DataImpulse + session-id en usuario, la “rotación” es crear nuevo contexto -> nueva sesión
                            notify(f"♻️ Nueva sesión de proxy (session-id) para {cons}.")
                        time.sleep(cfg.ROTATE_COOLDOWN_SEC)

            # espera humana entre rondas
            wait_s = rnd_wait_fn()
            print(f"[INFO] Esperando {wait_s}s antes de la siguiente ronda…", flush=True)
            notify(f"[INFO] Esperando {wait_s}s antes de la siguiente ronda…")
            time.sleep(wait_s)

        except KeyboardInterrupt:
            print("Bye!", flush=True)
            break
        except Exception as e:
            print(f"[ERROR] Loop: {e}", flush=True)
            time.sleep(60)

if __name__ == "__main__":
    main()
