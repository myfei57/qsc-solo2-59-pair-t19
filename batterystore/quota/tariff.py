"""峰谷电价。

按本地自然日的小时数区分峰谷段：白天按峰价、夜间按谷价。结算与配额
展示用同一份电价表，峰段边界同时供 :mod:`batterystore.quota.interval`
切分能量分片——任何电量都不会整段按"记账那一刻"的电价计价，跨峰谷段
的能量会被切开后各按各的档位结算。
"""

from __future__ import annotations

from ..config import QuotaLimits

PEAK_START_HOUR = 8
PEAK_END_HOUR = 22


class Tariff:
    """峰谷电价表。"""

    def __init__(self, limits: QuotaLimits, tz_offset_hours: float = 8.0) -> None:
        self._limits = limits
        self._offset = float(tz_offset_hours)

    @property
    def limits(self) -> QuotaLimits:
        return self._limits

    def boundary_hours(self) -> tuple[int, int]:
        """返回一天内的峰谷切换小时（本地时）。"""

        return PEAK_START_HOUR, PEAK_END_HOUR

    def kind(self, now: float) -> str:
        """返回时刻所属时段名。"""

        hour = int(((float(now) + self._offset * 3600.0) % 86400.0) // 3600)
        return "peak" if PEAK_START_HOUR <= hour < PEAK_END_HOUR else "valley"

    def rate(self, now: float) -> float:
        """返回时刻所属时段电价。"""

        return self.rate_for_kind(self.kind(now))

    def rate_for_kind(self, kind: str) -> float:
        """按时段名取电价。"""

        if kind == "peak":
            return self._limits.tariff_peak
        return self._limits.tariff_valley

    def as_dict(self) -> dict:
        return {
            "peak": self._limits.tariff_peak,
            "valley": self._limits.tariff_valley,
            "peak_window": [PEAK_START_HOUR, PEAK_END_HOUR],
            "tz_offset_hours": self._offset,
        }
