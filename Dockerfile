FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

ENV MUSIC_DIR=/music
ENV LYRICDARR_DB=/config/lyricdarr.db
ENV SCAN_INTERVAL_HOURS=6

EXPOSE 8686

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8686"]
