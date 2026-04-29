#!/bin/bash
pip install -r requirements.txt
mkdir -p fonts
FONT_URL="https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.tar.bz2"
echo "[build] Downloading DejaVu fonts..."
curl -fL "$FONT_URL" | tar -xj -C /tmp/
cp /tmp/dejavu-fonts-ttf-2.37/ttf/DejaVuSans.ttf fonts/
cp /tmp/dejavu-fonts-ttf-2.37/ttf/DejaVuSans-Bold.ttf fonts/
echo "[build] Fonts ready: $(ls fonts/)"
