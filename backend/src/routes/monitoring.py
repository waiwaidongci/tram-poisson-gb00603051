from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.models.identity import User
from src.models.jobs import Run
from src.models.monitoring import ActualReport, Monitor, MonitorAlarm
from src.schemas.requests import ActualRequest
from src.services import monitoring as service
from src.services.audit import record
from src.utils import views

router = APIRouter(prefix='/monitors')


def require_monitor(db, monitor_id):
    item = db.get(Monitor, monitor_id)
    if not item:
        raise HTTPException(404, '找不到该盯盘计划')
    return item


@router.get('')
def index(db: Session = Depends(connection), actor: User = Depends(current)):
    items = db.scalars(select(Monitor).order_by(Monitor.id.desc()).limit(100)).all()
    return [views.monitor(item) for item in items]


@router.post('/runs/{run_id}', status_code=201)
def create(run_id: int, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, '找不到该运行记录')
    item, created = service.create_monitor(db, run, actor.username)
    db.commit()
    return {**views.monitor(item), 'created': created}


@router.get('/{monitor_id}')
def detail(monitor_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    item = require_monitor(db, monitor_id)
    reports = db.scalars(select(ActualReport).where(ActualReport.monitor_id == monitor_id)
                         .order_by(ActualReport.id)).all()
    alarms = db.scalars(select(MonitorAlarm).where(MonitorAlarm.monitor_id == monitor_id)
                        .order_by(MonitorAlarm.window_date, MonitorAlarm.window_key)).all()
    run = db.get(Run, item.run_id)
    return {**views.monitor(item),
            'run_name': run.name,
            'reports': [views.report(row) for row in reports],
            'alarms': [views.alarm(row) for row in alarms]}


@router.post('/{monitor_id}/reports', status_code=201)
def add_report(monitor_id: int, body: ActualRequest, db: Session = Depends(connection),
               actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    item = require_monitor(db, monitor_id)
    snapshot = service.add_report(db, item, body.window_key, body.count,
                                  body.observed_on.isoformat(), actor.username)
    db.commit()
    db.refresh(item)
    return {**views.monitor(item), 'snapshot': snapshot}


@router.get('/{monitor_id}/reports')
def reports(monitor_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    require_monitor(db, monitor_id)
    rows = db.scalars(select(ActualReport).where(ActualReport.monitor_id == monitor_id)
                      .order_by(ActualReport.id.desc()).limit(500)).all()
    return [views.report(row) for row in rows]
