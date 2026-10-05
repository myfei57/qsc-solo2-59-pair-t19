"""峰谷电价。

按**本地时间**当天小时数区分峰谷段：白天按峰价、夜间按谷价。结算与
配额展示用同一份电价表，时区偏移与记账周期一致，避免"电价按 UTC、
配额按本地日"两套零点对不上账。
"""

from __future__ import annotations

from ..config import QuotaLimits

PEAK_START_HOUR = 8
PEAK_END_HOUR = 22


class Tariff:
    """峰谷电价表。"""

    def __init__(self, limits: QuotaLimits) -> None:
        self._limits = limits

    @property
    def limits(self) -> QuotaLimits:
        return self._limits

    def local_hour(self, now: float) -> float:
        """返回本地当天小时数（浮点，跨零点判断时不丢小数）。"""

        local = float(now) + self._limits.timezone_offset_hours * 3600.0
        return (local % 86400.0) / 3600.0

    def kind(self, now: float) -> str:
        """返回当前时段名。"""

        hour = int(self.local_hour(now))
        return "peak" if PEAK_START_HOUR <= hour < PEAK_END_HOUR else "valley"

    def rate(self, now: float) -> float:
        """返回当前时段电价。"""

        if self.kind(now) == "peak":
            return self._limits.tariff_peak
        return self._limits.tariff_valley

    def as_dict(self) -> dict:
        return {
            "peak": self._limits.tariff_peak,
            "valley": self._limits.tariff_valley,
            "peak_window": [PEAK_START_HOUR, PEAK_END_HOUR],
            "timezone_offset_hours": self._limits.timezone_offset_hours,
        }
