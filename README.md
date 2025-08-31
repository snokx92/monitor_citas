
# Monitor de Citas

Bot en **Python + Playwright** que revisa la página de citas de citaconsular.es y **envía alerta por Telegram** solo cuando detecta **huecos reales** (botón con hora + texto "Hueco libre").

## 1) Ejecutarlo en tu PC

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Mac/Linux:
source .venv/bin/activate

pip install -r requirements.txt
playwright install

# Opcional: variables para Telegram
# Windows PowerShell:
#   setx TELEGRAM_BOT_TOKEN "123:ABC"
#   setx TELEGRAM_CHAT_ID "999999999"
# Mac/Linux:
#   export TELEGRAM_BOT_TOKEN="123:ABC"
#   export TELEGRAM_CHAT_ID="999999999"

python monitor_citas_huecos.py
```

## 2) Subirlo a Render (24/7)

1. Sube esta carpeta a un repo de **GitHub**.
2. En **Render** → **New +** → **Web Service** → conecta tu repo.
3. **Build Command**:
   ```bash
   pip install -r requirements.txt
   ```
4. **Start Command**: Render usa el `Procfile` automáticamente (`worker: sh start.sh`).
5. **Environment Variables** en Render:
   - `TELEGRAM_BOT_TOKEN` → token de @BotFather
   - `TELEGRAM_CHAT_ID` → tu chat_id (con @userinfobot)
   - `CHECK_INTERVAL_SEC` → 60 (o el que prefieras)
6. Deploy → Ver **Logs**. Verás mensajes como _"Sin huecos reales por ahora."_


## Notas

- El script toma una captura `citas_disponibles.png` cuando encuentra huecos.
- Si el portal cambia textos/estilos, avísanos para ajustar el detector (palabras como “Hueco libre”).

