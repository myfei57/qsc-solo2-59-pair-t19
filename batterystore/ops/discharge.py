"""限额放电编排。

顺序固定为「并网限额钳位 → 配额预检 → 爬坡 → 电量累计 → 配额记账」。
钳位必须在爬坡之前完成：带限功率参数的放电指令如果先爬坡再钳位，并网点
会在一瞬间越限。配额预检必须在爬坡与出力之前完成：额度不足时直接拒绝，
功率保持在上一个有效值，不写电网电量、不动 SOC、不回滚已记账电量。
"""

from __future__ import annotations

from ..errors import StoreError


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
    derate = pcs.apply_derate(grid=clamped["ratio"], temperature=1.0)

    # 按钳位与降额后的计划出力预估本段电量，先授权后出力。
    planned_kw = clamped["allowed_kw"] * derate.ratio
    planned_kwh = abs(planned_kw) * dt / 3600.0
    preflight = quota.preflight("discharge", planned_kwh, now)
    flow.append({"component": "quota", "action": "preflight", "detail": preflight})
    if not preflight["allowed"]:
        held = pcs.hold_power("quota_rejected")
        flow.append({"component": "pcs", "action": "hold_last_valid_power", "detail": held})
        violations.append("quota_exceeded")
        return {"station": station, "ok": False, "steps": flow, "violations": violations}

    point = pcs.set_point(clamped["allowed_kw"], dt, derate)
    flow.append({"component": "pcs", "action": "ramp", "detail": point})
    if point["violations"]:
        violations.extend(point["violations"])

    energy_kwh = abs(point["ramped_kw"]) * dt / 3600.0
    arc = grid.record_flow(point["ramped_kw"], dt)
    flow.append({"component": "grid", "action": "energy", "detail": arc})
    try:
        receipt = quota.record_discharge(energy_kwh, now, duration_s=dt)
    except StoreError as error:
        # 预检已通过仍失败（并发/时钟异常）：保持上一个有效功率，不回滚。
        held = pcs.hold_power("quota_rejected")
        flow.append({"component": "pcs", "action": "hold_last_valid_power", "detail": held})
        violations.append("quota_exceeded")
        flow.append({"component": "quota", "action": "discharge_rejected", "detail": error.as_record()})
        return {"station": station, "ok": False, "steps": flow, "violations": violations}
    flow.append({"component": "quota", "action": "discharge_recorded", "detail": {"period": receipt["period"]}})
    if receipt.get("crossing"):
        flow.append({"component": "quota", "action": "cross_period_tail", "detail": receipt["crossing"]})

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
