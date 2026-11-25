#!/bin/bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/hound-robot}"
REPO_HTTPS="${REPO_HTTPS:-https://github.com/MarcosViniciusNBK/hound-robot.git}"
REPO_SSH_HOST="${REPO_SSH_HOST:-github-hound}"
REPO_PATH="${REPO_PATH:-MarcosViniciusNBK/hound-robot.git}"
BRANCH="${BRANCH:-main}"
KEY_PATH="/root/.ssh/hound_deploy_ed25519"
CRON_TIME="${CRON_TIME:-55 23}"

echo "[1/6] Clonando ou atualizando repositorio em ${APP_DIR}..."
if [ ! -d "${APP_DIR}/.git" ]; then
  git clone "${REPO_HTTPS}" "${APP_DIR}"
else
  git -C "${APP_DIR}" pull --ff-only origin "${BRANCH}"
fi

cd "${APP_DIR}"

echo "[2/6] Ajustando permissoes do volume persistente (uid 10001)..."
mkdir -p data/models data/betfair_hist
chown -R 10001:10001 data

echo "[3/6] Liberando porta 8000/tcp no UFW (se ativo)..."
if command -v ufw >/dev/null 2>&1; then
  ufw allow 8000/tcp >/dev/null 2>&1 || true
fi

echo "[4/6] Subindo container Docker..."
docker compose up -d --build

echo "[5/6] Configurando chave de deploy SSH dedicada e cron diario..."
mkdir -p /root/.ssh
chmod 700 /root/.ssh
if [ ! -f "${KEY_PATH}" ]; then
  ssh-keygen -t ed25519 -C "hound-robot-vps-deploy" -f "${KEY_PATH}" -N ""
fi

if ! grep -q "Host ${REPO_SSH_HOST}" /root/.ssh/config 2>/dev/null; then
  cat << EOF >> /root/.ssh/config
Host ${REPO_SSH_HOST}
  HostName github.com
  User git
  IdentityFile ${KEY_PATH}
  IdentitiesOnly yes
EOF
  chmod 600 /root/.ssh/config
fi

git config user.name "${GIT_AUTHOR_NAME:-Hound Robot VPS}"
git config user.email "${GIT_AUTHOR_EMAIL:-hound-robot@users.noreply.github.com}"
git remote set-url origin "git@${REPO_SSH_HOST}:${REPO_PATH}"

cat << EOF > /usr/local/bin/hound-daily-backup.sh
#!/bin/bash
set -euo pipefail
cd ${APP_DIR}
curl -fsS -X POST http://127.0.0.1:8000/api/admin/checkpoint >/dev/null || true
git add data/hound_robot.db data/models/latest_model.joblib
if [ -n "\$(git status --porcelain -- data/hound_robot.db data/models/latest_model.joblib)" ]; then
  STAMP="\$(date '+%Y-%m-%d %H:%M')"
  git commit -m "chore(data): estudo diario vps (\${STAMP})"
  git push origin ${BRANCH}
else
  git push origin ${BRANCH} || true
fi
EOF
chmod +x /usr/local/bin/hound-daily-backup.sh

cat << EOF > /etc/cron.d/hound-robot-backup
${CRON_TIME} * * * root /usr/local/bin/hound-daily-backup.sh >> /var/log/hound-backup.log 2>&1
EOF
chmod 644 /etc/cron.d/hound-robot-backup

echo "[6/6] Validando healthcheck e testes no container..."
docker compose ps
docker exec -e HOUND_DISABLE_BG_LOOP=1 hound-robot pytest -v -p no:cacheprovider

echo ""
echo "================================================================"
echo "Deploy concluido! Adicione esta chave publica no GitHub:"
echo "Settings -> Deploy keys -> Add deploy key (Allow write access)"
echo "----------------------------------------------------------------"
cat "${KEY_PATH}.pub"
echo "================================================================"
