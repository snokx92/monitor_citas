# monitor_citas_huecos.py (multi-consulado)
import os, sys, time, random, re, hashlib
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout
import requests

# =========================
# CONFIGURACIÓN
# =========================
@dataclass
class Config:
    # Intervalo base entre rondas completas (todos los consulados)
    CHECK_INTERVAL_SEC: int = int(os.getenv("CHECK_INTERVAL_SEC", "60"))

    # Notificaciones (Telegram)
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Pausas tipo humano entre acciones
    HUMAN_MIN: float = float(os.getenv("HUMAN_MIN", "0.7"))
    HUMAN_MAX: float = float(os.getenv("HUMAN_MAX", "1.5"))

    # Lista de consulados (nombre|url), separada por comas, configurable por ENV
    # Si no se define, usa la URL original que ya tenías, con nombre "Monterrey".
    CONSUL_URLS: str = os.getenv(
        "CONSUL_URLS",
        "Monterrey|https://www.citaconsular.es/es/hosteds/widgetdefault/25b18886db70f7ec9fd6dfd1a85d1395f/"
    )

    # Selectores/indicadores
    SELECTOR_CONTINUE: str = 'button.btn.btn-success, button:has-text("Continue"), button:has-text("Continuar")'
    TEXT_NO_CITAS: str = "No hay horas disponibles"
    BUTTON_CANDIDATES: str = "button, .btn, [role=button]"

    # Fecha visible, p.e. "Miércoles 3 de Septiembre de 2025"
    DIA_REGEX: str = r"(Lunes|Martes|Miércoles|Jueves|Viernes|Sábado|Domingo).*?\b\d{4}\b"

cfg = Config()

def parse_consul_list(env_val: str) -> List[Tuple[str, str]]:
    out = []
    for item in [s.strip() for s in env_val.split(",") if s.strip()]:
        if "|" in item:
            name, url = item.split("|", 1)
            out.append((name.strip(), url.strip()))
    return out

def notify(msg: str):
    """Envía texto por Telegram (y también lo imprime en logs)."""
    print(msg, flush=True)
    if cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": cfg.TELEGRAM_CHAT_ID, "text": msg},
                timeout=15
            )
        except Exception as e:
            print(f"[WARN] Telegram fallo: {e}", file=sys.stderr)

def send_photo(path: str, caption: str = ""):
    """Envía una foto por Telegram usando sendPhoto."""
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

# Navegadores/OS comunes (versiones recientes)
USER_AGENTS = [
    # Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    # macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    # iPhone (Safari)
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    # Android (Chrome)
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
]

def human_pause():
    time.sleep(random.uniform(cfg.HUMAN_MIN, cfg.HUMAN_MAX))

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")  # 0:00–23:59

def find_date_text(page) -> Optional[str]:
    """Intenta extraer fecha visible (día + año) del calendario."""
    try:
        content = (page.content() or "").strip()
    except Exception:
        return None
    m = re.search(cfg.DIA_REGEX, content, re.IGNORECASE | re.DOTALL)
    return m.group(0) if m else None

def extract_real_slots(page) -> List[Tuple[str, str]]:
    """
    Devuelve lista de (hora, texto_boton) solo si el botón contiene una hora
    y, en el mismo bloque, aparece 'Hueco libre' (evita falsos positivos).
    """
    slots: List[Tuple[str, str]] = []
    try:
        candidates = page.locator(cfg.BUTTON_CANDIDATES)
        count = candidates.count()
    except Exception:
        count = 0

    for i in range(min(count, 300)):  # límite de seguridad
        try:
            el = candidates.nth(i)
            if not el.is_visible():
                continue
            text = el.inner_text().strip()
            if not text:
                continue
            if "hueco libre" not in text.lower():
                continue
            m = TIME_RE.search(text)
            if m:
                slots.append((m.group(0), text))
        except Exception:
            continue
    return slots

def revisar_un_consulado(name: str, url: str, headless: bool = True) -> Tuple[bool, List[Tuple[str, str]], Optional[str], Optional[str]]:
    """
    Revisa un consulado. Retorna: (hay_huecos, slots, fecha_visible, screenshot_path)
    """
    jpg_path = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)

        # Rotar User-Agent y tamaño de ventana (parece más humano)
        ua = random.choice(USER_AGENTS)
        vw = random.randint(1200, 1440)
        vh = random.randint(800, 960)

        context = browser.new_context(
            viewport={"width": vw, "height": vh},
            user_agent=ua,
            locale="es-ES",
            extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"}
        )

        # Aceptar posibles dialogs (Welcome/Bienvenido)
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

        # Botón Continue / Continuar
        try:
            page.wait_for_selector(cfg.SELECTOR_CONTINUE, timeout=8000)
            page.click(cfg.SELECTOR_CONTINUE, force=True)
            human_pause()
        except PTimeout:
            pass  # a veces ya estás dentro

        # Mensaje explícito de no disponibilidad
        try:
            page.get_by_text(cfg.TEXT_NO_CITAS, exact=False).wait_for(timeout=3000)
            fecha = find_date_text(page)
            browser.close()
            return (False, [], fecha, None)
        except PTimeout:
            pass

        # Buscar huecos reales
        slots = extract_real_slots(page)
        fecha = find_date_text(page)

        # Revisar iframes si no encontramos todavía
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
            # Guardar captura JPEG (más ligera)
            jpg_path = f"/tmp/{name.replace(' ', '_').lower()}_citas.jpg"
            try:
                page.screenshot(path=jpg_path, type="jpeg", quality=70, full_page=True)
            except Exception:
                jpg_path = None
            browser.close()
            return (True, slots, fecha, jpg_path)

        # Sin huecos: guardar captura liviana (opcional)
        try:
            page.screenshot(path=f"/tmp/{name.replace(' ', '_').lower()}_sin_huecos.jpg",
                            type="jpeg", quality=60, full_page=True)
        except Exception:
            pass
        browser.close()
        return (False, [], fecha, None)

def slots_signature(slots: List[Tuple[str, str]]) -> str:
    """Hash simple para no spamear la misma disponibilidad."""
    horas = sorted({h for h, _ in slots})
    txt = ",".join(horas)
    return hashlib.sha256(txt.encode("utf-8")).hexdigest()

def main():
    # Modo prueba: envía mensaje y termina
    if os.getenv("FORCE_TEST") == "1":
        notify("🚀 Test OK: el bot está listo y puede enviarte alertas por Telegram.")
        print("[TEST] Notificación de prueba enviada.")
        time.sleep(5)
        sys.exit(0)

    consul_list = parse_consul_list(cfg.CONSUL_URLS)
    if not
