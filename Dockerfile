FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install chromium

COPY . .

ENV DATA_DIR=/data
ENV PANEL_PASSWORD=admin
ENV TARGET_URL=https://voxi.co.uk/sim-only-plans

EXPOSE 8765

CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp & DISPLAY=:99 exec python start_webui.py --host 0.0.0.0 --port 8765"]
