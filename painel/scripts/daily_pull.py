"""Coleta diária: puxa a nuvem Zepp pelo coletor amazfit-mcp, roda o engine v3 e grava
painel/data/raw/<dia>.json, painel/data/bio/<dia>.json, painel/data/weekly/<semana>.json,
painel/data/bio_meta.json e painel/data/publish_queue.json (o que falta gravar no artefato).

Uso:  uv run --directory ~/amazfit-mcp python painel/scripts/daily_pull.py [--days 45] [--from D --to D] [--no-fetch]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # ~/amazfit-mcp
PAINEL = ROOT / "painel"
DATA = PAINEL / "data"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PAINEL / "scripts"))

import engine as E  # noqa: E402

METHOD = ("Aproximação do modelo WHOOP com dados da nuvem Zepp. Esforço 0–21: TRIMP de Banister minuto a minuto "
          "(FC acordado acima de 30% da reserva) + carga muscular por sRPE + jiu-jitsu por blocos ou sRPE. "
          "Recuperação: HRV noturna (ponderada pelo sono profundo), FC de repouso no sono, desempenho do sono e "
          "temperatura da pele, cada um comparado com a mediana dos 30 dias anteriores. Sono: necessidade = 7h30 + "
          "esforço de ontem + dívida de 14 noites. Carga crônica: CTL/ATL/TSB, ACWR 7:28, monotonia.")


def jload(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        return default


def jdump(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1))


def fetch(lo: str, hi: str) -> dict[str, dict]:
    import server as S  # coletor (carrega .env)
    days = {}
    for d in S.get_daily_summary(lo, hi, include_sleep_stages=True):
        days.setdefault(d["date"], {})["summary"] = d
    for d in S.get_health_metrics(lo, hi):
        days.setdefault(d["date"], {})["metrics"] = d
    for d in S.get_heart_rate_minutes(lo, hi):
        days.setdefault(d["date"], {})["hr"] = d
    for n in S.get_hrv_samples(lo, hi):
        days.setdefault(n["date"], {})["hrv"] = n
    for w in S.list_workouts(lo, hi, limit=500):
        D = (w.get("start") or "")[:10]
        if D:
            days.setdefault(D, {}).setdefault("workouts", []).append(w)
    return days


def to_raw(D: str, day: dict, next_day: dict | None) -> dict:
    s = day.get("summary") or {}
    m = day.get("metrics") or {}
    sl = s.get("sleep") or {}
    nxt = ((next_day or {}).get("summary") or {}).get("sleep") or {}
    return {
        "date": D,
        "hr": (day.get("hr") or {}).get("hr"),
        "sleep": {k: v for k, v in sl.items() if k != "stages"},
        "stages": sl.get("stages"),
        "readiness": m.get("readiness") or {},
        "stress": m.get("stress") or {},
        "pai": m.get("pai") or {},
        "steps": s.get("steps"),
        "kcal_active": s.get("calories"),
        "watch_workouts": day.get("workouts") or [],
        "hrv_samples": (day.get("hrv") or {}).get("samples"),
        "next_bedtime": nxt.get("bedtime"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--from", dest="lo")
    ap.add_argument("--to", dest="hi")
    ap.add_argument("--no-fetch", action="store_true", help="recalcula só a partir de data/raw")
    a = ap.parse_args()
    today = date.today().isoformat()
    hi = a.hi or today
    lo = a.lo or (date.fromisoformat(hi) - timedelta(days=a.days - 1)).isoformat()

    log = PAINEL / "logs" / "daily_pull.log"
    log.parent.mkdir(exist_ok=True)

    if not a.no_fetch:
        days = fetch(lo, hi)
        ds = sorted(days)
        for i, D in enumerate(ds):
            nxt = days.get((date.fromisoformat(D) + timedelta(days=1)).isoformat())
            jdump(DATA / "raw" / f"{D}.json", to_raw(D, days[D], nxt))

    manual = jload(DATA / "manual" / "workouts.json", [])
    # histórico anterior ao intervalo
    history: dict[str, dict] = {}
    for p in sorted((DATA / "bio").glob("*.json")):
        if p.stem < lo:
            history[p.stem] = json.loads(p.read_text())
    changed_bio, d = [], date.fromisoformat(lo)
    while d.isoformat() <= hi:
        D = d.isoformat()
        raw = jload(DATA / "raw" / f"{D}.json")
        if raw and (raw.get("hr") or raw.get("sleep") or raw.get("readiness")):
            doc = E.compute_day(D, raw, history, manual)
            if D == today:
                doc["partial"] = True
            history[D] = doc
            old = jload(DATA / "bio" / f"{D}.json")
            # computedAt muda a cada rodada; só conta como alteração se algum valor mudou
            if {k: v for k, v in (old or {}).items() if k != "computedAt"} != {k: v for k, v in doc.items() if k != "computedAt"}:
                jdump(DATA / "bio" / f"{D}.json", doc)
                changed_bio.append(D)
            else:
                history[D] = old
        d += timedelta(days=1)

    # semanas
    weeks = sorted({E.iso_week(D) for D in changed_bio})
    changed_weeks = []
    by_week: dict[str, list[dict]] = {}
    for D, doc in sorted(history.items()):
        by_week.setdefault(E.iso_week(D), []).append(doc)
    wk_list = sorted(by_week)
    for w in weeks:
        i = wk_list.index(w)
        prev = by_week[wk_list[i - 1]] if i > 0 else []
        wdoc = E.compute_week(w, by_week[w], prev)
        if jload(DATA / "weekly" / f"{w}.json") != wdoc:
            jdump(DATA / "weekly" / f"{w}.json", wdoc)
            changed_weeks.append(w)

    # meta
    last = history[max(history)] if history else {}
    meta_old = jload(DATA / "bio_meta.json", {}) or {}
    meta = {
        "version": "v3", "updated": today,
        "range": f"{min(history)[8:10]}/{min(history)[5:7]} a {max(history)[8:10]}/{max(history)[5:7]}/{max(history)[:4]}" if history else "",
        "hrMax": E.CONFIG["hr_max"], "rhrBase": last.get("rhrBase"), "hrvBase": last.get("hrvBase"),
        "sleepBase": E.CONFIG["sleep_base"], "k": E.CONFIG["k"], "method": METHOD,
        "source": "coletor amazfit-mcp (nuvem Zepp), engine v3",
        "monthly": meta_old.get("monthly") or meta_old.get("insights") or [],
    }
    jdump(DATA / "bio_meta.json", meta)

    q = jload(DATA / "publish_queue.json", {"bio": [], "weekly": [], "meta": False}) or {"bio": [], "weekly": [], "meta": False}
    q["bio"] = sorted(set(q.get("bio", [])) | set(changed_bio))
    q["weekly"] = sorted(set(q.get("weekly", [])) | set(changed_weeks))
    q["meta"] = True
    q["updatedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
    jdump(DATA / "publish_queue.json", q)

    line = (f"{datetime.now().astimezone().isoformat(timespec='seconds')} intervalo {lo}..{hi} "
            f"dias={len([d for d in history if lo <= d <= hi])} alterados={len(changed_bio)} semanas={changed_weeks} "
            f"último={max(history) if history else '-'} rec={last.get('recovery')} esforço={last.get('strain')}")
    with log.open("a") as f:
        f.write(line + "\n")
    print(line)


if __name__ == "__main__":
    main()
