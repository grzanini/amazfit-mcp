"""Gera o markdown da aba "Relógio" do Doc Registro Corpo e Dieta a partir de painel/data (últimos 14 dias + semana + hoje)."""
import json, glob, sys
from pathlib import Path
DATA = Path(__file__).resolve().parents[1] / "data"
def hm(m): return f"{int(m)//60}h{int(m)%60:02d}" if m else "-"
def main():
    rows=[]
    bios=sorted(glob.glob(str(DATA/"bio"/"*.json")))[-14:]
    for p in bios:
        d=json.load(open(p)); s=d["sleep"]; f=d["fitness"]
        zone={"green":"verde","yellow":"amarelo","red":"vermelho"}.get(d.get("recoveryZone"),"-")
        sess=", ".join(f"{x['label']} {x['minutes']}min" for x in d["sessions"]) or "-"
        rows.append(f"| {d['date']} | {d.get('recovery') or '-'} ({zone}) | {d.get('hrv') or '-'} / {d.get('hrvBase') or '-'} | {d.get('rhr') or '-'} | {hm(s.get('total'))} ({s.get('perf') or '-'}%) | {d.get('strain') if d.get('strain') is not None else '-'} | {f.get('acwr') or '-'} | {sess} | {'; '.join(d.get('flags') or []) or '-'} |")
    last=json.load(open(bios[-1]))
    wk=sorted(glob.glob(str(DATA/"weekly"/"*.json")))[-1]; w=json.load(open(wk))
    md=("# Relógio\n\nDados do Amazfit T-Rex 3 processados pelo painel. Atualizado quando o usuário roda \"atualiza o painel\" no Mac. "
        "Recuperação: verde ≥ 67, amarelo 34–66, vermelho ≤ 33. Esforço 0–21: leve < 10, moderado 10–13,9, alto 14–17,9. ACWR seguro 0,8–1,3.\n\n"
        "## Últimos 14 dias\n\n| Dia | Recuperação | HRV / base (ms) | FC repouso | Sono (desempenho) | Esforço | ACWR | Sessões | Alertas |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        + "\n".join(rows)
        + f"\n\n## Semana {w['week']} ({w['from']} a {w['to']})\n\nVeredito: {w['verdict']} (push = apertar, maintain = manter, deload = aliviar). "
        f"Recuperação média {w['recoveryAvg']}% ({w['greenDays']} verdes, {w['yellowDays']} amarelos, {w['redDays']} vermelhos). HRV média {w['hrvAvg']} ms (anterior {w['hrvPrev']}). "
        f"FC de repouso {w['rhrAvg']} (anterior {w['rhrPrev']}). Sono médio {hm(w['sleepAvg'])}, {w['sleepPerfAvg']}% da necessidade, dívida {w['debtEnd']} min. "
        f"Carga {w['loadTotal']} (anterior {w['loadPrev']}), {w['sessions']['bjj']} jiu e {w['sessions']['lift']} musculação, ACWR {w['acwr']}, monotonia {w['monotony']}.\n\n"
        + "\n".join(f"- {i['t']}: {i['d']}" for i in w["insights"])
        + f"\n\n## Hoje ({last['date']})\n\n" + "\n".join(f"- {i['t']}: {i['d']}" for i in last["insights"]))
    out=DATA/"relogio_tab.md"; out.write_text(md); print(out)
if __name__=="__main__": main()
