"""Rotation model dataclasses."""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


@dataclass
class ETFRotationSnapshot:
    as_of_date: date
    ticker: str
    group_name: str
    close_price: float = 0.0
    rs_1w: Optional[float] = None
    rs_1m: Optional[float] = None
    rs_3m: Optional[float] = None
    rs_ratio: Optional[float] = None
    rs_momentum: Optional[float] = None
    rs_ratio_z: Optional[float] = None
    rs_momentum_z: Optional[float] = None
    rrg_quadrant: str = ""
    flow_score: Optional[float] = None
    rotation_score: Optional[float] = None
    source: str = "UW"
    retrieved_at: datetime = field(default_factory=datetime.now)
