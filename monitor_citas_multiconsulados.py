# -*- coding: utf-8 -*-
# monitor_citas_multiconsulados.py

import os, sys, time, random, re, io, traceback
from dataclasses import dataclass, field
from typing import Optional, Tuple, List
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout

# ============ Telegram ============
BOT = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

def tg_send_text(text: str):
    print(text, flush=True)
    if not (BOT and CHAT): return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            json={"chat_id": CHAT, "text": text},
            timeout=20
        )
    except Exception as e:
        print(f"[WARN] Telegram text fallo: {e}", file=sys.stderr, flush=True)

def tg_send_photo(path: str, caption: str = ""):
    if not (BOT and CHAT): return
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{BOT}/sendPhoto",
                data={"chat_id": CHAT, "caption": caption},
                files={"photo": f},
                timeout=60
            )
    except Exception as e:
        print(f"[WARN] Telegram photo fallo: {e}", file=sys.stderr, flush=True)

def tg_send_document(path: str, caption: str = ""):
    if not (BOT and CHAT): return
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{BOT}/sendDocument",
                data={"chat_id": CHAT, "caption": caption},
                files={"document": f},
                timeout=60
            )
    except Exception as e:
        print(f"[WARN] Telegram doc fallo: {e}", file=sys.stderr, flush=True)

# ============ Config ============
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]

NO_CITAS_TEXTS = [
    "No hay horas disponibles",
    "No hay horas disponibles. Inténtelo de nuevo dentro de unos días",
]

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
DAY_HINT_RE = re.compile(r"(Lunes|Martes|Miércoles|Jueves|Viernes|Sábado|Domingo).*\b(20\d{2})\b", re.I)

@dataclass
class Consulado:
    nombre: str
    entry_mode: str = "direct"  # "direct" | "via_landing"
    widget_url: Optional[str] = None
    landing_url: Optional[str] = None
    landing_link_text: Optional[str] = None
    mobile: bool = False
    needs_cdmx_panel_click: bool = False

@dataclass
class Config:
    CONSULADOS: List[Consulado] = field(default_factory=list)
    CHECK_MIN: int = int(os.getenv("CHECK_INTERVAL_SEC", "90"))
    CHECK_MAX: int = max(120, int(os.getenv("CHECK_INTERVAL_SEC", "120")) + 30)
    HUMAN_MIN: float = float(os.getenv("HUMAN_MIN", "0.7"))
    HUMAN_MAX: float = float(os.getenv("HUMAN_MAX", "1.5"))
    PROXY_URL: str = os.getenv("PROXY_URL", os.getenv("HTTP_PROXY", ""))
    BROWSER: str = os.getenv("BROWSER", "chromium").lower()
    PROOF: bool = os.getenv("PROOF", "0") == "1"
    SHOW_PUBLIC_IP: bool = os.getenv("SHOW_PUBLIC_IP", "0") == "1"
    BLOCK_IMAGES: bool = os.getenv("BLOCK_IMAGES", "1") == "1"
    DEBUG_STEPS: bool = os.getenv("DEBUG_STEPS", "0") == "1"
    RUN_ONCE: bool = os.getenv("RUN_ONCE", "0") == "1"

cfg = Config(
    CONSULADOS=[
        # MONTERREY → entrar desde landing y click "ELEGIR FECHA Y HORA"
        Consulado(
            nombre="Monterrey",
            entry_mode="via_landing",
            landing_url="https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
            landing_link_text="ELEGIR FECHA Y HORA",
            widget_url="https://www.citaconsular.es/es/hosteds/widgetdefault/25b18886db70f7ec9fd6dfd1a85d1395f/",
            mobile=False
        ),
        # CDMX → AHORA TAMBIÉN via landing + click panel (por si aparece)
        Consulado(
            nombre="Ciudad de México",
            entry_mode="via_landing",
            landing_url="https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
            landing_link_text="ELEGIR FECHA Y HORA",
            widget_url="https://www.citaconsular.es/es/hosteds/widgetdefault/21b7c1aaf9fef2785deb64ccab5ceca06/",
            mobile=True,
            needs_cdmx_panel_click=True
        ),
    ]
)

# ============ Helpers ============
def human_pause():
    time.sleep(random.uniform(cfg.HUMAN_MIN, cfg.HUMAN_MAX))

def get_public_ip_through_proxy() -> Optional[str]:
    try:
        r = requests.get("https://api.ipify.org", timeout=15,
                         proxies={"http": cfg.PROXY_URL, "https": cfg.PROXY_URL} if cfg.PROXY_URL else None)
        return r.text.strip()
    except Exception:
        return None

def text_has_no_citas(txt: str) -> bool:
    t = (txt or "").lower()
    return any(s.lower() in t for s in NO_CITAS_TEXTS)

def extract_real_slots_from(page) -> List[Tuple[str, str]]:
    slots = []
    try:
        candidates = page.locator("button, .btn, [role=button]")
        n = min(candidates.count(), 400)
        for i in range(n):
            el = candidates.nth(i)
            if not el.is_visible(): continue
            txt = (el.inner_text() or "").strip()
            if "hueco libre" not in txt.lower(): continue
            m = TIME_RE.search(txt)
            if m:
                slots.append((m.group(0), txt))
    except Exception:
        pass
    return slots

# ============ Navegación ============
def build_context(playwright, mobile: bool):
    proxy = {"server": cfg.PROXY_URL} if cfg.PROXY_URL else None
    browser_type = getattr(playwright, cfg.BROWSER, playwright.chromium)
    browser = browser_type.launch(headless=True, proxy=proxy)

    ua = random.choice(USER_AGENTS)
    if mobile:
        context = browser.new_context(
            user_agent=ua if "iPhone" in ua else "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
            viewport={"width": 390, "height": 844},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            locale="es-ES",
            extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"}
        )
    else:
        vw = random.randint(1200, 1440)
        vh = random.randint(800, 960)
        context = browser.new_context(
            user_agent=ua,
            viewport={"width": vw, "height": vh},
            locale="es-ES",
            extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"}
        )
    if cfg.BLOCK_IMAGES:
        context.route("**/*", lambda route: route.abort() if route.request.resource_type in ("image","font") else route.continue_())
    return browser, context

def goto_widget_from_landing(page, landing_url: str, link_text: str):
    if cfg.DEBUG_STEPS: print("[step] goto landing…", flush=True)
    page.goto(landing_url, wait_until="domcontentloaded")
    human_pause()

    # Acepta cookies simples
    try:
        page.get_by_role("button", name=re.compile("Aceptar|Accept", re.I)).first.click(timeout=3000)
    except Exception:
        pass

    # Click “ELEGIR FECHA Y HORA”
    if cfg.DEBUG_STEPS: print("[step] click ELEGIR FECHA Y HORA", flush=True)
    clicked = False
    try:
        page.get_by_role("link", name=re.compile(link_text, re.I)).first.click(timeout=6000)
        clicked = True
    except Exception:
        try:
            page.get_by_text(re.compile(link_text, re.I)).first.click(timeout=6000)
            clicked = True
        except Exception:
            pass
    if not clicked:
        raise RuntimeError("No se encontró el enlace ELEGIR FECHA Y HORA en la landing.")

    human_pause()
    # Si abre en misma pestaña, esperará URL de citaconsular; si abre nueva, tomamos la última page
    try:
        page.wait_for_url(re.compile(r"citaconsular\.es", re.I), timeout=12000)
    except Exception:
        try:
            page = page.context.pages[-1]
        except Exception:
            pass
    return page

def do_cdmx_panel_click(page):
    if cfg.DEBUG_STEPS: print("[step] CDMX: intentar click panel…", flush=True)
    human_pause()
    try:
        page.locator('button:has-text("Continue"), button:has-text("Continuar")').first.click(timeout=5000)
        human_pause()
    except Exception:
        pass
    try:
        any_panel = page.locator("section, .panel, .card, .list-group-item, .service, .service-box").first
        if any_panel and any_panel.is_visible():
            any_panel.click(timeout=5000, force=True)
            human_pause()
    except Exception:
        pass

# ============ Revisión ============
def _find_date_hint(s: str) -> Optional[str]:
    if not s: return None
    m = DAY_HINT_RE.search(s)
    return m.group(0) if m else None

def revisar_consulado(pw, cons: Consulado) -> Tuple[bool, List[Tuple[str, str]], Optional[str], dict]:
    meta = {"html_len": 0, "text_len": 0, "blank": False}
    browser, context = build_context(pw, cons.mobile)
    context.on("dialog", lambda d: (d.accept(), None))
    page = context.new_page()
    page.set_default_timeout(20000)

    try:
        # 1) entrar (landing o directo)
        if cons.entry_mode == "via_landing":
            page = goto_widget_from_landing(page, cons.landing_url, cons.landing_link_text)
        else:
            if cfg.DEBUG_STEPS: print("[step] goto widget directo…", flush=True)
            page.goto(cons.widget_url, wait_until="domcontentloaded")
            human_pause()

        # 2) botón continuar (si aparece)
        try:
            page.locator('button:has-text("Continue"), button:has-text("Continuar")').first.click(timeout=5000)
            human_pause()
        except Exception:
            pass

        # 3) cdmx panel
        if cons.needs_cdmx_panel_click:
            do_cdmx_panel_click(page)

        # 4) medir contenido
        try:
            html = page.content() or ""
        except Exception:
            html = ""
        txt = page.inner_text("body").strip() if page.locator("body").count() else ""
        meta["html_len"] = len(html)
        meta["text_len"] = len(txt)

        # 5) no-citas explícito
        if text_has_no_citas(txt):
            if cfg.PROOF:
                fname = f"{cons.nombre.lower().replace(' ','_')}_nocitas.html"
                with open(fname, "w", encoding="utf-8") as f:
                    f.write(html)
                tg_send_document(fname, caption=f"{cons.nombre}: sin huecos (no-citas). html_len={meta['html_len']}")
                try:
                    sp = f"{cons.nombre.lower().replace(' ','_')}_nocitas.png"
                    page.screenshot(path=sp, full_page=True)
                    tg_send_photo(sp, caption=f"{cons.nombre}: sin huecos (no-citas).")
                except Exception:
                    pass
            return (False, [], _find_date_hint(html), meta)

        # 6) slots reales
        slots = extract_real_slots_from(page)
        fecha = _find_date_hint(html) or _find_date_hint(txt)
        if slots:
            try:
                sp = f"{cons.nombre.lower().replace(' ','_')}_slots.png"
                page.screenshot(path=sp, full_page=True)
                tg_send_photo(sp, caption=f"{cons.nombre}: ¡Huecos! {', '.join(sorted({h for h,_ in slots})[:5])} {f'({fecha})' if fecha else ''}")
            except Exception:
                pass
            try:
                fname = f"{cons.nombre.lower().replace(' ','_')}_slots.html"
                with open(fname, "w", encoding="utf-8") as f:
                    f.write(html)
                tg_send_document(fname, caption=f"{cons.nombre}: HTML con huecos.")
            except Exception:
                pass
            return (True, slots, fecha, meta)

        # 7) “en blanco”
        blankish = (meta["html_len"] <= 80) or (meta["text_len"] <= 20)
        meta["blank"] = blankish
        if blankish:
            try:
                sp = f"{cons.nombre.lower().replace(' ','_')}_blank.png"
                page.screenshot(path=sp, full_page=True)
                tg_send_photo(sp, caption=f"{cons.nombre}: captura en blanco (posible bloqueo).")
            except Exception:
                pass
            try:
                fname = f"{cons.nombre.lower().replace(' ','_')}_blank.html"
                with open(fname, "w", encoding="utf-8") as f:
                    f.write(html)
                tg_send_document(fname, caption=f"{cons.nombre}: HTML en blanco (posible bloqueo).")
            except Exception:
                pass
            return (False, [], fecha, meta)

        # 8) sin huecos (con evidencia si PROOF)
        if cfg.PROOF:
            try:
                sp = f"{cons.nombre.lower().replace(' ','_')}_sin_huecos.png"
                page.screenshot(path=sp, full_page=True)
                tg_send_photo(sp, caption=f"{cons.nombre}: sin huecos por ahora.")
            except Exception:
                pass
            try:
                fname = f"{cons.nombre.lower().replace(' ','_')}_sin_huecos.html"
                with open(fname, "w", encoding="utf-8") as f:
                    f.write(html)
                tg_send_document(fname, caption=f"{cons.nombre}: HTML sin huecos por ahora.")
            except Exception:
                pass

        return (False, [], fecha, meta)

    finally:
        try:
            context.close(); browser.close()
        except Exception:
            pass

# ============ Loop ============
def main():
    if os.getenv("FORCE_TEST", "0") == "1":
        pub_ip = get_public_ip_through_proxy() if cfg.SHOW_PUBLIC_IP else None
        msg = "🚀 Test OK: el bot está listo y puede enviarte evidencias e IP."
        if pub_ip: msg += f"\n[INFO] IP pública: {pub_ip}"
        tg_send_text(msg)
        print("[TEST] Notificación de prueba enviada.", flush=True)
        return

    print("[start] Launching bot…", flush=True)
    print(f"[INFO] Config: proof={'ON' if cfg.PROOF else 'OFF'} debug={'ON' if cfg.DEBUG_STEPS else 'OFF'} block_images={'ON' if cfg.BLOCK_IMAGES else 'OFF'}", flush=True)
    cons_names = ", ".join([c.nombre for c in cfg.CONSULADOS])
    print(f"[INFO] Consulados: {cons_names}", flush=True)
    if cfg.PROXY_URL: print(f"[INFO] Proxy: {cfg.PROXY_URL}", flush=True)
    if cfg.SHOW_PUBLIC_IP:
        ip = get_public_ip_through_proxy()
        print(f"[INFO] IP pública: {ip or 'N/D'}", flush=True)

    with sync_playwright() as pw:
        while True:
            for cons in cfg.CONSULADOS:
                try:
                    print(f"[{cons.nombre}] goto…", flush=True)
                    ok, slots, fecha, meta = revisar_consulado(pw, cons)
                    marca = time.strftime("%Y-%m-%d %H:%M:%S")
                    if ok and slots:
                        primeras = ", ".join(sorted({h for h,_ in slots})[:5])
                        tg_send_text(f"[{marca}] {cons.nombre} → ¡HAY HUECOS! {primeras}{f' ({fecha})' if fecha else ''}")
                        if cons.widget_url:
                            tg_send_text(f"{cons.nombre} URL: {cons.widget_url}")
                        time.sleep(300)
                    else:
                        if meta.get("blank"):
                            tg_send_text(f"⚠️ {cons.nombre}: página vacía tras reintentos (bloqueo probable). [html_len={meta['html_len']}]")
                        else:
                            tg_send_text(f"[{marca}] {cons.nombre} → sin huecos por ahora.")
                except Exception as e:
                    tg_send_text(f"❌ {cons.nombre}: error {e}")
                    traceback.print_exc()

            if cfg.RUN_ONCE: break
            wait = random.randint(cfg.CHECK_MIN, cfg.CHECK_MAX)
            print(f"[INFO] Esperando {wait}s antes de la siguiente ronda…", flush=True)
            time.sleep(wait)

if __name__ == "__main__":
    main()
