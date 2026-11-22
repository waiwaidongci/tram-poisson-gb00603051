"""盯盘引擎的标准库单元测试：python -m tests.test_monitoring"""

import unittest
from math import exp, factorial

from src.domain.monitoring import (
    FAMILY_ALPHA, STATE_ALARM, STATE_CLEAR, STATE_PENDING, STATE_WAITING, STATE_WITHDRAWN,
    build_plan, critical_count, poisson_sf, replay, window_key, DAY_WAITING,
)


def exact_sf(k, mean):
    if k <= 0:
        return 1.0
    if mean <= 0:
        return 0.0
    return 1.0 - sum(exp(-mean) * mean ** i / factorial(i) for i in range(k))


class PoissonTailTest(unittest.TestCase):
    def test_against_direct_sum(self):
        for mean in (0.05, 0.5, 1.0, 2.3, 5.0, 12.8, 30.0):
            for k in range(0, 18):
                self.assertAlmostEqual(poisson_sf(k, mean), exact_sf(k, mean),
                                       places=12, msg=f"mean={mean}, k={k}")

    def test_boundary_values(self):
        self.assertEqual(poisson_sf(0, 3.2), 1.0)
        self.assertEqual(poisson_sf(5, 0.0), 0.0)
        self.assertLessEqual(poisson_sf(100, 2.0), 1e-80)
        self.assertLessEqual(poisson_sf(1, 1e-9), 1e-9 + 1e-20)

    def test_monotone(self):
        mean = 4.0
        previous = 1.0
        for k in range(1, 20):
            value = poisson_sf(k, mean)
            self.assertLessEqual(value, previous)
            previous = value


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.windows = [
            {"date": f"2026-11-{18 + d:02d}", "road": "港湾路", "intersection": "货栈街口",
             "shift": "平峰", "predicted": 1.2 + d * 0.1}
            for d in range(3)
        ] + [
            {"date": "2026-11-18", "road": "港湾路", "intersection": "渡口街口",
             "shift": "高峰", "predicted": 0.8},
        ]

    def test_bonferroni_budget(self):
        plan = build_plan(self.windows)
        self.assertEqual(len(plan), 4)
        self.assertAlmostEqual(plan[0]["alpha_per_window"], FAMILY_ALPHA / 4)
        for item in plan:
            self.assertIsNotNone(item["critical"])
            # 阈值定义：上尾概率首次不超过单窗显著水平
            self.assertLessEqual(poisson_sf(item["critical"], item["predicted"]),
                                 item["alpha_per_window"])
            self.assertGreater(poisson_sf(item["critical"] - 1, item["predicted"]),
                               item["alpha_per_window"])

    def test_zero_mean_threshold(self):
        plan = build_plan([{"date": "2026-11-18", "road": "r", "intersection": "i",
                            "shift": "平峰", "predicted": 0.0}])
        self.assertEqual(plan[0]["critical"], 1)


def report(row, count, observed_on, version=1):
    return {"key": window_key(row), "count": count, "observed_on": observed_on, "version": version}


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"date": "2026-11-18", "road": "港湾路", "intersection": "甲", "shift": "平峰", "predicted": 1.0},
            {"date": "2026-11-18", "road": "港湾路", "intersection": "乙", "shift": "平峰", "predicted": 1.0},
            {"date": "2026-11-19", "road": "港湾路", "intersection": "甲", "shift": "平峰", "predicted": 1.0},
            {"date": "2026-11-19", "road": "港湾路", "intersection": "乙", "shift": "平峰", "predicted": 1.0},
        ]
        self.plan = build_plan(self.rows)
        # N=4 -> alpha/window = 0.0125；Poisson(1) 下 P(X>=4)=0.018988… 不达标，
        # P(X>=5)=0.003659… 达标，故阈值为 5。
        self.assertEqual(self.plan[0]["critical"], 5)

    def test_missing_day_blocks_later_days(self):
        reports = [report(self.rows[0], 0, "2026-11-19")]  # 18 日缺一个窗
        snapshot = replay(self.plan, reports)
        self.assertEqual(snapshot["blocked_at"], "2026-11-18")
        self.assertEqual(snapshot["days"][0]["state"], DAY_WAITING)
        self.assertEqual(snapshot["days"][0]["missing"], 1)
        self.assertEqual(snapshot["first_crossing_day"], None)
        self.assertTrue(all(row["status"] in (STATE_PENDING, STATE_WAITING) for row in snapshot["windows"]))

    def test_alarm_fires_and_consumes_budget(self):
        reports = [
            report(self.rows[0], 5, "2026-11-19"),
            report(self.rows[1], 0, "2026-11-19"),
        ]
        snapshot = replay(self.plan, reports)  # 18 日等齐，19 日未到
        self.assertEqual(snapshot["first_crossing_day"], "2026-11-18")
        self.assertEqual(snapshot["active_first_crossing_day"], "2026-11-18")
        self.assertEqual(snapshot["budget_used"], 1)
        self.assertEqual(snapshot["budget_remaining"], 3)
        alarm = snapshot["alarms"][0]
        self.assertEqual(alarm["state"], STATE_ALARM)
        self.assertIn("Bonferroni", alarm["basis"])

    def test_withdrawal_keeps_budget_consumed(self):
        reports = [
            report(self.rows[0], 5, "2026-11-19"),
            report(self.rows[1], 0, "2026-11-19"),
            report(self.rows[2], 5, "2026-11-20"),
            report(self.rows[3], 0, "2026-11-20"),
        ]
        first = replay(self.plan, reports)
        self.assertEqual(first["budget_used"], 2)
        # 晚到更正：18 日甲窗口实绩从 5 改成 1，原告警失效撤回
        reports.append(report(self.rows[0], 1, "2026-11-22", version=2))
        second = replay(self.plan, reports)
        self.assertEqual(second["budget_used"], 2, "撤回不退还额度")
        self.assertEqual(second["budget_remaining"], 2)
        withdrawn = next(row for row in second["windows"] if row["key"] == window_key(self.rows[0]))
        self.assertEqual(withdrawn["status"], STATE_WITHDRAWN)
        self.assertEqual(withdrawn["alarm"]["withdrawn_on"], "2026-11-22")
        self.assertIsNotNone(withdrawn["alarm"]["withdraw_reason"])
        self.assertEqual(second["active_first_crossing_day"], "2026-11-19")
        self.assertEqual(second["first_crossing_day"], "2026-11-18", "首个越线日保留历史")
        self.assertEqual(len(second["active_alarms"]), 1)

    def test_correction_never_rearms_window(self):
        # 首版未越线 -> 后续更正成越线值也不能再告警（判定只在等齐日做一次）
        reports = [
            report(self.rows[0], 1, "2026-11-19"),
            report(self.rows[1], 1, "2026-11-19"),
        ]
        snapshot = replay(self.plan, reports)
        self.assertEqual(snapshot["budget_used"], 0)
        reports.append(report(self.rows[0], 9, "2026-11-22", version=2))
        snapshot = replay(self.plan, reports)
        self.assertEqual(snapshot["budget_used"], 0)
        self.assertEqual(snapshot["first_crossing_day"], None)
        self.assertEqual(snapshot["windows"][0]["status"], STATE_CLEAR)

    def test_late_first_report_after_clear_days(self):
        # 18 日两窗先到一窗，19 日数据齐了但 18 仍缺 -> 整日阻塞
        reports = [
            report(self.rows[0], 5, "2026-11-19"),
            report(self.rows[2], 5, "2026-11-20"),
            report(self.rows[3], 0, "2026-11-20"),
        ]
        snapshot = replay(self.plan, reports)
        self.assertEqual(snapshot["blocked_at"], "2026-11-18")
        self.assertEqual(len(snapshot["days"]), 1)
        # 补齐 18 日后，18、19 连续处理
        reports.append(report(self.rows[1], 0, "2026-11-21", version=1))
        snapshot = replay(self.plan, reports)
        self.assertIsNone(snapshot["blocked_at"])
        self.assertEqual(len(snapshot["days"]), 2)
        self.assertEqual(snapshot["first_crossing_day"], "2026-11-18")

    def test_correction_before_day_complete_uses_latest_value(self):
        # 18 日等齐之前就更正：等齐判定以最新版为准，历史版本不触发告警
        reports = [
            report(self.rows[0], 9, "2026-11-19"),
            report(self.rows[0], 0, "2026-11-19", version=2),
            report(self.rows[1], 0, "2026-11-19"),
        ]
        snapshot = replay(self.plan, reports)
        self.assertEqual(snapshot["budget_used"], 0)
        target = next(row for row in snapshot["windows"] if row["key"] == window_key(self.rows[0]))
        self.assertEqual(target["version"], 2)
        self.assertEqual(target["status"], STATE_CLEAR)


class FwerSimulationTest(unittest.TestCase):
    def test_familywise_error_within_five_percent(self):
        # N 个窗口全部在原预测下（无低估），蒙特卡洛验证全线误报率 ≤ 5%
        from random import Random
        rng = Random(20261122)
        n_windows = 24
        means = [0.4 + (i % 7) * 0.35 for i in range(n_windows)]
        plan_rows = [{"date": f"2026-11-{18 + i // 8:02d}", "road": "r",
                      "intersection": f"i{i}", "shift": "平峰", "predicted": means[i]}
                     for i in range(n_windows)]
        plan = build_plan(plan_rows)

        def poisson_draw(mean):
            limit = exp(-mean)
            product = rng.random()
            count = 0
            while product > limit:
                product *= rng.random()
                count += 1
            return count

        trials = 20000
        false_runs = 0
        for _ in range(trials):
            reports = [{"key": item["key"], "count": poisson_draw(item["predicted"]),
                        "observed_on": item["date"], "version": 1}
                       for item in plan]
            if replay(plan, reports)["budget_used"]:
                false_runs += 1
        rate = false_runs / trials
        # 蒙特卡洛标准误约 sqrt(0.05*0.95/trials) ≈ 0.00154，留 3 个标准误余量
        self.assertLess(rate, FAMILY_ALPHA + 3 * 0.00155)
        self.assertGreater(rate, 0.01)  # 健全性：Bonferroni 不会过紧到零告警


if __name__ == "__main__":
    unittest.main()
