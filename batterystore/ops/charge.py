"""温控充电编排。

顺序固定为「均衡完成核对 → 温控降额 → 电压核对 → 配额事前预检 → 电流
阶梯升流 → 分片记账 → 库仑累计」。配额预检排在升流之前：额度用满时
本步不再升流，PCS 保持上一个有效电流，电网侧不记本步电量，已经记账的
历史步不回滚。每步按 ``[step_start, step_start+dt)`` 区间切片记账，
跨零点或跨峰谷段的能量由台账按开始时刻归属。
"""

from __future__ import annotations

from ..errors import StoreError


def charge_rack(
    pcs,
    bms,
    temp,
    balance,
    quota,
    soc,
    rack,
    grid,
    station: str,
    *,
    steps: int,
    dt: float,
    now: float,
) -> dict:
    """执行一次充电编排，返回带步骤与违约项的报告。"""

    flow: list[dict] = []
    violations: list[str] = []

    derate_ratio = temp.charge_limit_ratio()
    plan = bms.charge_plan(
        rack,
        pcs.limits,
        balance_complete=balance.is_complete(),
        temp_derate_ratio=derate_ratio,
        balance_min_delta_mv=balance.limits.target_delta_mv,
    )
    flow.append({"component": "bms", "action": "charge_plan", "detail": plan.as_dict()})
    if not plan.allowed:
        return {"station": station, "ok": False, "steps": flow, "violations": list(plan.reasons)}

    derate = pcs.apply_derate(temperature=derate_ratio, grid=1.0)
    flow.append({"component": "temp", "action": "derate", "detail": derate.as_dict()})

    ladder = pcs.ladder
    ladder.begin(plan.current_a)
    current = ladder.lower_to(min(plan.current_a, pcs.limits.current_step_a))
    expected_v = sum(cell.rating_v for cell in rack.cells())
    tolerance_v = 0.02 * len(rack.cells())

    for index in range(steps):
        step_start = now + index * dt
        settled = ladder.verify_voltage(rack.voltage_v(), expected_v, tolerance_v)
        flow.append(
            {
                "component": "pcs",
                "action": "verify_voltage",
                "detail": {"index": index + 1, "settled": settled, "current_a": round(current, 3)},
            }
        )
        if not settled:
            violations.append(f"voltage_not_settled_step_{index + 1}")
            break

        target_a = max(0.0, min(pcs.limits.current_max_a, current + pcs.limits.current_step_a))
        estimated_kwh = abs(target_a) * rack.voltage_v() * dt / 3600.0 / 1000.0
        admission = quota.admit("charge", abs(target_a) * rack.voltage_v() / 1000.0, dt, step_start)
        flow.append(
            {
                "component": "quota",
                "action": "admit",
                "detail": {**admission, "target_current_a": round(target_a, 3), "estimated_kwh": round(estimated_kwh, 6)},
            }
        )
        if not admission["allowed"]:
            violations.append("quota_exceeded")
            flow.append(
                {
                    "component": "pcs",
                    "action": "hold_power",
                    "detail": {"held_current_a": round(current, 3), "target_current_a": round(target_a, 3)},
                }
            )
            break

        try:
            current = ladder.raise_to(target_a)
        except StoreError as error:
            violations.append("current_raise_rejected")
            flow.append({"component": "pcs", "action": "raise_rejected", "detail": error.as_record()})
            break
        energy_kwh = abs(current) * rack.voltage_v() * dt / 3600.0 / 1000.0
        booked = quota.book("charge", step_start, step_start + dt, energy_kwh, source="charge_step")
        flow.append(
            {
                "component": "quota",
                "action": "charge_booked",
                "detail": {
                    "index": index + 1,
                    "day_key": booked["day_key"],
                    "booked_kwh": booked["booked_kwh"],
                    "total_kwh": booked["total_kwh"],
                    "slices": [item.as_dict() for item in booked["booked_slices"]],
                    "over_quota": booked["over_quota"],
                },
            }
        )
        delta_ah = soc.accumulate(abs(current), dt)
        flow.append(
            {
                "component": "soc",
                "action": "accumulate",
                "detail": {"index": index + 1, "current_a": round(current, 3), "delta_ah": round(delta_ah, 6)},
            }
        )
        grid.record_flow(-abs(current) * rack.voltage_v() / 1000.0, dt)

    estimate = soc.value(now + steps * dt)
    rack.set_soc(estimate.soc_pct)
    flow.append({"component": "soc", "action": "estimate", "detail": estimate.as_dict()})
    return {"station": station, "ok": not violations, "steps": flow, "violations": violations}
