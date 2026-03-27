# 2026-03-17 LeRobot v2.1 转换修复核查

## 问题背景
用户要求确认当前代码是否已经修复 “LeRobot v2.1 不能正常转换” 的问题。

## 核查范围
- 检查 `src/data_converter/converters/lerobot_runner.py` 中 v3.0 -> v2.1 后处理与 fallback 逻辑。
- 检查 `src/data_converter/adapters/raw_to_any4.py` 对原始 joint 数据的适配是否覆盖先前已知问题。
- 检查运行时探测、并发保护和布局整理相关回归测试。

## 根因与代码现状分析
结合仓库已有实现和 `docs/工作记录/2026-03-16-lerobot-parquet-and-metadata-fix.md`，当前代码已经覆盖此前记录的两类关键根因：

1. 原始 joint 数据被错误清零
- 先前问题是 16 维 joint 数组与训练期 14 维结构不匹配时被整列填零。
- 当前仓库已有针对该问题的回归测试 `test/python/test_raw_to_any4_adapter.py`，验证 16 维输入能保留训练需要的关节数据。

2. `v3.0 -> v2.1` fallback 只改版本号、不重排真实训练结构
- 当前 `lerobot_runner.py` 已新增：
  - 读取 `meta/episodes/**/*.parquet`
  - 重写 `meta/info.json`
  - 切分 consolidated parquet 为 `episode_xxxxxx.parquet`
  - 重建 `tasks.jsonl` / `episodes.jsonl` / `episodes_stats.jsonl`
- 对应回归测试 `test/python/test_lerobot_version_fallback.py` 已验证这些关键输出。

3. EXE/并发路径的附带风险
- 当前代码新增 `_VERSION_POSTPROCESS_LOCK` 与 `_STDIO_GUARD_LOCK`，用于降低 bundled/并发场景下的后处理竞争和 `NoneType.write` 风险。
- 相关并发与 stdio guard 测试当前可通过。

## 验证命令与结果
### 通过
命令：
`python -m unittest discover -s test\python -p test_lerobot_version_fallback.py -v`

结果：
- 1 个测试通过

命令：
`python -m unittest discover -s test\python -p test_raw_to_any4_adapter.py -v`

结果：
- 1 个测试通过

命令：
`python -m pytest -q test/python/test_any4_health.py test/python/test_lerobot_stdio_guard.py test/python/test_backend_concurrency.py`

结果：
- 21 个测试通过

命令：
`python -m pytest -q test/python/test_raw_to_any4_adapter.py test/python/test_lerobot_version_fallback.py test/python/test_any4_health.py test/python/test_lerobot_stdio_guard.py test/python/test_backend_concurrency.py test/python/test_lerobot_layout.py`

结果：
- 24 个测试通过

### 未执行
- 未执行真实用户数据的端到端 smoke 转换。
- 未执行已打包 EXE 的实机 smoke，因为当前工作区未发现 `dist/DataConverterShell.exe` 产物。

## 量化结果
- 本轮共验证 24 个相关测试，全部通过。
- 覆盖面包含：raw->any4 关节适配、v3.0->v2.1 fallback、运行时探测、stdio guard、并发调度、输出布局整理。

## 当前结论
从当前代码与已执行回归测试看，仓库内针对 “LeRobot v2.1 不能正常转换” 的已知核心问题已经有实质性修复，且相关回归当前通过。

但结论仍然是“高概率已修复，不等于完成最终验收”：
- 现有证据主要来自单元/组件级回归；
- 还缺少一份真实样本的端到端 v2.1 smoke；
- 也缺少已打包 EXE 在干净环境下的自包含验证。

## 下一步建议
1. 使用一份真实原始包执行一次 LeRobot `v2.1` 端到端转换，核对 `meta/info.json`、`episodes.jsonl`、`episodes_stats.jsonl` 与 `data/chunk-*/episode_*.parquet`。
2. 如要确认“同事机器可运行”，按仓库规范重新做 `full` 打包并执行 `scripts/verify_packaged_any4.ps1`。
3. 在有产物的前提下补跑 EXE smoke，避免只验证源码路径而遗漏 bundled 路径差异。

## 真实样本 v2.1 端到端 smoke 补充
### 样本
- `D:\下载\windows-x64-agibot-isaac-downloader-真机真机\downloads\真机真机_2019353507798704130_20260306_181055\custom_task_pick_the_fruit_20260205181347.zip`

### 执行命令
`$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m data_converter.main --internal-run-any4-health --version v2.1`

结果：
- `ANY4_HEALTH_OK`
- `mode=bundled; root=D:\workspace\work\bwy\agibot-converter\any4lerobot; python=; force_bundled=0`

执行命令：
`$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m data_converter.main --internal-run-conversion --input-path <真实样本zip> --output-path D:\workspace\work\bwy\agibot-converter\test\smoke\lerobot-v21-realdata --target lerobot --version v2.1 --fps 30 --bag-type MCAP --concurrency 1`

结果：
- `RUN_SUMMARY total=1 success=1 failed=0 skipped=0`
- manifest 记录 `status=success`
- 真实耗时 `elapsed_seconds=17.555287`

### 输出核查结果
输出目录：
- `test\smoke\lerobot-v21-realdata\custom_task_pick_the_fruit_20260205181347__lerobot_v21`

核查通过项：
- `meta/info.json` 中 `codebase_version = v2.1`
- `meta/info.json` 中 `data_path = data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet`
- `meta/episodes.jsonl` 存在且可读
- `meta/episodes_stats.jsonl` 存在且可读
- `data/chunk-000/episode_000000.parquet` 存在且可读
- `episode_000000.parquet` 实际 `2422` 行、`21` 列
- `videos/chunk-000/.../episode_000000.mp4` 已生成

### 发现的剩余观察项
- 输出目录中仍保留 `data/chunk-000/file-000.parquet`，且大小与 `episode_000000.parquet` 相同。
- 当前 `info.json` 与 legacy 读取路径已指向 `episode_*.parquet`，因此本次 smoke 不构成阻塞；但这说明 fallback 后清理旧 v3 数据文件还不彻底，后续可考虑补充清理逻辑，避免目录中同时存在 legacy 与 v3 命名文件造成歧义。

### 补充结论
- 基于真实样本端到端 smoke，源码路径下 LeRobot `v2.1` 转换现在可以成功完成，且关键训练结构文件已经按目标版本产出。
- 当前更准确的状态是：`v2.1` 转换问题在源码路径上已验证修复；若要满足“跨机器可运行性”验收，还需要再做一次 `full` 打包产物验证。

## 新增：原始多维数组与 LeRobot 输出数组保真校验
### 目标
对同一真实原始样本，在转换为 LeRobot `v3.0` 与 `v2.1` 后，逐项比较原始多维数组映射到训练结构后的基准值，与最终 parquet 中的多维数组是否一致。

### 校验设计
- 基准真值不直接取 raw H5 原始 shape，而是先经过 `prepare_any4_source()` 生成 `proprio_stats.h5`。
- 这样可以把“原始 16 维 -> 训练 14 维 / 2 维”的合法映射固化下来，再验证 LeRobot 导出环节有没有丢值、改值、错位。
- 对齐比较范围覆盖 `proprio_stats.h5` 中全部 state/action 数值数组，并映射到：
  - `observation.states.*`
  - `actions.*`

### 新增回归测试
文件：`test/python/test_lerobot_array_fidelity.py`

覆盖内容：
- 使用真实样本 zip 执行 `v3.0` 转换
- 使用真实样本 zip 执行 `v2.1` 转换
- 逐列比较转换后的 parquet 数组与 `proprio_stats.h5` 基准数组
- 对每一列同时校验：列存在、shape 一致、数值 `allclose`

### 验证命令与结果
命令：
`python -m pytest -q test/python/test_lerobot_array_fidelity.py`

结果：
- `2 passed`
- `v3.0` 与 `v2.1` 两个版本的数组保真校验均通过

命令：
`python -m pytest -q test/python/test_raw_to_any4_adapter.py test/python/test_lerobot_version_fallback.py test/python/test_lerobot_array_fidelity.py`

结果：
- `4 passed`
- 新增校验未破坏既有 raw 适配与 v2.1 fallback 回归

### 本轮结论
- 当前真实样本下，LeRobot `v3.0` 与 `v2.1` 输出的多维数组，与原始数据映射到训练结构后的基准值一致。
- 本轮没有发现需要继续修复的数组值差异，因此未修改业务转换逻辑，只新增了可重复执行的保真回归测试。
