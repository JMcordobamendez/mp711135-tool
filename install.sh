#!/usr/bin/env bash
# Installer for the MP711135 tool on a Linux host (tested on Raspberry Pi OS).
#
# Sets up:
#   - a Python venv with the backend dependencies
#   - a udev rule that gives the MP711135's USB-serial adapter a stable path
#     at /dev/mp711135 (auto-detected from whatever adapter is plugged in
#     when this script runs, so it isn't hardcoded to one chipset)
#   - the invoking user's membership in the `dialout` group
#   - two systemd services (backend + frontend) enabled to start on boot
#
# Run as your normal user, NOT with sudo — it calls sudo itself only for the
# steps that need root (udev rule, group membership, systemd units).
set -euo pipefail

if [ "$EUID" -eq 0 ]; then
  echo "No ejecutes este script con sudo/root; pedirá permisos cuando los necesite." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="$(id -un)"
UDEV_RULE_FILE="/etc/udev/rules.d/99-mp711135.rules"
FALLBACK_VENDOR="1a86"   # CH340
FALLBACK_PRODUCT="7523"

echo "== Repo: $REPO_DIR"
echo "== Usuario: $TARGET_USER"

echo
echo "== 1/5 Entorno virtual e instalación de dependencias del backend"
if [ ! -d "$REPO_DIR/.venv" ]; then
  python3 -m venv "$REPO_DIR/.venv"
fi
"$REPO_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$REPO_DIR/.venv/bin/pip" install --quiet -r "$REPO_DIR/backend/requirements.txt"

echo
echo "== 2/5 Regla udev para una ruta estable del dispositivo (/dev/mp711135)"
detect_usb_serial_ids() {
  for dev in /dev/ttyUSB*; do
    [ -e "$dev" ] || continue
    local syspath
    syspath="$(udevadm info -q path -n "$dev" 2>/dev/null)" || continue
    local info vendor product
    info="$(udevadm info -a -p "$syspath" 2>/dev/null)"
    vendor="$(echo "$info" | grep -m1 'ATTRS{idVendor}==' | sed -E 's/.*=="([0-9a-f]+)".*/\1/')"
    product="$(echo "$info" | grep -m1 'ATTRS{idProduct}==' | sed -E 's/.*=="([0-9a-f]+)".*/\1/')"
    if [ -n "$vendor" ] && [ -n "$product" ]; then
      echo "$vendor:$product"
      return 0
    fi
  done
  return 1
}

if IDS="$(detect_usb_serial_ids)"; then
  VENDOR="${IDS%%:*}"
  PRODUCT="${IDS##*:}"
  echo "Adaptador USB-serie detectado: idVendor=$VENDOR idProduct=$PRODUCT"
else
  VENDOR="$FALLBACK_VENDOR"
  PRODUCT="$FALLBACK_PRODUCT"
  echo "Aviso: no se detectó ningún /dev/ttyUSB* conectado ahora mismo." >&2
  echo "Se usará el ID por defecto (CH340: $VENDOR:$PRODUCT)." >&2
  echo "Si tu adaptador es otro, conéctalo y vuelve a ejecutar este script," >&2
  echo "o edita $UDEV_RULE_FILE a mano con el idVendor/idProduct de 'lsusb'." >&2
fi

sudo tee "$UDEV_RULE_FILE" >/dev/null <<EOF
SUBSYSTEM=="tty", ATTRS{idVendor}=="$VENDOR", ATTRS{idProduct}=="$PRODUCT", SYMLINK+="mp711135"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger

echo
echo "== 3/5 Permisos del puerto serie (grupo dialout)"
if ! id -nG "$TARGET_USER" | grep -qw dialout; then
  sudo usermod -aG dialout "$TARGET_USER"
  echo "Añadido $TARGET_USER al grupo dialout (hace falta cerrar sesión y volver a entrar, o reiniciar, para que se aplique)."
else
  echo "$TARGET_USER ya pertenece al grupo dialout."
fi

echo
echo "== 4/5 Servicios systemd (arranque automático)"
sudo tee /etc/systemd/system/mp711135-backend.service >/dev/null <<EOF
[Unit]
Description=MP711135 backend (FastAPI + pyserial)
After=network.target

[Service]
Type=simple
User=$TARGET_USER
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/mp711135-frontend.service >/dev/null <<EOF
[Unit]
Description=MP711135 frontend (static HTTP server)
After=network.target

[Service]
Type=simple
User=$TARGET_USER
WorkingDirectory=$REPO_DIR/frontend
ExecStart=/usr/bin/env python3 -m http.server 8080 --bind 0.0.0.0
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable mp711135-backend.service mp711135-frontend.service
sudo systemctl restart mp711135-backend.service mp711135-frontend.service

echo
echo "== 5/5 Estado"
sudo systemctl --no-pager status mp711135-backend.service mp711135-frontend.service | cat

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Listo. Backend en http://$IP:8000, frontend en http://$IP:8080/MP711135.dc.html"
echo "Ambos servicios arrancarán solos en cada reinicio del equipo."
echo "Logs: journalctl -u mp711135-backend -f  /  journalctl -u mp711135-frontend -f"
