"""结算。

结算只认 :mod:`batterystore.quota.interval` 产出的能量分片：充电按分片开始
时刻的电价计费，放电按分片开始时刻的电价计价，峰、谷两档各自汇总后
折出电费与套利收益。配额台账与月结读的是同一组分片、同一套电价，
不存在"记账一套、结算一套"的第二口径。
"""

from __future__ import annotations

from .tariff import Tariff


def settle(charge_kwh: float, discharge_kwh: float, charge_rate: float, discharge_rate: float) -> dict:
    """按单一电价结算一次充放电（供单笔估算与兼容旧入口）。"""

    charge_cost = max(0.0, float(charge_kwh)) * float(charge_rate)
    discharge_value = max(0.0, float(discharge_kwh)) * float(discharge_rate)
    return {
        "charge_cost": round(charge_cost, 4),
        "discharge_value": round(discharge_value, 4),
        "margin": round(discharge_value - charge_cost, 4),
        "charge_rate": float(charge_rate),
        "discharge_rate": float(discharge_rate),
    }


def empty_buckets() -> dict:
    """返回清零的分档电量桶。"""

    return {"charge_peak_kwh": 0.0, "charge_valley_kwh": 0.0, "discharge_peak_kwh": 0.0, "discharge_valley_kwh": 0.0}


def bucket_slice(buckets: dict, energy_slice) -> dict:
    """把一条分片累加进分档电量桶。"""

    key = f"{energy_slice.direction}_{energy_slice.tariff_kind}_kwh"
    buckets[key] = round(buckets.get(key, 0.0) + energy_slice.kwh, 6)
    return buckets


def settle_buckets(buckets: dict, tariff: Tariff) -> dict:
    """按分档电量桶与峰谷电价结算。"""

    peak = tariff.rate_for_kind("peak")
    valley = tariff.rate_for_kind("valley")
    charge_kwh = round(buckets.get("charge_peak_kwh", 0.0) + buckets.get("charge_valley_kwh", 0.0), 6)
    discharge_kwh = round(buckets.get("discharge_peak_kwh", 0.0) + buckets.get("discharge_valley_kwh", 0.0), 6)
    charge_cost = buckets.get("charge_peak_kwh", 0.0) * peak + buckets.get("charge_valley_kwh", 0.0) * valley
    discharge_value = (
        buckets.get("discharge_peak_kwh", 0.0) * peak + buckets.get("discharge_valley_kwh", 0.0) * valley
    )
    return {
        "buckets": dict(buckets),
        "charge_kwh": charge_kwh,
        "discharge_kwh": discharge_kwh,
        "charge_cost": round(charge_cost, 4),
        "discharge_value": round(discharge_value, 4),
        "margin": round(discharge_value - charge_cost, 4),
        "tariff_peak": peak,
        "tariff_valley": valley,
    }


def settle_slices(slices, tariff: Tariff) -> dict:
    """把一组分片直接结算成分档账单。"""

    buckets = empty_buckets()
    for energy_slice in slices:
        bucket_slice(buckets, energy_slice)
    return settle_buckets(buckets, tariff)
