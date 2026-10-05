"""计量区间与能量分片。

跨零点和跨峰谷段的能量必须先切成**左闭右开** ``[start, end)`` 的分片再记账。
这里刻意分开两件事：

* **配额归属日**整段只有一个：取分片区间**开始时刻**所在的自然日（开始时刻
  归属，见 ADR-0002）。物理上发生在零点之后的尾段，配额仍扣开始日；
* **峰谷档**按分片的**物理交付时刻**定：跨 08:00/22:00 的能量按时长比例
  切开，各按各的电价结算。电表结算按物理时间积分，这一段没有歧义。

每条能量分片只有一个"归属日"和一个"峰谷档"，配额扣减、超额拒绝、
峰谷月结读的都是同一组分片。分片切分是纯函数、确定性可重放；归档后
重新执行同一切分得到同一份归属结果，重算不会改写历史。
"""

from __future__ import annotations

from dataclasses import dataclass

ROUND_KWH = 6


@dataclass(frozen=True)
class EnergySlice:
    """一段不再跨峰谷边界的能量；归属日固定为区间开始日。"""

    start: float
    end: float
    kwh: float
    direction: str
    day_key: str
    tariff_kind: str

    @property
    def duration_s(self) -> float:
        return self.end - self.start

    def as_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "kwh": self.kwh,
            "duration_s": round(self.duration_s, 6),
            "direction": self.direction,
            "day_key": self.day_key,
            "tariff_kind": self.tariff_kind,
        }


def boundary_points(start: float, end: float, calendar, tariff) -> list[float]:
    """收集区间内全部峰谷切换点（本地 08:00 / 22:00）。

    零点不作为切分点：归属按开始时刻整段归一开始日，零点两侧不拆账；
    峰谷档按物理时间计价，只在真正的峰谷切换点切开。
    """

    points = [float(start)]
    day_zero = calendar.day_start(start)
    while day_zero < end:
        for hour in tariff.boundary_hours():
            point = day_zero + hour * 3600.0
            if start < point < end:
                points.append(point)
        day_zero += 86_400.0
    points.append(float(end))
    return sorted(set(points))


def split_energy(start: float, end: float, kwh: float, direction: str, calendar, tariff) -> list[EnergySlice]:
    """把一段能量按峰谷边界切成归属确定的分片。

    全部分片的 ``day_key`` 都是区间开始日（开始时刻归属）；各分片的
    ``tariff_kind`` 按其物理交付时刻所在峰谷段定。分片电量按时长比例摊分，
    比例摊分四舍五入后把尾差放进最后一片，保证各片之和与计量总量严格相等。

    若计量系统直接给出了峰谷边界两侧的电表读数，应直接按读数逐段构造
    :class:`EnergySlice`，摊分只是没有分段读数时的兜底。
    """

    if end < start:
        raise ValueError("计量区间结束时刻不能早于开始时刻")
    total = max(0.0, float(kwh))
    points = boundary_points(start, end, calendar, tariff)
    total_duration = end - start
    raw = [total * (points[i + 1] - points[i]) / total_duration for i in range(len(points) - 1)]
    rounded = [round(value, ROUND_KWH) for value in raw[:-1]]
    remainder = round(total - sum(rounded), ROUND_KWH)
    values = rounded + [remainder]

    owner_day = calendar.day_key(start)
    slices: list[EnergySlice] = []
    for seg_start, seg_end, seg_kwh in zip(points[:-1], points[1:], values):
        if seg_end <= seg_start:
            continue
        slices.append(
            EnergySlice(
                start=seg_start,
                end=seg_end,
                kwh=round(seg_kwh, ROUND_KWH),
                direction=direction,
                day_key=owner_day,
                tariff_kind=tariff.kind(seg_start),
            )
        )
    return slices


def slices_sum(slices: list[EnergySlice]) -> float:
    """分片电量合计（对账用）。"""

    return round(sum(item.kwh for item in slices), ROUND_KWH)
