import normalize as N


def test_hr_minutes_decodes_sentinels():
    raw = bytes([60, 0, 254, 255, 120] + [70] * 1435)
    out = N.hr_minutes({"date": "2026-10-21", "hr_bytes": raw})
    assert len(out["hr"]) == 1440
    assert out["hr"][:5] == [60, None, None, None, 120]
    assert out["coverage_min"] == 1437
    assert out["min_hr"] == 60 and out["max_hr"] == 120


def test_hr_minutes_pads_short_days():
    out = N.hr_minutes({"date": "2026-10-21", "hr_bytes": bytes([80] * 600)})
    assert len(out["hr"]) == 1440 and out["coverage_min"] == 600 and out["hr"][-1] is None


def test_hrv_night_builds_samples():
    from datetime import timezone, timedelta
    tz = timezone(timedelta(hours=-3))
    item = {"start_ms": 1792541820000, "samples": [{"s": 0, "hrv": 71}, {"s": 60000, "hrv": 63}, {"s": 120000, "hrv": 0}]}
    out = N.hrv_night(item, tz)
    assert out["n"] == 2
    assert out["samples"][0]["rmssd"] == 71 and out["samples"][1]["t"].endswith("-03:00")
