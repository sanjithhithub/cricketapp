#!/bin/bash
# One-time EC2 setup for CricketApp (Ubuntu 24.04).
# Usage:  bash scripts/setup_ec2.sh
# Requires: DOMAIN, GIT_REPO_SSH, POSTGRES_PASSWORD, TWO_FACTOR_API_KEY exported
set -euo pipefail

DOMAIN="${DOMAIN:-}"
GIT_REPO_SSH="${GIT_REPO_SSH:-}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"
TWO_FACTOR_API_KEY="${TWO_FACTOR_API_KEY:-}"
TWO_FACTOR_OTP_TEMPLATE="${TWO_FACTOR_OTP_TEMPLATE:-}"

if [ -z "$DOMAIN" ] || [ -z "$GIT_REPO_SSH" ] || [ -z "$POSTGRES_PASSWORD" ]; then
    echo "ERROR: export these variables first: DOMAIN, GIT_REPO_SSH, POSTGRES_PASSWORD (TWO_FACTOR_API_KEY optional)" >&2
    exit 1
fi

echo ">> Installing Docker..."
sudo apt-get update -y
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update -y
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
sudo systemctl enable --now docker

echo ">> Generating a GitHub deploy key for this repo..."
if [ ! -f ~/.ssh/cricketapp_github ]; then
    ssh-keygen -t ed25519 -N "" -C "cricketapp-deploy" -f ~/.ssh/cricketapp_github
fi
cat ~/.ssh/cricketapp_github.pub

mkdir -p ~/cricketapp
cd ~/cricketapp

read -r -p ">> Add the public key above to GitHub: repo -> Settings -> Deploy keys -> Add deploy key (Read access). Then press Enter to continue..." _

ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null || true

echo ">> Cloning repository..."
if [ ! -d .git ]; then
    GIT_SSH_COMMAND="ssh -i ~/.ssh/cricketapp_github -o IdentitiesOnly=yes" git clone "$GIT_REPO_SSH" .
fi
git remote set-url origin "$GIT_REPO_SSH"

echo ">> Writing .env..."
cat > .env <<EOF
DATABASE_URL=postgresql+asyncpg://cricket:${POSTGRES_PASSWORD}@cricketdb:5432/cricketdb
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
TWO_FACTOR_API_KEY=${TWO_FACTOR_API_KEY}
TWO_FACTOR_OTP_TEMPLATE=${TWO_FACTOR_OTP_TEMPLATE}
DOMAIN=${DOMAIN}
EOF
chmod 600 .env

echo ">> Issuing SSL certificate..."
bash deploy/init-ssl.sh "$DOMAIN"

echo ">> Starting the stack..."
docker compose -f docker-compose.prod.yml up --build -d

echo ">> Scheduling nightly backups..."
crontab -l 2>/dev/null | { cat; echo "0 3 * * * /home/ubuntu/cricketapp/deploy/backup.sh >> /home/ubuntu/backups/backup.log 2>&1"; } | crontab -
mkdir -p ~/backups

echo ">> Waiting for the API to come up..."
for i in $(seq 1 30); do
    if curl -fsS "https://$DOMAIN/health" >/dev/null 2>&1; then
        echo ">> LIVE: https://$DOMAIN"
        exit 0
    fi
    sleep 5
done
echo ">> API did not respond yet. Check: docker compose -f docker-compose.prod.yml logs"
exit 1
