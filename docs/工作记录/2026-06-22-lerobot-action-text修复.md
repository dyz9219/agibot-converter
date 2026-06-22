# 2026-06-22 LeRobot action_text 丢失修复

## 问题背景

用户反馈使用 `DataConverterShell` 转换
`E:\Users\dyz\Documents\WXWork\1688858286666779\Cache\File\2026-06\task_2059925964389343234.zip`
为 LeRobot 时，平台数据采集生成的 `annotation_result.json[action_text]` 没有被识别，导致训练集
`task` 退化为 `auto-adapted scene` 占位串。

真实样例中的标注内容为：

```json
[{"start_frame":0,"end_frame":455,"action_text":"把瓶子放进筐子里"}]
```

## 现象复现

新增适配器回归测试，构造平台包结构：

- `episode_parent/annotation_result.json`
- `episode_parent/5/aligned_joints.h5`
- `episode_parent/5/state.json`
- `episode_parent/5/head.mp4`

修复前运行：

```powershell
pytest -q test\python\test_raw_to_any4_adapter.py
```

结果为 `1 failed, 1 passed`，失败原因：

```text
AssertionError: 'auto-adapted scene' != '把瓶子放进筐子里'
```

## 根因分析

原始 Agibot 包进入 LeRobot 转换前，会先通过 `raw_to_any4.py` 适配成最小 any4 目录。

当前逻辑在 `_normalize_raw_dir()` 中会选择包含 `aligned_joints.h5` 和 `state.json` 的子目录，例如
`.../test_gaok_1_1_172311_86/5/`，但平台标注文件 `annotation_result.json` 位于其父目录
`.../test_gaok_1_1_172311_86/`。

随后 `_build_min_any4_dataset()` 固定写入：

```json
"init_scene_text": "auto-adapted scene"
```

因此后续 any4/LeRobot 只会把占位串写入 `meta/tasks.jsonl` 和 `meta/episodes.jsonl`。

## 改动方案

在 `src/data_converter/adapters/raw_to_any4.py` 中补充平台标注读取逻辑：

- 优先查找 `raw_dir/annotation_result.json`。
- 再查找 `raw_dir.parent/annotation_result.json`，覆盖平台包常见结构。
- 从 JSON 对象或数组中提取非空 `action_text`。
- 多段标注按 `start_frame` 排序，去重后用 `；` 拼接。
- 没有可用标注或 JSON 解析失败时，保持原有 `auto-adapted scene` 回退行为。

## 验证方式

1. 回归测试：

```powershell
pytest -q test\python\test_raw_to_any4_adapter.py
```

结果：`2 passed in 0.45s`。

2. 真实 zip 后端烟测：

```powershell
$env:PYTHONPATH='D:\workspace\work\bwy\agibot-converter\src'
@'
from pathlib import Path
from data_converter.backend import ConversionBackend, build_options

source = Path(r'E:\Users\dyz\Documents\WXWork\1688858286666779\Cache\File\2026-06\task_2059925964389343234.zip')
out = Path(r'D:\workspace\work\bwy\agibot-converter\test\action-text-smoke')
opts = build_options(input_path=str(source), output_path=str(out), target='lerobot', version='v2.1', fps='30', bag_type='MCAP', concurrency='1')
backend = ConversionBackend()
pre = backend.precheck(opts)
summary = backend.run(opts, pre.tasks)
print(summary)
for task in pre.tasks:
    print((task.output_dir / 'meta' / 'tasks.jsonl').read_text(encoding='utf-8'))
    print((task.output_dir / 'meta' / 'episodes.jsonl').read_text(encoding='utf-8'))
'@ | python -
```

结果：

```text
RunSummary(total=1, success=1, failed=0, skipped=0)
{"task_index": 0, "task": "task_2059925964389343234 | 把瓶子放进筐子里"}
{"episode_index": 0, "tasks": ["task_2059925964389343234 | 把瓶子放进筐子里"], "length": 460, "action_config": []}
```

烟测产物位于 `test/action-text-smoke`，验证后已清理。

3. 全量测试：

```powershell
pytest -q
```

结果：`56 passed, 6 skipped in 13.19s`。

## 当前结论

问题根因位于 raw 包到 any4 的输入适配层，不是 any4/LeRobot 后处理阶段。修复后真实平台包的
`action_text` 已进入 LeRobot `task` 元数据，训练集不再退化为 `auto-adapted scene`。

## 下一步建议

- 后续如平台标注增加更多字段，可在同一提取函数中扩展字段优先级。
- 打包发布前继续执行 `full` 模式打包与 `verify_packaged_any4.ps1`，确保同事机器使用 EXE 时也包含本次修复。
