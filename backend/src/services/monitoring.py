from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import AlarmState, Status
from src.domain import monitoring as engine
from src.domain.monitoring import STATE_ALARM
from src.models.jobs import Run
from src.models.monitoring import ActualReport, Monitor, MonitorAlarm
from src.services.audit import record
from src.utils.time import now

HOLDOUT_TABLE = '留出窗口核对'


def holdout_windows(run: Run):
    """从运行结果的「留出窗口核对」表取出冻结的逐窗预测。"""
    if not run.result:
        raise ValueError('该运行还没有试算结果，无法建立盯盘')
    table = next((item for item in run.result['tables'] if item['title'] == HOLDOUT_TABLE), None)
    if not table or not table['rows']:
        raise ValueError('试算结果缺少留出窗口，无法建立盯盘')
    windows = []
    for row in table['rows']:
        windows.append({
            'date': row['date'], 'road': row['road'], 'intersection': row['intersection'],
            'shift': row['shift'], 'predicted': float(row['predicted']),
        })
    return windows


def create_monitor(db: Session, run: Run, actor: str):
    existing = db.scalar(select(Monitor).where(Monitor.run_id == run.id))
    if existing:
        return existing, False
    if run.status != Status.COMPLETE:
        raise ValueError('只有计算完成的运行才能建立盯盘')
    plan = engine.build_plan(holdout_windows(run))
    snapshot = engine.replay(plan, [])
    item = Monitor(run_id=run.id, window_count=snapshot['window_count'],
                   family_alpha=snapshot['family_alpha'], alpha_per_window=snapshot['alpha_per_window'],
                   snapshot=snapshot, author=actor)
    db.add(item)
    db.flush()
    record(db, actor, 'monitor.created', item.id, run_id=run.id, windows=item.window_count)
    return item, True


def _reports(db: Session, monitor_id: int):
    rows = db.scalars(select(ActualReport).where(ActualReport.monitor_id == monitor_id)
                      .order_by(ActualReport.id)).all()
    return [{'key': row.window_key, 'version': row.version, 'count': row.count,
             'observed_on': row.observed_on} for row in rows]


def _plan(db: Session, monitor: Monitor):
    run = db.get(Run, monitor.run_id)
    return engine.build_plan(holdout_windows(run))


def add_report(db: Session, monitor: Monitor, window_key: str, count: int,
               observed_on: str, actor: str):
    plan = _plan(db, monitor)
    keys = {item['key'] for item in plan}
    if window_key not in keys:
        raise ValueError('该窗口不属于本次盯盘计划，请核对日期、道路、路口与时段')
    if not isinstance(count, int) or count < 0 or count > 10000:
        raise ValueError('实绩阻断次数须为 0 至 10000 的整数')
    try:
        if date.fromisoformat(observed_on).isoformat() != observed_on:
            raise ValueError
    except ValueError:
        raise ValueError('观测/改动日期须为有效的 YYYY-MM-DD 日期') from None

    previous = db.scalar(select(ActualReport)
                         .where(ActualReport.monitor_id == monitor.id,
                                ActualReport.window_key == window_key)
                         .order_by(ActualReport.version.desc()))
    version = 1 if previous is None else previous.version + 1
    entry = ActualReport(monitor_id=monitor.id, window_key=window_key, version=version,
                         count=count, observed_on=observed_on, actor=actor)
    db.add(entry)
    db.flush()
    event = 'monitor.actual' if version == 1 else 'monitor.correction'
    record(db, actor, event, monitor.id, window=window_key, version=version,
           count=count, observed_on=observed_on)
    return _rebuild(db, monitor, plan, actor)


def _rebuild(db: Session, monitor: Monitor, plan, actor: str):
    """以全部报告事件重放引擎，并把告警台账同步到引擎快照（撤回保留依据）。"""
    snapshot = engine.replay(plan, _reports(db, monitor.id))
    existing = {row.window_key: row for row in
                db.scalars(select(MonitorAlarm).where(MonitorAlarm.monitor_id == monitor.id)).all()}
    for record_row in snapshot['alarms']:
        alarm = existing.get(record_row['key'])
        persisted_state = AlarmState.ACTIVE if record_row['state'] == STATE_ALARM else AlarmState.WITHDRAWN
        if alarm is None:
            db.add(MonitorAlarm(
                monitor_id=monitor.id, window_key=record_row['key'], window_date=record_row['date'],
                road=record_row['road'], intersection=record_row['intersection'], shift=record_row['shift'],
                predicted=record_row['predicted'], critical=record_row['critical'],
                alpha_per_window=record_row['alpha_per_window'], fired_on=record_row['fired_on'],
                fired_count=record_row['fired_count'], fired_p_value=record_row['fired_p_value'],
                state=persisted_state, withdrawn_on=record_row['withdrawn_on'],
                withdraw_reason=record_row['withdraw_reason'], basis=record_row['basis'],
                version=record_row['version']))
            record(db, actor, 'monitor.alarm', monitor.id, window=record_row['key'],
                   date=record_row['date'], count=record_row['fired_count'])
        elif alarm.state != persisted_state:
            alarm.state = AlarmState.WITHDRAWN
            alarm.withdrawn_on = record_row['withdrawn_on']
            alarm.withdraw_reason = record_row['withdraw_reason']
            alarm.version = record_row['version']
            alarm.updated_at = now()
            record(db, actor, 'monitor.withdrawn', monitor.id, window=record_row['key'],
                   withdrawn_on=record_row['withdrawn_on'], reason=record_row['withdraw_reason'])
        else:
            alarm.version = record_row['version']
    monitor.snapshot = snapshot
    monitor.updated_at = now()
    return snapshot
