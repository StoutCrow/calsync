#!/usr/bin/env bash
# calsync home-server setup. Run once on the server (Linux), from this folder:
#     bash install-server.sh
# Safe to run again: it updates packages and the service, and keeps your settings.
#
# What it does:
#   1. makes a private Python environment here (nothing installed system-wide)
#   2. creates config/calsync.env with a private access code (if you have none yet)
#   3. finds Immich on this server and points calsync at it
#   4. runs calsync as a background service that starts at boot
#   5. shares it ONLY on your Tailscale network, over https, with `tailscale serve`
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
PORT="${CALSYNC_PORT:-8766}"

say() { printf '\n== %s\n' "$*"; }
die() { printf '\nProblem: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null || die "python3 is missing. On Debian/Ubuntu: sudo apt install python3 python3-venv"

say "1/5 Python environment"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv || die "could not make a virtual environment. On Debian/Ubuntu: sudo apt install python3-venv"
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet anthropic pydantic pillow tzdata icalendar recurring-ical-events

say "2/5 Settings"
mkdir -p config
[ -f config/calsync.env ] || cp config/calsync.env.example config/calsync.env
chmod 600 config/calsync.env
# set KEY=value in config/calsync.env (replace the line if present, else append)
setenv() {
  if grep -q "^$1=" config/calsync.env; then
    sed -i "s|^$1=.*|$1=$2|" config/calsync.env
  else
    printf '%s=%s\n' "$1" "$2" >> config/calsync.env
  fi
}
getenv() { grep -m1 "^$1=" config/calsync.env | cut -d= -f2- || true; }
if [ -z "$(getenv CALSYNC_APP_TOKEN)" ]; then
  setenv CALSYNC_APP_TOKEN "$(.venv/bin/python -c 'import secrets;print(secrets.token_urlsafe(16))')"
fi
TOKEN="$(getenv CALSYNC_APP_TOKEN)"

say "3/5 Immich"
# Immich in Docker usually publishes port 2283 on this same machine.
if curl -fsS -m 3 http://127.0.0.1:2283/api/server/ping >/dev/null 2>&1; then
  setenv IMMICH_URL "http://127.0.0.1:2283"
  echo "Found Immich on this server (http://127.0.0.1:2283)."
  [ -n "$(getenv IMMICH_API_KEY)" ] || echo "Still needed: an Immich API key. Add it in calsync's Settings page."
else
  echo "Immich not found on port 2283 here. Skipping (set IMMICH_URL in Settings if it is elsewhere)."
fi

say "4/5 Background service"
if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/calsync.service" <<EOF
[Unit]
Description=calsync
After=network-online.target

[Service]
WorkingDirectory=$HERE
Environment=CALSYNC_PORT=$PORT
ExecStart=$HERE/.venv/bin/python $HERE/launcher.py --no-open
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable calsync.service >/dev/null
  systemctl --user restart calsync.service
  # lets the service run at boot without anyone logged in
  loginctl enable-linger "$USER" 2>/dev/null || sudo -n loginctl enable-linger "$USER" 2>/dev/null \
    || echo "To start at boot with nobody logged in, run once: sudo loginctl enable-linger $USER"
  STOPCMD="systemctl --user stop calsync"
  LOGCMD="journalctl --user -u calsync -f"
else
  echo "systemd not available; starting in the background instead (it will not restart after a reboot)."
  nohup env CALSYNC_PORT="$PORT" .venv/bin/python launcher.py --no-open > calsync.log 2>&1 &
  STOPCMD="pkill -f 'launcher.py --no-open'"
  LOGCMD="tail -f $HERE/calsync.log"
fi

for _ in $(seq 1 30); do
  curl -fsS -m 2 "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS -m 2 "http://127.0.0.1:$PORT/" >/dev/null 2>&1 || die "calsync did not start. See: $LOGCMD"

say "5/5 Tailscale"
URL="http://$(hostname):$PORT"
if command -v tailscale >/dev/null; then
  # calsync listens only on this machine (127.0.0.1); tailscale serve is the only way in.
  if tailscale serve --bg "$PORT" >/dev/null 2>&1 || sudo -n tailscale serve --bg "$PORT" >/dev/null 2>&1; then
    DNS="$(tailscale status --json 2>/dev/null | .venv/bin/python -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
    [ -n "$DNS" ] && URL="https://$DNS"
  else
    echo "Could not run 'tailscale serve'. Try:  sudo tailscale serve --bg $PORT"
    echo "If it says HTTPS is off, turn on HTTPS certificates in the Tailscale admin console (DNS page)."
  fi
else
  echo "Tailscale is not installed here. Install it from https://tailscale.com/download, run 'sudo tailscale up',"
  echo "then run this script again."
fi

cat <<EOF

Done.

  Open on your PC or phone (both need Tailscale on):
    $URL/#t=$TOKEN

  On iPhone: open that link in Safari, then Share > Add to Home Screen.
  Keep the part after # private: it is your access code.

  Stop:   $STOPCMD
  Logs:   $LOGCMD
  Update: copy the new calsync folder over this one, then run: bash install-server.sh
EOF
