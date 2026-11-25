# Hound Robot — Banca de Testes (R$) & Autoaprendizado Lay

Laboratório analítico e banca simulada em Reais (`R$`) para corridas de galgos do Reino Unido utilizando dados públicos da **GBGB (Greyhound Board of Great Britain)**.

## Recursos Principais
- **Coleta Pública & Enriquecimento:** Sincroniza reuniões e resultados da GBGB (`api.gbgb.org.uk`) e histórico individual de cada galgo (janela de 30 dias).
- **Zero Vazamento Temporal (*No Data Leakage*):** Calcula 20 indicadores cronológicos (velocidade ajustada normalizada, *split*, forma ponderada, incidentes, vantagem de trap, treinador e comparação relativa ao páreo).
- **Ensemble Calibrado & Autoaprendizado:** Compete modelos (`LogisticCalibrated`, `HistGBDTCalibrated`, `EnsembleBlend` e variantes com peso nos erros) pelo menor *Log-Loss*, além de bloquear automaticamente perfis de pista/distância com ROI negativo persistente.
- **Simulador Conservador de Exchange:** Grade oficial de ticks da Betfair, comissão de `5%`, *spread* + `1` tick de *slippage*, filtro `+EV`, teto de risco de `15%` (`10%` em modo defensivo) e importador opcional para **Betfair Historical Data** (`data/betfair_hist/`).
- **Painel Web em Tempo Real:** Interface corporativa via WebSocket (`/ws`) mostrando corridas futuras travadas em `R$`, acertos/erros, banca atual, lucro/prejuízo e auditoria.

---

## Implantação Rápida com Docker (Recomendado)

### 1. Subir o robô em 1 comando
```powershell
cd C:\Users\chanx\hound-robot
docker compose up -d --build
```
Acesse no navegador: `http://127.0.0.1:8000`

*(Se estiver usando o servidor Python local diretamente na porta `8000`, pare-o antes de subir o container na mesma porta. Em servidores Linux, garanta permissão na pasta persistente: `mkdir -p data/models data/betfair_hist && chown -R 10001:10001 data`.)*

### 2. Commit e Push Automático Diário

* **No Windows (recomendado — usa suas credenciais atuais do Git):**
  Execute uma vez no PowerShell para agendar o backup diário às `23:55`:
  ```powershell
  powershell -ExecutionPolicy Bypass -File .\scripts\install_daily_task.ps1
  ```
  Ou rode manualmente a qualquer momento:
  ```powershell
  powershell -ExecutionPolicy Bypass -File .\scripts\daily_backup_commit.ps1
  ```

* **Em Servidor Linux / VPS (via container sidecar):**
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
