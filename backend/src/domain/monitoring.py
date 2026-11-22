"""低估盯盘引擎：在试算留出窗口的逐窗预测上做泊松尾部检验。

口径（与值班组约定一致）：

* 试算给出每个留出窗口的泊松期望 ``predicted``，实绩另报。原预测成立时
  ``X ~ Poisson(λ)``，且各窗、各版实绩相互独立。
* 全盯盘期共 N 个窗口，采用 Bonferroni 分配，单窗显著水平 ``α/N``。
  由并集界，全期全线至少误报一次的概率不超过 N·(α/N) = α = 5%。
* 每个窗口最多只发一次告警：首次判定发生在该观测日“等齐”（当日全部窗口
  都已上报实绩）时。撤回也占用该窗口的告警额度，不会重新武装。
* 晚到的更正只可能让既发告警失效（撤回），不会在原判定日之外再产生新告警，
  因此无限次更正下并集界依然成立，FWER 始终不超过 5%。
"""

from collections import defaultdict
from math import exp, fsum, log, lgamma

FAMILY_ALPHA = 0.05
STATE_PENDING = "待实绩"
STATE_WAITING = "待等齐"
STATE_CLEAR = "未越线"
STATE_ALARM = "告警中"
STATE_WITHDRAWN = "已撤回"
DAY_WAITING = "缺窗等待"
DAY_COMPLETE = "等齐"


def poisson_sf(count, mean):
    """上尾概率 P(X >= count)，X ~ Poisson(mean)。

    用首项（k=count）乘以递推比例的级数求和，并对极大均值做中心项缩放，
    避免 exp(-mean) 下溢与大阶乘溢出。
    """
    if count <= 0:
        return 1.0
    if mean <= 0:
        return 0.0
    # log P(X=count) = -mean + count*log(mean) - log(count!)
    log_first = count * log(mean) - lgamma(count + 1) - mean
    first = exp(log_first)
    if first == 0.0:
        # 极端参数下首项下溢，改用对数域累加（本场景 λ、k 都很小，通常走不到）
        return _sf_log_domain(count, mean)
    # P(X>=count) = P(X=count) * (1 + mean/(count+1) + mean^2/((count+1)(count+2)) + ...)
    ratio_sum = 1.0
    k = count
    add = mean / (k + 1)
    while add > 1e-15:
        ratio_sum += add
        k += 1
        add *= mean / (k + 1)
    return min(1.0, first * ratio_sum)


def _sf_log_domain(count, mean):
    log_terms = [-mean + count * log(mean) - lgamma(count + 1)]
    k = count
    while True:
        k += 1
        log_terms.append(log_terms[-1] + log(mean / k))
        if log_terms[-1] < log_terms[0] - 40:
            break
    center = max(log_terms)
    return min(1.0, exp(center) * fsum(exp(item - center) for item in log_terms))


def critical_count(mean, alpha_per_window):
    """最小整数 k，使 P(X >= k | mean) <= alpha_per_window；找不到时返回 None。"""
    if mean <= 0:
        return 1
    k = max(1, int(mean))
    # 向上搜索到上尾概率首次达标
    ceiling = max(20, int(mean * 6) + 50)
    while k <= ceiling:
        if poisson_sf(k, mean) <= alpha_per_window:
            return k
        k += 1
    return None


def build_plan(windows, family_alpha=FAMILY_ALPHA):
    """根据试算留出窗口生成盯盘计划。

    每个窗口需要包含 date/road/intersection/shift/predicted 字段，
    返回带窗口键、单窗显著水平与越线阈值的计划列表（按观测日、窗口键排序）。
    """
    if not windows:
        raise ValueError("试算结果没有留出窗口，无法建立盯盘计划")
    total = len(windows)
    alpha_per_window = family_alpha / total
    plan = []
    for row in windows:
        mean = float(row["predicted"])
        threshold = critical_count(mean, alpha_per_window)
        plan.append({
            "key": window_key(row),
            "date": row["date"],
            "road": row["road"],
            "intersection": row["intersection"],
            "shift": row["shift"],
            "predicted": mean,
            "alpha_per_window": alpha_per_window,
            "critical": threshold,
        })
    plan.sort(key=lambda item: (item["date"], item["key"]))
    return plan


def window_key(row):
    return "|".join((row["date"], row["road"], row["intersection"], row["shift"]))


def replay(plan, reports):
    """按观测日顺序重放全部实绩报告（含晚到更正），返回盯盘快照。

    ``reports`` 为报告事件列表，每条含 ``key/version/count/observed_on``：
    version=1 是首版实绩，version≥2 是该窗口的更正记录。引擎按
    (observed_on, version) 时序应用事件，并从完整历史确定性地重建状态。

    规则：
    1. 观测日按日期排序；当日计划窗口全部有首版实绩才“等齐”，缺窗整日
       等待，后续观测日也不处理（观测日顺序阻塞）。
    2. 等齐日对各窗口用当时已知的最新版实绩做一次尾检，越线即告警，
       告警额度永久占用；每个窗口至多判定、至多告警一次。
    3. 更正从改动日起重放：只作用于既发告警——当前实绩不再越线则撤回并
       保留依据；撤回为终态，额度不还，也不会重新武装。未告警窗口的更正
       不会补提告警。
    """
    by_key = {item["key"]: item for item in plan}
    days = defaultdict(list)
    for item in plan:
        days[item["date"]].append(item)
    day_order = sorted(days)

    events = sorted(reports, key=lambda event: (event["observed_on"], event["version"], event["key"]))
    unknown = {event["key"] for event in events} - set(by_key)
    if unknown:
        raise ValueError(f"实绩报告包含盯盘计划之外的窗口：{sorted(unknown)[:3]}")

    current_count = {}
    latest_observed_on = {}
    latest_version = {}
    status = {item["key"]: STATE_PENDING for item in plan}
    alarms = {}
    day_status = {}
    frontier = 0  # 第一个尚未等齐的观测日
    first_crossing_day = None
    blocked_at = None

    def judge_day(day):
        """等齐判定：当日窗口各判一次，越线即告警。"""
        nonlocal first_crossing_day
        for item in days[day]:
            key = item["key"]
            count = current_count[key]
            p_value = poisson_sf(count, item["predicted"])
            if _crosses(item, count, p_value):
                alarms[key] = {
                    "key": key, "date": day,
                    "road": item["road"], "intersection": item["intersection"], "shift": item["shift"],
                    "predicted": item["predicted"], "critical": item["critical"],
                    "alpha_per_window": item["alpha_per_window"],
                    "fired_on": latest_observed_on[key],
                    "fired_count": count, "fired_p_value": p_value,
                    "current_count": count, "current_p_value": p_value,
                    "state": STATE_ALARM,
                    "withdrawn_on": None, "withdraw_reason": None,
                    "version": latest_version[key],
                    "basis": _basis(item, count, p_value),
                }
                status[key] = STATE_ALARM
                if first_crossing_day is None:
                    first_crossing_day = day
            else:
                status[key] = STATE_CLEAR

    # 按改动日分组应用事件；同一改动日的多事件先全部落账，再统一判定/撤回
    cursor = 0
    while cursor < len(events):
        stamp = events[cursor]["observed_on"]
        corrections_at_stamp = []
        while cursor < len(events) and events[cursor]["observed_on"] == stamp:
            event = events[cursor]
            key = event["key"]
            if key in current_count and event["version"] <= latest_version[key]:
                raise ValueError(f"窗口 {key} 的版本号 {event['version']} 不是递增更正")
            is_correction = key in current_count
            current_count[key] = event["count"]
            latest_observed_on[key] = stamp
            latest_version[key] = event["version"]
            if status[key] == STATE_PENDING:
                status[key] = STATE_WAITING
            if is_correction:
                corrections_at_stamp.append(key)
            cursor += 1

        # 观测日顺序推进：从前沿开始，等齐一个判定一个，遇缺窗即停
        while frontier < len(day_order):
            day = day_order[frontier]
            items = days[day]
            reported = sum(1 for item in items if item["key"] in current_count)
            if reported < len(items):
                day_status[day] = {"date": day, "state": DAY_WAITING,
                                   "expected": len(items), "reported": reported,
                                   "missing": len(items) - reported}
                blocked_at = day
                break
            day_status[day] = {"date": day, "state": DAY_COMPLETE,
                               "expected": len(items), "reported": len(items), "missing": 0}
            blocked_at = None
            judge_day(day)
            frontier += 1

        # 改动日的更正重放：既发告警失效就撤回（终态，不再武装）
        for key in corrections_at_stamp:
            record = alarms.get(key)
            if not record or record["state"] != STATE_ALARM:
                continue
            item = by_key[key]
            count = current_count[key]
            p_value = poisson_sf(count, item["predicted"])
            record["current_count"] = count
            record["current_p_value"] = p_value
            record["version"] = latest_version[key]
            if not _crosses(item, count, p_value):
                record.update({
                    "state": STATE_WITHDRAWN,
                    "withdrawn_on": stamp,
                    "withdraw_reason": _withdraw_reason(item, count, p_value),
                })
                status[key] = STATE_WITHDRAWN

    if frontier < len(day_order) and blocked_at is None:
        blocked_at = day_order[frontier]

    window_rows = []
    for item in plan:
        key = item["key"]
        count = current_count.get(key)
        window_rows.append({
            **item,
            "status": status[key],
            "actual_count": count,
            "observed_on": latest_observed_on.get(key),
            "version": latest_version.get(key),
            "p_value": poisson_sf(count, item["predicted"]) if count is not None else None,
            "alarm": alarms.get(key),
        })

    active = [record for record in alarms.values() if record["state"] == STATE_ALARM]
    active.sort(key=lambda record: (record["date"], record["key"]))
    fired_total = len(alarms)
    family_alpha = plan[0]["alpha_per_window"] * len(plan) if plan else FAMILY_ALPHA
    return {
        "window_count": len(plan),
        "family_alpha": family_alpha,
        "alpha_per_window": plan[0]["alpha_per_window"] if plan else None,
        "budget_total": len(plan),
        "budget_used": fired_total,
        "budget_remaining": len(plan) - fired_total,
        "first_crossing_day": first_crossing_day,
        "active_first_crossing_day": active[0]["date"] if active else None,
        "blocked_at": blocked_at,
        "days": [day_status[day] for day in day_order if day in day_status],
        "windows": window_rows,
        "alarms": sorted(alarms.values(), key=lambda record: (record["date"], record["key"])),
        "active_alarms": active,
    }


def _crosses(item, count, p_value):
    return item["critical"] is not None and count >= item["critical"] \
        and p_value <= item["alpha_per_window"] + 1e-15


def _basis(item, count, p_value):
    return (f"窗口 {item['date']} {item['road']}·{item['intersection']}·{item['shift']}："
            f"原预测期望 {item['predicted']:.4g} 次，实绩 {count} 次，"
            f"Poisson 上尾 P(X≥{count})={p_value:.4g}，"
            f"单窗显著水平 {item['alpha_per_window']:.4g}（Bonferroni，阈值 ≥{item['critical']}）。")


def _withdraw_reason(item, count, p_value):
    return (f"更正后实绩 {count} 次，上尾概率 {p_value:.4g} 高于单窗显著水平 "
            f"{item['alpha_per_window']:.4g}（阈值 ≥{item['critical']}），原告警依据不再成立。")
