"""配额口径：自然日周期、开始时刻归属、跨零点拆分、拒绝保持、归档只追加。"""

from __future__ import annotations

import pytest

from batterystore.config import QuotaLimits
from batterystore.errors import QuotaError
from batterystore.quota.accounting import settle
from batterystore.quota.quota import QuotaLedger


# 2025-09-16 00:00:00 UTC+8
LOCAL_MIDNIGHT = 1757952000.0


def make_limits(period_hours=24.0, charge=2000.0, discharge=2000.0):
    return QuotaLimits(period_hours, charge, discharge, 1.2, 0.4, 8.0)


def test_settle_computes_margin():
    report = settle(100.0, 80.0, 0.4, 1.2)
    assert report["charge_cost"] == pytest.approx(40.0)
    assert report["discharge_value"] == pytest.approx(96.0)
    assert report["margin"] == pytest.approx(56.0)
    assert settle(0.0, 0.0, 0.4, 1.2)["margin"] == 0.0


def test_period_is_calendar_day_aligned_not_rolling(platform):
    quota = platform.quota
    before = LOCAL_MIDNIGHT - 1.0
    after = LOCAL_MIDNIGHT
    key_before = quota.period.key_for(before)
    key_after = quota.period.key_for(after)
    assert key_before != key_after
    assert key_before.endswith("d00") and key_after.endswith("d00")
    assert quota.period.key_for(after + 86400 - 1) == key_after  # 同属一个自然日
    assert quota.period.key_for(after + 86400) != key_after       # 次日零点才换槽


def test_period_length_must_divide_a_day():
    with pytest.raises(Exception):
        QuotaLedger(QuotaLimits(7.0, 100.0, 100.0, 1.2, 0.4, 8.0))


def test_tariff_uses_local_time(platform):
    tariff = platform.quota.tariff
    assert tariff.as_dict()["peak_window"] == [8, 22]
    assert tariff.kind(LOCAL_MIDNIGHT - 3600) == "valley"        # 23:00 本地
    assert tariff.kind(LOCAL_MIDNIGHT + 8 * 3600) == "peak"      # 08:00 本地
    assert tariff.kind(LOCAL_MIDNIGHT + 21 * 3600) == "peak"
    assert tariff.kind(LOCAL_MIDNIGHT + 22 * 3600) == "valley"


def test_quota_records_and_limits(platform, clock):
    quota = platform.quota
    now = clock.now()
    assert quota.preflight("charge", 10.0, now)["allowed"] is True
    assert quota.record_charge(10.0, now)["charged_kwh"] == 10.0
    assert quota.record_discharge(4.0, now)["discharged_kwh"] == 4.0
    totals = quota.totals()
    assert totals["charge_kwh"] == 10.0
    assert totals["discharge_kwh"] == 4.0
    remaining = quota.remaining()
    assert remaining["charge_kwh"] == pytest.approx(platform.config.quota.charge_quota_kwh - 10.0)
    with pytest.raises(QuotaError):
        quota.record_charge(10_000.0, now)
    assert quota.snapshot(now).rejections == 1
    # 已记账电量不因拒绝而回滚
    assert quota.totals()["charge_kwh"] == 10.0


def test_preflight_rejection_has_no_side_effects():
    ledger = QuotaLedger(make_limits(charge=10.0, discharge=10.0))
    now = LOCAL_MIDNIGHT + 100.0
    pre = ledger.preflight("charge", 11.0, now)
    assert pre["allowed"] is False
    assert ledger.totals()["charge_kwh"] == 0.0
    assert ledger.snapshot(now).rejections_by_direction["charge"] == 1
    # 预检放行也不扣额度
    ok = ledger.preflight("charge", 5.0, now)
    assert ok["allowed"] is True
    assert ledger.totals()["charge_kwh"] == 0.0


def test_cross_midnight_energy_is_listed_and_balanced():
    ledger = QuotaLedger(make_limits())
    start = LOCAL_MIDNIGHT - 600.0  # 23:50 本地
    receipt = ledger.record_discharge(20.0, start, duration_s=1200.0)
    crossing = receipt["crossing"]
    assert crossing["tail_kwh"] == pytest.approx(10.0)  # 00:00 之后 600s
    assert crossing["start_period"] != crossing["tail_period"]
    # 跨午夜两端同为谷价：挪日不挪价
    assert crossing["start_rate"] == crossing["tail_rate"] == 0.4
    # 整段全额记入开始周期并占额
    assert ledger.totals()["discharge_kwh"] == 20.0

    ledger.roll(LOCAL_MIDNIGHT + 1.0)
    history = ledger.period.history()
    assert history[0]["discharge_kwh"] == pytest.approx(20.0)
    assert ledger.totals()["discharge_kwh"] == 0.0
    snap = ledger.snapshot(LOCAL_MIDNIGHT + 1.0)
    assert len(snap.crossings) == 1
    assert snap.crossings[0]["tail_kwh"] == pytest.approx(10.0)
    assert snap.reconciliation["discharge_balanced"] is True


def test_cross_boundary_tariff_difference_explains_monthly_gap():
    # 2 小时槽跨 08:00 谷峰切换点
    ledger = QuotaLedger(make_limits(period_hours=2.0))
    switch = LOCAL_MIDNIGHT + 8 * 3600.0
    ledger.record_discharge(20.0, switch - 600.0, duration_s=1200.0)
    diff = ledger.attribution_difference(LOCAL_MIDNIGHT, switch + 1.0)
    assert diff["segments"] == 1
    assert diff["discharge_shift_kwh"] == pytest.approx(20.0)
    # 结束归属整段按峰价 1.2，开始归属整段按谷价 0.4：差 20*(0.4-1.2) = -16
    assert diff["amount_delta_end_minus_start"] == pytest.approx(-16.0)
    assert diff["policy"] == "start_time_attribution"


def test_archived_period_is_immutable_late_booking_appends_note():
    ledger = QuotaLedger(make_limits())
    old_start = LOCAL_MIDNIGHT - 600.0
    ledger.record_charge(5.0, old_start, duration_s=60.0)
    ledger.roll(LOCAL_MIDNIGHT + 1.0)
    key = ledger.period.history()[0]["key"]
    assert ledger.period.archive(key)["charge_kwh"] == pytest.approx(5.0)

    late = ledger.record_charge(2.0, old_start + 10.0, duration_s=1.0)
    assert late["booked"] is False
    archive = ledger.period.archive(key)
    assert archive["charge_kwh"] == pytest.approx(5.0)  # 归档数字没变
    assert len(archive["notes"]) == 1
    assert archive["notes"][0]["kwh"] == pytest.approx(2.0)
    settlement = ledger.period_settlement(key)
    assert settlement["charge_kwh"] == pytest.approx(5.0)
    assert settlement["late_kwh"] == pytest.approx(2.0)
    assert settlement["late_notes"] == 1
    assert ledger.reconciliation()["charge_balanced"] is True


def test_clock_going_backwards_does_not_roll_back():
    ledger = QuotaLedger(make_limits())
    ledger.roll(LOCAL_MIDNIGHT + 3600.0)
    key = ledger.period.current()
    ledger.record_charge(1.0, LOCAL_MIDNIGHT + 3600.0)
    ledger.roll(LOCAL_MIDNIGHT - 100.0)
    assert ledger.period.current() == key
    assert ledger.totals()["charge_kwh"] == 1.0


def test_discharge_rejection_holds_last_valid_power(platform, clock, monkeypatch):
    p = platform
    p.start()
    p.transfer_grid_tie(power_kw=100.0, dt=1.0)
    ok = p.discharge(power_kw=100.0, dt=60.0)
    assert ok["ok"] is True
    held_kw = p.pcs.snapshot().output_kw

    # 换成已耗尽的小额度台账
    from batterystore.quota.quota import QuotaLedger as Q

    p.quota = Q(make_limits(charge=0.001, discharge=0.001))
    bad = p.discharge(power_kw=300.0, dt=60.0)
    assert bad["ok"] is False
    assert "quota_exceeded" in bad["violations"]
    actions = [step["action"] for step in bad["steps"]]
    assert "hold_last_valid_power" in actions
    assert "energy" not in actions  # 被拒绝的一段没有记电网电量
    assert p.pcs.snapshot().as_dict()["output_kw"] == pytest.approx(held_kw)
