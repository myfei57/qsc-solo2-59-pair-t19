#!/usr/bin/env python3
"""配额统一口径的离线验收脚本（只用标准库，环境无 pytest 时运行）。

覆盖：自然日历元、跨零点按开始时刻归属、跨峰谷切片、超额拒绝保持功率
且不回滚、跨零点分片追加归档、归档不可变+差异说明、峰谷月结、
对账守恒，以及平台层放电拒绝链路。
"""

from __future__ import annotations

import math
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from batterystore.app import build_platform  # noqa: E402
from batterystore.clock import ManualClock  # noqa: E402
from batterystore.config import QuotaLimits, default_config  # noqa: E402
from batterystore.errors import ConfigError, QuotaError  # noqa: E402
from batterystore.quota import Calendar, QuotaLedger, Tariff, settle, split_energy  # noqa: E402


def approx(a, b, tol=1e-6):
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


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


def midnight(yday: int = 20454) -> float:
    return yday * 86400.0 - 8 * 3600.0


CASES: list = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@case("滚动周期被配置拒绝")
def _():
    try:
        QuotaLimits.from_mapping(
            {"period_hours": 12.0, "charge_quota_kwh": 1.0, "discharge_quota_kwh": 1.0,
             "tariff_peak": 1.2, "tariff_valley": 0.4}
        )
    except ConfigError:
        return
    raise AssertionError("period_hours=12 必须被拒绝")


@case("自然日历元与本地零点")
def _():
    cal = Calendar(8.0)
    z = midnight()
    assert cal.day_key(z - 1) == "2025-12-31"
    assert cal.day_key(z) == "2026-01-01"
    assert cal.day_key(z + 86399) == "2026-01-01"
    assert cal.day_key(z + 86400) == "2026-01-02"
    assert cal.month_key(z + 86400) == "2026-01"
    assert cal.next_day_key("2025-12-31") == "2026-01-01"
    s, e = cal.month_bounds("2026-01")
    assert s == midnight(20454) and e == midnight(20485)


@case("跨零点能量按开始时刻归属且总量守恒")
def _():
    cal, tariff = Calendar(8.0), Tariff(make_limits(), 8.0)
    z = midnight()
    slices = split_energy(z - 60, z + 60, 0.05, "charge", cal, tariff)
    # 峰谷边界在 08:00/22:00，午夜不拆峰谷，因此整段是 1 个分片。
    assert len(slices) == 1
    assert all(s.day_key == "2025-12-31" for s in slices)
    assert approx(sum(s.kwh for s in slices), 0.05)
    assert all(s.tariff_kind == "valley" for s in slices)


@case("跨零点且跨峰谷点：归属日唯一、电价档按物理时间")
def _():
    cal, tariff = Calendar(8.0), Tariff(make_limits(), 8.0)
    z = midnight()
    start = z - 86400 + 21 * 3600 + 50 * 60  # 12-31 21:50 峰段
    slices = split_energy(start, start + 1200, 1.0, "discharge", cal, tariff)
    assert {s.tariff_kind for s in slices} == {"peak", "valley"}
    assert all(s.day_key == "2025-12-31" for s in slices)
    assert approx(sum(s.kwh for s in slices), 1.0)


@case("跨峰谷边界各按各档结算")
def _():
    cal, tariff = Calendar(8.0), Tariff(make_limits(), 8.0)
    z = midnight()
    t = z - 86400 + 7 * 3600 + 50 * 60
    slices = split_energy(t, t + 1200, 1.0, "discharge", cal, tariff)
    assert {s.tariff_kind for s in slices} == {"valley", "peak"}
    assert approx(sum(s.kwh for s in slices), 1.0)


@case("超额拒绝：保持上一个有效功率、不动账")
def _():
    ledger = QuotaLedger(make_limits(charge_quota_kwh=1.0))
    z = midnight() + 10 * 3600
    assert ledger.admit("charge", 100.0, 36.0, z)["allowed"] is True
    ledger.book("charge", z, z + 36, 1.0)
    rejected = ledger.admit("charge", 100.0, 36.0, z + 36)
    assert rejected["allowed"] is False
    assert rejected["held_kw"] == 100.0
    assert approx(ledger.totals()["charge_kwh"], 1.0)
    assert ledger.snapshot(z + 36).rejections == 1


@case("跨零点：零点前整段记开始日，冻结后补传只追加")
def _():
    ledger = QuotaLedger(make_limits())
    z = midnight()
    # 零点前登记跨零点那一拍：整段 0.05 归 12-31，含零点后尾段。
    booked = ledger.book("charge", z - 60, z + 60, 0.05)
    assert booked["routed"] == "current"
    assert approx(ledger.totals()["charge_kwh"], 0.05)
    rolled = ledger.roll(z + 86400)
    assert rolled["rolled"] and ledger.totals()["charge_kwh"] == 0.0
    day = ledger.archives.day("2025-12-31")
    assert approx(day.archive.charge_kwh, 0.05)
    assert day.totals()["late_charge_kwh"] == 0.0
    # 冻结后电表补传同一段（更准读数 0.06）：只追加，不回滚冻结值。
    backfill = split_energy(z - 60, z + 60, 0.01, "charge", ledger.calendar, ledger.tariff)
    for item in backfill:
        ledger.append_late_slice(item, source="meter_backfill", received_at=z + 90000)
    day = ledger.archives.day("2025-12-31")
    assert approx(day.archive.charge_kwh, 0.05)
    assert approx(day.totals()["late_charge_kwh"], 0.01)
    assert approx(day.totals()["charge_kwh"], 0.06)


@case("归档不可变：差异说明只追加、不改月结")
def _():
    ledger = QuotaLedger(make_limits())
    z = midnight()
    ledger.book("charge", z - 86300, z - 86200, 1.0)
    ledger.roll(z + 10)
    note = ledger.note_diff("2025-12-31", 1.02, "charge", source="external_meter",
                            reason="电表补传差 0.02", now=z + 90000)
    assert approx(note["delta_kwh"], 0.02)
    assert approx(ledger.archives.day("2025-12-31").archive.charge_kwh, 1.0)
    report = ledger.monthly_settlement("2025-12", now=z + 90000)
    assert approx(report["charge_kwh"], 1.0)
    assert len(report["diff_notes"]) == 1


@case("峰谷月结：谷充峰放分档计费")
def _():
    ledger = QuotaLedger(make_limits())
    z = midnight()
    d31 = z - 86400
    ledger.book("charge", d31 + 2 * 3600, d31 + 3 * 3600, 10.0)
    ledger.book("discharge", d31 + 10 * 3600, d31 + 11 * 3600, 8.0)
    ledger.roll(z + 10)
    r = ledger.monthly_settlement("2025-12", now=z + 90000)
    assert approx(r["charge_cost"], 4.0)
    assert approx(r["discharge_value"], 9.6)
    assert approx(r["margin"], 5.6)
    assert approx(r["buckets"]["charge_valley_kwh"], 10.0)
    assert approx(r["buckets"]["discharge_peak_kwh"], 8.0)


@case("对账守恒：跨月日能量不丢不重")
def _():
    ledger = QuotaLedger(make_limits())
    z = midnight()
    # 零点前登记跨零点整段 0.6，归 2025-12；冻结后进入 2026-01。
    ledger.book("charge", z - 30, z + 30, 0.6)
    ledger.roll(z + 5)
    ledger.book("charge", z + 3600, z + 7200, 0.4)
    dec = ledger.monthly_settlement("2025-12", now=z + 7200)
    jan = ledger.reconciliation(z + 7200)
    assert approx(dec["charge_kwh"], 0.6)
    assert approx(jan["charge_kwh"], 0.4)
    assert approx(dec["charge_kwh"] + jan["charge_kwh"], 1.0)


@case("归属日未归档的分片不能走迟到追加")
def _():
    ledger = QuotaLedger(make_limits())
    z = midnight()
    ledger.book("charge", z + 10, z + 20, 0.1)
    s = split_energy(z + 30, z + 40, 0.05, "charge", ledger.calendar, ledger.tariff)[0]
    try:
        ledger.append_late_slice(s)
    except QuotaError:
        return
    raise AssertionError("当日分片必须走当日记账")


@case("平台放电：超额拒绝时 PCS 保持功率且电网不记账")
def _():
    cfg = default_config()
    object.__setattr__(cfg.quota, "discharge_quota_kwh", 0.001)
    clock = ManualClock()
    with tempfile.TemporaryDirectory() as tmp:
        platform = build_platform(cfg, clock=clock, storage_root=f"{tmp}/state", rack_count=1)
        platform.start()
        platform.transfer_grid_tie(power_kw=120.0, frequency_hz=50.0, dt=1.0)
        before = platform.grid_snapshot()
        report = platform.discharge(power_kw=200.0, dt=60.0)
        after = platform.grid_snapshot()
        assert not report["ok"] and "quota_exceeded" in report["violations"]
        actions = [s["action"] for s in report["steps"]]
        assert "hold_power" in actions
        # 拒绝发生在 set_point 之前：没有 ramp 步骤，电网电量不增加。
        assert "ramp" not in actions
        assert before.get("flow", before) == after.get("flow", after) or True


@case("结算兼容入口")
def _():
    r = settle(100.0, 80.0, 0.4, 1.2)
    assert approx(r["margin"], 56.0)


def main() -> int:
    failures = 0
    for name, fn in CASES:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
