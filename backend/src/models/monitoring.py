from datetime import datetime
from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now


class Monitor(Base):
    """一次试算运行对应的低估盯盘计划（计划随运行结果冻结，不可变）。"""

    __tablename__ = 'monitors'
    __table_args__ = (UniqueConstraint('run_id'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'))
    window_count: Mapped[int]
    family_alpha: Mapped[float]
    alpha_per_window: Mapped[float]
    snapshot: Mapped[dict] = mapped_column(JSON)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now)


class ActualReport(Base):
    """窗口实绩报告：version=1 为首版，version≥2 为晚到更正（保留全部版本）。"""

    __tablename__ = 'actual_reports'
    __table_args__ = (UniqueConstraint('monitor_id', 'window_key', 'version'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey('monitors.id'), index=True)
    window_key: Mapped[str] = mapped_column(String(160))
    version: Mapped[int]
    count: Mapped[int]
    observed_on: Mapped[str] = mapped_column(String(10), index=True)
    actor: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)


class MonitorAlarm(Base):
    """告警台账：告警与撤回的完整依据，撤回为终态（额度不还、不再武装）。"""

    __tablename__ = 'monitor_alarms'
    __table_args__ = (UniqueConstraint('monitor_id', 'window_key'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey('monitors.id'), index=True)
    window_key: Mapped[str] = mapped_column(String(160))
    window_date: Mapped[str] = mapped_column(String(10), index=True)
    road: Mapped[str] = mapped_column(String(80))
    intersection: Mapped[str] = mapped_column(String(80))
    shift: Mapped[str] = mapped_column(String(20))
    predicted: Mapped[float]
    critical: Mapped[int]
    alpha_per_window: Mapped[float]
    fired_on: Mapped[str] = mapped_column(String(10))
    fired_count: Mapped[int]
    fired_p_value: Mapped[float]
    state: Mapped[str] = mapped_column(String(20), index=True)
    withdrawn_on: Mapped[str | None] = mapped_column(String(10))
    withdraw_reason: Mapped[str | None] = mapped_column(String(500))
    basis: Mapped[str] = mapped_column(String(800))
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now)
