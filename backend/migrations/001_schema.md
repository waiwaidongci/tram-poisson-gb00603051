# 001 初始表结构

应用首次启动以 SQLAlchemy 模型创建表与唯一约束。现有数据表只补建缺失表，升级到修改过字段的版本需要显式迁移。

- `users` 账号角色和加盐密码摘要。
- `datasets` 不可变记录批次，以名称和 SHA-256 去重。
- `runs` 记录参数、数据批次、运行租约、分析结果、导出文件及复核决定。
- `schedules` 保存定时计划和下一到期时间。
- `deliveries` 保存站内分发回执，每次运行最多一个回执。
- `audit_events` 记录登录、数据入库、运行、复核和报表分发动作。
- `monitors` 一次试算运行对应一个低估盯盘计划（Bonferroni 单窗显著水平与重放快照），按 `run_id` 唯一。
- `actual_reports` 窗口实绩的全部版本：version=1 为首版，version≥2 为晚到更正；按 `(monitor_id, window_key, version)` 唯一。
- `monitor_alarms` 告警与撤回台账，撤回为终态并保留判定依据；按 `(monitor_id, window_key)` 唯一。
