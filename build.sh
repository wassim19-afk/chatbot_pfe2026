#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "[build] Updating apt package lists"
apt-get update

echo "[build] Installing Linux packages required for pyodbc and SQL Server ODBC"
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  gnupg \
  unixodbc \
  unixodbc-dev \
  gcc \
  g++ \
  apt-transport-https

echo "[build] Adding Microsoft package repository for ODBC Driver 18"
. /etc/os-release
if [[ "${ID:-}" == "debian" ]]; then
  MS_REPO_URL="https://packages.microsoft.com/config/debian/${VERSION_ID}/prod.list"
elif [[ "${ID:-}" == "ubuntu" ]]; then
  MS_REPO_URL="https://packages.microsoft.com/config/ubuntu/${VERSION_ID}/prod.list"
else
  echo "[build] Unsupported distro '${ID:-unknown}', defaulting to Debian 12 Microsoft repo"
  MS_REPO_URL="https://packages.microsoft.com/config/debian/12/prod.list"
fi

curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg
curl -fsSL "$MS_REPO_URL" | sed 's#^deb https://packages.microsoft.com#deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com#' > /etc/apt/sources.list.d/microsoft-prod.list

echo "[build] Installing msodbcsql18"
apt-get update
ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18

echo "[build] Verifying ODBC drivers visible to unixODBC"
if command -v odbcinst >/dev/null 2>&1; then
  odbcinst -q -d || true
else
  echo "[build] odbcinst not available"
fi

python - <<'PY'
import pyodbc
print('pyodbc.drivers() =', pyodbc.drivers())
PY

echo "[build] Installing Python dependencies"
pip install -r requirements.txt

echo "[build] Build script complete"
