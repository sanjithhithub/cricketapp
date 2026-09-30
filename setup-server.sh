#!/usr/bin/env bash
set -euo pipefail

cd ~/cricketapp

# Replace the JWT placeholder with a secret generated here on the box, so the
# real value never passes through a workstation shell, a transcript, or git.
if grep -q '^JWT_SECRET_KEY=REPLACED_ON_SERVER$' .env; then
  NEW_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')"
  sed -i "s|^JWT_SECRET_KEY=.*|JWT_SECRET_KEY=${NEW_SECRET}|" .env
  echo "JWT secret generated and installed (value withheld)"
else
  echo "JWT_SECRET_KEY already set, leaving alone"
fi

chmod 600 .env
chmod 600 firebase-service-account.json
chmod +x deploy.sh

echo "--- verify .env shape (values masked) ---"
grep -E '^(DATABASE_URL|CORS_ORIGINS|SMTP_HOST)=' .env
awk -F= '/^JWT_SECRET_KEY=/{printf "JWT_SECRET_KEY=<%d chars>\n", length($2)}' .env
echo "--- files ---"
ls -la
