# -*- coding: utf-8 -*-
"""
Monitor de citas (Bookitit/citaconsular) con evidencia SIEMPRE.
- Soporta múltiples consulados (Monterrey, CDMX vía página de Exteriores -> "ELEGIR FECHA Y HORA").
- Envía SIEMPRE captura PNG y HTML a Telegram por cada consulado y cada ronda.
- Proxy residencial (DataImpulse) opcional por variables de entorno.
- Esperas "humanizadas", rotación ligera de User-Agent y viewport.
"""

import os, re, sys, time, random, tempfile, pathlib, io, json
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, List
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout, Page, BrowserContext

# =========================
# Configuración desde ENV
# =========================
@dataclass
class Cfg:
    # --- Telegram ---
    TG_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TG_CHAT: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # --- Timers ---
    LANDING_TIMEOUT_MS: int = int(os.getenv("LANDING_TIMEOUT_MS", "25000"))  # exteriores
    WIDGET_TIMEOUT_MS: int  = int(os.getenv("WIDGET_TIMEOUT_MS",  "20000"))  # widget citas
    GOTO_RETRIES: int = int(os.getenv("GOTO_RETRIES", "2"))

    # Intervalo ENTRE rondas (humanizado)
    ROUND_MIN_SEC: int = int(os.getenv("ROUND_MIN_SEC", "300"))   # 5 min
    ROUND_MAX_SEC: int = int(os.getenv("ROUND_MAX_SEC", "420"))   # 7 min

    # Anti-bloqueos (delays humanos cortos)
    HUMAN_MIN: float = float(os.getenv("HUMAN_MIN", "0.7"))
    HUMAN_MAX: float = float(os.getenv("HUMAN_MAX", "1.5"))

    # Muestra IP pública al iniciar
    SHOW_PUBLIC_IP: str = os.getenv("SHOW_PUBLIC_IP", "1")

    # Bloquear imágenes para ser más ligeros (0/1)
    BLOCK_IMAGES: str = os.getenv("BLOCK_IMAGES", "1")

    # Proxy (DataImpulse)
    PROXY_HOST: str = os.getenv("PROXY_HOST", "")          # p.ej. gw.dataimpulse.com
    PROXY_PORT: str = os.getenv("PROXY_PORT", "823")       # 823 recomendado
    PROXY_USER: str = os.getenv("PROXY_USER", "")          # tu login
    PROXY_PASS: str = os.getenv("PROXY_PASS", "")          # tu password
    # Añadir prefijo de segmentación si usas sesiones: os.getenv("PROXY_SESSION_IN_USER", "")

cfg = Cfg()

# =========================
# Utilidades Telegram
# =========================
TG_API = f"https://api.telegram.org/bot{cfg.TG_TOKEN}" if cfg.TG_TOKEN else ""

def tg_text(text: str):
    print(text, flush=True)
    if not (cfg.TG_TOKEN and cfg.TG_CHAT): return
    try:
        requests.post(f"{TG_API}/sendMessage",
                      json={"chat_id": cfg.TG_CHAT, "text": text},
                      timeout=15)
    except Exception as e:
        print(f"[WARN] Telegram text error: {e}", flush=True)

def tg_photo(path: str, caption: str):
    print(f"[evidence] photo -> {path}", flush=True)
    if not (cfg.TG_TOKEN and cfg.TG_CHAT): return
    try:
        with open(path, "rb") as f:
            requests.post(f"{TG_API}/sendPhoto",
                          data={"chat_id": cfg.TG_CHAT, "caption": caption},
                          files={"photo": f},
                          timeout=30)
    except Exception as e:
        print(f"[WARN] Telegram photo error: {e}", flush=True)

def tg_doc(path: str, caption: str):
    print(f"[evidence] doc -> {path}", flush=True)
    if not (cfg.TG_TOKEN and cfg.TG_CHAT): return
    try:
        with open(path, "rb") as f:
            requests.post(f"{TG_API}/sendDocument",
                          data={"chat_id": cfg.TG_CHAT, "caption": caption},
                          files={"document": f},
                          timeout=30)
    except Exception as e:
        print(f"[WARN] Telegram doc error: {e}", flush=True)

# =========================
# User-Agents y helpers
# =========================
USER_AGENTS = [
    # Win / Chrome
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    # Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    # macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

def human_pause(a: float = None, b: float = None):
    lo = cfg.HUMAN_MIN if a is None else a
    hi = cfg.HUMAN_MAX if b is None else b
    time.sleep(random.uniform(lo, hi))

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")  # HH:MM

def has_real_slots(page: Page) -> Tuple[bool, List[str]]:
    """
    Busca botones/labels con 'Hueco libre' + hora HH:MM.
    """
    try:
        html = (page.content() or "")
    except Exception:
        return False, []

    if "Hueco libre" not in html and "Huecos libres" not in html and "hueco libre" not in html.lower():
        return False, []

    hours = sorted(set(m.group(0) for m in TIME_RE.finditer(html)))
    return (len(hours) > 0), hours

def looks_blank(page: Page) -> bool:
    try:
        html = (page.content() or "")
        return len(html.strip()) < 100
    except Exception:
        return True

def save_evidence(prefix: str, page: Page) -> Tuple[str, str]:
    """
    Guarda PNG + HTML y devuelve paths.
    """
    tmpdir = tempfile.mkdtemp(prefix="citas_")
    png = os.path.join(tmpdir, f"{prefix}.png")
    html = os.path.join(tmpdir, f"{prefix}.html")
    try:
        page.screenshot(path=png, full_page=True)
    except Exception:
        # fallback viewport
        try:
            page.screenshot(path=png)
        except Exception:
            pass
    try:
        with open(html, "w", encoding="utf-8") as f:
            f.write(page.content() or "")
    except Exception:
        pass
    return png, html

# =========================
# Definición de consulados
# =========================
@dataclass
class Consulado:
    name: str
    exteriores_url: str           # Página de Exteriores con el enlace "ELEGIR FECHA Y HORA"
    elegir_selector: str          # Selector para el enlace (texto)
    # Texto alternativo (por si cambian mayúsculas/acentos)
    elegir_text: str

CONSULADOS: List[Consulado] = [
    Consulado(
        name="Monterrey",
        exteriores_url="https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
        elegir_selector="text=ELEGIR FECHA Y HORA",
        elegir_text="ELEGIR FECHA Y HORA",
    ),
    Consulado(
        name="Ciudad de México",
        exteriores_url="https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
        elegir_selector="text=ELEGIR FECHA Y HORA",
        elegir_text="ELEGIR FECHA Y HORA",
    ),
]

# =========================
# Navegación por consulado
# =========================
def revisar_consulado(p: Page, cons: Consulado) -> Tuple[str, str]:
    """
    Devuelve (status, resumen)
    status:
      - OK (hay huecos)
      - NO (sin huecos)
      - BLANK (html/captura muy corta)
      - ERR (excepción)
    """
    name = cons.name
    status, resumen = "ERR", "Error desconocido"
    tg_text(f"[{name}] goto…")

    # 1) Abrir Exteriores
    for att in range(cfg.GOTO_RETRIES):
        try:
            p.goto(cons.exteriores_url, wait_until="domcontentloaded", timeout=cfg.LANDING_TIMEOUT_MS)
            break
        except Exception as e:
            if att == cfg.GOTO_RETRIES - 1:
                raise
            human_pause(1.2, 2.2)

    human_pause(0.9, 1.6)

    # 2) Click en "ELEGIR FECHA Y HORA" (abre nueva pestaña)
    try:
        # A veces el enlace está más abajo
        try:
            p.get_by_text(cons.elegir_text, exact=False).scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass

        with p.context.expect_page() as new_page_info:
            # Usa selector por texto
            p.get_by_text(cons.elegir_text, exact=False).click(timeout=8000)
        widget_page = new_page_info.value
    except Exception as e:
        # Evidencia en fallo y retorno
        png, html = save_evidence(f"{name.lower().replace(' ', '_')}_exteriores_error", p)
        tg_photo(png, f"{name}: error abriendo enlace de Exteriores")
        tg_doc(html,  f"{name}: HTML Exteriores (error)")
        return "ERR", "Error al abrir 'ELEGIR FECHA Y HORA'"

    # 3) Widget en nueva pestaña
    human_pause(0.9, 1.5)
    widget_page.set_default_timeout(cfg.WIDGET_TIMEOUT_MS)

    # Aceptar posible alert("Bienvenido")
    def _on_dialog(dlg):
        try: dlg.accept()
        except: pass
    widget_page.on("dialog", _on_dialog)

    # Espera a que cargue algo visible (el botón verde Continue/Continuar o texto común)
    try:
        # Si aparece botón continuar
        try:
            widget_page.get_by_text("Continue", exact=False).wait_for(timeout=6000)
            widget_page.get_by_text("Continue", exact=False).click(timeout=3000)
            human_pause()
        except Exception:
            # si no, seguimos: algunos widgets no lo muestran
            pass

        # Espera un poco a que renderice el calendario/estado
        human_pause(1.0, 2.0)

        # Captura temprana para evidencia SIEMPRE
        early_png, early_html = save_evidence(f"{name.lower().replace(' ', '_')}_before_check", widget_page)
        tg_photo(early_png, f"{name}: evidencia inicial (antes de parsear)")
        tg_doc(early_html,  f"{name}: HTML inicial")

        # Heurísticas de estado
        blank = looks_blank(widget_page)
        ok, hours = has_real_slots(widget_page)

        # Frases negativas típicas
        neg_txts = [
            "No hay horas disponibles", "No hay citas disponibles",
            "Inténtelo de nuevo dentro de unos días", "Inténtelo de nuevo en unos días",
        ]
        html = (widget_page.content() or "").lower()
        no = any(t.lower() in html for t in neg_txts)

        if ok and hours:
            status = "OK"
            resumen = f"Huecos: {', '.join(hours[:6])}"
        elif blank:
            status = "BLANK"
            resumen = "HTML/captura vacíos (posible bloqueo)."
        elif no:
            status = "NO"
            resumen = "Sin huecos por ahora."
        else:
            # ni huecos ni texto negativo claro
            status = "NO"
            resumen = "Sin huecos claros (no se detectó 'No hay horas…')."

        # Evidencia final SIEMPRE (ya con decisión)
        suf = "ok" if status == "OK" else ("blank" if status == "BLANK" else "no")
        end_png, end_html = save_evidence(f"{name.lower().replace(' ', '_')}_{suf}_final", widget_page)
        cap = f"{name}: {resumen}"
        tg_photo(end_png, cap)
        tg_doc(end_html,  f"{name}: HTML final — {status}")

        # Si hay huecos, intenta resaltar primeras horas en el mensaje
        if status == "OK":
            tg_text(f"✅ {name}: {resumen}\nEntra ya desde tu navegador.")
        elif status == "BLANK":
            tg_text(f"⚠️ {name}: {resumen}")
        else:
            tg_text(f"[{name}] → {resumen}")

        return status, resumen

    except Exception as e:
        # Evidencia de error
        png, html = save_evidence(f"{name.lower().replace(' ', '_')}_exception", widget_page if 'widget_page' in locals() else p)
        tg_photo(png, f"{name}: excepción durante revisión ({e.__class__.__name__})")
        tg_doc(html,  f"{name}: HTML (excepción)")
        return "ERR", f"Excepción: {e}"

# =========================
# Contexto Playwright
# =========================
def build_launch_args() -> Dict:
    args = {
        "headless": True,
        "args": [
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    }
    if cfg.PROXY_HOST and cfg.PROXY_PORT:
        if cfg.PROXY_USER and cfg.PROXY_PASS:
            proxy = {
                "server": f"http://{cfg.PROXY_HOST}:{cfg.PROXY_PORT}",
                "username": cfg.PROXY_USER,
                "password": cfg.PROXY_PASS,
            }
        else:
            proxy = {"server": f"http://{cfg.PROXY_HOST}:{cfg.PROXY_PORT}"}
        args["proxy"] = proxy
    return args

def new_context(browser, block_images: bool = True) -> BrowserContext:
    ua = random.choice(USER_AGENTS)
    vw = random.randint(1200, 1440)
    vh = random.randint(800,  960)
    context = browser.new_context(
        viewport={"width": vw, "height": vh},
        user_agent=ua,
        locale="es-ES",
        extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"},
    )
    if block_images and cfg.BLOCK_IMAGES not in ("0", "false", "False"):
        context.route("**/*", lambda route: route.abort() if route.request.resource_type == "image" else route.continue_())
    return context

def public_ip_via_httpbin() -> Optional[str]:
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=10)
        return r.json().get("ip")
    except:
        return None

# =========================
# Main loop
# =========================
def main():
    print("[start] Launching bot…", flush=True)
    print(f"[INFO] Config: proof=OFF debug=ON block_images={cfg.BLOCK_IMAGES}", flush=True)

    # Proxy / IP info
    if cfg.PROXY_HOST:
        print(f"[INFO] Proxy: http://{cfg.PROXY_HOST}:{cfg.PROXY_PORT}", flush=True)
    if cfg.SHOW_PUBLIC_IP not in ("0", "false", "False"):
        ip = public_ip_via_httpbin()
        if ip:
            print(f"[INFO] IP pública: {ip}", flush=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**build_launch_args())
        try:
            while True:
                print(f"[INFO] Consulados: {', '.join(c.name for c in CONSULADOS)}", flush=True)

                # Un solo contexto por ronda, tabs por consulado
                context = new_context(browser, block_images=True)

                for cons in CONSULADOS:
                    page = context.new_page()
                    try:
                        status, resumen = revisar_consulado(page, cons)
                    except Exception as e:
                        tg_text(f"⚠️ {cons.name}: error inesperado — {e}")
                    finally:
                        try:
                            page.close()
                        except:
                            pass
                    human_pause(0.8, 1.6)

                try:
                    context.close()
                except:
                    pass

                # Espera humanizada 5–7 min (configurable)
                wait = random.randint(cfg.ROUND_MIN_SEC, cfg.ROUND_MAX_SEC)
                print(f"[INFO] Esperando {wait}s antes de la siguiente ronda…", flush=True)
                time.sleep(wait)

        finally:
            try:
                browser.close()
            except:
                pass

# Entrypoint
if __name__ == "__main__":
    main()
