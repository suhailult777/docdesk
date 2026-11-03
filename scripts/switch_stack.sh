#!/usr/bin/env bash
# Fetch the pinned Switch server stack and give it fresh secrets.
#
#   scripts/switch_stack.sh [dir]     (default: .switch-stack, git-ignored)
#
# Downloads the official standalone compose file and .env.example from the
# switch-v0.31.0 tag, pins SWITCH_VERSION=0.31.0 and fills every secret with
# openssl. An existing .env is never overwritten.
set -euo pipefail
dir="${1:-.switch-stack}"
tag="switch-v0.31.0"
raw="https://raw.githubusercontent.com/sandbox-quantum/switch/${tag}"
mkdir -p "$dir"
curl -fsSL "${raw}/deploy/local/standalone-docker-compose.yml" -o "$dir/docker-compose.yml"
if [ -f "$dir/.env" ]; then
  echo "$dir/.env exists, keeping it"
  exit 0
fi
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }
curl -fsSL "${raw}/.env.example" -o "$dir/.env"
chmod 600 "$dir/.env"
sed -i "s|^SWITCH_VERSION=.*|SWITCH_VERSION=0.31.0|" "$dir/.env"
for key in DB_PASSWORD DB_OWNER_PASSWORD AGENT_REGISTRATION_TOKEN GATEWAY_ADMIN_PASSWORD \
           MATTERMOST_ADMIN_PASSWORD MATTERMOST_USER_PASSWORD; do
  sed -i "s|^${key}=.*|${key}=$(openssl rand -hex 24)|" "$dir/.env"
done
sed -i "s|^SECRET_KEYS=.*|SECRET_KEYS=local:$(openssl rand -hex 32)|" "$dir/.env"
echo "wrote $dir/docker-compose.yml and $dir/.env (secrets generated, not printed)"
