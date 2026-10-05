"""充放电配额台账。

**统一口径**：配额周期、超额拒绝与峰谷结算共用同一份周期键（本地自然日
对齐）和同一条归属规则——一段能量按它的**开始时刻**归属周期、按开始
时刻的峰谷段定价。

为什么选「开始时刻归属」而不是「结束时刻归属」：

1. 控制链是先授权（核对剩余额度）后输出能量。授权发生在开始时刻，额度
   扣减与授权落在同一周期，判定无歧义；按结束时刻归属会出现"在 A 周期
   拿到授权、账却记到 B 周期"的跨周期授权，月结时必然累积差额。
2. 调度策略无法把零点前的能量"推"到零点后计价，归属口径不可被越权
   利用；结束时刻归属可以靠压/抬开始时刻调节结算周期。
3. 两种口径的月结差额是确定可解释的：差额只来自跨零点那几段，公式为
   ``Σ 跨段电量 × (结束时段电价 − 开始时段电价)``。台账把每一段跨周期
   能量单独留痕，差额可逐条复核，总量守恒（见 :meth:`reconciliation`）。

跨零点的能量在开始周期**全额记账、全额占额**，同时登记一条跨周期
明细（crossing）标出落在边界之后的尾部能量。这样跨周期的那段能量既
"出来"（明细可见）又"对得上"（各周期累计 + 跨段尾部守恒）。

额度用满后拒绝，已记账电量不回滚；编排层在拒绝时保持上一个有效功率。
周期滚动时上一周期累计原样归档，归档数字不可改写，迟到的修正只能以
差异说明（note）追加。
"""

from __future__ import annotations

from ..config import QuotaLimits
from ..errors import QuotaError
from .period import PeriodTracker
from .snapshot import QuotaSnapshot
from .tariff import Tariff

_DIRECTIONS = ("charge", "discharge")


class QuotaLedger:
    """充放电配额台账。"""

    def __init__(self, limits: QuotaLimits) -> None:
        self._limits = limits
        self._period = PeriodTracker(limits.period_hours, limits.timezone_offset_hours)
        self._tariff = Tariff(limits)
        self._charge_kwh = 0.0
        self._discharge_kwh = 0.0
        self._rejections = {"charge": 0, "discharge": 0}
        # 全量记账流水（当期与历史的金额都从这里汇总），与 totals 分离。
        self._entries: list[dict] = []
        # 跨周期边界（跨零点）的能量明细，月结差额只可能来自这些段。
        self._crossings: list[dict] = []
        # 落在当前周期槽内的"跨段尾部"：只用于对账展示，不占本周期额度。
        self._cross_in: list[dict] = []

    @property
    def limits(self) -> QuotaLimits:
        return self._limits

    @property
    def period(self) -> PeriodTracker:
        return self._period

    @property
    def tariff(self) -> Tariff:
        return self._tariff

    def totals(self) -> dict:
        """返回本周期累计电量。"""

        return {
            "charge_kwh": round(self._charge_kwh, 6),
            "discharge_kwh": round(self._discharge_kwh, 6),
            "period": self._period.current(),
        }

    def remaining(self) -> dict:
        """返回本周期剩余额度。"""

        return {
            "charge_kwh": round(max(0.0, self._limits.charge_quota_kwh - self._charge_kwh), 6),
            "discharge_kwh": round(max(0.0, self._limits.discharge_quota_kwh - self._discharge_kwh), 6),
        }

    def roll(self, now: float) -> dict:
        """按当前时间滚动周期，归档上一周期并清零当期累计。"""

        result = self._period.observe(now, self.totals())
        if result["rolled"]:
            self._charge_kwh = 0.0
            self._discharge_kwh = 0.0
            self._rebuild_cross_in(now)
        return result

    def preflight(self, direction: str, kwh: float, now: float) -> dict:
        """授权前预检：只核对额度，不产生任何记账副作用。

        返回剩余额度与是否放行。被拒绝时不写流水、不动累计，编排层据此
        保持上一个有效功率。
        """

        if direction not in _DIRECTIONS:
            raise ValueError(f"未知记账方向: {direction}")
        self.roll(now)
        remaining = self.remaining()[f"{direction}_kwh"]
        allowed = float(kwh) <= remaining
        if not allowed:
            self._rejections[direction] += 1
        return {
            "direction": direction,
            "requested_kwh": round(float(kwh), 6),
            "remaining_kwh": round(remaining, 6),
            "allowed": allowed,
            "period": self._period.current(),
        }

    def record_charge(self, kwh: float, now: float, duration_s: float = 0.0) -> dict:
        """登记一段充电电量。开始时刻决定归属周期与电价，超额即拒绝。"""

        return self._book("charge", kwh, now, duration_s)

    def record_discharge(self, kwh: float, now: float, duration_s: float = 0.0) -> dict:
        """登记一段放电电量。开始时刻决定归属周期与电价，超额即拒绝。"""

        return self._book("discharge", kwh, now, duration_s)

    def _book(self, direction: str, kwh: float, now: float, duration_s: float) -> dict:
        energy = max(0.0, float(kwh))
        start = float(now)
        self.roll(start)
        start_key = self._period.key_for(start)
        quota_name = f"{direction}_kwh"
        quota = getattr(self._limits, f"{direction}_quota_kwh")

        # 迟到记账：开始周期已归档。归档数字不改写，只追加差异说明。
        archive = self._period.archive(start_key)
        if archive is not None:
            rate = self._tariff.rate(start)
            amount = energy * rate
            note = {
                "kind": f"late_{direction}",
                "ts": round(start, 3),
                "kwh": round(energy, 6),
                "rate": rate,
                "amount": round(amount, 4),
                "reason": "开始周期已归档，按差异说明追加，不改写归档电量",
            }
            self._period.add_note(start_key, note)
            self._entries.append(
                {
                    "direction": direction,
                    "kwh": round(energy, 6),
                    "rate": rate,
                    "amount": round(amount, 4),
                    "period": start_key,
                    "kind": "late",
                    "late": True,
                }
            )
            return {
                f"{direction}d_kwh": round(energy, 6),
                "totals": self.totals(),
                "period": start_key,
                "booked": False,
                "late_note": note,
            }

        # 正常路径：开始周期即当前周期，先看额度（拒绝不写任何账）。
        used = self._charge_kwh if direction == "charge" else self._discharge_kwh
        if energy > quota - used:
            self._rejections[direction] += 1
            raise QuotaError(
                f"本周期{('充电' if direction == 'charge' else '放电')}额度不足",
                component="quota",
            )

        end = start + max(0.0, float(duration_s))
        _slot_start, slot_end = self._period.slot_bounds(start)
        rate = self._tariff.rate(start)
        amount = energy * rate
        crossing = None

        if duration_s > 0.0 and end > slot_end:
            # 跨周期（跨零点）能量：开始周期全额记账，尾部单独留痕。
            next_slot_end = slot_end + self._period.period_hours * 3600.0
            tail_s = min(end, next_slot_end) - slot_end
            tail_energy = energy * (tail_s / float(duration_s))
            end_key = self._period.key_for(end)
            crossing = {
                "direction": direction,
                "start_ts": round(start, 3),
                "end_ts": round(end, 3),
                "boundary_ts": round(slot_end, 3),
                "start_period": start_key,
                "tail_period": end_key,
                "total_kwh": round(energy, 6),
                "tail_kwh": round(tail_energy, 6),
                "start_rate": rate,
                "tail_rate": self._tariff.rate(slot_end),
            }
            self._crossings.append(crossing)
            if end_key == self._period.current():
                self._cross_in.append(dict(crossing))

        if direction == "charge":
            self._charge_kwh += energy
        else:
            self._discharge_kwh += energy
        self._entries.append(
            {
                "direction": direction,
                "kwh": round(energy, 6),
                "rate": rate,
                "amount": round(amount, 4),
                "period": start_key,
                "kind": "crossing" if crossing else "normal",
                "late": False,
            }
        )
        receipt = {
            f"{direction}d_kwh": round(energy, 6),
            "totals": self.totals(),
            "period": start_key,
            "booked": True,
            "rate": rate,
            "amount": round(amount, 4),
        }
        if crossing:
            receipt["crossing"] = dict(crossing)
        return receipt

    def settlement(self, now: float | None = None) -> dict:
        """按当期记账流水汇总峰谷结算（与配额累计同源）。"""

        key = self._period.key_for(now) if now is not None else self._period.current()
        entries = [item for item in self._entries if item["period"] == key and not item["late"]]
        charge_cost = sum(item["amount"] for item in entries if item["direction"] == "charge")
        discharge_value = sum(item["amount"] for item in entries if item["direction"] == "discharge")
        report = {
            "charge_cost": round(charge_cost, 4),
            "discharge_value": round(discharge_value, 4),
            "margin": round(discharge_value - charge_cost, 4),
            "charge_kwh": round(sum(item["kwh"] for item in entries if item["direction"] == "charge"), 6),
            "discharge_kwh": round(sum(item["kwh"] for item in entries if item["direction"] == "discharge"), 6),
        }
        if now is not None:
            report["current_rate"] = self._tariff.rate(now)
            report["kind"] = self._tariff.kind(now)
        return report

    def period_settlement(self, key: str) -> dict:
        """结算任意周期（含已归档周期），金额全部来自不可变流水。

        归档期的正常金额不可变；迟到追加的差异电量与金额单列，不并进
        归档数字。
        """

        entries = [item for item in self._entries if item["period"] == key and not item["late"]]
        late_entries = [item for item in self._entries if item["period"] == key and item["late"]]
        charge_kwh = sum(item["kwh"] for item in entries if item["direction"] == "charge")
        discharge_kwh = sum(item["kwh"] for item in entries if item["direction"] == "discharge")
        charge_cost = sum(item["amount"] for item in entries if item["direction"] == "charge")
        discharge_value = sum(item["amount"] for item in entries if item["direction"] == "discharge")
        notes = self._period.archive(key)["notes"] if self._period.archive(key) else []
        return {
            "period": key,
            "charge_kwh": round(charge_kwh, 6),
            "discharge_kwh": round(discharge_kwh, 6),
            "charge_cost": round(charge_cost, 4),
            "discharge_value": round(discharge_value, 4),
            "margin": round(discharge_value - charge_cost, 4),
            "late_notes": len(notes),
            "late_kwh": round(sum(item["kwh"] for item in late_entries), 6),
            "late_amount": round(sum(item["amount"] for item in late_entries), 4),
        }

    def crossings(self) -> list[dict]:
        """返回全部跨周期能量明细（不可变副本）。"""

        return [dict(item) for item in self._crossings]

    def attribution_difference(self, window_start: float, window_end: float) -> dict:
        """月结：开始时刻归属相对结束时刻归属的结算差额。

        两种口径对同一段跨边界能量的计价不同：开始时刻归属整段按开始
        时刻电价，结束时刻归属整段按结束时刻电价，单段差额为
        ``整段电量 × (结束时段电价 − 开始时段电价)``；周期归属差异则是
        整段电量从开始槽整体挪到结束槽（不是只挪尾部，尾部只是物理上
        落在边界之后的那部分，用于守恒拆分）。月结差额可逐条复核。
        """

        amount_delta = 0.0
        charge_shift = 0.0
        discharge_shift = 0.0
        matched: list[dict] = []
        for item in self._crossings:
            boundary = item["boundary_ts"]
            if not (window_start <= boundary < window_end):
                continue
            delta = item["total_kwh"] * (item["tail_rate"] - item["start_rate"])
            amount_delta += delta
            if item["direction"] == "charge":
                charge_shift += item["total_kwh"]
            else:
                discharge_shift += item["total_kwh"]
            matched.append({**dict(item), "amount_delta": round(delta, 4)})
        return {
            "window_start": round(window_start, 3),
            "window_end": round(window_end, 3),
            "policy": "start_time_attribution",
            "segments": len(matched),
            "charge_shift_kwh": round(charge_shift, 6),
            "discharge_shift_kwh": round(discharge_shift, 6),
            "amount_delta_end_minus_start": round(-amount_delta, 4),
            "amount_delta_start_minus_end": round(amount_delta, 4),
            "details": matched,
        }

    def reconciliation(self) -> dict:
        """守恒对账：正常账 = 各归档周期之和 + 当期；迟到差异单列。

        全部流水电量必须等于「归档正常账 + 当期正常账 + 迟到差异账」。
        归档数字不因迟到账变化，迟到账只通过差异说明与单独金额存在。
        """

        normal = [item for item in self._entries if not item["late"]]
        late = [item for item in self._entries if item["late"]]
        booked_charge = sum(item["kwh"] for item in self._entries if item["direction"] == "charge")
        booked_discharge = sum(item["kwh"] for item in self._entries if item["direction"] == "discharge")
        normal_charge = sum(item["kwh"] for item in normal if item["direction"] == "charge")
        normal_discharge = sum(item["kwh"] for item in normal if item["direction"] == "discharge")
        late_charge = sum(item["kwh"] for item in late if item["direction"] == "charge")
        late_discharge = sum(item["kwh"] for item in late if item["direction"] == "discharge")
        archived = self._period.history()
        archived_charge = sum(item["charge_kwh"] for item in archived)
        archived_discharge = sum(item["discharge_kwh"] for item in archived)
        return {
            "booked_charge_kwh": round(booked_charge, 6),
            "booked_discharge_kwh": round(booked_discharge, 6),
            "archived_charge_kwh": round(archived_charge, 6),
            "archived_discharge_kwh": round(archived_discharge, 6),
            "current_charge_kwh": round(self._charge_kwh, 6),
            "current_discharge_kwh": round(self._discharge_kwh, 6),
            "late_charge_kwh": round(late_charge, 6),
            "late_discharge_kwh": round(late_discharge, 6),
            "crossing_tails": len(self._crossings),
            "charge_balanced": abs(normal_charge - archived_charge - self._charge_kwh) < 1e-6
            and abs(booked_charge - normal_charge - late_charge) < 1e-6,
            "discharge_balanced": abs(normal_discharge - archived_discharge - self._discharge_kwh) < 1e-6
            and abs(booked_discharge - normal_discharge - late_discharge) < 1e-6,
        }

    def _rebuild_cross_in(self, now: float) -> None:
        current = self._period.current()
        self._cross_in = [dict(item) for item in self._crossings if item["tail_period"] == current]

    def snapshot(self, now: float) -> QuotaSnapshot:
        """返回配额状态快照。"""

        self.roll(now)
        return QuotaSnapshot(
            period=self._period.current(),
            rolls=self._period.rolls(),
            totals=self.totals(),
            remaining=self.remaining(),
            tariff=self._tariff.as_dict(),
            settlement=self.settlement(now),
            rejections=self._rejections["charge"] + self._rejections["discharge"],
            history=self._period.history(),
            policy={
                "period": "local_calendar_day",
                "attribution": "start_time",
                "crossing": "booked_to_start_period_tail_listed",
                "archive": "immutable_append_notes_only",
                "period_hours": self._limits.period_hours,
                "timezone_offset_hours": self._limits.timezone_offset_hours,
            },
            rejections_by_direction=dict(self._rejections),
            crossings=[dict(item) for item in self._cross_in],
            reconciliation=self.reconciliation(),
        )
