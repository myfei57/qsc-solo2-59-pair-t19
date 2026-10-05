"""周期归档：只追加、不可改写的日台账。

自然日滚动时，把上一周期（自然日）的累计量冻结成一份 :class:`DayArchive`。
冻结后的快照是**不可变**的：

* 已经记账的电量不会因为新的一天开始而回滚；
* 跨零点那段能量的归属在分片生成时已经确定（归开始日），重算同一段
  计量数据只会得到同一份结果，不需要也不允许回头改快照；
* 如果冻结之后才有属于该日的分片到达（例如电表补传了跨零点那一拍的
  读数），只能往该日台账**追加**一条 :class:`LateSlice`；
* 发现冻结值与外部对账数据有出入，只能**追加**一条 :class:`DiffNote`
  说明差异，归档原值保持不变。

月结 = Σ 冻结快照 + Σ 追加分片，逐日落库，全程可复算、可审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .accounting import bucket_slice, empty_buckets
from .interval import EnergySlice


@dataclass(frozen=True)
class DayArchive:
    """一个自然日滚动时冻结的累计量（含分档电量桶，供月结复算）。"""

    day_key: str
    charge_kwh: float
    discharge_kwh: float
    rejections: int
    rolled_at: float
    buckets: dict

    def as_dict(self) -> dict:
        return {
            "day_key": self.day_key,
            "charge_kwh": self.charge_kwh,
            "discharge_kwh": self.discharge_kwh,
            "rejections": self.rejections,
            "rolled_at": self.rolled_at,
            "buckets": dict(self.buckets),
            "frozen": True,
        }


@dataclass(frozen=True)
class LateSlice:
    """归档冻结后才到达、但归属该日的分片。"""

    slice: EnergySlice
    received_at: float
    source: str

    def as_dict(self) -> dict:
        return {"kind": "late_slice", "received_at": self.received_at, "source": self.source, **self.slice.as_dict()}


@dataclass(frozen=True)
class DiffNote:
    """对归档原值差异的说明；只追加，不改值。"""

    day_key: str
    recorded_kwh: float
    claimed_kwh: float
    delta_kwh: float
    source: str
    noted_at: float
    reason: str

    def as_dict(self) -> dict:
        return {
            "kind": "diff_note",
            "day_key": self.day_key,
            "recorded_kwh": self.recorded_kwh,
            "claimed_kwh": self.claimed_kwh,
            "delta_kwh": self.delta_kwh,
            "source": self.source,
            "noted_at": self.noted_at,
            "reason": self.reason,
        }


@dataclass
class DayLedger:
    """一个自然日的完整台账：冻结快照 + 只追加的附加记录。"""

    archive: DayArchive
    late_slices: list[LateSlice] = field(default_factory=list)
    diff_notes: list[DiffNote] = field(default_factory=list)

    def late_kwh(self, direction: str) -> float:
        return round(sum(item.slice.kwh for item in self.late_slices if item.slice.direction == direction), 6)

    def late_buckets(self) -> dict:
        buckets = empty_buckets()
        for item in self.late_slices:
            bucket_slice(buckets, item.slice)
        return buckets

    def effective_buckets(self) -> dict:
        """有效分档桶 = 冻结桶 + 追加分片桶（冻结桶本身不动）。"""

        merged = dict(self.archive.buckets)
        for key, value in self.late_buckets().items():
            merged[key] = round(merged.get(key, 0.0) + value, 6)
        return merged

    def totals(self) -> dict:
        """有效日累计 = 冻结值 + 追加分片（快照本身不动）。"""

        buckets = self.effective_buckets()
        return {
            "day_key": self.archive.day_key,
            "charge_kwh": round(buckets["charge_peak_kwh"] + buckets["charge_valley_kwh"], 6),
            "discharge_kwh": round(buckets["discharge_peak_kwh"] + buckets["discharge_valley_kwh"], 6),
            "frozen_charge_kwh": self.archive.charge_kwh,
            "frozen_discharge_kwh": self.archive.discharge_kwh,
            "late_charge_kwh": self.late_kwh("charge"),
            "late_discharge_kwh": self.late_kwh("discharge"),
            "rejections": self.archive.rejections,
            "diff_notes": len(self.diff_notes),
        }

    def as_dict(self) -> dict:
        return {
            "archive": self.archive.as_dict(),
            "late_slices": [item.as_dict() for item in self.late_slices],
            "diff_notes": [item.as_dict() for item in self.diff_notes],
            "totals": self.totals(),
        }


class ArchiveLedger:
    """全部历史自然日的只追加台账。"""

    def __init__(self) -> None:
        self._days: dict[str, DayLedger] = {}

    def freeze(self, archive: DayArchive) -> DayLedger:
        """冻结一个自然日；已冻结的日子不能再次冻结（防重写）。"""

        if archive.day_key in self._days:
            raise ValueError(f"自然日 {archive.day_key} 已归档，不能重复冻结")
        ledger = DayLedger(archive=archive)
        self._days[archive.day_key] = ledger
        return ledger

    def is_frozen(self, day_key: str) -> bool:
        return day_key in self._days

    def day(self, day_key: str) -> DayLedger | None:
        return self._days.get(day_key)

    def append_late_slice(self, record: LateSlice) -> None:
        """把迟到分片追加到其归属日；归属日尚未归档时先补一份零值冻结。"""

        day_key = record.slice.day_key
        ledger = self._days.get(day_key)
        if ledger is None:
            ledger = self.freeze(
                DayArchive(
                    day_key=day_key,
                    charge_kwh=0.0,
                    discharge_kwh=0.0,
                    rejections=0,
                    rolled_at=record.received_at,
                    buckets=empty_buckets(),
                )
            )
        ledger.late_slices.append(record)

    def append_diff_note(self, note: DiffNote) -> None:
        """追加一条差异说明；归档原值不随之改变。"""

        ledger = self._days.get(note.day_key)
        if ledger is None:
            raise ValueError(f"自然日 {note.day_key} 尚未归档，无需差异说明")
        ledger.diff_notes.append(note)

    def day_keys(self) -> list[str]:
        return sorted(self._days)

    def month_keys(self) -> list[str]:
        return sorted({key[:7] for key in self._days})

    def month_day_keys(self, month_key: str) -> list[str]:
        return sorted(key for key in self._days if key.startswith(f"{month_key}-"))

    def month_totals(self, month_key: str) -> dict:
        """自然月合计：冻结值与追加分片分列，分档桶供月结复算。"""

        buckets = empty_buckets()
        late_buckets = empty_buckets()
        rejections = 0
        days = self.month_day_keys(month_key)
        for key in days:
            ledger = self._days[key]
            totals = ledger.totals()
            rejections += totals["rejections"]
            for bucket_key, value in ledger.effective_buckets().items():
                buckets[bucket_key] = round(buckets[bucket_key] + value, 6)
            for bucket_key, value in ledger.late_buckets().items():
                late_buckets[bucket_key] = round(late_buckets[bucket_key] + value, 6)
        return {
            "month_key": month_key,
            "days": days,
            "charge_kwh": round(buckets["charge_peak_kwh"] + buckets["charge_valley_kwh"], 6),
            "discharge_kwh": round(buckets["discharge_peak_kwh"] + buckets["discharge_valley_kwh"], 6),
            "late_charge_kwh": round(late_buckets["charge_peak_kwh"] + late_buckets["charge_valley_kwh"], 6),
            "late_discharge_kwh": round(late_buckets["discharge_peak_kwh"] + late_buckets["discharge_valley_kwh"], 6),
            "rejections": rejections,
            "buckets": buckets,
        }

    def as_dict(self) -> dict:
        return {"days": [self._days[key].as_dict() for key in self.day_keys()]}
