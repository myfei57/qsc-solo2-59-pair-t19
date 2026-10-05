"""配额：自然日周期、分片归属、超额拒绝、归档与峰谷月结。"""

from __future__ import annotations

import pytest

from batterystore.config import QuotaLimits
from batterystore.errors import ConfigError, QuotaError
from batterystore.quota import (
    Calendar,
    EnergySlice,
    QuotaLedger,
    Tariff,
    settle,
    split_energy,
)


def make_limits(**overrides) -> QuotaLimits:
    base = {
        "period_hours": 24.0,
        "charge_quota_kwh": 30.0,
        "discharge_quota_kwh": 30.0,
        "tariff_peak": 1.2,
        "tariff_valley": 0.4,
        "tz_offset_hours": 8.0,
    }
    base.update(overrides)
    return QuotaLimits(**base)


def local_midnight_before(yday: int = 20454) -> float:
    """2026-01-01 本地零点（UTC+8）对应的 UTC 秒，z-60 落在 2025-12-31。"""

    return yday * 86400.0 - 8 * 3600.0


def test_settle_computes_margin():
    report = settle(100.0, 80.0, 0.4, 1.2)
    assert report["charge_cost"] == pytest.approx(40.0)
    assert report["discharge_value"] == pytest.approx(96.0)
    assert report["margin"] == pytest.approx(56.0)
    assert settle(0.0, 0.0, 0.4, 1.2)["margin"] == 0.0


def test_rolling_period_is_rejected_by_config():
    with pytest.raises(ConfigError):
        QuotaLimits.from_mapping(
            {
                "period_hours": 12.0,
                "charge_quota_kwh": 1.0,
                "discharge_quota_kwh": 1.0,
                "tariff_peak": 1.2,
                "tariff_valley": 0.4,
            }
        )


def test_period_keys_are_natural_days_at_local_midnight():
    calendar = Calendar(8.0)
    zero = local_midnight_before()
    assert calendar.day_key(zero - 1) == "2025-12-31"
    assert calendar.day_key(zero) == "2026-01-01"
    assert calendar.day_key(zero + 86399.0) == "2026-01-01"
    assert calendar.day_key(zero + 86400.0) == "2026-01-02"
    assert calendar.month_key(zero + 86400.0) == "2026-01"


def test_split_attributes_cross_midnight_by_start_day():
    calendar = Calendar(8.0)
    tariff = Tariff(make_limits(), 8.0)
    zero = local_midnight_before()
    start = zero - 60.0
    # 23:59-00:01（谷段）120 秒，50Wh：午夜不拆账，整段一个分片归开始日。
    slices = split_energy(start, start + 120.0, 0.05, "charge", calendar, tariff)
    assert len(slices) == 1
    assert all(item.day_key == "2025-12-31" for item in slices)
    assert sum(item.kwh for item in slices) == pytest.approx(0.05)
    assert [item.tariff_kind for item in slices] == ["valley"]


def test_split_splits_tariff_boundary_too():
    calendar = Calendar(8.0)
    tariff = Tariff(make_limits(), 8.0)
    zero = local_midnight_before()
    d31 = zero - 86400.0
    seven_fifty = d31 + 7 * 3600.0 + 50 * 60.0
    slices = split_energy(seven_fifty, seven_fifty + 1200.0, 1.0, "discharge", calendar, tariff)
    assert {item.tariff_kind for item in slices} == {"valley", "peak"}
    assert all(item.day_key == "2025-12-31" for item in slices)
    assert sum(item.kwh for item in slices) == pytest.approx(1.0)


def test_admit_rejects_and_holds_power_without_touching_ledger():
    ledger = QuotaLedger(make_limits(charge_quota_kwh=1.0))
    zero = local_midnight_before() + 10 * 3600.0
    first = ledger.admit("charge", 100.0, 36.0, zero)
    assert first["allowed"] is True
    ledger.book("charge", zero, zero + 36.0, 1.0)
    # 再来一段必超额：拒绝，保持上一个有效功率，台账仍是 1.0。
    rejected = ledger.admit("charge", 100.0, 36.0, zero + 36.0)
    assert rejected["allowed"] is False
    assert rejected["held_kw"] == 100.0
    assert ledger.totals()["charge_kwh"] == pytest.approx(1.0)
    assert ledger.snapshot(zero + 36.0).rejections == 1


def test_record_exceed_raises_and_books_nothing():
    ledger = QuotaLedger(make_limits(charge_quota_kwh=10.0))
    now = local_midnight_before() + 10 * 3600.0
    ledger.book("charge", now, now + 1.0, 10.0)
    assert ledger.can_charge(1.0, now + 2.0) is False
    assert ledger.totals()["charge_kwh"] == pytest.approx(10.0)


def test_rollover_freezes_day_and_late_slice_is_appended():
    ledger = QuotaLedger(make_limits())
    zero = local_midnight_before()
    start = zero - 60.0
    # 零点前登记跨零点整拍 0.05：整段归 12-31（含零点后尾段）。
    result = ledger.book("charge", start, start + 120.0, 0.05)
    assert result["day_key"] == "2025-12-31"
    assert result["routed"] == "current"
    # 显式滚动后：旧日冻结 0.05，当日清零。
    rolled = ledger.roll(zero + 10.0)
    assert rolled["rolled"] is True
    assert ledger.totals()["charge_kwh"] == 0.0
    assert ledger.period.current() == "2026-01-01"
    day = ledger.archives.day("2025-12-31")
    assert day.archive.charge_kwh == pytest.approx(0.05)
    # 冻结后电表补传 0.01：只追加，冻结值不变。
    backfill = split_energy(start, start + 120.0, 0.01, "charge", ledger.calendar, ledger.tariff)
    ledger.append_late_slice(backfill[0], source="meter_backfill", received_at=zero + 90000.0)
    day = ledger.archives.day("2025-12-31")
    assert day.archive.charge_kwh == pytest.approx(0.05)
    assert day.totals()["charge_kwh"] == pytest.approx(0.06)
    assert day.totals()["late_charge_kwh"] == pytest.approx(0.01)


def test_archive_is_immutable_and_diff_is_append_only():
    ledger = QuotaLedger(make_limits())
    zero = local_midnight_before()
    d31 = zero - 86400.0
    ledger.book("charge", d31 + 100.0, d31 + 200.0, 1.0)
    ledger.roll(zero + 10.0)
    frozen_value = ledger.archives.day("2025-12-31").archive.charge_kwh
    note = ledger.note_diff(
        "2025-12-31", 1.02, "charge", source="external_meter", reason="电表补传差 0.02", now=zero + 90000.0
    )
    assert note["delta_kwh"] == pytest.approx(0.02)
    # 差异说明不改归档原值，也不进月结金额。
    assert ledger.archives.day("2025-12-31").archive.charge_kwh == frozen_value
    settlement = ledger.monthly_settlement("2025-12", now=zero + 90000.0)
    assert settlement["charge_kwh"] == pytest.approx(1.0)
    assert len(settlement["diff_notes"]) == 1


def test_append_late_slice_rejected_before_archive():
    ledger = QuotaLedger(make_limits())
    zero = local_midnight_before()
    ledger.book("charge", zero + 10.0, zero + 20.0, 0.1)
    slice_today = split_energy(zero + 30.0, zero + 40.0, 0.05, "charge", ledger.calendar, ledger.tariff)[0]
    with pytest.raises(QuotaError):
        ledger.append_late_slice(slice_today)


def test_monthly_settlement_uses_peak_and_valley_buckets():
    ledger = QuotaLedger(make_limits())
    zero = local_midnight_before()
    d31 = zero - 86400.0
    # 谷段充 10kWh，峰段放 8kWh，都在 2025-12-31。
    ledger.book("charge", d31 + 2 * 3600.0, d31 + 3 * 3600.0, 10.0)
    ledger.book("discharge", d31 + 10 * 3600.0, d31 + 11 * 3600.0, 8.0)
    ledger.roll(zero + 10.0)
    report = ledger.monthly_settlement("2025-12", now=zero + 90000.0)
    assert report["charge_cost"] == pytest.approx(4.0)
    assert report["discharge_value"] == pytest.approx(9.6)
    assert report["margin"] == pytest.approx(5.6)
    assert report["buckets"]["charge_valley_kwh"] == pytest.approx(10.0)
    assert report["buckets"]["discharge_peak_kwh"] == pytest.approx(8.0)


def test_reconciliation_conservation_across_days():
    ledger = QuotaLedger(make_limits())
    zero = local_midnight_before()
    ledger.book("charge", zero - 30.0, zero + 30.0, 0.6)
    ledger.roll(zero + 5.0)
    ledger.book("charge", zero + 3600.0, zero + 7200.0, 0.4)
    view = ledger.reconciliation(zero + 7200.0)
    # 旧年 0.6 已归档在 2025-12，新年当日 0.4 活着，总量 1.0 不丢不重。
    assert view["charge_kwh"] == pytest.approx(0.4)
    assert view["archived_charge_kwh"] == 0.0  # 当日所在 2026-01 月还没有归档日
    dec = ledger.monthly_settlement("2025-12", now=zero + 7200.0)
    assert dec["charge_kwh"] == pytest.approx(0.6)


def test_quota_records_and_limits(platform, clock):
    quota = platform.quota
    assert quota.can_charge(10.0, clock.now()) is True
    quota.book("charge", clock.now(), clock.now() + 1.0, 10.0)
    quota.book("discharge", clock.now() + 2.0, clock.now() + 3.0, 4.0)
    totals = quota.totals()
    assert totals["charge_kwh"] == pytest.approx(10.0)
    assert totals["discharge_kwh"] == pytest.approx(4.0)
    remaining = quota.remaining()
    assert remaining["charge_kwh"] == pytest.approx(platform.config.quota.charge_quota_kwh - 10.0)
    assert quota.can_charge(10_000.0, clock.now()) is False
    assert quota.snapshot(clock.now()).rejections == 1


def test_quota_rolls_into_new_natural_day(platform, clock):
    quota = platform.quota
    now = clock.now()
    quota.book("charge", now, now + 1.0, 100.0)
    day_key = quota.period.current()
    zero = quota.calendar.day_start(now)
    clock.advance((zero + 86400.0) - now + 1.0)
    rolled = quota.roll(clock.now())
    assert rolled["rolled"] is True
    assert quota.totals()["charge_kwh"] == 0.0
    assert quota.period.rolls() == 1
    assert quota.period.current() == quota.calendar.next_day_key(day_key)
    assert quota.archives.day(day_key).archive.charge_kwh == pytest.approx(100.0)
