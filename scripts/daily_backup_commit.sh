#!/bin/sh
set -eu

REPO_DIR="/repo"
INTERVAL="${BACKUP_INTERVAL_SEC:-86400}"
BRANCH="${GIT_BRANCH:-main}"

git config --global --add safe.directory "$REPO_DIR"
cd "$REPO_DIR"

if [ -n "${GIT_AUTHOR_NAME:-}" ]; then
  git config user.name "$GIT_AUTHOR_NAME"
fi
if [ -n "${GIT_AUTHOR_EMAIL:-}" ]; then
  git config user.email "$GIT_AUTHOR_EMAIL"
fi

echo "[git-autocommit] Iniciado. Intervalo: ${INTERVAL}s | Branch: ${BRANCH}"

while true; do
  sleep "$INTERVAL"
  # Solicita checkpoint do SQLite antes do commit (falha silenciosa se offline)
  wget -qO- --post-data="" "http://hound-robot:8000/api/admin/checkpoint" >/dev/null 2>&1 || true

  git add data/hound_robot.db data/models/latest_model.joblib >/dev/null 2>&1 || true
  if [ -n "$(git status --porcelain -- data/hound_robot.db data/models/latest_model.joblib)" ]; then
    STAMP="$(date '+%Y-%m-%d %H:%M')"
    git commit -m "chore(data): estudo diario automatico (${STAMP})"
    if [ -n "${GITHUB_TOKEN:-}" ]; then
      REMOTE_URL="$(git remote get-url origin | sed "s#https://#https://x-access-token:${GITHUB_TOKEN}@#")"
      git push "$REMOTE_URL" "$BRANCH" >/dev/null 2>&1 || echo "[git-autocommit] Falha no push (verifique GITHUB_TOKEN)."
    else
      git push origin "$BRANCH" || echo "[git-autocommit] Falha no push (configure GITHUB_TOKEN ou chave SSH)."
    fi
    echo "[git-autocommit] Backup diario enviado em ${STAMP}."
  else
    echo "[git-autocommit] Nenhuma alteracao para commitar."
  fi
done
