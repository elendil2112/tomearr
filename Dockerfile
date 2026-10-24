# --- build the React UI ---------------------------------------------------
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim

# Calibre's command-line tools (calibredb, ebook-convert, ebook-meta) from calibre's official build, so they
# match a current Calibre desktop / Calibre-Web install. Pin a version with --build-arg CALIBRE_VERSION=8.x.y;
# empty means the latest release. bsdtar unpacks zip/rar/7z downloads.
ARG CALIBRE_VERSION=""
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates wget xz-utils libarchive-tools \
      libegl1 libopengl0 libgl1 libglx0 libxcb-cursor0 libxkbcommon0 libfontconfig1 libfreetype6 \
      libnss3 libxcomposite1 libxdamage1 libxrandr2 libxtst6 libxkbfile1 libasound2 libdbus-1-3 \
 && wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh \
      | sh /dev/stdin install_dir=/opt isolated=y ${CALIBRE_VERSION:+version=$CALIBRE_VERSION} \
 && apt-get purge -y wget xz-utils && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* /tmp/calibre-installer-cache

ENV PYTHONUNBUFFERED=1 \
    TOMEARR_CONFIG=/config \
    TOMEARR_DATA=/data \
    TOMEARR_STATIC=/app/static \
    PATH=/opt/calibre:$PATH \
    QT_QPA_PLATFORM=offscreen \
    TOMEARR_CALIBRE_CONFIG=/config/calibre-cli
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=ui /ui/dist ./static
VOLUME ["/config", "/data"]
EXPOSE 8222
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8222"]
