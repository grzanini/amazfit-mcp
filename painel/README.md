# Painel Corpo e Dieta

Painel estilo WHOOP que cruza alimentação, peso e dados do relógio Amazfit T-Rex 3.

## Onde cada coisa fica

| O quê | Onde | Observação |
|---|---|---|
| Página (HTML/CSS/JS) | `index.html` aqui e publicada como artefato no claude.ai | A cópia aqui é a fonte; a publicada é a que você abre no celular |
| Dados vivos (refeições, treinos, pesagens, metas, notas diárias do relógio) | Banco de dados do artefato no claude.ai (coleções `meals`, `workouts`, `weights`, `settings`, `bio`, `bio_meta`) | É o que a página lê. Claude grava lá pelo chat; a página também grava pelos formulários |
| Backup dos dados | `data/backup_AAAA-MM-DD.json`, `data/bio_2026-10.json`, `data/bio_meta.json` | Cópia feita pelo Claude a pedido; não é automática |
| Dados brutos do relógio | `../` (coletor amazfit-mcp, lê a nuvem do Zepp) e `data/zepp_export_2026-10-31/` (export CSV manual) | O coletor é a fonte principal a partir de nov/2026 |
| Scripts de cálculo | `scripts/` | Reproduzem as notas (Recuperação, Esforço, Sono) a partir dos dados do coletor |

## Fluxo de atualização (v3, desde 01/11/2026)

1. O relógio sincroniza com o app Zepp; o app manda para a nuvem Zepp (a HRV da noite costuma chegar horas depois de acordar).
2. `scripts/daily_pull.sh` (agendado pelo launchd `launchd/com.example.amazfit-daily.plist` às 09:30 e 21:30) puxa os últimos 45 dias
   pelo coletor (`get_daily_summary`, `get_health_metrics`, `get_heart_rate_minutes`, `get_hrv_samples`, `list_workouts`), roda
   `scripts/engine.py` e grava `data/raw/`, `data/bio/`, `data/weekly/`, `data/bio_meta.json` e `data/publish_queue.json`.
   Log em `logs/daily_pull.log` e `~/Library/Logs/amazfit/`.
3. "atualiza o painel" no chat: o Claude sincroniza os treinos manuais do banco do artefato para `data/manual/workouts.json`,
   roda o passo 2 e grava no banco do artefato os documentos listados em `publish_queue.json` (bio, weekly, bio_meta).
4. Refeições, jiu-jitsu (com sRPE e blocos) e pesagens entram pelo chat ou pelos formulários da página.

## Fórmulas (engine v3, resumo)

- **Esforço (0–21)** = 21·(1 − e^(−T/175)). T = TRIMP de Banister minuto a minuto nos minutos acordado com FC acima de 30% da
  reserva (`HRR·0,64·e^(1,92·HRR)`) + musculação (sRPE × min × 0,15; sRPE padrão 5) + jiu-jitsu (blocos: técnica 50% FCR, rola leve
  65%, moderada 76%, dura 84%, pausas 50%; ou sRPE × min × 0,25). Faixa ideal do dia pela recuperação: verde 13,5–17,5, amarelo 9,5–13,5,
  vermelho até 9,5.
- **Recuperação (0–100)** = 100/(1+e^(−(1,6·z̄+0,4))), z̄ = 0,60·z_HRV + 0,20·z_FCrep + 0,15·z_sono + 0,05·z_pele; z contra mediana/MAD dos
  30 dias anteriores (ln HRV, piso 0,10; FC repouso, piso 2 bpm). HRV = RMSSD por minuto ponderada (×2 em sono profundo, peso crescente
  na noite). Verde ≥ 67, amarelo 34–66, vermelho ≤ 33.
- **Sono**: necessidade = 450 + 4·max(0, esforço_ontem − 8) + dívida (14 noites, pesos 0,75^k, teto 120). Desempenho = dormido/necessidade;
  eficiência = dormido/(dormido+acordado); consistência = 100 − desvio médio dos horários (4 noites)/1,2.
- **Carga crônica**: CTL (τ 42) / ATL (τ 7) / TSB; ACWR 7:28 (zona 0,8–1,3); monotonia e strain de Foster.
- **Gasto (página)**: TMB da balança × 1,35 + kcal dos treinos; ou gasto adaptativo pela tendência de peso quando há ≥2 pesagens e ≥10 dias de refeições.

## Limitações conhecidas

- Coletor não oficial (engenharia reversa da API do Zepp); pode quebrar quando o Zepp mudar algo.
- O T-Rex 3 não mede frequência respiratória nem SpO2 noturno contínuo; recuperação física/mental vem como 255 (sem dado).
- Jiu-jitsu sem sensor entra por estimativa (blocos ou sRPE). Calibrar com 4–6 sessões de Polar Verity Sense.
- O launchd só calcula localmente; a gravação no artefato ainda passa pelo chat.
- Calorias de refeições são estimativas (arredondadas para cima, por escolha do usuário).
