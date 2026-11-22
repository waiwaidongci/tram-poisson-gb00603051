"""盯盘接口端到端测试：内存 SQLite + FastAPI TestClient。

运行：cd backend && PYTHONPATH=. python -m tests.test_monitoring_api
"""

import os
import unittest

os.environ.setdefault('DATABASE_URL', 'sqlite+pysqlite://')
os.environ.setdefault('JWT_SECRET', 'test-secret-monitoring-2611')
os.environ.setdefault('SEED_DEMO', 'false')

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.config.database as db_config  # noqa: E402
from src.config.database import Base, connection  # noqa: E402
from src.constants.enums import Role, Status  # noqa: E402
from src.domain.demo import demo_rows  # noqa: E402
from src.main import app  # noqa: E402
from src.models.identity import Audit, User  # noqa: E402
from src.models.jobs import Run  # noqa: E402
from src.models.monitoring import ActualReport, Monitor, MonitorAlarm  # noqa: E402
from src.services.monitoring import holdout_windows  # noqa: E402
from src.utils.security import hash_password, token  # noqa: E402

engine = create_engine('sqlite+pysqlite://', connect_args={'check_same_thread': False},
                       poolclass=StaticPool)
TestingSession = sessionmaker(engine)
Base.metadata.create_all(engine)
db_config.engine = engine
db_config.Session = TestingSession


def override_connection():
    with TestingSession() as db:
        yield db


app.dependency_overrides[connection] = override_connection


def make_run():
    """用真实 analyze 跑一遍演示数据，得到含留出窗口的已完成运行。"""
    from src.domain.analysis import analyze
    rows = demo_rows()
    result = analyze(rows, {'holdout_days': 3, 'severity_rate': 9})
    with TestingSession() as db:
        run = Run(name='样本阻断模型试算', dataset_id=1, status=Status.COMPLETE,
                  params={'holdout_days': 3, 'severity_rate': 9}, result=result, author='analyst')
        db.add(run)
        db.commit()
        return run.id, result


class MonitoringApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        with TestingSession() as db:
            if not db.get(User, 'analyst'):
                db.add(User(username='analyst', role=Role.ANALYST,
                            password_hash=hash_password('analyst-demo-2611')))
            if not db.get(User, 'viewer'):
                db.add(User(username='viewer', role=Role.VIEWER,
                            password_hash=hash_password('viewer-demo-2611')))
            db.commit()
        cls.analyst_headers = {'Authorization': f"Bearer {token('analyst', Role.ANALYST)}"}
        cls.viewer_headers = {'Authorization': f"Bearer {token('viewer', Role.VIEWER)}"}
        cls.run_id, cls.result = make_run()
        cls.holdout = holdout_windows(Run(result=cls.result))

    def setUp(self):
        # 每个用例一个全新盯盘（内存库跨用例共享，先清盯盘相关表）
        with TestingSession() as db:
            for model in (MonitorAlarm, ActualReport, Monitor):
                db.query(model).delete()
            db.commit()
        response = self.client.post(f'/monitors/runs/{self.run_id}', headers=self.analyst_headers)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.monitor_id = body['id']
        self.assertTrue(body['created'])
        self.assertEqual(body['window_count'], len(self.holdout))
        self.assertAlmostEqual(body['family_alpha'], 0.05)

    def test_duplicate_monitor_is_idempotent(self):
        response = self.client.post(f'/monitors/runs/{self.run_id}', headers=self.analyst_headers)
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()['created'])
        self.assertEqual(response.json()['id'], self.monitor_id)

    def test_viewer_cannot_create_or_report(self):
        response = self.client.post(f'/monitors/runs/999', headers=self.viewer_headers)
        self.assertEqual(response.status_code, 403)
        response = self.client.post(f'/monitors/{self.monitor_id}/reports',
                                    headers=self.viewer_headers,
                                    json={'window_key': 'x', 'count': 1, 'observed_on': '2026-11-21'})
        self.assertEqual(response.status_code, 403)

    def test_unknown_window_rejected(self):
        response = self.client.post(f'/monitors/{self.monitor_id}/reports',
                                    headers=self.analyst_headers,
                                    json={'window_key': '2026-11-18|不存在|x|平峰',
                                          'count': 1, 'observed_on': '2026-11-21'})
        self.assertEqual(response.status_code, 422)

    def test_full_flow_alarm_block_withdraw(self):
        windows = self.holdout
        days = sorted({row['date'] for row in windows})
        first_day, second_day = days[0], days[1]
        day_one = [row for row in windows if row['date'] == first_day]

        # 1) 首日缺一个窗 -> 阻塞，后续日不处理
        partial = day_one[0]
        response = self.client.post(f'/monitors/{self.monitor_id}/reports', headers=self.analyst_headers,
                                    json={'window_key': key(partial), 'count': 0,
                                          'observed_on': '2026-11-21'})
        self.assertEqual(response.status_code, 201)
        snap = response.json()['snapshot']
        self.assertEqual(snap['blocked_at'], first_day)
        self.assertEqual(snap['first_crossing_day'], None)

        # 2) 补齐首日，其中一个窗口构造越线（取阈值最大的，报一个远超阈值的数）
        target = max(day_one, key=lambda row: row['predicted'])
        big = int(target['predicted']) + 60
        for row in day_one:
            count = big if row is target else 0
            response = self.client.post(f'/monitors/{self.monitor_id}/reports',
                                        headers=self.analyst_headers,
                                        json={'window_key': key(row), 'count': count,
                                              'observed_on': '2026-11-21'})
            self.assertEqual(response.status_code, 201)
        snap = response.json()['snapshot']
        self.assertEqual(snap['blocked_at'], second_day, '首日等齐后，阻塞推进到未上报的次日')
        self.assertEqual(snap['first_crossing_day'], first_day)
        self.assertEqual(snap['active_first_crossing_day'], first_day)
        self.assertEqual(snap['budget_used'], 1)
        self.assertEqual(snap['budget_remaining'], snap['window_count'] - 1)
        self.assertEqual(snap['active_alarms'][0]['fired_count'], big)

        # 3) 详情接口能查到首个越线日、窗口与额度，且告警已落库
        detail = self.client.get(f'/monitors/{self.monitor_id}', headers=self.analyst_headers).json()
        self.assertEqual(detail['snapshot']['first_crossing_day'], first_day)
        self.assertEqual(len(detail['alarms']), 1)
        self.assertEqual(detail['alarms'][0]['state'], 'active')
        self.assertIn('Bonferroni', detail['alarms'][0]['basis'])

        # 4) 晚到更正把越线窗口改回正常值 -> 撤回，额度不还
        response = self.client.post(f'/monitors/{self.monitor_id}/reports',
                                    headers=self.analyst_headers,
                                    json={'window_key': key(target), 'count': 0,
                                          'observed_on': '2026-11-22'})
        snap = response.json()['snapshot']
        self.assertEqual(snap['budget_used'], 1, '撤回也算发过，额度不还')
        self.assertEqual(len(snap['active_alarms']), 0)
        withdrawn = next(row for row in snap['windows'] if row['key'] == key(target))
        self.assertEqual(withdrawn['status'], '已撤回')
        self.assertEqual(withdrawn['alarm']['withdrawn_on'], '2026-11-22')
        self.assertTrue(withdrawn['alarm']['withdraw_reason'])

        detail = self.client.get(f'/monitors/{self.monitor_id}', headers=self.analyst_headers).json()
        self.assertEqual(detail['alarms'][0]['state'], 'withdrawn')
        self.assertEqual(detail['reports'][-1]['version'], 2)
        # 首个越线日仍保留历史
        self.assertEqual(detail['snapshot']['first_crossing_day'], first_day)
        self.assertIsNone(detail['snapshot']['active_first_crossing_day'])

    def test_audit_events_recorded(self):
        from src.models.identity import Audit
        windows = self.holdout
        first_day = sorted({row['date'] for row in windows})[0]
        day_one = [row for row in windows if row['date'] == first_day]
        for row in day_one:
            self.client.post(f'/monitors/{self.monitor_id}/reports', headers=self.analyst_headers,
                             json={'window_key': key(row), 'count': 0, 'observed_on': '2026-11-21'})
        with TestingSession() as db:
            events = {row.event for row in db.scalars(select(Audit)).all()}
        self.assertIn('monitor.created', events)
        self.assertIn('monitor.actual', events)


def key(row):
    return '|'.join((row['date'], row['road'], row['intersection'], row['shift']))


if __name__ == '__main__':
    unittest.main()
