"""记账周期：本地自然日，固定不滚动。

周期键就是自然日键 ``YYYY-MM-DD``（本地时区，见 :mod:`batterystore.quota.calendar`）。
周期键变化即跨零点：把上一自然日的累计量冻结归档（见
:mod:`batterystore.quota.archive`），再清零本周期累计。配置里的
``period_hours`` 仅为兼容旧配置而保留，且只允许取 24——记账口径不允许
再退回滚动周期。
"""

from __future__ import annotations

from .calendar import Calendar

CANONICAL_PERIOD_HOURS = 24.0


class PeriodTracker:
    """自然日周期键跟踪器。"""

    def __init__(self, calendar: Calendar, period_hours: float = CANONICAL_PERIOD_HOURS) -> None:
        if abs(float(period_hours) - CANONICAL_PERIOD_HOURS) > 1e-9:
            raise ValueError("记账周期固定为本地自然日（period_hours=24），不支持滚动周期")
        self._calendar = calendar
        self._period_hours = CANONICAL_PERIOD_HOURS
        self._key = ""
        self._rolls = 0

    @property
    def period_hours(self) -> float:
        return self._period_hours

    @property
    def calendar(self) -> Calendar:
        return self._calendar

    def key_for(self, now: float) -> str:
        """按时间戳算所属自然日键。"""

        return self._calendar.day_key(now)

    def current(self) -> str:
        """返回当前自然日键。"""

        return self._key

    def adopt(self, key: str) -> None:
        """以一个已知自然日键初始化当前周期（不产生滚动次数）。"""

        if not self._key:
            self._key = key

    def switch(self, now: float) -> dict:
        """跨过一个或多个自然日，返回本次跨越的全部旧日键（含闲置空日）。"""

        candidate = self.key_for(now)
        if candidate == self._key:
            return {"rolled": False, "key": self._key, "previous": None, "crossed": []}
        previous = self._key or None
        crossed: list[str] = []
        if previous is not None:
            cursor = previous
            while cursor != candidate:
                cursor = self._calendar.next_day_key(cursor)
                if cursor != candidate:
                    crossed.append(cursor)
            self._rolls += 1 + len(crossed)
        self._key = candidate
        return {"rolled": previous is not None, "key": self._key, "previous": previous, "crossed": crossed}

    def rolls(self) -> int:
        """返回跨零点滚动次数。"""

        return self._rolls
