# App "Baixar Músicas" — imagem para rodar online (Coolify/Docker).
FROM python:3.12-slim

# ffmpeg (conversão/normalização) + yt-dlp (download), via binário oficial.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
 && curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp \
 && chmod a+rx /usr/local/bin/yt-dlp \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py .

# Servidor: escuta em todas as interfaces, modo online (sem "Salvar no Mac").
ENV BAIXAR_HOST=0.0.0.0 \
    BAIXAR_PORT=8420 \
    BAIXAR_ONLINE=1

EXPOSE 8420
CMD ["python", "app.py"]
