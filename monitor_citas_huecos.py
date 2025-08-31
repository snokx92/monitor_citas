# monitor_citas_huecos.py
import os, sys, time, random, re
from dataclasses import dataclass
from typing import List, Optional, Tuple
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout
import requests

# =========================
# CONFIGURACIÓN
# =========================
@dataclass
class Config:
    URL: str = "https://www.citaconsular.es/es/hosteds/widgetdefault/25b18886db70f7ec9fd6dfd1a85d1395f/"

    # Selectores/indicadores
    SELECTOR_CONTINUE: str = 'button.btn.btn-success, button:has-text("Continue"), button:has-text("Continuar")'
    TEXT_NO_CITAS: str = "No hay horas disponibles"
    # Candidatos a botones que contienen hora y la leyenda "Hueco libre"
    BUTTON_CANDIDATES: str = "button, .btn, [role=button]"

    # Fecha visible, p.e. "Miércoles 3 de Septiembre de 2025"
    DIA_REGEX: str = r"(Lunes|Martes|Miércoles|Jueves|Viernes|Sábado|Domingo).*?\b\d{4}\b"

    # Intervalo base entre chequeos (segundos)
    CHECK_INTERVAL_SEC: int = int(os.getenv("CHECK_INTERVAL_SEC", "60"))

    # Notificaciones (opcional)
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Pausas tipo humano entre acciones
    HUMAN_MIN: float = float(os.getenv("HUMAN_MIN", "0.7"))
    HUMAN_MAX: float = float(os.getenv("HUMAN_MAX", "1.5"))

cfg = Config()

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
    """Intenta extraer la fecha visible (día + año) del calendario."""
    try:
        content = (page.content() or "").strip()
    except Exception:
        return None
    m = re.search(cfg.DIA_REGEX, content, re.IGNORECASE | re.DOTALL)
    return m.group(0) if m else None

def extract_real_slots(page) -> List[Tuple[str, str]]:
    """
    Devuelve lista de (hora, texto_boton) únicamente si el botón contiene una hora
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

def revisar_una_vez(headless: bool = True) -> Tuple[bool, List[Tuple[str, str]], Optional[str]]:
    """
    Retorna: (hay_huecos, slots, fecha_visible)
    slots = lista [(hora, texto_boton)...]
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)

        # Rotar User-Agent y tamaño de ventana en cada pasada (parece más humano)
        ua = random.choice(USER_AGENTS)
        vw = random.randint(1200, 1440)
        vh = random.randint(800, 960)

        context = browser.new_context(
            viewport={"width": vw, "height": vh},
            user_agent=ua,
            locale="es-ES",
            extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"}
        )

        # Aceptar el dialog inicial (Welcome/Bienvenido)
        def on_dialog(dialog):
            try:
                dialog.accept()
            except Exception:
                pass
        context.on("dialog", on_dialog)

        page = context.new_page()
        page.set_default_timeout(20000)

        page.goto(cfg.URL, wait_until="domcontentloaded")
        human_pause()

        # Botón Continue / Continuar
        try:
            page.wait_for_selector(cfg.SELECTOR_CONTINUE, timeout=8000)
            page.click(cfg.SELECTOR_CONTINUE, force=True)
            human_pause()
        except PTimeout:
            # A veces ya estás dentro; seguimos.
            pass

        # Si aparece explícitamente el mensaje de no disponibilidad, salimos
        try:
            page.get_by_text(cfg.TEXT_NO_CITAS, exact=False).wait_for(timeout=3000)
            fecha = find_date_text(page)
            browser.close()
            return (False, [], fecha)
        except PTimeout:
            pass

        # Buscar huecos reales
        slots = extract_real_slots(page)
        fecha = find_date_text(page)

        # Por si el contenido está en iframe (pasa en algunos Bookitit)
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
            # Guardar captura JPEG (más ligera) para enviar por Telegram
            jpg_path = "/tmp/citas_disponibles.jpg"
            try:
                page.screenshot(path=jpg_path, type="jpeg", quality=70, full_page=True)
            except Exception:
                jpg_path = None
            browser.close()
            # Devolvemos slots + fecha; la captura se enviará desde main()
            return (True, slots, fecha)

        # No hay texto de “no hay horas…”, pero tampoco huecos reales:
        try:
            page.screenshot(path="/tmp/sin_huecos.jpg", type="jpeg", quality=60, full_page=True)
        except Exception:
            pass
        browser.close()
        return (False, [], fecha)

def main():
    # Modo prueba: envía un mensaje y termina
    if os.getenv("FORCE_TEST") == "1":
        notify("🚀 Test OK: el bot está listo y puede enviarte alertas por Telegram.")
        print("[TEST] Notificación de prueba enviada.")
        time.sleep(5)
        sys.exit(0)

    # Bucle normal de monitoreo
    while True:
        try:
            ok, slots, fecha = revisar_una_vez(headless=True)
            if ok and slots:
                primeras = ", ".join(sorted({h for h, _ in slots})[:5])
                f = f" ({fecha})" if fecha else ""
                caption = f"✅ ¡HAY HUECOS!{f} → Horas: {primeras}\nEntra ya: {cfg.URL}"
                notify(caption)
                # Intentar enviar captura si existe
                jpg_path = "/tmp/citas_disponibles.jpg"
                if os.path.exists(jpg_path):
                    send_photo(jpg_path, caption)
                # Espera 5 min para evitar spam si siguen los mismos huecos
                time.sleep(300)
            else:
                marca = time.strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{marca}] Sin huecos reales por ahora.", flush=True)

                # Intervalo aleatorio para simular comportamiento humano
                min_wait = max(30, cfg.CHECK_INTERVAL_SEC - 15)
                max_wait = cfg.CHECK_INTERVAL_SEC + 30
                wait_time = random.randint(min_wait, max_wait)

                print(f"[INFO] Esperando {wait_time} segundos antes del siguiente chequeo...", flush=True)
                time.sleep(wait_time)

        except Exception as e:
            print(f"[ERROR] {e}", flush=True)
            time.sleep(120)

if __name__ == "__main__":
    # Ejecuta:  python monitor_citas_huecos.py
    # Debug con navegador visible:  python monitor_citas_huecos.py headed
    headed = len(sys.argv) > 1 and sys.argv[1].lower().startswith("head")
    if headed:
        ok, slots, fecha = revisar_una_vez(headless=False)
        print("OK:", ok, "slots:", slots, "fecha:", fecha)
    else:
        main()

