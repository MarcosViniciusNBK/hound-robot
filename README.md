# Hound Robot — Banca de Testes (R$) & Autoaprendizado Lay

Laboratório analítico e banca simulada em Reais (`R$`) para corridas de galgos do Reino Unido utilizando dados públicos da **GBGB (Greyhound Board of Great Britain)**.

## Recursos Principais
- **Coleta Pública & Enriquecimento:** Sincroniza reuniões e resultados da GBGB (`api.gbgb.org.uk`) e histórico individual de cada galgo (janela de 30 dias).
- **Zero Vazamento Temporal (*No Data Leakage*):** Calcula 20 indicadores cronológicos (velocidade ajustada normalizada, *split*, forma ponderada, incidentes, vantagem de trap, treinador e comparação relativa ao páreo).
- **Ensemble Calibrado & Autoaprendizado:** Compete modelos (`LogisticCalibrated`, `HistGBDTCalibrated`, `EnsembleBlend` e variantes com peso nos erros) pelo menor *Log-Loss*, além de bloquear automaticamente perfis de pista/distância com ROI negativo persistente.
- **Simulador Conservador de Exchange:** Grade oficial de ticks da Betfair, comissão de `5%`, *spread* + `1` tick de *slippage*, filtro `+EV`, teto de risco de `15%` (`10%` em modo defensivo) e importador opcional para **Betfair Historical Data** (`data/betfair_hist/`).
- **Painel Web em Tempo Real:** Interface corporativa via WebSocket (`/ws`) mostrando corridas futuras travadas em `R$`, acertos/erros, banca atual, lucro/prejuízo e auditoria.

---

## Implantação em VPS Ubuntu (Passo a Passo Completo)

### Pré-requisitos na VPS
- Ubuntu/Debian com `docker`, `docker compose`, `git`, `curl` e `openssh-client` instalados.
- Porta `8000/tcp` livre.

### Implantação automatizada (1 script)
Na VPS (como `root`), baixe e execute [scripts/setup_vps.sh](scripts/setup_vps.sh):

```bash
curl -fsSL https://raw.githubusercontent.com/MarcosViniciusNBK/hound-robot/main/scripts/setup_vps.sh | bash
```

O script executa automaticamente:
1. Clona o repositório em `/opt/hound-robot` (ou faz `git pull --ff-only` se já existir).
2. Cria `./data/models` e `./data/betfair_hist` com permissão do usuário não-root do container (`chown -R 10001:10001 data`).
3. Libera a porta `8000/tcp` no `ufw` (se ativo).
4. Constrói e sobe o container `hound-robot` em segundo plano (`docker compose up -d --build`).
5. Gera uma chave SSH exclusiva (`/root/.ssh/hound_deploy_ed25519`), configura o host `github-hound` em `/root/.ssh/config` e aponta o `origin` para `git@github-hound:MarcosViniciusNBK/hound-robot.git`.
6. Instala o script `/usr/local/bin/hound-daily-backup.sh` e o cron diário `/etc/cron.d/hound-robot-backup` (todo dia às `23:55`), que faz *checkpoint* do SQLite via `POST http://127.0.0.1:8000/api/admin/checkpoint`, commita `data/hound_robot.db` e `data/models/latest_model.joblib` e envia `git push origin main`.
7. Executa os testes dentro do container (`pytest`) e exibe a chave pública de deploy.

### Passo final no GitHub (uma única vez por VPS)
1. Copie a chave pública exibida ao final do script (`cat /root/.ssh/hound_deploy_ed25519.pub`).
2. No GitHub do repositório, abra **Settings → Deploy keys → Add deploy key**.
3. Cole a chave e marque **"Allow write access"**.
4. Teste o backup manualmente na VPS:
   ```bash
   /usr/local/bin/hound-daily-backup.sh
   ```

### Operação e Manutenção na VPS
- **Logs do robô:** `cd /opt/hound-robot && docker compose logs -f --tail=100`
- **Logs do backup diário:** `cat /var/log/hound-backup.log`
- **Atualizar código na VPS após mudanças:**
  ```bash
  cd /opt/hound-robot && git pull --rebase origin main && docker compose up -d --build
  ```

---

## Implantação Local com Docker (Windows / Mac / Linux)

### 1. Subir o robô em 1 comando
```powershell
cd C:\Users\chanx\hound-robot
docker compose up -d --build
```
Acesse no navegador: `http://127.0.0.1:8000`

*(Se estiver usando o servidor Python local diretamente na porta `8000`, pare-o antes de subir o container na mesma porta.)*

### 2. Commit e Push Automático Diário no Windows
Execute uma vez no PowerShell para agendar o backup diário às `23:55` usando suas credenciais atuais do Git:
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_daily_task.ps1
```
Ou execute manualmente quando quiser:
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\daily_backup_commit.ps1
```

### 3. Alternativa via Sidecar Docker (`git-autocommit`)
Caso prefira rodar o backup em container separado em um servidor Linux:
1. Copie `.env.example` para `.env` e preencha `GITHUB_TOKEN`, `GIT_AUTHOR_NAME` e `GIT_AUTHOR_EMAIL`.
2. Suba com o profile `autocommit`:
   ```bash
   docker compose --profile autocommit up -d --build
   ```

---

## Execução Local (sem Docker)
```powershell
cd C:\Users\chanx\hound-robot
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Testes
```powershell
.\.venv\Scripts\python.exe -m pytest -v -p no:cacheprovider
```
