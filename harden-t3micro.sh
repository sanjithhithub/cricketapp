#!/usr/bin/env bash
# Hardening for a t3.micro free-tier box: ~7GB disk is the binding constraint,
# not RAM (the app uses ~95MB, the DB ~32MB).
set -euo pipefail

echo "== 1. Cap container log growth =="
# Without this, json-file logs grow forever and fill the root volume.
sudo mkdir -p /etc/docker
if [ ! -f /etc/docker/daemon.json ]; then
  sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
  echo "wrote /etc/docker/daemon.json"
else
  echo "daemon.json exists, leaving it alone"
fi

echo "== 2. Shrink swapfile 2G -> 1G =="
# Frees ~1.1GB of disk. Memory headroom is 450MB+, so 1G of swap is ample
# insurance against an OOM kill without eating a third of the disk.
CUR=$(free -m | awk '/Swap:/ {print $2}')
if [ "$CUR" -gt 1024 ]; then
  sudo swapoff /swapfile
  sudo rm -f /swapfile
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  echo "swapfile resized to 1G"
else
  echo "swap already <= 1G, skipping"
fi

echo "== 3. Drop the build-only base image =="
# python:3.12-slim is only needed to build. GitHub Actions builds the image, so
# this server never needs it again.
sudo docker image rm python:3.12-slim 2>/dev/null || echo "python base already gone"

echo "== 4. Disk guard =="
# If the volume creeps toward full, prune rather than fail. Postgres refuses to
# accept writes on a full disk, which would take the app down.
sudo tee /usr/local/bin/disk-guard.sh >/dev/null <<'GUARD'
#!/usr/bin/env bash
# Prune Docker reclaimable data when the root volume passes 90%.
USE=$(df --output=pcent / | tail -1 | tr -dc '0-9')
if [ "$USE" -ge 90 ]; then
  echo "$(date -Is) disk at ${USE}% - pruning" >> /var/log/disk-guard.log
  docker image prune -f >/dev/null 2>&1 || true
  docker builder prune -f >/dev/null 2>&1 || true
  sudo journalctl --vacuum-size=50M >/dev/null 2>&1 || true
fi
GUARD
sudo chmod +x /usr/local/bin/disk-guard.sh
sudo tee /etc/cron.d/disk-guard >/dev/null <<'CRON'
*/30 * * * * root /usr/local/bin/disk-guard.sh
CRON
echo "disk guard installed (every 30 min, prunes at >=90%)"

echo "== 5. Restart docker so log caps apply to containers =="
sudo systemctl restart docker

echo "== done =="
