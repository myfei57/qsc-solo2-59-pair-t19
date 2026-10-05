"""配额状态快照。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class QuotaSnapshot:
    """配额某一时刻的状态。"""

    period: str
    rolls: int
    totals: dict
    remaining: dict
    tariff: dict
    settlement: dict
    rejections: int
    history: list
    policy: dict = field(default_factory=dict)
    rejections_by_direction: dict = field(default_factory=dict)
    crossings: list = field(default_factory=list)
    reconciliation: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "period": self.period,
            "rolls": self.rolls,
            "totals": dict(self.totals),
            "remaining": dict(self.remaining),
            "tariff": dict(self.tariff),
            "settlement": dict(self.settlement),
            "rejections": self.rejections,
            "history": list(self.history),
            "policy": dict(self.policy),
            "rejections_by_direction": dict(self.rejections_by_direction),
            "crossings": list(self.crossings),
            "reconciliation": dict(self.reconciliation),
        }
