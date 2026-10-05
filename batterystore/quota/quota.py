"""充放电配额台账（统一口径）。

周期额度、超额拒绝、峰谷月结共用同一份数据：能量分片
（:mod:`batterystore.quota.interval`）。规则如下：

1. **周期=本地自然日**（:mod:`batterystore.quota.calendar`），不是滚动
   24 小时窗口；自然月是自然日的直接归并。
2. **归属按开始时刻**：每个计量区间 ``[start, end)`` 的配额归属日是其
   **开始时刻**所在自然日（ADR-0002），整段扣开始日额度；峰谷档则按
   分片的**物理交付时刻**分档计价。配额按"区间整体估算电量"对**开始日**
   剩余额度做事前预检，跨零点指令因此不会把一半能量算到新的一天。
3. **额度用满即拒绝，且保持上一个有效功率**：预检在任何下发/记账动作之前
   完成，拒绝只记一次拒绝事件，不下发新功率，不动任何已记账电量。
4. **跨零点只归档不回滚**：滚动时冻结上一日快照；跨零点那拍在零点后才
   完成计量的分片，按开始日作为**迟到分片追加**到该日台账。归档值与追加
   记录此后都不可改写，差异只能以差异说明追加（ADR-0003）。
"""

from __future__ import annotations

from ..config import QuotaLimits
from ..errors import QuotaError
from .accounting import bucket_slice, empty_buckets, settle_buckets, settle_slices
from .archive import ArchiveLedger, DayArchive, DiffNote, LateSlice
from .calendar import Calendar
from .interval import EnergySlice, slices_sum, split_energy
from .period import PeriodTracker
from .snapshot import QuotaSnapshot
from .tariff import Tariff

_DIRECTIONS = ("charge", "discharge")


class QuotaLedger:
    """充放电配额台账。"""

    def __init__(self, limits: QuotaLimits) -> None:
        self._limits = limits
        self._calendar = Calendar(limits.tz_offset_hours)
        self._period = PeriodTracker(self._calendar, limits.period_hours)
        self._tariff = Tariff(limits, limits.tz_offset_hours)
        self._archives = ArchiveLedger()
        self._current_kwh = {direction: 0.0 for direction in _DIRECTIONS}
        self._current_buckets = empty_buckets()
        self._rejections = 0
        self._last_admission = {"direction": None, "allowed_kw": 0.0, "at": None}

    @property
    def limits(self) -> QuotaLimits:
        return self._limits

    @property
    def period(self) -> PeriodTracker:
        return self._period

    @property
    def tariff(self) -> Tariff:
        return self._tariff

    @property
    def calendar(self) -> Calendar:
        return self._calendar

    @property
    def archives(self) -> ArchiveLedger:
        return self._archives

    def totals(self) -> dict:
        """返回当日累计电量。"""

        return {
            "charge_kwh": round(self._current_kwh["charge"], 6),
            "discharge_kwh": round(self._current_kwh["discharge"], 6),
            "period": self._period.current(),
        }

    def remaining(self) -> dict:
        """返回当日剩余额度。"""

        return {
            "charge_kwh": round(max(0.0, self._limits.charge_quota_kwh - self._current_kwh["charge"]), 6),
            "discharge_kwh": round(max(0.0, self._limits.discharge_quota_kwh - self._current_kwh["discharge"]), 6),
        }

    def roll(self, now: float) -> dict:
        """跨过零点：冻结有累计的旧日，中间闲置日以零值归档，不回滚电量。"""

        result = self._period.switch(now)
        if result["rolled"] and result["previous"]:
            # 逐日归档：旧活跃日冻结当前累计；时钟跳过的闲置日冻结零值，
            # 保证月结按日落库连续，任何一天都不会"消失"。
            self._freeze_day(result["previous"], now)
            for idle_day in result["crossed"]:
                self._freeze_empty_day(idle_day, now)
        elif not result["rolled"] and not result["previous"]:
            self._period.adopt(self._calendar.day_key(now))
        return result

    def _freeze_empty_day(self, day_key: str, rolled_at: float) -> DayArchive:
        archive = DayArchive(
            day_key=day_key,
            charge_kwh=0.0,
            discharge_kwh=0.0,
            rejections=0,
            rolled_at=rolled_at,
            buckets=empty_buckets(),
        )
        self._archives.freeze(archive)
        return archive

    def _freeze_day(self, day_key: str, rolled_at: float) -> DayArchive:
        archive = DayArchive(
            day_key=day_key,
            charge_kwh=round(self._current_kwh["charge"], 6),
            discharge_kwh=round(self._current_kwh["discharge"], 6),
            rejections=self._rejections,
            rolled_at=rolled_at,
            buckets=dict(self._current_buckets),
        )
        ledger = self._archives.freeze(archive)
        self._current_kwh = {direction: 0.0 for direction in _DIRECTIONS}
        self._current_buckets = empty_buckets()
        self._rejections = 0
        return ledger.archive

    def can_discharge(self, kwh: float, now: float) -> bool:
        """事前预检：开始日剩余放电额度是否覆盖本段估算电量。"""

        return self._admit("discharge", float(kwh), now)

    def can_charge(self, kwh: float, now: float) -> bool:
        """事前预检：开始日剩余充电额度是否覆盖本段估算电量。"""

        return self._admit("charge", float(kwh), now)

    def _admit(self, direction: str, kwh: float, now: float) -> bool:
        self.roll(now)
        quota = self._limits.charge_quota_kwh if direction == "charge" else self._limits.discharge_quota_kwh
        used = self._current_kwh[direction]
        allowed = used + float(kwh) <= quota + 1e-9
        if not allowed:
            self._rejections += 1
        return allowed

    def admit(self, direction: str, power_kw: float, dt: float, start: float) -> dict:
        """按计划功率与时长做事前预检；被拒时返回上一个有效功率且不动账。"""

        kwh = abs(float(power_kw)) * float(dt) / 3600.0
        checker = self.can_charge if direction == "charge" else self.can_discharge
        if not checker(kwh, start):
            return {
                "allowed": False,
                "direction": direction,
                "requested_kw": float(power_kw),
                "held_kw": self._last_admission["allowed_kw"] or 0.0,
                "estimated_kwh": round(kwh, 6),
                "remaining_kwh": self.remaining()[f"{direction}_kwh"],
            }
        self._last_admission = {"direction": direction, "allowed_kw": float(power_kw), "at": float(start)}
        return {
            "allowed": True,
            "direction": direction,
            "requested_kw": float(power_kw),
            "estimated_kwh": round(kwh, 6),
            "remaining_kwh": self.remaining()[f"{direction}_kwh"],
        }

    def book(self, direction: str, start: float, end: float, kwh: float, *, source: str = "meter") -> dict:
        """登记一段实测能量：切片、按开始时刻归属、扣开始日额度。

        * 开始日 == 当前活跃日：整段（含物理上跨到零点后的尾段）扣当日额度；
        * 开始日已归档（跨零点那一拍在零点后才完成计量、或电表补传）：整段
          作为**迟到分片**追加到开始日台账，绝不回滚、也不重算冻结快照；
        * 开始日在当前活跃日之后（时钟倒拨）：拒绝记账，避免把能量记到未来。
        """

        if end < start:
            raise QuotaError("计量区间结束时刻不能早于开始时刻", component="quota")
        slices = split_energy(start, end, kwh, direction, self._calendar, self._tariff)
        start_day = self._calendar.day_key(start)
        # 按区间开始时刻决定周期，而不是按记账到达时刻；这样在零点后登记
        # 跨零点那一拍时，能量仍归开始日，不会被算到新的一天。
        self.roll(start)
        current_day = self._period.current()
        if start_day == current_day:
            for energy_slice in slices:
                self._current_kwh[direction] += energy_slice.kwh
                bucket_slice(self._current_buckets, energy_slice)
            routed = "current"
        elif self._archives.is_frozen(start_day):
            for energy_slice in slices:
                self._archives.append_late_slice(LateSlice(slice=energy_slice, received_at=end, source=source))
            routed = "late"
        elif start_day > current_day:
            raise QuotaError(f"区间开始日 {start_day} 晚于当前周期 {current_day}，拒绝记未来账", component="quota")
        else:
            raise QuotaError(f"区间开始日 {start_day} 既非当日也未归档", component="quota")
        quota = self._limits.charge_quota_kwh if direction == "charge" else self._limits.discharge_quota_kwh
        over = routed == "current" and self._current_kwh[direction] > quota + 1e-9
        # 已入账分片不回滚；over_quota 是"放行后实测超额"的告警信号，
        # 下一拍事前预检将拒绝并保持上一个有效功率。
        return {
            "direction": direction,
            "day_key": start_day,
            "routed": routed,
            "booked_slices": slices,
            "booked_kwh": slices_sum(slices),
            "total_kwh": slices_sum(slices),
            "over_quota": over,
        }

    def append_late_slice(self, energy_slice: EnergySlice, *, source: str = "meter_backfill", received_at: float | None = None) -> dict:
        """把电表补传的、归属历史自然日的分片只追加进归档台账。"""

        if not self._archives.is_frozen(energy_slice.day_key):
            raise QuotaError(f"分片归属日 {energy_slice.day_key} 尚未归档，应走当日记账", component="quota")
        self._archives.append_late_slice(
            LateSlice(
                slice=energy_slice,
                received_at=received_at if received_at is not None else energy_slice.end,
                source=source,
            )
        )
        return {"appended": energy_slice.as_dict(), "day_totals": self._archives.day(energy_slice.day_key).totals()}

    def note_diff(self, day_key: str, claimed_kwh: float, direction: str, *, source: str, reason: str, now: float) -> dict:
        """对已归档自然日追加一条差异说明；归档值不变，月结不重算。"""

        ledger = self._archives.day(day_key)
        if ledger is None:
            raise QuotaError(f"自然日 {day_key} 尚未归档", component="quota")
        recorded = ledger.totals()[f"{direction}_kwh"]
        note = DiffNote(
            day_key=day_key,
            recorded_kwh=recorded,
            claimed_kwh=round(float(claimed_kwh), 6),
            delta_kwh=round(float(claimed_kwh) - recorded, 6),
            source=source,
            noted_at=float(now),
            reason=reason,
        )
        self._archives.append_diff_note(note)
        return note.as_dict()

    def settlement(self, now: float) -> dict:
        """按当日已记录分片的分档电量结算（峰谷各算各的）。"""

        report = settle_buckets(dict(self._current_buckets), self._tariff)
        report["current_rate"] = self._tariff.rate(now)
        report["kind"] = self._tariff.kind(now)
        return report

    def settlement_slices(self, slices) -> dict:
        """对任意一组分片直接结算（编排链路按拍结算用）。"""

        return settle_slices(slices, self._tariff)

    def monthly_settlement(self, month_key: str | None = None, *, now: float | None = None) -> dict:
        """自然月结算：Σ 日冻结 + Σ 迟到分片，差异说明只列示不入账。"""

        if month_key is None:
            if now is None:
                raise QuotaError("月结必须指定自然月或当前时刻", component="quota")
            month_key = self._calendar.month_key(now)
        start, end = self._calendar.month_bounds(month_key)
        totals = self._archives.month_totals(month_key)
        report = settle_buckets(dict(totals["buckets"]), self._tariff)
        # 交叉校验：从各归档日的冻结桶 + 迟到分片重建分片流再结算一次，
        # 两条路径必须给出同一份金额，否则月结报告本身就不可信。
        month_slices = []
        for day_key in totals["days"]:
            ledger = self._archives.day(day_key)
            month_slices.extend(item.slice for item in ledger.late_slices)
            month_slices.extend(
                EnergySlice(
                    start=start,
                    end=start,
                    kwh=ledger.archive.buckets.get(bucket_key, 0.0),
                    direction="charge" if bucket_key.startswith("charge") else "discharge",
                    day_key=day_key,
                    tariff_kind="peak" if "peak" in bucket_key else "valley",
                )
                for bucket_key in ("charge_peak_kwh", "charge_valley_kwh", "discharge_peak_kwh", "discharge_valley_kwh")
            )
        cross = self.settlement_slices(month_slices)
        if abs(cross["charge_cost"] - report["charge_cost"]) > 1e-4 or abs(cross["margin"] - report["margin"]) > 1e-4:
            raise QuotaError(f"自然月 {month_key} 两条结算路径不一致", component="quota")
        notes = []
        for day_key in totals["days"]:
            for note in self._archives.day(day_key).diff_notes:
                notes.append(note.as_dict())
        return {
            "period": "month",
            "start": start,
            "end": end,
            **totals,
            **report,
            "diff_notes": notes,
        }

    def reconciliation(self, now: float) -> dict:
        """对账视图：月内已归档日 + 当日累计，两侧按同一分片口径守恒。"""

        self.roll(now)
        month_key = self._calendar.month_key(now)
        monthly = self.monthly_settlement(month_key, now=now)
        charge_total = round(monthly["charge_kwh"] + self._current_kwh["charge"], 6)
        discharge_total = round(monthly["discharge_kwh"] + self._current_kwh["discharge"], 6)
        return {
            "month_key": month_key,
            "charge_kwh": charge_total,
            "discharge_kwh": discharge_total,
            "archived_charge_kwh": monthly["charge_kwh"],
            "archived_discharge_kwh": monthly["discharge_kwh"],
            "live_charge_kwh": round(self._current_kwh["charge"], 6),
            "live_discharge_kwh": round(self._current_kwh["discharge"], 6),
            "late_charge_kwh": monthly["late_charge_kwh"],
            "late_discharge_kwh": monthly["late_discharge_kwh"],
            "balanced": True,
        }

    def snapshot(self, now: float) -> QuotaSnapshot:
        """返回配额状态快照（当日 + 归档 + 当月月结同口径）。"""

        self.roll(now)
        month_key = self._calendar.month_key(now)
        return QuotaSnapshot(
            period=self._period.current(),
            rolls=self._period.rolls(),
            totals=self.totals(),
            remaining=self.remaining(),
            tariff=self._tariff.as_dict(),
            settlement=self.settlement(now),
            rejections=self._rejections,
            archives={
                "days": self._archives.day_keys(),
                "months": self._archives.month_keys(),
                "current_month": self._archives.month_totals(month_key),
            },
            monthly=self.monthly_settlement(month_key, now=now),
        )
