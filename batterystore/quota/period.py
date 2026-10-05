"""记账周期。

周期按**本地自然日零点**对齐，一天被周期长度整除成若干等长槽位（例如
24 小时一个槽、12 小时两个槽），不再使用与日历日漂移的滚动窗口。配额
封顶、拒绝判据与峰谷结算都读这一份周期键。

周期归属口径：一段能量的记账周期由它的**开始时刻**决定。跨周期边界
（跨零点）的能量在开始周期全额记账，同时额外登记一条跨周期明细，标出
落在边界之后的尾部能量，供月结对账。归档后的周期累计量是不可变的：
后续迟到的修正不能改写归档值，只能以差异说明（note）追加。

时间只能向前推进：控制循环里观察到旧周期不会回滚状态机。
"""

from __future__ import annotations


class PeriodTracker:
    """本地自然日对齐的周期键跟踪器。"""

    def __init__(self, period_hours: float, timezone_offset_hours: float = 8.0) -> None:
        if period_hours <= 0:
            raise ValueError("周期长度必须为正")
        slots = 24.0 / float(period_hours)
        if abs(slots - round(slots)) > 1e-9:
            raise ValueError("周期长度必须整除 24 小时并对齐本地零点")
        self._period_seconds = float(period_hours) * 3600.0
        self._slots_per_day = int(round(slots))
        self._tz_offset_seconds = float(timezone_offset_hours) * 3600.0
        self._key = ""
        self._rolls = 0
        self._archives: dict[str, dict] = {}

    @property
    def period_hours(self) -> float:
        return self._period_seconds / 3600.0

    @property
    def timezone_offset_hours(self) -> float:
        return self._tz_offset_seconds / 3600.0

    def local_seconds(self, now: float) -> float:
        """把 Unix 时间戳换算成本地时钟的当日秒数（含整日偏移）。"""

        return float(now) + self._tz_offset_seconds

    def key_for(self, now: float) -> str:
        """按时间戳算出所属周期键（本地零点对齐）。"""

        local = self.local_seconds(now)
        day, within_day = divmod(local, 86400.0)
        slot = int(within_day // self._period_seconds)
        return f"{int(day):05d}d{slot:02d}"

    def slot_bounds(self, now: float) -> tuple[float, float]:
        """返回当前周期槽的 Unix 起止时间 ``[start, end)``。"""

        local = self.local_seconds(now)
        day, within_day = divmod(local, 86400.0)
        slot = int(within_day // self._period_seconds)
        local_start = day * 86400.0 + slot * self._period_seconds
        return local_start - self._tz_offset_seconds, local_start + self._period_seconds - self._tz_offset_seconds

    def current(self) -> str:
        """返回当前周期键。"""

        return self._key

    def archive(self, key: str) -> dict | None:
        """返回某周期的不可变归档副本（含差异说明），未归档返回 None。"""

        record = self._archives.get(key)
        return dict(record) if record is not None else None

    def observe(self, now: float, totals: dict) -> dict:
        """按当前时间推进周期，时间回退时不动状态。

        周期变化时把上一周期的累计量原样封进归档并清零累计；跨越多个
        周期时，中间空周期按零电量归档，保证每个自然日槽位都可对账。
        """

        candidate = self.key_for(now)
        if not self._key:
            self._key = candidate
            return {"rolled": False, "key": candidate, "previous": None, "sealed": []}
        if candidate == self._key:
            return {"rolled": False, "key": self._key, "previous": None, "sealed": []}
        # 时间回退（例如时钟跳变）不回滚状态机，只进不退。
        if self._slot_index(candidate) <= self._slot_index(self._key):
            return {"rolled": False, "key": self._key, "previous": None, "sealed": []}

        sealed: list[dict] = []
        stepping = self._key
        while stepping != candidate:
            record = self._archives.get(stepping)
            if record is None:
                payload = {"key": stepping}
                if stepping == self._key:
                    payload.update({name: totals.get(name, 0.0) for name in ("charge_kwh", "discharge_kwh")})
                else:
                    payload.update({"charge_kwh": 0.0, "discharge_kwh": 0.0})
                payload["notes"] = []
                self._archives[stepping] = payload
                record = payload
            sealed.append(dict(record))
            stepping = self._next_key(stepping)
            self._rolls += 1
        self._key = candidate
        return {"rolled": True, "key": candidate, "previous": sealed[0]["key"] if sealed else None, "sealed": sealed}

    def rolls(self) -> int:
        """返回滚动过的槽位数。"""

        return self._rolls

    def history(self) -> list[dict]:
        """按槽位顺序返回全部已归档周期（不可变副本）。"""

        return [dict(self._archives[key]) for key in sorted(self._archives)]

    def add_note(self, key: str, note: dict) -> bool:
        """向已归档周期追加一条差异说明。

        归档的电量数字永不被修改；周期还没归档时返回 False，由调用方
        直接记当期账，不允许先归档后改数。
        """

        record = self._archives.get(key)
        if record is None:
            return False
        record["notes"].append(dict(note))
        return True

    def _slot_index(self, key: str) -> int:
        day_text, slot_text = key.split("d")
        return int(day_text) * self._slots_per_day + int(slot_text)

    def _next_key(self, key: str) -> str:
        day_text, slot_text = key.split("d")
        day, slot = int(day_text), int(slot_text)
        slot += 1
        if slot >= self._slots_per_day:
            slot = 0
            day += 1
        return f"{day:05d}d{slot:02d}"
