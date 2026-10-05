"""配额状态快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuotaSnapshot:
    """配额某一时刻的状态（当日累计、归档台账与月结视图同口径）。"""

    period: str
    rolls: int
    totals: dict
    remaining: dict
    tariff: dict
    settlement: dict
    rejections: int
    archives: dict
    monthly: dict

    def as_dict(self) -> dict:
        return {
            "period": self.period,
            "rolls": self.rolls,
            "totals": dict(self.totals),
            "remaining": dict(self.remaining),
            "tariff": dict(self.tariff),
            "settlement": dict(self.settlement),
            "rejections": self.rejections,
            "archives": dict(self.archives),
            "monthly": dict(self.monthly),
        }
