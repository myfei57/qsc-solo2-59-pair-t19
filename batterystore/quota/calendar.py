"""自然日历元：日、月与本地零点。

配额、超额拒绝与峰谷结算共用同一份日历口径：

* **日额度**：本地时区的自然日 ``[00:00, 次日 00:00)``，固定 UTC 偏移
  （电站不处理夏令时跳变）；
* **月结算**：本地时区的自然月，是自然日的直接归并，不另立一套起点；
* 记账路径里**不允许出现滚动 24h 窗口**——滚动窗口与本地零点、峰谷时段
  都不对齐，月结时无法切分出可复现的账单。若监控想展示"最近 24 小时"，
  只能从已归档的分片台账派生，且明确标注为非记账口径。
"""

from __future__ import annotations

DAY_SECONDS = 86_400.0
MIN_TZ_OFFSET_HOURS = -12.0
MAX_TZ_OFFSET_HOURS = 14.0


class Calendar:
    """固定 UTC 偏移下的自然日/自然月历元。"""

    def __init__(self, tz_offset_hours: float = 8.0) -> None:
        offset = float(tz_offset_hours)
        if not (MIN_TZ_OFFSET_HOURS <= offset <= MAX_TZ_OFFSET_HOURS):
            raise ValueError("时区偏移必须在 [-12, 14] 小时内")
        self._offset = offset

    @property
    def tz_offset_hours(self) -> float:
        return self._offset

    def day_start(self, t: float) -> float:
        """返回时刻 ``t`` 所属自然日的本地零点（UTC 秒）。"""

        local = float(t) + self._offset * 3600.0
        first = local - local % DAY_SECONDS
        return first - self._offset * 3600.0

    def day_bounds(self, day_key: str) -> tuple[float, float]:
        """返回自然日 ``[零点, 次日零点)`` 的 UTC 秒边界。"""

        start = _iso_date_to_days(day_key) * DAY_SECONDS - self._offset * 3600.0
        return start, start + DAY_SECONDS

    def day_key(self, t: float) -> str:
        """返回时刻所属自然日的键 ``YYYY-MM-DD``（本地日期）。"""

        local_seconds = float(t) + self._offset * 3600.0
        first = local_seconds - local_seconds % DAY_SECONDS
        days = int(first // DAY_SECONDS)
        # 1970-01-01 为第 0 天，用 civil 日历算法换算成公历日期。
        return _days_to_iso_date(days)

    def month_key(self, t: float) -> str:
        """返回时刻所属自然月的键 ``YYYY-MM``。"""

        return self.day_key(t)[:7]

    def month_bounds(self, month_key: str) -> tuple[float, float]:
        """返回自然月 ``[月初零点, 次月零点)`` 的 UTC 秒边界。"""

        year, month = _parse_month_key(month_key)
        start, _ = self.day_bounds(f"{year:04d}-{month:02d}-01")
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        next_start, _ = self.day_bounds(f"{next_year:04d}-{next_month:02d}-01")
        return start, next_start

    def next_day_key(self, day_key: str) -> str:
        """返回给定自然日的次日键。"""

        return _days_to_iso_date(_iso_date_to_days(day_key) + 1)


def _days_to_iso_date(days: int) -> str:
    """把 1970-01-01 起的天数换成 ``YYYY-MM-DD``（Howard Hinnant 算法）。"""

    shifted = days + 719_468
    era = (shifted if shifted >= 0 else shifted - 146_096) // 146_097
    doe = shifted - era * 146_097
    yoe = (doe - doe // 1460 + doe // 36_524 - doe // 146_096) // 365
    year = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + 3 if mp < 10 else mp - 9
    if month <= 2:
        year += 1
    return f"{year:04d}-{month:02d}-{day:02d}"


def _days_from_civil(year: int, month: int, day: int) -> int:
    """公历日期到 1970-01-01 起天数（与 :func:`_days_to_iso_date` 互逆）。"""

    y = year - (1 if month <= 2 else 0)
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146_097 + doe - 719_468


def _iso_date_to_days(day_key: str) -> int:
    year, month, day = _parse_day_key(day_key)
    return _days_from_civil(year, month, day)


def _parse_day_key(day_key: str) -> tuple[int, int, int]:
    try:
        year_text, month_text, day_text = day_key.split("-")
        year, month, day = int(year_text), int(month_text), int(day_text)
    except (ValueError, AttributeError) as error:
        raise ValueError(f"非法自然日键: {day_key!r}") from error
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError(f"非法自然日键: {day_key!r}")
    return year, month, day


def _parse_month_key(month_key: str) -> tuple[int, int]:
    try:
        year_text, month_text = month_key.split("-")
        year, month = int(year_text), int(month_text)
    except (ValueError, AttributeError) as error:
        raise ValueError(f"非法自然月键: {month_key!r}") from error
    if not 1 <= month <= 12:
        raise ValueError(f"非法自然月键: {month_key!r}")
    return year, month
