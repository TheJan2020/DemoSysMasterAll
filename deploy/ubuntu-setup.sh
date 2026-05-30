#!/usr/bin/env bash
# PW Demo Master — Ubuntu 24.04 / Debian 12 VM bootstrap.
#
# Idempotent. Run on a fresh VM as root (or via sudo):
#
#     curl -fsSL https://raw.githubusercontent.com/TheJan2020/DemoSysMasterAll/main/deploy/ubuntu-setup.sh \
#       | sudo bash
#
# Or, after cloning manually:
#
#     sudo bash deploy/ubuntu-setup.sh
#
# After it finishes there are FOUR manual steps the script cannot do on its
# own (each requires interactive auth):
#
#   1. sudo tailscale up
#        (browser auth — get the VM on your tailnet)
#
#   2. cloudflared tunnel login
#        (browser auth — picks the Cloudflare zone for your domain)
#
#   3. cloudflared tunnel create pwdemo
#        Note the tunnel UUID it prints, then:
#
#        sudo install -m 600 /root/.cloudflared/<UUID>.json \
#                            /etc/cloudflared/<UUID>.json
#        sudo cp /opt/pwdemo/deploy/cloudflared-config.yml \
#                /etc/cloudflared/config.yml
#        sudo sed -i "s|YOUR_TUNNEL_ID|<UUID>|g; \
#                     s|pwdemo.yourdomain.com|<your hostname>|g" \
#                /etc/cloudflared/config.yml
#
#   4. cloudflared tunnel route dns pwdemo <your hostname>
#        sudo cloudflared service install
#
# Then edit /etc/pwdemo.env to set PW_MAIN_AUTH_USER / PW_MAIN_AUTH_PASS
# (these protect the master admin app + landing; sub-demos have their own
# login).  systemctl restart pwdemo.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/TheJan2020/DemoSysMasterAll.git}"
APP_USER="pwdemo"
APP_HOME="/opt/pwdemo"
APP_PORT="${APP_PORT:-8080}"
APP_BIND="${APP_BIND:-127.0.0.1}"   # cloudflared is local — don't bind public

step() { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root (or via sudo)." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
step "Installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    git curl ca-certificates lsb-release gnupg \
    ffmpeg \
    build-essential pkg-config \
    ufw

# ---------------------------------------------------------------------------
step "Creating system user '$APP_USER'"
if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "$APP_HOME" --shell /bin/bash "$APP_USER"
fi

# ---------------------------------------------------------------------------
step "Cloning / refreshing repo at $APP_HOME"
if [ ! -d "$APP_HOME/.git" ]; then
    # `useradd --create-home` already populated $APP_HOME with bash
    # skeleton dotfiles, so `git clone` would refuse the non-empty
    # target. Clone into a temp dir, then move everything (incl. .git)
    # into $APP_HOME and reassign ownership.
    TMP="$(mktemp -d)"
    git clone -q "$REPO_URL" "$TMP/repo"
    shopt -s dotglob nullglob
    cp -a "$TMP/repo/." "$APP_HOME/"
    shopt -u dotglob nullglob
    rm -rf "$TMP"
    chown -R "$APP_USER":"$APP_USER" "$APP_HOME"
else
    sudo -u "$APP_USER" git -C "$APP_HOME" fetch --all --prune
    # Don't reset --hard if the user opted into Mutagen sync (which mutates
    # tracked files). The setup script's job is bootstrap; updates come via
    # `git pull` or Mutagen, not from re-running this.
fi

# ---------------------------------------------------------------------------
step "Creating venv + installing requirements"
sudo -u "$APP_USER" python3 -m venv "$APP_HOME/.venv"
sudo -u "$APP_USER" "$APP_HOME/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_HOME/.venv/bin/pip" install --quiet \
    -r "$APP_HOME/backend/requirements.txt"

# ---------------------------------------------------------------------------
step "Installing cloudflared"
if ! command -v cloudflared >/dev/null 2>&1; then
    mkdir -p /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
        | tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
    CODENAME="$(. /etc/os-release; echo "${VERSION_CODENAME:-bookworm}")"
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared ${CODENAME} main" \
        > /etc/apt/sources.list.d/cloudflared.list
    apt-get update -qq
    apt-get install -y --no-install-recommends cloudflared
fi

# ---------------------------------------------------------------------------
step "Installing tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
fi

# ---------------------------------------------------------------------------
step "Writing /etc/pwdemo.env (edit me!)"
if [ ! -f /etc/pwdemo.env ]; then
    cat > /etc/pwdemo.env <<'EOF'
# Edge HTTP Basic Auth for the master admin app + /demo landing.
# Sub-demos (clinic, restaurant) have their own login layer.
PW_MAIN_AUTH_USER=admin
PW_MAIN_AUTH_PASS=change-me-before-first-boot
EOF
    chmod 600 /etc/pwdemo.env
    chown root:"$APP_USER" /etc/pwdemo.env
fi

# ---------------------------------------------------------------------------
step "Installing systemd unit"
sed -e "s|@APP_USER@|$APP_USER|g" \
    -e "s|@APP_HOME@|$APP_HOME|g" \
    -e "s|@APP_PORT@|$APP_PORT|g" \
    -e "s|@APP_BIND@|$APP_BIND|g" \
    "$APP_HOME/deploy/pwdemo.service" > /etc/systemd/system/pwdemo.service
systemctl daemon-reload
systemctl enable pwdemo.service
systemctl restart pwdemo.service

# ---------------------------------------------------------------------------
step "Configuring firewall (UFW)"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable >/dev/null

# ---------------------------------------------------------------------------
step "Done."
cat <<EOF

Next manual steps (interactive auth — script can't do these):
  1. sudo tailscale up
  2. cloudflared tunnel login
  3. cloudflared tunnel create pwdemo                  # note the UUID
     sudo install -m 600 /root/.cloudflared/<UUID>.json /etc/cloudflared/
     sudo cp $APP_HOME/deploy/cloudflared-config.yml /etc/cloudflared/config.yml
     sudo \$EDITOR /etc/cloudflared/config.yml          # set tunnel id + hostname
  4. cloudflared tunnel route dns pwdemo <hostname>
     sudo cloudflared service install

Master-app credentials live in /etc/pwdemo.env — edit then:
     sudo systemctl restart pwdemo

Status / logs:
     systemctl status pwdemo cloudflared
     journalctl -u pwdemo -f
EOF
