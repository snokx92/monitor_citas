# monitor_citas_multiconsulados.py (multi-consulados con lista por ENV)
import os, sys, time, random, re, hashlib
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout
import requests

# ────────────────────────────────────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────────────────────────────────────
@dataclass
class Config:
    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str   = os.getenv("TELEGRAM_CHAT_ID", "")

    # Intervalo base entre rondas completas (segundos)
    CHECK_INTERVAL_SEC: int = int(os.getenv("CHECK_INTERVAL_SEC", "60"))

    # Pausas entre clics (simular humano)
    HUMAN_MIN: float = float(os.getenv("HUMAN_MIN", "0.7"))
    HUMAN_MAX: float = float(os.getenv("HUMAN_MAX", "1.5"))

    # Selectores / textos comunes
    SELECTOR_CONTINUE: str = 'button.btn.btn-success, button:has-text("Continue"), button:has-text("Continuar")'
    TEXT_NO_CITAS: str     = "No hay horas disponibles"
    BUTTON_CANDIDATES: str = "button, .btn, [role=button]"
    DIA_REGEX: str         = r"(Lunes|Martes|Miércoles|Jueves|Viernes|Sábado|Domingo).*?\b\d{4}\b"

    # Lista de consulados por ENV:
    # Formato: Nombre|URL|modo,Nombre|URL|modo,...
    # modo: "default" (clic Continuar) o "cdmx_panel" (clic en el cuadro grande)
    CONSUL_URLS: str = os.getenv(
        "CONSUL_URLS",
        ",".join([
            "Monterrey|https://www.citaconsular.es/es/hosteds/widgetdefault/25b18886db70f7ec9fd6dfd1a85d1395f/|default",
            "Ciudad de México|https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/|cdmx_panel",
            "Miami|https://www.citaconsular.es/es/hosteds/widgetdefault/2533f04b1d3e818b66f175afc9c24cf63/|default",
        ])
    )

cfg = Config()

# ────────────────────────────────────────────────────────────────────────────────
# Utilidades de notificación
# ────────────────────────────────────────────────────────────────────────────────
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

# ────────────────────────────────────────────────────────────────────────────────
# Anti-detección: UAs y pausas
# ────────────────────────────────────────────────────────────────────────────────
USER_AGENTS = [
    # Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    # macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    # iPhone / Android
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
]
def human_pause():
    time.sleep(random.uniform(cfg.HUMAN_MIN, cfg.HUMAN_MAX))

# ────────────────────────────────────────────────────────────────────────────────
# Parsers
# ────────────────────────────────────────────────────────────────────────────────
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")

def find_date_text(page) -> Optional[str]:
    try:
        content = (page.content() or "").strip()
    except Exception:
        return None
    m = re.search(cfg.DIA_REGEX, content, re.IGNORECASE | re.DOTALL)
    return m.group(0) if m else None

def extract_real_slots(page) -> List[Tuple[str, str]]:
    """Devuelve [(hora, texto_boton)] solo si el botón muestra hora y 'Hueco libre'."""
    slots: List[Tuple[str, str]] = []
    try:
        candidates = page.locator(cfg.BUTTON_CANDIDATES)
        count = candidates.count()
    except Exception:
        count = 0

    for i in range(min(count, 300)):
        try:
            el = candidates.nth(i)
            if not el.is_visible():
                continue
            txt = el.inner_text().strip()
            if not txt or ("hueco libre" not in txt.lower()):
                continue
            m = TIME_RE.search(txt)
            if m:
                slots.append((m.group(0), txt))
        except Exception:
            continue
    return slots

# ────────────────────────────────────────────────────────────────────────────────
# Helper ENV
# ────────────────────────────────────────────────────────────────────────────────
def parse_consul_list(env_val: str) -> List[Tuple[str, str, str]]:
    """
    Devuelve lista de tuplas (nombre, url, modo). Modo por defecto: 'default'.
    Formatos aceptados por elemento:
      - Nombre|URL
      - Nombre|URL|cdmx_panel
    """
    out: List[Tuple[str, str, str]] = []
    for item in [s.strip() for s in env_val.split(",") if s.strip()]:
        parts = [p.strip() for p in item.split("|")]
        if len(parts) >= 2:
            name, url = parts[0], parts[1]
            mode = parts[2].lower() if len(parts) >= 3 else "default"
            out.append((name, url, mode))
    return out

# ────────────────────────────────────────────────────────────────────────────────
# Navegación (con variante especial para CDMX)
# ────────────────────────────────────────────────────────────────────────────────
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
            # Portales tipo Bookitit con botón "Continuar"
            try:
                page.wait_for_selector(cfg.SELECTOR_CONTINUE, timeout=8000)
                page.click(cfg.SELECTOR_CONTINUE, force=True)
                human_pause()
            except PTimeout:
                pass
        elif modo == "cdmx_panel":
            # CDMX: click al cuadro del aviso para abrir el calendario
            try:
                # Buscar por texto característico del panel
                panel = page.locator("text=/PRESENTACION|CONTINUAR SUPONE|LEY MEMORIA/i")
                if panel.count() > 0:
                    panel.first.click(force=True)
                else:
                    # Fallback: paneles comunes
                    page.locator(".panel, .well, .card, .panel-body").first.click(force=True)
                human_pause()
            except Exception:
                pass

        # Si aparece "No hay horas disponibles", salir rápido
        try:
            page.get_by_text(cfg.TEXT_NO_CITAS, exact=False).wait_for(timeout=3000)
            fecha = find_date_text(page)
            browser.close()
            return (False, [], fecha, None)
        except PTimeout:
            pass

        # Buscar huecos
        slots = extract_real_slots(page)
        fecha = find_date_text(page)

        # Probar también iframes
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

        if slots:
            shot_path = f"/tmp/{name.replace(' ', '_').lower()}_citas.jpg"
            try:
                page.screenshot(path=shot_path, type="jpeg", quality=70, full_page=True)
            except Exception:
                shot_path = None
            browser.close()
            return (True, slots, fecha, shot_path)

        # Sin huecos, opcional: captura liviana
        try:
            page.screenshot(path=f"/tmp/{name.replace(' ', '_').lower()}_sin_huecos.jpg",
                            type="jpeg", quality=60, full_page=True)
        except Exception:
            pass
        browser.close()
        return (False, [], fecha, None)

# ────────────────────────────────────────────────────────────────────────────────
# Anti-spam (misma disponibilidad → no repetir alerta)
# ────────────────────────────────────────────────────────────────────────────────
def slots_signature(slots: List[Tuple[str, str]]) -> str:
    horas = sorted({h for h, _ in slots})
    return hashlib.sha256(",".join(horas).encode("utf-8")).hexdigest()

# ────────────────────────────────────────────────────────────────────────────────
def main():
    # Test rápido de Telegram
    if os.getenv("FORCE_TEST") == "1":
        notify("🚀 Test OK: bot listo para enviar alertas.")
        print("[TEST] Notificación de prueba enviada.")
        time.sleep(3)
        sys.exit(0)

    consulados = parse_consul_list(cfg.CONSUL_URLS)
    if not consulados:
        print("[ERROR] CONSUL_URLS vacío o mal formateado.", flush=True)
        sys.exit(1)

    last_sig: Dict[str, str] = {}  # nombre_consulado -> firma última disponibilidad

    while True:
        try:
            for (name, url, modo) in consulados:
                ok, slots, fecha, shot = revisar_un_consulado(name, url, modo, headless=True)

                if ok and slots:
                    sig = slots_signature(slots)
                    if last_sig.get(name) == sig:
                        # Mismas horas que la vez anterior → evita spam
                        continue
                    last_sig[name] = sig

                    primeras = ", ".join(sorted({h for h, _ in slots})[:6])
                    suf_fecha = f" ({fecha})" if fecha else ""
                    caption = f"✅ ¡HAY HUECOS en {name}!{suf_fecha}\nHoras: {primeras}\nEntra ya: {url}"
                    notify(caption)
                    if shot and os.path.exists(shot):
                        send_photo(shot, caption)
                    # Pausa breve antispam si hubo hallazgo en uno
                    time.sleep(60)
                else:
                    marca = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(f"[{marca}] {name} → Sin huecos reales por ahora.", flush=True)

            # Al terminar la ronda completa, espera un intervalo aleatorio
            min_wait = max(30, cfg.CHECK_INTERVAL_SEC - 15)
            max_wait = cfg.CHECK_INTERVAL_SEC + 30
            wait_time = random.randint(min_wait, max_wait)
            print(f"[INFO] Esperando {wait_time}s antes de la siguiente ronda...", flush=True)
            time.sleep(wait_time)

        except Exception as e:
            print(f"[ERROR] {e}", flush=True)
            time.sleep(120)

# ────────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    headed = len(sys.argv) > 1 and sys.argv[1].lower().startswith("head")
    if headed:
        # Demo visual de CDMX
        print("Headed demo de CDMX…")
        print(revisar_un_consulado("Ciudad de México",
                                   "https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/",
                                   "cdmx_panel", headless=False))
    else:
        main()
