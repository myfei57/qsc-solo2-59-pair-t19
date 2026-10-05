"""限额放电编排。

顺序固定为「并网限额钳位 → 配额事前预检 → 爬坡下发 → 分片记账 → 电量
累计」。配额预检必须排在爬坡下发之前：额度用满时新功率指令直接拒绝，
PCS 保持上一个有效功率，电网侧也不记这一段电量——拒绝不回滚任何已记账
数据，因为拒绝时根本还没有新数据落账。记账按 ``[now, now+dt)`` 区间
切片，跨零点或跨峰谷段的能量由台账按开始时刻归属到对应自然日与档位。
"""

from __future__ import annotations


def discharge_rack(
    pcs,
    grid,
    quota,
    soc,
    rack,
    station: str,
    *,
    power_kw: float,
    dt: float,
    now: float,
) -> dict:
    """执行一次放电编排，返回带步骤与违约项的报告。"""

    flow: list[dict] = []
    violations: list[str] = []

    if pcs.cut.is_tripped():
        return {"station": station, "ok": False, "steps": flow, "violations": ["pcs_tripped"]}

    if not pcs.is_tied():
        pcs.transfer("off_grid", "discharge_request")
        pcs.transfer("grid_tie", "discharge_request")
    clamped = grid.clamp_for_ramp(abs(float(power_kw)))
    flow.append({"component": "grid", "action": "clamp_before_ramp", "detail": clamped})

    # 事前预检：只按估算电量问一次额度，拒绝时不下发、不记账。
    admission = quota.admit("discharge", clamped["allowed_kw"], dt, now)
    flow.append({"component": "quota", "action": "admit", "detail": admission})
    if not admission["allowed"]:
        violations.append("quota_exceeded")
        flow.append(
            {
                "component": "pcs",
                "action": "hold_power",
                "detail": {"held_kw": admission["held_kw"], "requested_kw": admission["requested_kw"]},
            }
        )
        return {"station": station, "ok": False, "steps": flow, "violations": violations}

    derate = pcs.apply_derate(grid=clamped["ratio"], temperature=1.0)
    point = pcs.set_point(clamped["allowed_kw"], dt, derate)
    flow.append({"component": "pcs", "action": "ramp", "detail": point})
    if point["violations"]:
        violations.extend(point["violations"])

    energy_kwh = abs(point["ramped_kw"]) * dt / 3600.0
    # 按实测功率对应的区间切片记账：跨零点分片归开始日，不回滚。
    booked = quota.book("discharge", now, now + dt, energy_kwh, source="discharge_step")
    flow.append({"component": "quota", "action": "discharge_booked", "detail": {
        "day_key": booked["day_key"],
        "booked_kwh": booked["booked_kwh"],
        "total_kwh": booked["total_kwh"],
        "slices": [item.as_dict() for item in booked["booked_slices"]],
        "over_quota": booked["over_quota"],
    }})
    arc = grid.record_flow(point["ramped_kw"], dt)
    flow.append({"component": "grid", "action": "energy", "detail": arc})

    delta_ah = soc.accumulate(-abs(point["ramped_kw"]) * 1000.0 / max(rack.voltage_v(), 1e-9), dt)
    estimate = soc.value(now)
    rack.set_soc(estimate.soc_pct)
    flow.append(
        {
            "component": "soc",
            "action": "estimate",
            "detail": {**estimate.as_dict(), "delta_ah": round(delta_ah, 6)},
        }
    )
    return {"station": station, "ok": not violations, "steps": flow, "violations": violations}
