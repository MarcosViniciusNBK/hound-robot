# Hound Robot — Estudo & Análise Lay em Tempo Real

Laboratório analítico e simulador (*paper trading*) para corridas de galgos do Reino Unido utilizando dados públicos da **GBGB (Greyhound Board of Great Britain)**.

## Recursos Principais
- **Coleta Pública & Enriquecimento:** Sincroniza reuniões e resultados da GBGB (`api.gbgb.org.uk`) e histórico individual de corridas de cada galgo.
- **Zero Vazamento Temporal (*No Data Leakage*):** Todas as *features* (velocidade normalizada por pista/distância, *split* de largada, forma ponderada, incidentes, taxa da trap e treinador) são calculadas estritamente a partir de corridas anteriores ao horário do páreo analisado.
- **Previsão Calibrada & Seleção Lay:** Treina modelos probabilísticos (*Log-Loss* + normalização *softmax* por páreo) e realiza avaliação *walk-forward out-of-sample* para indicar:
  - **Resultado Provável** (favorito + pódio 1º/2º/3º + Fair Odd)
  - **Hound que Não Ganhará** (candidato principal ao Lay, probabilidade de derrota, EV e auditoria de erros)
- **Painel Web em Tempo Real:** Interface com WebSocket (`/ws`) que exibe o andamento de cada ciclo de estudo ao vivo, corridas do dia, curva da banca virtual e simulador de páreos futuros.

## Como Executar
```powershell
cd C:\Users\chanx\hound-robot
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```
Acesse no navegador: `http://127.0.0.1:8000`

## Testes
```powershell
.\.venv\Scripts\python.exe -m pytest -v
```
