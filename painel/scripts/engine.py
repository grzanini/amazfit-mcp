"""Engine v3 do Painel Corpo e Dieta: Recuperação, Esforço, Sono, carga crônica e insights.

Aproximação do modelo WHOOP com os dados do Amazfit T-Rex 3 (nuvem Zepp):
  Esforço (0–21)   = 21·(1 − e^(−T/K)), T = TRIMP de Banister minuto a minuto (acordado, acima de
                     30% da FC de reserva) + carga muscular (sRPE × min × 0,15) + jiu-jitsu (blocos ou sRPE).
  Recuperação      = sigmoide(1,6·z̄ + 0,4), z̄ = 0,60·z_HRV + 0,20·z_FCrep + 0,15·z_sono + 0,05·z_pele,
                     z contra mediana/MAD dos 30 dias anteriores (ln HRV; FC de repouso no sono).
  Sono             = necessidade = base + 4·max(0, esforço_ontem − 8) + dívida (14 noites, decaimento 0,75,
                     teto 120); desempenho = dormido/necessidade; eficiência; consistência (4 dias).
  Carga crônica    = CTL (τ 42) / ATL (τ 7) / TSB, ACWR 7:28, monotonia e strain de Foster.
Funções puras; a entrada/saída de dados fica em daily_pull.py.
"""
from __future__ import annotations

import math
import statistics as st
from datetime import date, datetime, timedelta

CONFIG = {
    "hr_max": 187,
    "k": 150,               # escala do esforço (calibrada em 01/11/2026 com corridas 2026: dia de descanso ~5, musculação ~10, jiu ~13, corrida 40 min ~11)
    "hrr_cutoff": 0.30,     # abaixo disso a FC não conta como carga
    "sleep_base": 465,      # min (7h45; meta definida em 01/11/2026 a partir de 516 noites)
    "debt_cap": 120,
    "debt_decay": 0.75,
    "debt_nights": 14,
    "baseline_days": 30,
    "baseline_min_n": 5,
    "ln_hrv_spread_floor": 0.10,   # CV diário de rMSSD na literatura: 10–20%
    "rhr_spread_floor": 2.0,
    "lift_coef": 0.15,      # TRIMP-eq por (sRPE × min) na musculação
    "bjj_srpe_coef": 0.25,  # TRIMP-eq por (sRPE × min) no jiu-jitsu (até calibrar com sensor)
    "bjj_hrr": {"warmup": 0.45, "technique": 0.50, "positional": 0.55, "rest": 0.50,
                "light": 0.65, "moderate": 0.76, "hard": 0.84},
    "kcal_hr": {"a": -4.1, "b": 0.0818},  # kcal/min = a + b·FC, ajustado em 111 treinos do relógio (jun/2025–out/2026): musculação 4,0/min a 92 bpm, corrida 7,9/min a 139 bpm
    "default_srpe": {"lift": 4, "bjj": 6, "other": 5},   # musculação normalmente 4, 5 quando força mais
    "rec_intercept": 0.4,
    "rec_gain": 1.6,
    "weights": {"hrv": 0.60, "rhr": 0.20, "sleep": 0.15, "skin": 0.05},
    "strain_target": {"green": [13.5, 17.5], "yellow": [9.5, 13.5], "red": [4.0, 9.5]},
}

REC_ZONES = [(67, "green"), (34, "yellow"), (0, "red")]


# ---------------------------------------------------------------- utilidades
def clip(v, lo, hi):
    return max(lo, min(hi, v))


def rec_zone(r):
    if r is None:
        return None
    for th, name in REC_ZONES:
        if r >= th:
            return name
    return "red"


def strain_zone(s):
    if s is None:
        return None
    return "allout" if s >= 18 else "high" if s >= 14 else "moderate" if s >= 10 else "light"


def robust(xs, floor):
    m = st.median(xs)
    mad = st.median([abs(x - m) for x in xs]) * 1.4826
    return m, max(mad, floor)


def hm(m):
    m = int(round(m or 0))
    return f"{m // 60}h{m % 60:02d}"


def pct(a, b):
    return round((a / b - 1) * 100) if a and b else None


def mins_from_hhmm(s: str | None, anchor: int = 18 * 60) -> int | None:
    """Minutos desde as 18:00 do dia anterior, para comparar horários que cruzam a meia-noite."""
    if not s:
        return None
    h, m = map(int, s[:5].split(":"))
    v = h * 60 + m - anchor
    return v + 1440 if v < 0 else v


# ---------------------------------------------------------------- carga
def banister_minute(hr, rhr, hrmax, cutoff):
    x = (hr - rhr) / max(1, hrmax - rhr)
    if x <= cutoff:
        return 0.0
    return x * 0.64 * math.exp(1.92 * x)


def strain_from_load(T, K):
    return round(21 * (1 - math.exp(-max(0.0, T) / K)), 1)


def cardio_load(hr_minutes, rhr, hrmax, cutoff, asleep):
    """TRIMP e minutos por zona de FC de reserva (50–60 … 90–100%) nos minutos acordado."""
    T = 0.0
    zones = [0, 0, 0, 0, 0]
    cover = 0
    for i, h in enumerate(hr_minutes or []):
        if h is None or asleep[i]:
            continue
        cover += 1
        T += banister_minute(h, rhr, hrmax, cutoff)
        x = (h - rhr) / max(1, hrmax - rhr)
        if x >= 0.5:
            zones[min(4, int((x - 0.5) / 0.1))] += 1
    return round(T, 1), zones, cover


def bjj_load_blocks(blocks: dict, rhr, hrmax, cfg) -> float:
    z = cfg["bjj_hrr"]

    def tm(frac, minutes):
        h = rhr + frac * (hrmax - rhr)
        return minutes * banister_minute(h, rhr, hrmax, 0.0)

    b = {k: float(blocks.get(k) or 0) for k in ("warmup", "technique", "positional", "rest", "rollsN", "rollMin", "hard", "moderate", "light")}
    n = int(b["rollsN"])
    hard, mod, light = int(b["hard"]), int(b["moderate"]), int(b["light"])
    rest_n = max(0, n - hard - mod - light)
    mod += rest_n  # rolas sem intensidade marcada contam como moderadas
    T = tm(z["warmup"], b["warmup"]) + tm(z["technique"], b["technique"]) + tm(z["positional"], b["positional"]) + tm(z["rest"], b["rest"])
    T += tm(z["hard"], hard * b["rollMin"]) + tm(z["moderate"], mod * b["rollMin"]) + tm(z["light"], light * b["rollMin"])
    return round(T, 1)


def bjj_kcal_blocks(blocks: dict, rhr, hrmax, cfg) -> int:
    """Calorias do jiu-jitsu na mesma escala do relógio: kcal/min = a + b·FC, com a FC de cada bloco
    pela mesma fração da reserva usada na carga (bjj_hrr). Usado quando não há sessão no relógio."""
    z = cfg["bjj_hrr"]; k = cfg["kcal_hr"]
    b = {kk: float(blocks.get(kk) or 0) for kk in ("warmup", "technique", "positional", "rest", "rollsN", "rollMin", "hard", "moderate", "light")}
    n = int(b["rollsN"]); hard, mod, light = int(b["hard"]), int(b["moderate"]), int(b["light"])
    mod += max(0, n - hard - mod - light)
    def kc(frac, minutes):
        h = rhr + frac * (hrmax - rhr)
        return minutes * max(0.0, k["a"] + k["b"] * h)
    total = kc(z["warmup"], b["warmup"]) + kc(z["technique"], b["technique"]) + kc(z["positional"], b["positional"]) + kc(z["rest"], b["rest"])
    total += kc(z["hard"], hard * b["rollMin"]) + kc(z["moderate"], mod * b["rollMin"]) + kc(z["light"], light * b["rollMin"])
    return int(round(total, -1))


def bjj_kcal_srpe(minutes, srpe, rhr, hrmax, cfg) -> int:
    z = cfg["bjj_hrr"]; k = cfg["kcal_hr"]
    frac = z["hard"] if (srpe or 6) >= 8 else z["moderate"] if (srpe or 6) >= 6 else z["light"]
    h = rhr + frac * (hrmax - rhr)
    return int(round(minutes * max(0.0, k["a"] + k["b"] * h), -1))


def session_load(w: dict, rhr, hrmax, cfg) -> dict:
    kind = w.get("kind") or infer_kind(w.get("type", ""))
    minutes = float(w.get("minutes") or 0)
    srpe = w.get("srpe")
    method = "srpe"
    if kind == "bjj":
        if w.get("blocks") and (w["blocks"].get("rollsN") or w["blocks"].get("technique")):
            load = bjj_load_blocks(w["blocks"], rhr, hrmax, cfg)
            method = "blocks"
        else:
            s = srpe if srpe is not None else cfg["default_srpe"]["bjj"]
            load = round(cfg["bjj_srpe_coef"] * s * minutes, 1)
            method = "srpe" if srpe is not None else "default"
    elif kind == "lift":
        s = srpe if srpe is not None else cfg["default_srpe"]["lift"]
        load = round(cfg["lift_coef"] * s * minutes, 1)
        method = "srpe" if srpe is not None else "default"
    else:
        load = 0.0
        method = "watch" if w.get("hrAvg") else "none"
    kcal = w.get("kcal"); kcal_method = "watch" if kcal else None
    if kind == "bjj" and not kcal and minutes:
        if w.get("blocks") and (w["blocks"].get("rollsN") or w["blocks"].get("technique")):
            kcal = bjj_kcal_blocks(w["blocks"], rhr, hrmax, cfg)
        else:
            kcal = bjj_kcal_srpe(minutes, srpe, rhr, hrmax, cfg)
        kcal_method = "est"
    return {"id": w.get("id"), "type": kind, "minutes": int(minutes), "load": load, "srpe": srpe, "method": method,
            "label": w.get("type") or kind, "hrAvg": w.get("hrAvg"), "kcal": kcal, "kcalMethod": kcal_method, "gi": w.get("gi")}


def infer_kind(t: str) -> str:
    t = (t or "").lower()
    if "jiu" in t or "bjj" in t:
        return "bjj"
    if "muscula" in t or "força" in t or "strength" in t:
        return "lift"
    return "other"


# ---------------------------------------------------------------- sono
def hrv_weighted(samples, stages, sleep_start, sleep_end):
    """Média de RMSSD ponderada: ×2 em sono profundo, peso crescente ao longo da noite (0,5 → 1,5)."""
    if not samples or len(samples) < 30:
        return None
    deep = []
    for s in stages or []:
        if s.get("stage") == "deep":
            deep.append((datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"])))
    t0 = datetime.fromisoformat(sleep_start) if sleep_start else datetime.fromisoformat(samples[0]["t"])
    t1 = datetime.fromisoformat(sleep_end) if sleep_end else datetime.fromisoformat(samples[-1]["t"])
    span = max(60.0, (t1 - t0).total_seconds())
    num = den = 0.0
    for smp in samples:
        t = datetime.fromisoformat(smp["t"])
        frac = clip((t - t0).total_seconds() / span, 0, 1)
        w = 0.5 + frac
        if any(a <= t < b for a, b in deep):
            w *= 2
        num += w * smp["rmssd"]
        den += w
    return round(num / den) if den else None


def sleep_consistency(hist_sleeps):
    """100 − desvio médio (min) dos horários de deitar/acordar nas últimas 4 noites / 1,2. 30 min ≈ 75%."""
    beds = [mins_from_hhmm(s.get("start")) for s in hist_sleeps if s and s.get("start")]
    wakes = [mins_from_hhmm(s.get("end")) for s in hist_sleeps if s and s.get("end")]
    if len(beds) < 2:
        return None
    dev = []
    for xs in (beds, wakes):
        m = st.mean(xs)
        dev += [abs(x - m) for x in xs]
    return int(clip(round(100 - st.mean(dev) / 1.2), 0, 100))


# ---------------------------------------------------------------- dia
def compute_day(D: str, raw: dict, history: dict, manual: list[dict], cfg: dict = CONFIG) -> dict:
    """raw: {hr: [1440], sleep: {...}, stages: [...], readiness: {...}, stress: {...}, steps, kcal_active,
             watch_workouts: [...], hrv_samples: [...]}.  history: {date: doc} dos dias anteriores."""
    hrmax = cfg["hr_max"]
    prev_days = sorted(d for d in history if d < D)
    base_days = [history[d] for d in prev_days[-cfg["baseline_days"]:]]

    rd = raw.get("readiness") or {}
    sl = raw.get("sleep") or {}
    stress = raw.get("stress") or {}
    doc = {"date": D, "source": "engine-v3", "computedAt": datetime.now().astimezone().isoformat(timespec="minutes")}

    # --- FC de repouso e HRV da noite
    rhr = sl.get("resting_hr") or rd.get("sleeping_rhr")
    hrv_raw = rd.get("overnight_hrv_ms")
    hrv_w = hrv_weighted(raw.get("hrv_samples"), raw.get("stages"), sl.get("bedtime"), sl.get("wake_time"))
    hrv = hrv_w or hrv_raw
    doc.update(hrv=hrv, hrvRaw=hrv_raw, hrvWeighted=hrv_w, hrvZeppBase=rd.get("hrv_baseline_ms"), rhr=rhr,
               readiness=rd.get("readiness_score"), skinTemp=rd.get("skin_temp_score"),
               stressAvg=stress.get("avg"), stressMax=stress.get("max"),
               stressRelaxedPct=stress.get("pct_relaxed"),
               stressHighPct=(stress.get("pct_medium") or 0) + (stress.get("pct_high") or 0) if stress else None,
               steps=raw.get("steps"), kcalActive=raw.get("kcal_active"))

    # --- linhas de base (30 dias anteriores; fallback: base do Zepp)
    hrvs = [math.log(h["hrv"]) for h in base_days if h.get("hrv")]
    rhrs = [h["rhr"] for h in base_days if h.get("rhr")]
    if len(hrvs) >= cfg["baseline_min_n"]:
        c_ln, s_ln = robust(hrvs, cfg["ln_hrv_spread_floor"])
    else:
        c_ln, s_ln = (math.log(rd["hrv_baseline_ms"]) if rd.get("hrv_baseline_ms") else (math.log(hrv) if hrv else None)), 0.10
    if len(rhrs) >= cfg["baseline_min_n"]:
        c_rhr, s_rhr = robust(rhrs, cfg["rhr_spread_floor"])
    else:
        c_rhr, s_rhr = (rd.get("rhr_baseline") or rhr), 3.0
    rhr_base = round(c_rhr) if c_rhr else None
    doc.update(hrvBase=round(math.exp(c_ln)) if c_ln else None,
               hrvBand=[round(math.exp(c_ln - s_ln)), round(math.exp(c_ln + s_ln))] if c_ln else None,
               rhrBase=rhr_base, rhrBand=[round(c_rhr - s_rhr), round(c_rhr + s_rhr)] if c_rhr else None)

    # --- esforço
    rhr_for_hrr = rhr_base or rhr or 55
    asleep = [False] * 1440
    for key in ("bedtime", "wake_time"):
        pass
    if sl.get("bedtime") and sl.get("wake_time"):
        b = datetime.fromisoformat(sl["bedtime"]); w = datetime.fromisoformat(sl["wake_time"])
        day0 = datetime.fromisoformat(D).replace(tzinfo=b.tzinfo)
        for i in range(1440):
            t = day0 + timedelta(minutes=i)
            if b <= t < w:
                asleep[i] = True
    nxt = raw.get("next_bedtime")
    if nxt:
        b = datetime.fromisoformat(nxt)
        day0 = datetime.fromisoformat(D).replace(tzinfo=b.tzinfo)
        for i in range(1440):
            if day0 + timedelta(minutes=i) >= b:
                asleep[i] = True
    T_cardio, zones, cover = cardio_load(raw.get("hr"), rhr_for_hrr, hrmax, cfg["hrr_cutoff"], asleep)
    hr_valid = [h for h in (raw.get("hr") or []) if h is not None]
    sessions = []
    for w in manual:
        if w.get("date") == D:
            sessions.append(session_load(w, rhr_for_hrr, hrmax, cfg))
    manual_lift_ids = {s["id"] for s in sessions if s["type"] == "lift"}
    for ww in raw.get("watch_workouts") or []:
        kind = infer_kind(ww.get("sport", ""))
        # sessão do relógio sem registro manual equivalente no dia: usa sRPE padrão
        if kind == "lift" and not manual_lift_ids:
            sessions.append(session_load({"id": f"zepp-{ww.get('trackid')}", "type": "Musculação", "kind": "lift",
                                          "minutes": round((ww.get("duration_s") or 0) / 60), "hrAvg": ww.get("avg_hr"),
                                          "kcal": ww.get("calories")}, rhr_for_hrr, hrmax, cfg))
    T_musc = round(sum(s["load"] for s in sessions if s["type"] == "lift"), 1)
    T_bjj = round(sum(s["load"] for s in sessions if s["type"] == "bjj"), 1)
    T_total = round(T_cardio + T_musc + T_bjj, 1)
    doc.update(load={"cardio": T_cardio, "muscular": T_musc, "bjj": T_bjj, "total": T_total}, zones=zones,
               sessions=sessions, hrCoverageMin=cover, hrMaxDay=max(hr_valid) if hr_valid else None,
               strainCardio=strain_from_load(T_cardio, cfg["k"]) if cover else None,
               strain=strain_from_load(T_total, cfg["k"]) if (cover or sessions) else None)
    doc["strainZone"] = strain_zone(doc["strain"])

    # --- sono
    prev = history.get(prev_days[-1]) if prev_days else None
    ps = prev.get("strain") if prev else None
    need_strain = round(4 * max(0.0, (ps or 0) - 8))
    debt_num = debt_den = 0.0
    for k, d in enumerate(reversed(prev_days[-cfg["debt_nights"]:]), start=1):
        s_ = history[d].get("sleep") or {}
        if s_.get("need") and s_.get("total") is not None:
            w_ = cfg["debt_decay"] ** (k - 1)
            base_need = s_["need"] - (s_.get("debt") or 0)
            debt_num += w_ * max(0.0, base_need - s_["total"]); debt_den += w_
    debt = round(min(cfg["debt_cap"], debt_num / debt_den)) if debt_den else 0
    need = cfg["sleep_base"] + need_strain + debt
    total = sl.get("total_min")
    perf = int(round(min(1.0, total / need) * 100)) if total else None
    awake = sl.get("awake_min") or 0
    eff = int(round(total / (total + awake) * 100)) if total else None
    recent_sleeps = [history[d].get("sleep") for d in prev_days[-3:]] + [{"start": _hhmm(sl.get("bedtime")), "end": _hhmm(sl.get("wake_time"))}]
    cons = sleep_consistency([s for s in recent_sleeps if s]) if total else None
    sleep = {"start": _hhmm(sl.get("bedtime")), "end": _hhmm(sl.get("wake_time")), "total": total,
             "deep": sl.get("deep_min"), "light": sl.get("light_min"), "rem": sl.get("rem_min"), "wake": awake,
             "score": sl.get("score"), "rhr": rhr, "rhrBase": rhr_base, "efficiency": eff, "consistency": cons,
             "base": cfg["sleep_base"], "needStrain": need_strain, "debt": debt, "need": need, "perf": perf}
    doc.update(sleep=sleep, sleepPerf=perf, sleepNeed=need)

    # --- recuperação
    if hrv and c_ln and rhr and c_rhr:
        zh = clip((math.log(hrv) - c_ln) / s_ln, -3, 3)
        zr = clip((c_rhr - rhr) / s_rhr, -3, 3)
        zs = clip(((perf or 85) / 100 - 0.85) / 0.10, -3, 3)
        skin = doc.get("skinTemp")
        zk = clip((skin - 90) / 10, -3, 0.5) if skin is not None else 0.0
        W = cfg["weights"]
        zbar = W["hrv"] * zh + W["rhr"] * zr + W["sleep"] * zs + W["skin"] * zk
        rec = int(clip(round(100 / (1 + math.exp(-(cfg["rec_gain"] * zbar + cfg["rec_intercept"])))), 1, 99))
        doc.update(recovery=rec, recoveryZone=rec_zone(rec),
                   z={"hrv": round(zh, 2), "rhr": round(zr, 2), "sleep": round(zs, 2), "skin": round(zk, 2), "bar": round(zbar, 2)})
    else:
        doc.update(recovery=None, recoveryZone=None, z=None)

    # --- tendência de 7 dias (Plews/Buchheit 2012: média móvel e CV dizem se está acumulando; a leitura isolada diz o que fazer hoje)
    hrv7 = None
    last7 = [history[d]["hrv"] for d in prev_days[-6:] if history[d].get("hrv")] + ([hrv] if hrv else [])
    if len(last7) >= 4 and c_ln:
        ln7 = st.mean(math.log(x) for x in last7)
        trend = round((math.exp(ln7) / math.exp(c_ln) - 1) * 100)
        cv7 = round(100 * st.pstdev(last7) / st.mean(last7), 1)
        below = 0
        for d_ in [D] + prev_days[::-1]:
            dd = doc if d_ == D else history[d_]
            band = dd.get("hrvBand")
            if dd.get("hrv") and band and dd["hrv"] < band[0]:
                below += 1
            else:
                break
        if below >= 3 or trend <= -8:
            ctx = "chronic"
        elif doc.get("recoveryZone") in ("yellow", "red"):
            ctx = "acute"
        else:
            ctx = None
        hrv7 = {"mean": round(math.exp(ln7)), "trendPct": trend, "cv": cv7, "daysBelowBand": below, "context": ctx}
    doc["hrv7"] = hrv7
    tz_ = cfg["strain_target"].get(doc["recoveryZone"] or "yellow")
    doc["strainTarget"] = list(tz_)

    # --- carga crônica
    loads = [history[d].get("load", {}).get("total") or 0.0 for d in prev_days]
    ctl_prev = (prev.get("fitness") or {}).get("ctl") if prev else None
    atl_prev = (prev.get("fitness") or {}).get("atl") if prev else None
    L = T_total
    if ctl_prev is None or atl_prev is None:
        ctl_prev = atl_prev = L
    ctl = L * (1 - math.exp(-1 / 42)) + ctl_prev * math.exp(-1 / 42)
    atl = L * (1 - math.exp(-1 / 7)) + atl_prev * math.exp(-1 / 7)
    tsb = (ctl_prev - atl_prev) if len(prev_days) >= 28 else None
    last7 = loads[-6:] + [L]
    last28 = loads[-27:] + [L]
    acwr = round(st.mean(last7) / st.mean(last28), 2) if len(last28) >= 14 and st.mean(last28) > 0 else None
    mono = round(st.mean(last7) / max(1.0, st.pstdev(last7)), 2) if len(last7) >= 5 else None
    doc["fitness"] = {"ctl": round(ctl, 1), "atl": round(atl, 1), "tsb": round(tsb, 1) if tsb is not None else None,
                      "acwr": acwr, "weekLoad": round(sum(last7)), "monotony": mono,
                      "fosterStrain": round(sum(last7) * mono) if mono else None}

    # --- flags e insights
    doc["flags"] = day_flags(doc, cover, prev)
    doc["insights"] = daily_insights(doc, prev)
    return doc


def _hhmm(iso):
    return iso[11:16] if iso and len(iso) >= 16 else None


def day_flags(doc, cover, prev=None):
    f = []
    z = doc.get("z") or {}
    s = doc.get("sleep") or {}
    # infecção provável só com os três sinais juntos (FC de repouso +3, pele ≤ 92, HRV ≤ −15%); um só não basta
    if (doc.get("rhr") and doc.get("rhrBase") and doc["rhr"] >= doc["rhrBase"] + 3
            and doc.get("skinTemp") is not None and doc["skinTemp"] <= 92
            and doc.get("hrv") and doc.get("hrvBase") and doc["hrv"] <= 0.85 * doc["hrvBase"]):
        f.append("infection_pattern")
    # queda esperada: véspera com esforço ≥ 12 derruba a HRV ~10 ms por 24–48 h (Stanley 2013; dados do próprio usuário)
    if prev and (prev.get("strain") or 0) >= 12 and doc.get("recoveryZone") in ("yellow", "red"):
        f.append("expected_dip")
    if z and z.get("hrv", 0) <= -1.5 and z.get("rhr", 0) <= -1.5:
        f.append("alcohol_pattern")
    elif doc.get("skinTemp") is not None and doc["skinTemp"] < 80 and z.get("hrv", 0) <= -1:
        f.append("sick_pattern")
    b = mins_from_hhmm(s.get("start"))
    if b is not None and 360 <= b < 1080:  # entre 00:00 e 12:00
        f.append("late_bedtime")
    if s.get("deep") is not None and s.get("total") and s["deep"] / s["total"] < 0.10:
        f.append("low_deep")
    if (doc.get("stressAvg") or 0) >= 40 or (doc.get("stressHighPct") or 0) >= 25:
        f.append("high_stress")
    if any(x["type"] == "bjj" for x in doc.get("sessions") or []):
        f.append("bjj_estimated")
    if not cover:
        f.append("no_watch_data")
    return f


def daily_insights(doc, prev):
    out = []
    rec, zone = doc.get("recovery"), doc.get("recoveryZone")
    z = doc.get("z") or {}
    lo, hi = doc.get("strainTarget") or (None, None)
    drivers = []
    if doc.get("hrv") and doc.get("hrvBase"):
        p = pct(doc["hrv"], doc["hrvBase"])
        if abs(p) >= 6:
            drivers.append(f"HRV {doc['hrv']} ms, {abs(p)}% {'acima' if p > 0 else 'abaixo'} da sua base ({doc['hrvBase']})")
    if doc.get("rhr") and doc.get("rhrBase"):
        d = doc["rhr"] - doc["rhrBase"]
        if abs(d) >= 2:
            drivers.append(f"FC de repouso {doc['rhr']} bpm, {abs(d)} {'acima' if d > 0 else 'abaixo'} da base ({doc['rhrBase']})")
    s = doc.get("sleep") or {}
    if s.get("perf") is not None and s["perf"] < 85:
        drivers.append(f"sono em {s['perf']}% da necessidade")
    if doc.get("skinTemp") is not None and doc["skinTemp"] < 85:
        drivers.append(f"temperatura da pele fora do normal (nota {doc['skinTemp']})")
    why = ("; ".join(drivers) + ".") if drivers else "HRV, FC de repouso e sono dentro do seu normal."
    if rec is not None:
        if zone == "green":
            t, d = f"Dia verde ({rec}%)", f"Corpo pronto. {why} Bom dia para rola dura ou treino pesado; esforço ideal {fmt1(lo)}–{fmt1(hi)}."
        elif zone == "yellow":
            t, d = f"Dia amarelo ({rec}%)", f"Treine, sem buscar o limite. {why} Esforço ideal {fmt1(lo)}–{fmt1(hi)}: técnica, rolas controladas ou musculação moderada."
        else:
            t, d = f"Dia vermelho ({rec}%)", f"Priorize recuperação. {why} Mantenha o esforço abaixo de {fmt1(hi)}: mobilidade, caminhada ou técnica leve; nada de open mat."
        if "expected_dip" in (doc.get("flags") or []) and prev:
            d += f" Queda esperada: ontem o esforço foi {fmt1(prev.get('strain'))}; depois de treino forte a HRV cai ~10 ms por 24–48 h e volta (ou passa da base) em 48–72 h. Compare com a tendência de 7 dias antes de concluir que está acumulando."
        out.append({"kind": "rec", "t": t, "d": d})
        h7 = doc.get("hrv7")
        if h7:
            tp = h7["trendPct"]
            tt = f"Tendência de 7 dias: HRV {h7['mean']} ms ({'+' if tp > 0 else ''}{tp}% da base), CV {fmt1(h7['cv'])}%"
            if h7["context"] == "chronic":
                td = (f"{h7['daysBelowBand']} dias seguidos abaixo da faixa. " if h7["daysBelowBand"] >= 2 else "") + "A média de 7 dias caiu: isso é fadiga acumulada ou doença, não uma noite ruim. Semana de alívio até a média voltar à faixa."
            elif h7["context"] == "acute":
                td = ("Segundo dia seguido abaixo da faixa; se amanhã continuar, passa a contar como acúmulo. " if h7["daysBelowBand"] == 2 else "Queda isolada: ") + "A média de 7 dias segue dentro do normal. Resposta aguda (treino forte, noite tarde, álcool, começo de resfriado), não fadiga acumulada. A cor do dia manda em hoje; a tendência diz que não há acúmulo."
            else:
                td = "Dentro do normal." + (" CV alto: fins de semana muito diferentes dos dias úteis; regularidade de horário reduz isso." if h7["cv"] >= 15 else "")
            out.append({"kind": "trend", "t": tt, "d": td})
    else:
        out.append({"kind": "rec", "t": "Sem recuperação calculada", "d": "A HRV desta noite ainda não chegou do relógio (o Zepp costuma enviar algumas horas depois de acordar) ou o relógio ficou fora do pulso. A nota aparece na próxima coleta."})
    if s.get("total"):
        d = f"Dormiu {hm(s['total'])} de {hm(s['need'])} necessárias ({s['perf']}%)."
        if s.get("debt"):
            d += f" Dívida acumulada: {s['debt']} min, já somada à necessidade de hoje."
        if s.get("needStrain"):
            d += f" O esforço de ontem pediu +{s['needStrain']} min."
        if s.get("consistency") is not None and s["consistency"] < 60:
            d += " Horários de dormir muito irregulares nesta semana: regularidade vale mais que uma noite longa."
        if s.get("deep") and s.get("total") and s["deep"] / s["total"] < 0.10:
            d += f" Sono profundo baixo ({hm(s['deep'])})."
        out.append({"kind": "sleep", "t": "Sono", "d": d})
    if prev and prev.get("strain") is not None and prev.get("strainTarget"):
        plo, phi = prev["strainTarget"]
        ps = prev["strain"]
        where = "dentro da" if plo <= ps <= phi else ("acima da" if ps > phi else "abaixo da")
        out.append({"kind": "strain", "t": f"Ontem: esforço {fmt1(ps)}", "d": f"{where.capitalize()} faixa ideal de ontem ({fmt1(plo)}–{fmt1(phi)})." + (" Esforço alto com recuperação baixa é a receita de acumular fadiga." if ps > phi and prev.get("recoveryZone") != "green" else "")})
    f = doc.get("fitness") or {}
    bits = []
    if f.get("acwr") is not None:
        a = f["acwr"]
        if a >= 1.5:
            bits.append(f"Carga aguda {fmt1(a)}× a crônica: zona de risco. Dois ou três dias leves.")
        elif a > 1.3:
            bits.append(f"Carga aguda {fmt1(a)}× a crônica, acima da zona segura (0,8–1,3).")
        elif a < 0.8:
            bits.append(f"Carga aguda {fmt1(a)}× a crônica: há espaço para treinar mais.")
        else:
            bits.append(f"Carga aguda {fmt1(a)}× a crônica, dentro da zona segura.")
    if f.get("tsb") is not None and f.get("ctl"):
        rel = f["tsb"] / max(1.0, f["ctl"])
        if rel <= -0.25:
            bits.append("Fadiga acumulada alta: carga recente bem acima da sua média de 6 semanas.")
        elif rel >= 0.15:
            bits.append("Corpo fresco: carga recente abaixo da média de 6 semanas; dia bom para intensidade, se a recuperação permitir.")
    if f.get("monotony") and f["monotony"] >= 2.0:
        bits.append("Semana monótona (carga parecida todo dia): alterne dias duros e leves.")
    if bits:
        out.append({"kind": "load", "t": "Carga de treino", "d": " ".join(bits)})
    flags = doc.get("flags") or []
    if "infection_pattern" in flags:
        out.append({"kind": "flag", "t": "Padrão de infecção provável", "d": f"FC de repouso {doc.get('rhr')} ({doc.get('rhr') - doc.get('rhrBase')} acima da base), temperatura da pele {doc.get('skinTemp')}/100 e HRV {abs(pct(doc['hrv'], doc['hrvBase']))}% abaixo na mesma noite: os três sinais juntos apontam para infecção em curso, não cansaço. Repouso; com febre, dor ou ouvido fechando, médico."})
    if "alcohol_pattern" in flags:
        out.append({"kind": "flag", "t": "Padrão de álcool ou doença", "d": "HRV muito abaixo e FC de repouso muito acima da base na mesma noite. Se foi álcool, o efeito costuma durar 2 a 4 dias; hidrate e durma cedo."})
    elif "sick_pattern" in flags:
        out.append({"kind": "flag", "t": "Sinais de doença", "d": "Temperatura da pele fora do normal com HRV baixa. Observe sintomas antes de treinar forte."})
    if "late_bedtime" in flags:
        out.append({"kind": "flag", "t": "Deitou depois da meia-noite", "d": "Nas suas noites iniciadas após 0h a FC de repouso sobe e a recuperação cai. Mirar 23h."})
    if "high_stress" in flags:
        out.append({"kind": "flag", "t": "Estresse alto no dia", "d": f"Média {doc.get('stressAvg')}/100 e {doc.get('stressHighPct')}% do dia em estresse médio/alto. Respiração lenta 5 min antes de dormir ajuda a HRV."})
    return out


def fmt1(v):
    return (f"{v:.1f}").replace(".", ",") if isinstance(v, (int, float)) else "–"


# ---------------------------------------------------------------- semana
def iso_week(D: str) -> str:
    y, w, _ = date.fromisoformat(D).isocalendar()
    return f"{y}-W{w:02d}"


def compute_week(week: str, docs: list[dict], prev_docs: list[dict]) -> dict:
    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(st.mean(xs), 1) if xs else None
    recs = [d.get("recovery") for d in docs]
    out = {"week": week, "from": docs[0]["date"], "to": docs[-1]["date"],
           "recoveryAvg": round(mean(recs)) if mean(recs) is not None else None,
           "recoveryPrev": round(mean([d.get("recovery") for d in prev_docs])) if prev_docs and mean([d.get("recovery") for d in prev_docs]) is not None else None,
           "greenDays": sum(1 for r in recs if r is not None and r >= 67),
           "yellowDays": sum(1 for r in recs if r is not None and 34 <= r < 67),
           "redDays": sum(1 for r in recs if r is not None and r < 34),
           "strainAvg": mean([d.get("strain") for d in docs]), "strainPrev": mean([d.get("strain") for d in prev_docs]),
           "loadTotal": round(sum((d.get("load") or {}).get("total") or 0 for d in docs)),
           "loadPrev": round(sum((d.get("load") or {}).get("total") or 0 for d in prev_docs)) if prev_docs else None,
           "hrvAvg": round(mean([d.get("hrv") for d in docs])) if mean([d.get("hrv") for d in docs]) else None,
           "hrvPrev": round(mean([d.get("hrv") for d in prev_docs])) if prev_docs and mean([d.get("hrv") for d in prev_docs]) else None,
           "rhrAvg": mean([d.get("rhr") for d in docs]), "rhrPrev": mean([d.get("rhr") for d in prev_docs]),
           "sleepAvg": round(mean([(d.get("sleep") or {}).get("total") for d in docs]) or 0) or None,
           "sleepPerfAvg": round(mean([(d.get("sleep") or {}).get("perf") for d in docs]) or 0) or None,
           "debtEnd": (docs[-1].get("sleep") or {}).get("debt"),
           "consistencyAvg": round(mean([(d.get("sleep") or {}).get("consistency") for d in docs]) or 0) or None,
           "sessions": {"bjj": sum(1 for d in docs for s in d.get("sessions") or [] if s["type"] == "bjj"),
                        "lift": sum(1 for d in docs for s in d.get("sessions") or [] if s["type"] == "lift")},
           "acwr": (docs[-1].get("fitness") or {}).get("acwr"), "monotony": (docs[-1].get("fitness") or {}).get("monotony"),
           "tsb": (docs[-1].get("fitness") or {}).get("tsb"),
           "hrvTrendPct": (docs[-1].get("hrv7") or {}).get("trendPct"), "hrvCv": (docs[-1].get("hrv7") or {}).get("cv")}
    # veredito
    ra, hp, hv, rp, rv = out["recoveryAvg"], out["hrvPrev"], out["hrvAvg"], out["rhrPrev"], out["rhrAvg"]
    hrv_drop = hp and hv and hv < hp * 0.93
    rhr_up = rp and rv and rv > rp + 3
    if out["redDays"] >= 2 or (ra is not None and ra < 50) or (out["acwr"] or 0) > 1.5 or (hrv_drop and rhr_up) or ((out["monotony"] or 0) > 2.0 and out["loadPrev"] and out["loadTotal"] > 1.1 * out["loadPrev"]):
        v = "deload"
    elif ra is not None and ra >= 70 and (out["acwr"] or 1) < 1.1 and (out["tsb"] is None or out["tsb"] >= -5) and (out["debtEnd"] or 0) < 30:
        v = "push"
    else:
        v = "maintain"
    out["verdict"] = v
    ins = []
    head = {"deload": ("Semana para aliviar", "Os sinais pedem menos carga: "),
            "push": ("Semana para apertar", "Recuperação alta e carga controlada: "),
            "maintain": ("Semana para manter", "Nada fora do lugar: ")}[v]
    reasons = []
    if out["redDays"] >= 2:
        reasons.append(f"{out['redDays']} dias vermelhos")
    if ra is not None:
        reasons.append(f"recuperação média {ra}%" + (f" (semana anterior {out['recoveryPrev']}%)" if out["recoveryPrev"] is not None else ""))
    if hv and hp:
        reasons.append(f"HRV média {hv} ms contra {hp} na semana anterior")
    if rv and rp:
        reasons.append(f"FC de repouso {fmt1(rv)} contra {fmt1(rp)}")
    if out["acwr"] is not None:
        reasons.append(f"carga aguda/crônica {fmt1(out['acwr'])}")
    if out["hrvTrendPct"] is not None:
        reasons.append(f"tendência de HRV em 7 dias {'+' if out['hrvTrendPct'] > 0 else ''}{out['hrvTrendPct']}% da base (CV {fmt1(out['hrvCv'])}%)")
    ins.append({"t": head[0], "d": head[1] + "; ".join(reasons) + "."})
    if out["loadPrev"] and out["loadTotal"]:
        p = pct(out["loadTotal"], out["loadPrev"]) or 0
        ins.append({"t": f"Carga total {out['loadTotal']} ({'+' if p >= 0 else ''}{p}%)", "d": f"{out['sessions']['bjj']} sessões de jiu-jitsu e {out['sessions']['lift']} de musculação. Semana anterior: {out['loadPrev']}." + (" Salto acima de 30% numa semana costuma cobrar na seguinte." if p > 30 else "")})
    beds = [mins_from_hhmm((d.get("sleep") or {}).get("start")) for d in docs if (d.get("sleep") or {}).get("start")]
    short = sum(1 for d in docs if (d.get("sleep") or {}).get("total") and d["sleep"]["total"] < 420)
    deep_pct = [100 * d["sleep"]["deep"] / d["sleep"]["total"] for d in docs if (d.get("sleep") or {}).get("total") and d["sleep"].get("deep") is not None]
    out["bedMedian"] = (lambda m: f"{(m + 18 * 60) // 60 % 24:02d}:{m % 60:02d}")(int(st.median(beds))) if beds else None
    out["shortNights"] = short
    out["deepPctAvg"] = round(st.mean(deep_pct)) if deep_pct else None
    if out["sleepAvg"]:
        bed_txt = f" Deitou em mediana às {out['bedMedian']} (meta 22:00)." if out["bedMedian"] else ""
        short_txt = f" {short} noite(s) abaixo de 7h (meta zero)." if short else " Nenhuma noite abaixo de 7h."
        deep_txt = f" Sono profundo {out['deepPctAvg']}% (meta ≥ 15%)." if out["deepPctAvg"] is not None else ""
        ins.append({"t": f"Sono médio {hm(out['sleepAvg'])} (meta 7h45; {out['sleepPerfAvg']}% da necessidade)", "d": (f"Dívida no fim da semana: {out['debtEnd']} min." if out["debtEnd"] else "Sem dívida acumulada.") + bed_txt + short_txt + deep_txt + (f" Consistência de horários {out['consistencyAvg']}%: " + ("boa." if out['consistencyAvg'] >= 70 else "irregular; horários fixos de deitar melhoram HRV e sono profundo.") if out["consistencyAvg"] is not None else "")})
    best = max((d for d in docs if d.get("recovery") is not None), key=lambda d: d["recovery"], default=None)
    worst = min((d for d in docs if d.get("recovery") is not None), key=lambda d: d["recovery"], default=None)
    if best and worst and best is not worst:
        ins.append({"t": "Melhor e pior dia", "d": f"Melhor recuperação em {br(best['date'])} ({best['recovery']}%); pior em {br(worst['date'])} ({worst['recovery']}%)" + (": " + ", ".join(LABELS.get(f, f) for f in worst.get("flags", []) if f in LABELS) if any(f in LABELS for f in worst.get("flags", [])) else "") + "."})
    out["insights"] = ins
    return out


LABELS = {"alcohol_pattern": "padrão de álcool/doença", "sick_pattern": "sinais de doença", "late_bedtime": "deitou depois da meia-noite",
          "low_deep": "sono profundo baixo", "high_stress": "estresse alto", "infection_pattern": "padrão de infecção provável", "expected_dip": "queda esperada após treino forte"}


def br(D):
    return f"{D[8:10]}/{D[5:7]}"
