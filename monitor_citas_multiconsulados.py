# monitor_citas_multiconsulados.py
# -*- coding: utf-8 -*-

import os, sys, time, random, re, json, io, traceback
from dataclasses import dataclass
from typing import List, Tuple, Optional

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PTimeout, Error as PWError

# =========================
# Configuración
# =========================

@dataclass
class Consulado:
    nombre: str
    exteriores_url: str            # Página de Exteriores con el enlace amarillo ELEGIR FECHA Y HORA
    tipo: str = "default"          # "default" vale para Mty y CDMX usando el flujo por Exteriores


@dataclass
class AppCfg:
    # Lista de consulados a revisar (puedes agregar más)
    CONSULADOS: List[Consulado] = (
        # Monterrey
        # Abre Exteriores y desde ahí “ELEGIR FECHA Y HORA” -> citaconsular.es
        [
            Consulado(
                "Monterrey",
                "https://www.exteriores.gob.es/Consulados/monterrey/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
                "default",
            ),
            Consulado(
                "Ciudad de México",
                "https://www.exteriores.gob.es/Consulados/mexico/es/ServiciosConsulares/Paginas/CitaNacionalidadLMD.aspx",
                "default",
            ),
        ]
    )[0]

    # Selectores / textos típicos
    LINK_ELEGIR: str = "text=/ELEGIR\\s+FECHA\\s+Y\\s+HORA/i"
    SELECTOR_CONTINUE: str = 'button.btn.btn-success, button:has-text("Continue"), button:has-text("Continuar")'
    TEXT_NO_CITAS: str = "No hay horas disponibles"
    BUTTON_CANDIDATES: str = "button, .btn, [role=button]"

    # Anti-bloqueos / pausas humanas
    HUMAN_MIN: float = float(os.getenv("HUMAN_MIN", "0.7"))
    HUMAN_MAX: float = float(os.getenv("HUMAN_MAX", "1.8"))

    # Intervalo entre rondas (simular humano) -> 5–7 min por defecto
    MIN_WAIT_BETWEEN_ROUNDS: int = int(os.getenv("CHECK_MIN_SEC", "300"))   # 5 min
    MAX_WAIT_BETWEEN_ROUNDS: int = int(os.getenv("CHECK_MAX_SEC", "420"))   # 7 min

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Modo test para mandar un ping de “listo”
    FORCE_TEST: str = os.getenv("FORCE_TEST", "0")

    # Timeouts
    NAV_TIMEOUT_MS: int = int(os.getenv("NAV_TIMEOUT_MS", "20000"))
    SEL_TIMEOUT_MS: int = int(os.getenv("SEL_TIMEOUT_MS", "8000"))

    # Rotación de User-Agent / viewport
    USER_AGENTS: List[str] = (
        [
            # Windows / Chrome
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            # Windows / Edge
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
            # macOS / Safari
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
            # macOS / Chrome
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            # iPhone / Safari
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        ]
    )

cfg = AppCfg()

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b", re.IGNORECASE)


# =========================
# Utilidades
# =========================

def hsleep(a=None, b=None):
    """Pausa humana con rango (o usa HUMAN_MIN/HUMAN_MAX)."""
    if a is None: a = cfg.HUMAN_MIN
    if b is None: b = cfg.HUMAN_MAX
    time.sleep(random.uniform(a, b))

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
            print(f"[WARN] Telegram sendMessage falló: {e}", flush=True)

def send_photo(caption: str, image_bytes: bytes, filename="evidencia.png"):
    if not (cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID):
        return
    try:
        files = {"photo": (filename, image_bytes, "image/png")}
        data = {"chat_id": cfg.TELEGRAM_CHAT_ID, "caption": caption}
        requests.post(
            f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendPhoto",
            data=data, files=files, timeout=20
        )
    except Exception as e:
        print(f"[WARN] Telegram sendPhoto falló: {e}", flush=True)

def send_document(caption: str, content_bytes: bytes, filename="contenido.html", mime="text/html"):
    if not (cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID):
        return
    try:
        files = {"document": (filename, content_bytes, mime)}
        data = {"chat_id": cfg.TELEGRAM_CHAT_ID, "caption": caption}
        requests.post(
            f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendDocument",
            data=data, files=files, timeout=20
        )
    except Exception as e:
        print(f"[WARN] Telegram sendDocument falló: {e}", flush=True)

def page_screenshot_bytes(page) -> bytes:
    try:
        return page.screenshot(full_page=True)
    except Exception:
        try:
            return page.screenshot()
        except Exception:
            return b""


# =========================
# Detección de huecos
# =========================

def extract_real_slots(page) -> List[Tuple[str, str]]:
    slots = []
    try:
        candidates = page.locator(cfg.BUTTON_CANDIDATES)
        count = candidates.count()
    except Exception:
        count = 0

    for i in range(min(count, 400)):
        try:
            el = candidates.nth(i)
            if not el.is_visible():
                continue
            txt = (el.inner_text() or "").strip()
            if not txt:
                continue
            if "hueco libre" not in txt.lower():
                continue
            m = TIME_RE.search(txt)
            if m:
                slots.append((m.group(0), txt))
        except Exception:
            continue
    return slots


# =========================
# Flujo por Exteriores -> nueva pestaña (citaconsular.es)
# =========================

def goto_widget_from_exteriores(context, page, cons: Consulado):
    """
    1) Entra a Exteriores
    2) Click en “ELEGIR FECHA Y HORA”
    3) Captura la nueva pestaña que abre citaconsular.es
    4) Devuelve la page de citaconsular o None
    """
    # 1) Exteriores
    page.goto(cons.exteriores_url, wait_until="domcontentloaded", timeout=cfg.NAV_TIMEOUT_MS)
    hsleep()
    # a veces tarda en renderizar el enlace, esperamos un poco extra
    for _ in range(3):
        try:
            # 2) Esperar enlace visible
            page.wait_for_selector(cfg.LINK_ELEGIR, timeout=cfg.SEL_TIMEOUT_MS)
            break
        except PTimeout:
            hsleep(0.6, 1.2)
    # 3) Capturar nueva pestaña
    try:
        with context.expect_page() as new_page_event:
            page.click(cfg.LINK_ELEGIR, force=True)
        new_page = new_page_event.value
    except Exception:
        # Fallback: quizá abre en la misma pestaña
        new_page = page

    # 4) Esperar carga del widget
    try:
        new_page.wait_for_load_state("domcontentloaded", timeout=cfg.NAV_TIMEOUT_MS)
    except Exception:
        pass

    return new_page


def accept_dialogs(ctx):
    def _on_dialog(dialog):
        try:
            dialog.accept()
        except Exception:
            pass
    ctx.on("dialog", _on_dialog)


def revisar_consulado(context, cons: Consulado) -> Tuple[bool, List[Tuple[str, str]], Optional[str], Optional[str]]:
    """
    Devuelve: (hay_huecos, [(hora, txt_btn)...], fecha_textual, status_msg)
    Y envía evidencias por Telegram en caso de pantalla vacía / error / o hallazgo.
    """
    page = context.new_page()
    page.set_default_timeout(cfg.SEL_TIMEOUT_MS)

    # Rotar user-agent / viewport
    ua = random.choice(cfg.USER_AGENTS)
    vw = random.randint(1200, 1440)
    vh = random.randint(800, 960)
    context.set_default_navigation_timeout(cfg.NAV_TIMEOUT_MS)
    accept_dialogs(context)

    # viewport + headers para esta page
    page = context.new_page(
        user_agent=ua, viewport={"width": vw, "height": vh},
    )
    page.set_default_timeout(cfg.SEL_TIMEOUT_MS)

    # === EXTERIORES -> WIDGET ===
    try:
        new_page = goto_widget_from_exteriores(context, page, cons)
    except PTimeout:
        msg = f"⚠️ {cons.nombre}: Timeout al abrir Exteriores."
        notify(msg)
        try:
            img = page_screenshot_bytes(page)
            if img:
                send_photo(f"{cons.nombre}: Exteriores timeout", img, f"{cons.nombre.lower()}_timeout_ext.png")
        except Exception:
            pass
        try:
            html = (page.content() or "").encode("utf-8", "ignore")
            if html:
                send_document(f"{cons.nombre}: HTML exteriores (timeout)", html, f"{cons.nombre.lower()}_ext_timeout.html")
        except Exception:
            pass
        page.close()
        return (False, [], None, "timeout_exteriores")
    except Exception as e:
        msg = f"⚠️ {cons.nombre}: Error al entrar a Exteriores: {e}"
        notify(msg)
        try:
            img = page_screenshot_bytes(page)
            if img:
                send_photo(f"{cons.nombre}: Exteriores error", img, f"{cons.nombre.lower()}_err_ext.png")
        except Exception:
            pass
        try:
            html = (page.content() or "").encode("utf-8", "ignore")
            if html:
                send_document(f"{cons.nombre}: HTML exteriores (error)", html, f"{cons.nombre.lower()}_ext_error.html")
        except Exception:
            pass
        page.close()
        return (False, [], None, "error_exteriores")

    target = new_page

    # === Dentro del widget citaconsular ===
    # 1) aceptar alert de "Welcome/Bienvenido" si aparece (ya hay listener)
    hsleep()

    # 2) clic en CONTINUAR/CONTINUE si existe
    try:
        target.wait_for_selector(cfg.SELECTOR_CONTINUE, timeout=4000)
        target.click(cfg.SELECTOR_CONTINUE, force=True)
        hsleep(0.5, 1.2)
    except Exception:
        pass

    # 3) check de "No hay horas..."
    try:
        target.get_by_text(cfg.TEXT_NO_CITAS, exact=False).wait_for(timeout=3000)
        # evidencia rápida + salida
        html = (target.content() or "").encode("utf-8", "ignore")
        img = page_screenshot_bytes(target)
        if img:
            send_photo(f"{cons.nombre}: sin huecos (mensaje explícito)", img, f"{cons.nombre.lower()}_no_hay.png")
        if html:
            send_document(f"{cons.nombre}: HTML sin huecos", html, f"{cons.nombre.lower()}_no_hay.html")
        target.close()
        return (False, [], None, "no_hay_explicito")
    except Exception:
        pass

    # 4) extraer slots reales
    slots = extract_real_slots(target)

    # sacar una fecha visible si la hay (no siempre)
    fecha_text = None
    try:
        txt = (target.content() or "")
        m = re.search(r"(Lunes|Martes|Miércoles|Jueves|Viernes|Sábado|Domingo).*?\b\d{4}\b", txt, re.IGNORECASE | re.DOTALL)
        if m:
            fecha_text = m.group(0)
    except Exception:
        pass

    if slots:
        # Evidencias de hallazgo
        firsts = ", ".join(sorted({h for h, _ in slots})[:5])
        msg = f"✅ {cons.nombre}: ¡HUECOS! {f'({fecha_text})' if fecha_text else ''} Horas: {firsts}"
        notify(msg)
        try:
            img = page_screenshot_bytes(target)
            if img:
                send_photo(f"{cons.nombre}: huecos detectados", img, f"{cons.nombre.lower()}_huecos.png")
        except Exception:
            pass
        try:
            html = (target.content() or "").encode("utf-8", "ignore")
            if html:
                send_document(f"{cons.nombre}: HTML con huecos", html, f"{cons.nombre.lower()}_huecos.html")
        except Exception:
            pass
        target.close()
        return (True, slots, fecha_text, "ok")
    else:
        # si no hay texto "no hay horas" pero tampoco slots -> posible bloqueo (página vacía)
        html = ""
        try:
            html = target.content() or ""
        except Exception:
            pass

        text_len = len(html.strip())
        if text_len < 60:
            # Página en blanco o bloqueada
            try:
                img = page_screenshot_bytes(target)
                if img:
                    send_photo(f"{cons.nombre}: página vacía (bloqueo probable)", img, f"{cons.nombre.lower()}_blank.png")
            except Exception:
                pass
            try:
                if html is not None:
                    send_document(f"{cons.nombre}: HTML en blanco (posible bloqueo)",
                                  (html or "").encode("utf-8", "ignore"),
                                  f"{cons.nombre.lower()}_blank.html")
            except Exception:
                pass
            notify(f"⚠️ {cons.nombre}: página vacía tras reintentos (bloqueo probable). [html_len={text_len}]")
            target.close()
            return (False, [], fecha_text, "blank")
        else:
            # Contenido hay, pero sin huecos reales
            try:
                img = page_screenshot_bytes(target)
                if img:
                    send_photo(f"{cons.nombre}: sin huecos visibles por ahora", img, f"{cons.nombre.lower()}_sin_huecos.png")
            except Exception:
                pass
            target.close()
            return (False, [], fecha_text, "sin_huecos")


# =========================
# Bucle principal
# =========================

def main():
    # Test “estoy vivo”
    if cfg.FORCE_TEST == "1":
        notify("🚀 Test OK: el bot está listo y te enviará evidencias e IP cuando corresponda.")
        time.sleep(3)
        return

    while True:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)  # Railway/servidor
                context = browser.new_context(locale="es-ES", extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.7"})
                # ciclo por consulados
                for cons in cfg.CONSULADOS:
                    try:
                        ok, slots, fecha, status = revisar_consulado(context, cons)
                        marca = time.strftime("%Y-%m-%d %H:%M:%S")
                        if ok and slots:
                            primeras = ", ".join(sorted({h for h,_ in slots})[:5])
                            notify(f"[{marca}] {cons.nombre} -> HUECOS: {primeras}{f' ({fecha})' if fecha else ''}")
                            # si hay huecos, esperamos un poco más para no re-pegar de inmediato
                            time.sleep(60)
                        else:
                            notify(f"[{marca}] {cons.nombre} -> sin huecos por ahora.")
                        hsleep(1.0, 1.8)  # pequeña pausa entre consulados
                    except Exception as e_cons:
                        notify(f"⚠️ {cons.nombre}: error inesperado -> {e_cons}")
                        traceback.print_exc()
                        hsleep(1.0, 1.8)

                # cerrar y esperar próxima ronda
                try:
                    context.close()
                    browser.close()
                except Exception:
                    pass

            wait = random.randint(cfg.MIN_WAIT_BETWEEN_ROUNDS, cfg.MAX_WAIT_BETWEEN_ROUNDS)
            notify(f"[INFO] Esperando {wait}s antes de la siguiente ronda…")
            time.sleep(wait)

        except Exception as e:
            notify(f"[ERROR] ciclo principal: {e}")
            traceback.print_exc()
            time.sleep(90)


if __name__ == "__main__":
    main()
