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

    # Intervalo base entre rondas completas (segundos)
    CHECK_INTERVAL_SEC: int = int(os.getenv("CHECK_INTERVAL_SEC", "60"))

    # Pausas tipo humano
    HUMAN_MIN: float = float(os.getenv("HUMAN_MIN", "0.7"))
    HUMAN_MAX: float = float(os.getenv("HUMAN_MAX", "1.5"))

    # Selectores / textos comunes
    SELECTOR_CONTINUE: str = 'button.btn.btn-success, button:has-text("Continue"), button:has-text("Continuar")'
    TEXT_NO_CITAS: str     = "No hay horas disponibles"  # se usa como pista, pero ahora tenemos patrones más amplios
    BUTTON_CANDIDATES: str = "button, .btn, [role=button], a"
    DIA_REGEX: str         = r"(Lunes|Martes|Miércoles|Jueves|Viernes|Sábado|Domingo).*?\b\d{4}\b"

    # Lista de consulados por ENV
    # Formatos por elemento:
    #   Nombre|URL                → modo "default"
    #   Nombre|URL|cdmx_panel     → modo CDMX (click al cuadro)
    CONSUL_URLS: str = os.getenv(
        "CONSUL_URLS",
        ",".join([
            # Monterrey (default)
            "Monterrey|https://www.citaconsular.es/es/hosteds/widgetdefault/25b18886db70f7ec9fd6dfd1a85d1395f/|default",
            # Ciudad de México (panel)
            "Ciudad de México|https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/|cdmx_panel",
            # Miami (default)
            "Miami|https://www.citaconsular.es/es/hosteds/widgetdefault/2533f04b1d3e818b66f175afc9c24cf63/|default",
        ])
    )

    # Capturas de depuración (1 = activadas)
    DEBUG_SHOT: bool = os.getenv("DEBUG_SHOT", "0") == "1"

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
    # Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    # macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    # Móviles
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
]
def human_pause():
    time.sleep(random.uniform(cfg.HUMAN_MIN, cfg.HUMAN_MAX))

# ─────────────────────────────────────────────────────────────
# Parsers y señales
# ─────────────────────────────────────────────────────────────
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")

# Mensajes inequívocos de “no hay” (insensible a mayúsculas/acentos)
NO_CITAS_PATTERNS = [
    r"No hay horas disponibles",
    r"No hay citas disponibles",
    r"No hay disponibilidad",
    r"Inténtelo de nuevo dentro de unos días",
]

def find_date_text(page) -> Optional[str]:
    try:
        content = (page.content() or "").strip()
    except Exception:
        return None
    m = re.search(cfg.DIA_REGEX, content, re.IGNORECASE | re.DOTALL)
    return m.group(0) if m else None

def page_has_no_citas(page) -> bool:
    """True si detecta algún mensaje inequívoco de 'no hay citas' en la página."""
    try:
        html = (page.content() or "")
        for pat in NO_CITAS_PATTERNS:
            if re.search(pat, html, flags=re.IGNORECASE):
                return True
    except Exception:
        pass
    return False

def wait_calendar_ready(page, timeout_ms: int = 8000) -> str:
    """
    Espera hasta que vea horas (HH:MM) o un mensaje 'no hay citas'.
    Revisa también iframes. Devuelve: 'hours', 'no_citas' o 'timeout'.
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        try:
            # Documento principal
            body = (page.inner_text("body") or "")
            if TIME_RE.search(body):
                return "hours"
            if page_has_no_citas(page):
                return "no_citas"
            # Iframes
            for fr in page.frames:
                if fr == page.main_frame:
                    continue
                try:
                    fbody = (fr.inner_text("body") or "")
                    if TIME_RE.search(fbody):
                        return "hours"
                    if page_has_no_citas(fr):
                        return "no_citas"
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(0.25)
    return "timeout"

def extract_real_slots(page) -> List[Tuple[str, str]]:
    """
    Devuelve lista (hora, texto_boton). Tolerante:
    - Busca hora en texto, aria-label o title.
    - Si no encuentra botones, barre el DOM y deduce horas únicas.
    """
    slots: List[Tuple[str, str]] = []

    selectors = [
        cfg.BUTTON_CANDIDATES,
        "[class*='hour'], [class*='time'], [id*='hour'], [id*='time']",
        "[aria-label], [title]",
    ]
    try:
        candidates = page.locator(", ".join(selectors))
        count = candidates.count()
    except Exception:
        count = 0

    for i in range(min(count, 600)):
        try:
            el = candidates.nth(i)
            if not el.is_visible():
                continue
            text  = (el.inner_text() or "").strip()
            aria  = (el.get_attribute("aria-label") or "").strip()
            title = (el.get_attribute("title") or "").strip()
            raw = " ".join([text, aria, title])
            m = TIME_RE.search(raw)
            if m:
                hora = m.group(0)
                label = text or aria or title or hora
                slots.append((hora, label))
        except Exception:
            continue

    if not slots:
        try:
            html = (page.content() or "")
            horas = sorted(set(re.findall(r"\b([01]?\d|2[0-3]):[0-5]\d\b", html)))
            for h in horas:
                slots.append((h, f"Hora {h}"))
        except Exception:
            pass

    # Deduplicar por hora
    seen = {}
    for h, t in slots:
        if h not in seen:
            seen[h] = t
    return sorted([(h, seen[h]) for h in seen.keys()])

# ─────────────────────────────────────────────────────────────
# Helpers ENV
# ─────────────────────────────────────────────────────────────
def parse_consul_list(env_val: str) -> List[Tuple[str, str, str]]:
    """
    Devuelve lista (nombre, url, modo). Modo por defecto: 'default'.
    Separa consulados por coma, campos por '|'.
    """
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
    Retorna: (hay_huecos, slots, fecha_visible, screenshot_path)
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

        # Aceptar popups "Welcome / Bienvenido"
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
            # Botón "Continuar"
            try:
                page.wait_for_selector(cfg.SELECTOR_CONTINUE, timeout=8000)
                page.click(cfg.SELECTOR_CONTINUE, force=True)
                human_pause()
            except PTimeout:
                pass
        elif modo == "cdmx_panel":
            # CDMX: click al cuadro grande del aviso
            try:
                panel = page.locator("text=/PRESENTACION|LEY MEMORIA|CONTINUAR SUPONE/i, .panel, .well, .panel-body, .card")
                if panel.count() > 0:
                    panel.first.click(force=True)
                    human_pause()
                    # Reintento si aún no aparece nada
                    if not page.locator("text=/\\b([01]?\\d|2[0-3]):[0-5]\\d\\b/").count():
                        panel.first.click(force=True)
                        human_pause()
            except Exception:
                pass

        # Espera a que se vea algo concluyente: horas o mensaje "no hay"
        status = wait_calendar_ready(page, timeout_ms=8000)
        if status == "no_citas":
            if cfg.DEBUG_SHOT:
                try:
                    page.screenshot(path=f"/tmp/{name.replace(' ', '_').lower()}_no_citas.jpg",
                                    type="jpeg", quality=70, full_page=True)
                except Exception:
                    pass
            fecha = find_date_text(page)
            browser.close()
            return (False, [], fecha, None)
        # 'hours' → seguimos; 'timeout' → igual probamos extractor (por si tarda más)

        # Buscar huecos
        slots = extract_real_slots(page)
        fecha = find_date_text(page)

        # Probar iframes si aún no hay
        if not slots:
            for fr in page.frames:
                if fr == page.main_frame:
                    continue
                try:
                    slots = extract_real_slots(fr)
                    if not fecha:
                        fecha = find_date_text(fr)
                    if slots:
                        break
                except Exception:
                    continue

        # Captura debug si está activada
        if cfg.DEBUG_SHOT:
            try:
                page.screenshot(path=f"/tmp/debug_{name.replace(' ', '_').lower()}.jpg",
                                type="jpeg", quality=70, full_page=True)
            except Exception:
                pass

        if slots:
            shot_path = f"/tmp/{name.replace(' ', '_').lower()}_citas.jpg"
            try:
                page.screenshot(path=shot_path, type="jpeg", quality=70, full_page=True)
            except Exception:
                shot_path = None
            browser.close()
            return (True, slots, fecha, shot_path)

        # Sin huecos: captura liviana opcional
        try:
            page.screenshot(path=f"/tmp/{name.replace(' ', '_').lower()}_sin_huecos.jpg",
                            type="jpeg", quality=60, full_page=True)
        except Exception:
            pass
        browser.close()
        return (False, [], fecha, None)

# ─────────────────────────────────────────────────────────────
# Anti-spam (misma disponibilidad → no repetir)
# ─────────────────────────────────────────────────────────────
def slots_signature(slots: List[Tuple[str, str]]) -> str:
    horas = sorted({h for h, _ in slots})
    return hashlib.sha256(",".join(horas).encode("utf-8")).hexdigest()

# ─────────────────────────────────────────────────────────────
def main():
    # Mensaje de prueba
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

    last_sig: Dict[str, str] = {}  # nombre -> firma última disponibilidad

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
                    if shot and os.path.exists(shot):
                        send_photo(shot, caption)
                    time.sleep(60)  # breve antispam tras encontrar huecos
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
