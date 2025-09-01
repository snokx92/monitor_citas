# monitor_citas_multiconsulados.py
import os, sys, time, random, re, hashlib
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout
import requests

# ─────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────
@dataclass
class Config:
    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str   = os.getenv("TELEGRAM_CHAT_ID", "")

    # Intervalo base entre rondas (segundos)
    CHECK_INTERVAL_SEC: int = int(os.getenv("CHECK_INTERVAL_SEC", "60"))

    # Pausas tipo humano
    HUMAN_MIN: float = float(os.getenv("HUMAN_MIN", "0.7"))
    HUMAN_MAX: float = float(os.getenv("HUMAN_MAX", "1.5"))

    # Selectores / textos comunes
    SELECTOR_CONTINUE: str = 'button.btn.btn-success, button:has-text("Continue"), button:has-text("Continuar")'
    DIA_REGEX: str         = r"(Lunes|Martes|Miércoles|Jueves|Viernes|Sábado|Domingo).*?\b\d{4}\b"

    # Lista de consulados (Nombre|URL|modo)
    # modo: default  → botón "Continuar"
    #       cdmx_panel → click al panel de aviso
    CONSUL_URLS: str = os.getenv(
        "CONSUL_URLS",
        ",".join([
            "Monterrey|https://www.citaconsular.es/es/hosteds/widgetdefault/25b18886db70f7ec9fd6dfd1a85d1395f/|default",
            "Ciudad de México|https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/|cdmx_panel",
            "Miami|https://www.citaconsular.es/es/hosteds/widgetdefault/2533f04b1d3e818b66f175afc9c24cf63/|default",
        ])
    )

    # Capturas de depuración (1 = guarda .jpg en /tmp/)
    DEBUG_SHOT: bool = os.getenv("DEBUG_SHOT", "0") == "1"

    # Enviar SIEMPRE las capturas por Telegram (haya o no huecos)
    SEND_ALL_SHOTS: bool = os.getenv("SEND_ALL_SHOTS", "0") == "1"

cfg = Config()

# ─────────────────────────────────────────────────────────────
# Notificaciones
# ─────────────────────────────────────────────────────────────
def notify(msg: str):
    print(msg, flush=True)
    if cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": cfg.TELEGRAM_CHAT_ID, "text": msg},
                timeout=15,
            )
        except Exception as e:
            print(f"[WARN] Telegram fallo: {e}", file=sys.stderr)

def send_photo(path: str, caption: str = ""):
    if not (cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID):
        print("[WARN] Telegram no configurado; no se puede enviar foto.")
        return
    try:
        with open(path, "rb") as f:
            url = f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendPhoto"
            data = {"chat_id": cfg.TELEGRAM_CHAT_ID, "caption": caption}
            files = {"photo": (os.path.basename(path), f, "image/jpeg")}
            requests.post(url, data=data, files=files, timeout=30)
    except Exception as e:
        print(f"[WARN] Falló send_photo: {e}", file=sys.stderr)

# ─────────────────────────────────────────────────────────────
# Anti-detección
# ─────────────────────────────────────────────────────────────
USER_AGENTS = [
    # Desktop
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    # Mobile
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
]
def human_pause():
    time.sleep(random.uniform(cfg.HUMAN_MIN, cfg.HUMAN_MAX))

# ─────────────────────────────────────────────────────────────
# Utilidades de parsing / señales
# ─────────────────────────────────────────────────────────────
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")

# Mensajes “no hay” (solo si el elemento es VISIBLE)
NO_CITAS_PATTERNS = [
    "No hay horas disponibles",
    "No hay citas disponibles",
    "No hay disponibilidad",
    "Inténtelo de nuevo dentro de unos días",
]

def visible_text(page) -> str:
    try:
        return (page.inner_text("body") or "")
    except Exception:
        return ""

def find_date_text(page) -> Optional[str]:
    try:
        content = (page.content() or "")
    except Exception:
        return None
    m = re.search(cfg.DIA_REGEX, content, re.IGNORECASE | re.DOTALL)
    return m.group(0) if m else None

def page_has_no_citas_visible(page) -> bool:
    """
    True si encuentra texto de NO_CITAS_PATTERNS visible en página o iframes.
    """
    try:
        for pat in NO_CITAS_PATTERNS:
            loc = page.locator(f"text=/{re.escape(pat)}/i")
            n = loc.count()
            for i in range(min(n, 10)):
                try:
                    if loc.nth(i).is_visible():
                        return True
                except Exception:
                    continue
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            for pat in NO_CITAS_PATTERNS:
                try:
                    loc = fr.locator(f"text=/{re.escape(pat)}/i")
                    n = loc.count()
                    for i in range(min(n, 10)):
                        try:
                            if loc.nth(i).is_visible():
                                return True
                        except Exception:
                            continue
                except Exception:
                    continue
    except Exception:
        pass
    return False

def find_time_nodes_anywhere(page) -> List[str]:
    """
    Devuelve HH:MM visibles en cualquier nodo de la página o sus iframes.
    """
    horas = set()

    try:
        times = page.locator(r"text=/\b([01]?\d|2[0-3]):[0-5]\d\b/")
        n = times.count()
        for i in range(min(n, 500)):
            try:
                node = times.nth(i)
                if not node.is_visible():
                    continue
                txt = (node.inner_text() or "").strip()
                m = TIME_RE.search(txt)
                if m:
                    horas.add(m.group(0))
            except Exception:
                continue
    except Exception:
        pass

    try:
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            times = fr.locator(r"text=/\b([01]?\d|2[0-3]):[0-5]\d\b/")
            n = times.count()
            for i in range(min(n, 500)):
                try:
                    node = times.nth(i)
                    if not node.is_visible():
                        continue
                    txt = (node.inner_text() or "").strip()
                    m = TIME_RE.search(txt)
                    if m:
                        horas.add(m.group(0))
                except Exception:
                    continue
    except Exception:
        pass

    return sorted(horas)

def wait_calendar_ready(page, timeout_ms: int = 20000) -> str:
    """
    Espera hasta ver HH:MM visibles (página o iframes).
    Si no aparecen, intenta reconocer “no hay” visible.
    Devuelve: 'hours', 'no_citas' o 'timeout'.
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        if find_time_nodes_anywhere(page):
            return "hours"
        if page_has_no_citas_visible(page):
            return "no_citas"
        time.sleep(0.25)
    return "timeout"

# ─────────────────────────────────────────────────────────────
# Helpers ENV
# ─────────────────────────────────────────────────────────────
def parse_consul_list(env_val: str) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    for item in [s.strip() for s in env_val.split(",") if s.strip()]:
        parts = [p.strip() for p in item.split("|")]
        if len(parts) >= 2:
            name, url = parts[0], parts[1]
            mode = parts[2].lower() if len(parts) >= 3 else "default"
            out.append((name, url, mode))
    return out

# ─────────────────────────────────────────────────────────────
# Navegación (incluye variante CDMX)
# ─────────────────────────────────────────────────────────────
def revisar_un_consulado(name: str, url: str, modo: str = "default", headless: bool = True
                         ) -> Tuple[bool, List[Tuple[str, str]], Optional[str], Optional[str]]:
    """
    Retorna: (hay_huecos, slots[(hora,texto)], fecha_visible, screenshot_path)
    """
    shot_path = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)

        # UA y viewport aleatorios
        ua = random.choice(USER_AGENTS)
        vw = random.randint(1200, 1440)
        vh = random.randint(800, 960)

        context = browser.new_context(
            viewport={"width": vw, "height": vh},
            user_agent=ua,
            locale="es-ES",
            extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"},
        )

        # Aceptar "Welcome / Bienvenido"
        def on_dialog(dialog):
            try:
                dialog.accept()
            except Exception:
                pass
        context.on("dialog", on_dialog)

        page = context.new_page()
        page.set_default_timeout(20000)

        page.goto(url, wait_until="domcontentloaded")
        human_pause()

        if modo == "default":
            try:
                page.wait_for_selector(cfg.SELECTOR_CONTINUE, timeout=8000)
                page.click(cfg.SELECTOR_CONTINUE, force=True)
                human_pause()
            except PTimeout:
                pass
        elif modo == "cdmx_panel":
            try:
                panel = page.locator(
                    "text=/PRESENTACION|LEY MEMORIA|CONTINUAR SUPONE/i, .panel, .well, .panel-body, .card"
                )
                if panel.count() > 0:
                    panel.first.click(force=True)
                    human_pause()
                    if not find_time_nodes_anywhere(page):
                        panel.first.click(force=True)
                        human_pause()
            except Exception:
                pass

        # Espera a ver horas O un “no hay” visible
        status = wait_calendar_ready(page, timeout_ms=20000)

        if status == "hours":
            horas = find_time_nodes_anywhere(page)
            slots = [(h, f"Hora {h}") for h in horas]
            fecha = find_date_text(page)

            # Capturas
            if cfg.DEBUG_SHOT:
                try:
                    page.screenshot(path=f"/tmp/debug_{name.replace(' ', '_').lower()}_hours.jpg",
                                    type="jpeg", quality=70, full_page=True)
                except Exception:
                    pass
            shot_path = f"/tmp/{name.replace(' ', '_').lower()}_citas.jpg"
            try:
                page.screenshot(path=shot_path, type="jpeg", quality=70, full_page=True)
            except Exception:
                shot_path = None

            # Enviar foto siempre si está habilitado, o solo cuando hay huecos (si no)
            if shot_path and os.path.exists(shot_path) and (cfg.SEND_ALL_SHOTS or slots):
                caption = f"📸 Vista de {name} (horas detectadas: {', '.join(horas)})"
                send_photo(shot_path, caption)

            browser.close()
            return (True, slots, fecha, shot_path)

        if status == "no_citas":
            fecha = find_date_text(page)
            # Captura y envío si aplica
            no_shot = f"/tmp/{name.replace(' ', '_').lower()}_no_citas.jpg"
            try:
                page.screenshot(path=no_shot, type="jpeg", quality=70, full_page=True)
            except Exception:
                no_shot = None
            if cfg.SEND_ALL_SHOTS and no_shot and os.path.exists(no_shot):
                send_photo(no_shot, f"📸 {name}: sin huecos (señal visible de 'no hay').")
            browser.close()
            return (False, [], fecha, None)

        # status == "timeout"
        fecha = find_date_text(page)
        horas = find_time_nodes_anywhere(page)
        slots = [(h, f"Hora {h}") for h in horas]

        timeout_shot = f"/tmp/{name.replace(' ', '_').lower()}_timeout.jpg"
        try:
            page.screenshot(path=timeout_shot, type="jpeg", quality=60, full_page=True)
        except Exception:
            timeout_shot = None
        if cfg.SEND_ALL_SHOTS and timeout_shot and os.path.exists(timeout_shot):
            send_photo(timeout_shot, f"⏳ {name}: timeout. Horas detectadas: {', '.join(horas) or 'ninguna'}")

        browser.close()
        return (bool(slots), slots, fecha, None)

# ─────────────────────────────────────────────────────────────
# Anti-spam (misma disponibilidad → no repetir)
# ─────────────────────────────────────────────────────────────
def slots_signature(slots: List[Tuple[str, str]]) -> str:
    horas = sorted({h for h, _ in slots})
    return hashlib.sha256(",".join(horas).encode("utf-8")).hexdigest()

# ─────────────────────────────────────────────────────────────
def main():
    if os.getenv("FORCE_TEST") == "1":
        notify("🚀 Test OK: bot listo para enviar alertas.")
        print("[TEST] Notificación de prueba enviada.")
        time.sleep(3)
        sys.exit(0)

    consulados = parse_consul_list(cfg.CONSUL_URLS)
    print("[INFO] Consulados configurados:",
          ", ".join(f"{n}({m})" for n,_,m in consulados), flush=True)

    if not consulados:
        print("[ERROR] CONSUL_URLS vacío o mal formateado.", flush=True)
        sys.exit(1)

    last_sig: Dict[str, str] = {}

    while True:
        try:
            for (name, url, modo) in consulados:
                ok, slots, fecha, shot = revisar_un_consulado(name, url, modo, headless=True)

                if ok and slots:
                    sig = slots_signature(slots)
                    if last_sig.get(name) == sig:
                        continue  # mismas horas → evita spam
                    last_sig[name] = sig

                    primeras = ", ".join(sorted({h for h, _ in slots})[:6])
                    suf_fecha = f" ({fecha})" if fecha else ""
                    caption = f"✅ ¡HAY HUECOS en {name}!{suf_fecha}\nHoras: {primeras}\nEntra ya: {url}"
                    notify(caption)

                    # Enviar la captura de “citas” si existe
                    if shot and os.path.exists(shot):
                        send_photo(shot, caption)

                    time.sleep(60)  # antispam tras encontrar huecos
                else:
                    marca = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(f"[{marca}] {name} → Sin huecos reales por ahora.", flush=True)

            # Espera aleatoria entre rondas
            min_wait = max(30, cfg.CHECK_INTERVAL_SEC - 15)
            max_wait = cfg.CHECK_INTERVAL_SEC + 30
            wait_time = random.randint(min_wait, max_wait)
            print(f"[INFO] Esperando {wait_time}s antes de la siguiente ronda...", flush=True)
            time.sleep(wait_time)

        except Exception as e:
            print(f"[ERROR] {e}", flush=True)
            time.sleep(120)

# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    headed = len(sys.argv) > 1 and sys.argv[1].lower().startswith("head")
    if headed:
        print("Headed demo de CDMX…")
        print(revisar_un_consulado(
            "Ciudad de México",
            "https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/",
            "cdmx_panel",
            headless=False
        ))
    else:
        main()
