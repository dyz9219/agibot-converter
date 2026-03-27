# 2026-03-18 AgiBot 转 LeRobot 结构复核与上游仓库核查

## 问题背景

今天重新核对 AgiBot 原始数据到 LeRobot 的转换链路，重点确认两件事：

1. 上游 `Tavish9/any4lerobot` GitHub 仓库是否已有更新；
2. 当前仓库里的 AgiBot 原始输入（`h5 + mp4`）在转成 LeRobot 后，是否完整保留了 14 维 joint + 2 维 left/right effector 的 16 维信息，以及视频是否被写入 parquet。

## 现象与证据

### 1. 上游仓库状态

- GitHub 仓库首页：`https://github.com/Tavish9/any4lerobot`
- 首页 `What's New` 显示最近更新条目已到 `2025-10-04`
- 首页显示仓库当前为 `77 commits`
- 更新内容包含：
  - `2025-04-14` 支持 AgiBotWorld -> LeRobot
  - `2025-05-12` 支持 RoboMIND -> LeRobot
  - `2025-05-16` 支持 LeRobot -> RLDS
  - `2025-06-27` 支持 LIBERO -> LeRobot
  - `2025-09-28` 升级到 LeRobot v3.0
  - `2025-10-04` 补齐 Dataset Version Conversion Scripts

### 2. 本地仓库当前转换链路

- 当前 LeRobot 转换入口：`src/data_converter/converters/lerobot_runner.py`
- 当前 raw -> any4 适配器：`src/data_converter/adapters/raw_to_any4.py`
- 当前 vendored any4：`any4lerobot/agibot2lerobot/agibot_h5.py`

### 3. 16 维状态未被完整保留

在现有真实转换产物日志中，已看到以下告警：

- `state/joint/position` 原始形状为 `(1769, 16)`，目标期望为 `(1769, 14)`，最终被判定不匹配并填零
- `action/joint/position` 原始形状为 `(1769, 16)`，目标期望为 `(1769, 14)`，最终被判定不匹配并填零
- `state/effector/position` / `action/effector/position` 为空或不匹配时，也可能被填零

对应证据文件：

- `test/packaging/regression-8thread-test/custom_task_pick_the_fruit_20260205182313__lerobot_v30/manifest.json`

### 4. 根因定位

根因不是 any4 本身不知道 effector，而是当前 raw 适配器的 schema 假设与智元原始 h5 不一致：

- `src/data_converter/adapters/raw_to_any4.py`
  - `state_shapes["joint/position"] = (14,)`
  - `action_shapes["joint/position"] = (14,)`
  - 只在 `effector.position` 缺失时，才尝试从 `joint[:, 14:16]` 派生 2 维 effector

这意味着当前实现默认认为：

- `joint.position` 只应该保留前 14 维
- 第 15/16 维应拆出去作为 `effector.position`

但当前真实数据里：

- `joint.position` 本身就是完整的 16 维
- 因此适配器把 `(N,16)` 与期望 `(N,14)` 比较后直接判错，导致关键信息丢失或补零

### 5. 视频没有写进 parquet

当前 any4 / LeRobot v3.0 的保存方式仍然是“parquet 存索引，视频外置 mp4”：

- 产物 `meta/info.json` 中：
  - `data_path = data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet`
  - `video_path = videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4`
- `any4lerobot/agibot2lerobot/agibot_h5.py`
  - 对 `video` 类型特征，`save_episode()` 中写入的是视频路径字符串
  - 并不会把每一帧 RGB 解码后直接嵌入 parquet

因此当前产物不是“所有图片帧都在 parquet 中”，而是：

- parquet 中存非视频结构化字段
- 视频单独存成 mp4 文件

## 分析结论

### 结论 1：上游仓库确实已有较多更新

如果参照早期只支持 OpenX 或刚接入 AgiBot 的理解，那么上游已经明显更新，且当前仓库内 vendored 的 any4 快照已经包含 v3.0、版本转换、RLDS、RoboMIND、LIBERO 等能力。

### 结论 2：当前仓库不满足“parquet 内保留完整 16 维 joint+effector”

当前实现会把原始 `(N,16)` 的 `joint.position` 与 `(N,14)` 目标 schema 比较，最终出现丢失或补零，不满足“完整 16 维全部保留”的要求。

### 结论 3：当前仓库也不满足“head/hand_left/hand_right 全帧写入 parquet”

当前 LeRobot 产物仍然使用外置 mp4，不是把 `head.mp4`、`hand_left.mp4`、`hand_right.mp4` 逐帧展开写入 parquet。

## 建议改动方案

如果目标是严格满足你的新要求，需要做两类改造：

1. 状态 schema 改造
   - 明确决定目标表示：
     - 方案 A：`joint.position` 保留完整 16 维，同时额外冗余保留 `effector.position`
     - 方案 B：`joint.position` 保留 14 维，`effector.position` 单独保留 2 维，但必须从原始 16 维稳定拆分，不能补零
   - 当前需求更接近方案 A 或 “A+B 同时保留”

2. 图像存储格式改造
   - 放弃当前 LeRobot `video_path + mp4` 约定
   - 改为把三路 mp4 解码成逐帧 RGB 数组后直接写入 parquet
   - 这会显著增大单集体积，10 倍到数十倍膨胀是合理预期
   - 同时会影响：
     - 写入速度
     - parquet chunk 切分策略
     - stats 计算与读取性能
     - 下游是否仍兼容 LeRobot 官方工具链

## 本轮修改内容

- 未修改业务代码
- 新增本日志文件，记录本轮核查结论与证据

## 验证方式与结果

### 本地代码核查

- 检查 `src/data_converter/adapters/raw_to_any4.py`
- 检查 `src/data_converter/converters/lerobot_runner.py`
- 检查 `any4lerobot/agibot2lerobot/agibot_h5.py`
- 检查 `any4lerobot/agibot2lerobot/agibot_utils/config.py`
- 检查真实转换产物：
  - `test/packaging/regression-8thread-test/custom_task_pick_the_fruit_20260205182313__lerobot_v30/manifest.json`
  - `test/packaging/regression-8thread-test/custom_task_pick_the_fruit_20260205182313__lerobot_v30/meta/info.json`

### 上游仓库核查

- 查看 GitHub 仓库首页 `Tavish9/any4lerobot`
- 结果：确认仓库存在后续更新，README 的最新公开更新条目到 `2025-10-04`

## 当前结论与下一步建议

当前仓库的 AgiBot -> LeRobot 逻辑，不能直接满足“16 维完整保留 + 三路视频逐帧写入 parquet”的新目标。

下一步建议直接开展一轮定向改造，优先顺序如下：

1. 先统一 state/action 目标 schema，避免 16->14 被截断或补零；
2. 再决定是否继续兼容 LeRobot 官方 `video_path` 方案；
3. 若必须把视频帧写进 parquet，则需要单独设计新的 dataset writer，而不是继续沿用当前 any4 的 `video` 字段落盘方式。

## 16D 结构保留回归

### 问题背景

补一个针对 raw adapter 的回归：原始 `state/joint/position` 和 `action/joint/position` 为 `(N, 16)` 时，转换后的 any4 H5 必须完整保留 16 维 joint，并继续从第 15/16 维派生 2 维 effector。

### 根因分析

`src/data_converter/adapters/raw_to_any4.py` 仍将 joint 目标 shape 设为 14，导致 `(N, 16)` 输入被视为不匹配并可能截断/补零；effector 虽然可从 joint 派生，但前提是 joint schema 没有先被降维。

### 改动方案

- 新增根目录回归测试 `test_raw_video_mapping.py`，用临时 H5 构造 `(2, 16)` 的 `state/joint/position` 和 `action/joint/position`。
- 断言生成的 any4 H5 中：
  - `state/joint/position` 保持 16D
  - `action/joint/position` 保持 16D
  - `state/effector/position == state/joint/position[:, 14:16]`
  - `action/effector/position == action/joint/position[:, 14:16]`
- 将 adapter 里的 joint target shape 从 14 提升到 16。

### 量化结果

- 目标测试 `python -m pytest -q test_raw_video_mapping.py -k 16d`：`1 passed, 5 deselected`

### 当前结论与下一步建议

本轮已修复 raw adapter 对 16D joint 向量的 schema 假设。后续如果继续推进 LeRobot/parquet 视频相关功能，可以在此基础上继续验证更大样本和真实数据集。

## 16D 规格复审补强

### 问题背景

收到规格复审反馈后，确认 raw adapter 还存在两个问题：

1. effector 逻辑在源 H5 中已经存在同形状数据时，仍可能直接采信旧 effector，而不是把 `joint[:, 14:16]` 作为源头；
2. 16D 回归测试还缺少“源 H5 中存在伪造 effector 数据”的覆盖。

### 根因分析

`_normalize_raw_array()` 先执行了“形状完全匹配就直接返回”的短路逻辑，导致 `state/effector/position` 与 `action/effector/position` 在数据形状合法时可能绕过 joint 派生路径。这样会让 effector 数据而不是 joint slice 成为输出来源。

### 改动方案

- 在 `src/data_converter/adapters/raw_to_any4.py` 中让 effector 键优先走 `joint[:, 14:16]` 派生逻辑，避免同形状 bogus effector 数据覆盖正确结果。
- 在 `test_raw_video_mapping.py` 中新增 bogus effector 数据集输入，断言输出仍严格等于 joint 的最后两维。

### 验证方式与结果

- 运行 `python -m pytest -q test_raw_video_mapping.py -k 16d`
- 结果：`1 passed, 5 deselected`

### 当前结论与下一步建议

当前 16D 适配规则已按 joint slice 统一，且 bogus effector 数据不会再影响输出。后续如果继续扩展 raw 适配，可考虑把同类“派生数据优先级”规则收敛成显式策略，减少形状短路带来的歧义。

## 14D 兼容与 effector 兜底修复

### 问题背景

复审指出两个遗漏：

1. 16D joint 需要优先从 `joint[:, 14:16]` 派生 effector；
2. 当 joint 只有 14D 时，如果源 H5 里已经有合法 `(N,2)` effector，不能因为无法派生就直接零填充。

### 根因分析

原先 `_normalize_raw_array()` 对 effector 键只处理了“优先从 joint 派生”，没有在 joint 不可派生时回退到合法 raw effector；因此 legacy 14D 数据会在 effector 路径上被静默丢失。

### 改动方案

- 对 effector 键改成明确的优先级：
  - joint 可派生时，输出 `joint[:, 14:16]`
  - joint 不可派生但 raw effector 形状合法时，保留 raw effector
  - 仅当两者都不可用时才零填充
- 增加 legacy 14D + 合法 effector 的回归测试
- 保留 16D joint + bogus effector 的回归测试

### 验证方式与结果

- 运行 `python -m pytest -q test_raw_video_mapping.py -k 16d`
- 结果：`2 passed, 5 deselected`

### 当前结论与下一步建议

目前 effector 选择策略已同时覆盖 16D 新数据和 14D legacy 数据。后续如果继续扩展 raw 适配，建议把“派生优先、raw fallback、最后零填充”的规则抽成统一 helper，减少同类分支回退错误。

## 1D/2D 向量窄化与 effector 覆盖告警修复

### 问题背景

代码复审指出两个遗漏：

1. 非 joint 的 1D 向量特征不应沿用 joint 兼容性里的 padding/truncation 逻辑；
2. 16D joint 优先派生 effector 时，若原始 effector 数据被覆盖，诊断日志应明确提示。

### 根因分析

当前 `_normalize_raw_array()` 的 `(N,k)` 兼容分支对所有 1D 向量字段都生效，容易把本应保持原样或零填充的非 joint 特征误当成 joint 向量处理。

### 改动方案

- 将 `(N,k)` 的 padding/truncation 限定到 joint 相关字段：
  - `state/joint/position`
  - `action/joint/position`
  - `state/joint/current_value`
- 对 effector 键，在 joint 派生值可用且被采用时输出告警，说明原始 effector 被覆盖。
- 新增回归：非 joint 1D 向量不再触发 padding；同时保留 16D bogus effector 和 14D fallback 的既有覆盖。

### 验证方式与结果

- 运行 `python -m pytest -q test_raw_video_mapping.py -k 16d`
- 结果：`3 passed, 5 deselected`

### 当前结论与下一步建议

joint 兼容性与非 joint 特征的边界已拆开，effector 覆盖也能在日志中看到。后续如果继续扩 raw 适配，建议把 joint 兼容策略抽成明确 helper，减少类似的条件泄漏。


## Task 2: vendored any4 schema sync to 16D joint features

### 问题背景

Task 1 已经让 raw adapter 产生 16D joint 数组，但 vendored any4 的 AgiBot gripper schema 仍在 `any4lerobot/agibot2lerobot/agibot_utils/config.py` 中声明为 14D，导致上层 feature metadata 与下游实际数据长度不一致。

### 根因分析

`AgiBotWorld_BETA_GRIPPER_CONFIG` 里的 `states.joint.current_value`、`states.joint.position` 和 `actions.joint.position` 仍然是 `(14,)`，而 `effector.position` 仍保持 `(2,)`。这会让 `generate_features_from_config()` 输出的特征元数据继续广告 14D joint，与新 raw adapter 的 16D 输出不匹配。

### 改动方案

- 在 `test_lerobot_layout.py` 新增回归测试，直接通过 `generate_features_from_config(AgiBotWorld_BETA_GRIPPER_CONFIG)` 检查：
  - `observation.states.joint.position == (16,)`
  - `actions.joint.position == (16,)`
  - `observation.states.effector.position == (2,)`
  - `actions.effector.position == (2,)`
- 在 `any4lerobot/agibot2lerobot/agibot_utils/config.py` 中把 gripper joint schema 从 14D 提升到 16D，并补上 gripper 维度命名，effector 仍保留 2D。

### 量化结果

- 目标测试 `python -m pytest -q .worktrees\lerobot-parquet-option\test_lerobot_layout.py -k joint`
- 结果：`1 passed, 1 deselected`

### 当前结论与下一步建议

vendored any4 的 gripper schema 已与 raw adapter 的 16D joint 输出对齐。后续若继续扩展 AgiBot 其他任务类型，建议同样优先检查 feature metadata 与 raw adapter 的 shape contract 是否一致。


## worker shard / EXE smoke 测试收尾修复

### 问题背景

继续收敛今天这轮改动时，针对新增的 worker shard 与 any4 临时视频目录逻辑跑回归测试，发现还有两个收尾问题：

1. `test/python/test_exe.py` 仍保留旧脚本式写法，`test_any4lerobot_conversion(version: str)` 被 pytest 当成缺失 fixture；
2. `src/data_converter/any4lerobot_bridge.py` 的 `_cleanup_any4_temp_video_dir()` 只删除当前 pid 目录，未充分清理 `.tmp-any4-video` 下的空父目录，导致回归断言失败。

### 根因分析

- `test_exe.py` 不是标准 pytest 测试：函数返回 `bool`，且直接声明了 `version` 参数但没有 `parametrize`；因此 pytest 收集时会报 `fixture 'version' not found`。
- any4 临时目录清理只处理了一层父目录；在同一进程或历史残留存在空 `pid-*` 目录时，`.tmp-any4-video` 目录仍会留在仓库根目录。

### 改动方案

- 将 `test/python/test_exe.py` 改为标准 pytest smoke 测试：
  - 用 `@pytest.mark.parametrize("version", ["v3.0", "v2.1", "v2.0"])` 驱动版本覆盖；
  - 在缺少 `dist/DataConverterShell/DataConverterShell.exe` 或 `temp_test/4` 样例数据时显式 `skip`；
  - 去掉返回 `bool` 的旧脚本式约定，改为断言退出码。
- 加强 `src/data_converter/any4lerobot_bridge.py` 的 `_cleanup_any4_temp_video_dir()`：
  - 删除当前 pid 临时目录后，继续清理 `.tmp-any4-video` 下残留的空子目录；
  - 再自底向上删除空父目录，直到遇到仓库根目录或非空目录为止。

### 量化结果

- `python -m pytest -q test/python/test_raw_video_mapping.py test/python/test_backend_concurrency.py test/python/test_lerobot_layout.py test/python/test_exe.py`
  - 结果：`23 passed, 4 skipped`
  - 说明：`test_exe.py` 在缺少本地 EXE / smoke 数据时按预期跳过，不再因收集错误阻断回归。
- `python -m pytest -q test/python/test_main_entry.py test/python/test_any4_health.py test/python/test_lerobot_version_fallback.py`
  - 结果：`7 passed`

### 当前结论

本轮收尾后，新增 worker shard / any4 临时视频目录 / LeRobot 相关回归已恢复稳定，测试层面不存在已知的收集错误或临时目录泄漏断言失败。

### 下一步建议

1. 若继续推进 `embed_videos_in_parquet`，优先补齐对应选项的真实代码落点与集成测试，避免当前只停留在设计文档；
2. 在准备打包前，再补一轮与 `full` 打包产物相关的 smoke 验证，确保 EXE 场景不是仅在源码环境通过。


## v3.0 真实 smoke parquet 产物复跑

### 问题背景

用户要求确认“代码级冒烟测试之后是否真的有 parquet 产物”，因此补做一轮基于真实 zip 样例的 LeRobot v3.0 smoke 转换，并要求最终给出实际落盘路径。

### 现象与根因

首次复跑 `v3.0` / `v2.1` 都失败，报错为：

- `Couldn't cast array of type list<item: float> to List(Value('float32'), length=14)`

根因不是 `raw_to_any4`，而是 vendored any4 的 `any4lerobot/agibot2lerobot/agibot_utils/config.py` 仍把 gripper 路径下的以下 schema 定义为 14 维：

- `states.joint.current_value`
- `states.joint.position`
- `actions.joint.position`

这会导致真实转换时，虽然 adapter 已经输出 16 维 joint，但 any4 dataset feature 仍尝试按 14 维写 parquet，从而在 cast 阶段失败。

### 改动方案

- 将 `any4lerobot/agibot2lerobot/agibot_utils/config.py` 中 gripper 配置的上述 3 个 joint 字段 shape 从 `(14,)` 调整为 `(16,)`；
- 同步把 motor names 扩展为 16 项，在左右 7 关节后追加：
  - `left_gripper`
  - `right_gripper`

### 验证方式与结果

#### 回归测试

- `python -m pytest -q test/python/test_raw_video_mapping.py test/python/test_lerobot_array_fidelity.py -k "v30 or v21 or 16d"`
- 结果：`2 passed, 6 deselected`

#### 真实 v3.0 smoke 转换

- 输入样例：
  - `D:\下载\windows-x64-agibot-isaac-downloader-真机真机\downloads\真机真机_2019353507798704130_20260306_181055\custom_task_pick_the_fruit_20260205181347.zip`
- 输出目录：
  - `smoke-runs/2026-03-18-v30-smoke-rerun-1/custom_task_pick_the_fruit_20260205181347__lerobot_v30`
- manifest 状态：`success`
- 总耗时：`10.726129s`

实际生成的 parquet 文件：

- `smoke-runs/2026-03-18-v30-smoke-rerun-1/custom_task_pick_the_fruit_20260205181347__lerobot_v30/data/chunk-000/file-000.parquet`
  - `2422 rows, 21 cols`
- `smoke-runs/2026-03-18-v30-smoke-rerun-1/custom_task_pick_the_fruit_20260205181347__lerobot_v30/meta/episodes/chunk-000/file-000.parquet`
  - `1 row, 262 cols`
- `smoke-runs/2026-03-18-v30-smoke-rerun-1/custom_task_pick_the_fruit_20260205181347__lerobot_v30/meta/tasks.parquet`
  - `1 row, 2 cols`

#### 16D 关键列抽查

从 `data/chunk-000/file-000.parquet` 读取首行验证：

- `observation.states.joint.position` 长度 `16`
- `actions.joint.position` 长度 `16`
- `observation.states.effector.position` 长度 `2`
- `actions.effector.position` 长度 `2`

### 当前结论

现在已经有一份本轮新生成、真实落盘的 v3.0 parquet 产物，且 joint 相关列在 parquet 中确认为 16 维，没有再被 any4 schema 截回 14 维。

### 下一步建议

1. 若还需要 v2.1 / v2.0 真实 smoke 产物，也应在当前 16D schema 修复基础上分别补跑一轮；
2. 若要继续推进 `embed_videos_in_parquet`，下一步应在当前已跑通的 v3.0 基线上实现并验证视频列写回 parquet 的后处理逻辑。


## 视频写入 parquet 功能落地与真实 smoke 验证

### 问题背景

用户在 2026-03-18 明确指出：仅有“标准 LeRobot smoke 跑通”并不满足要求，目标必须是把视频真正写进 `.parquet`，而不是继续停留在外置 mp4。

### 改动方案

本轮按 `docs/plans/2026-03-18-lerobot-parquet-video-option-design.md` 的后处理路线实现首版功能：

1. 选项层
   - `src/data_converter/models.py`
     - 新增 `ConversionOptions.embed_videos_in_parquet: bool = False`
   - `src/data_converter/backend.py`
     - `build_options()` 支持该布尔选项
     - worker payload 透传该选项
   - `src/data_converter/worker_shard_cli.py`
     - 解析 worker payload 中的 `embed_videos_in_parquet`
   - `src/data_converter/main.py`
     - LeRobot 非 HDF5 模式下新增 UI 复选框：`将视频逐帧写入 parquet（体积会显著增大）`
     - 内部 CLI 新增 `--embed-videos-in-parquet`
   - `src/data_converter/manifest.py`
     - manifest 记录 `embed_videos_in_parquet`

2. LeRobot 导出后处理
   - `src/data_converter/converters/lerobot_runner.py`
     - 在标准 any4 导出完成后，若开关开启，则对数据集根目录执行 parquet 重写
     - 目标视频键固定为：
       - `observation.images.head`
       - `observation.images.hand_left`
       - `observation.images.hand_right`
     - 向主数据 parquet 追加定制列：
       - `observation.frames.head`
       - `observation.frames.hand_left`
       - `observation.frames.hand_right`
     - 每个单元格写入 JPEG 编码后的 `bytes`
     - `meta/info.json` 追加：
       - 新 feature 定义
       - `video_embedding.enabled = true`
       - `video_embedding.encoding = jpeg`

### 嵌入阶段问题与修正

首版后处理最初假设“视频帧数必须等于 parquet 行数”，真实样例立刻暴露该假设不成立：

- `head.mp4` 帧数：`729`
- `file-000.parquet` 行数：`2422`

失败 manifest 位置：
- `smoke-runs/2026-03-18-v30-embed-parquet-smoke/custom_task_pick_the_fruit_20260205181347__lerobot_v30/manifest.json`

这说明真实 AgiBot -> LeRobot 产物中，视频时间轴与训练行数不是简单 1:1。

修正方式：

- 将“必须等长”改为“按比例重采样到 parquet 行数”
- 当视频帧数与 parquet 行数不一致时：
  - 使用比例索引把原视频帧扩展/抽样到 `target_count = parquet_rows`
  - 保证每一行 parquet 都有对应视频字节列

### 新增测试

新增：`test/python/test_lerobot_video_embedding.py`

覆盖内容：

1. `build_options()` 能解析 `embed_videos_in_parquet`
2. 构造最小 dataset root + 3 路 mp4 + 1 个 parquet，验证：
   - `observation.frames.head` 存在
   - `observation.frames.hand_left` 存在
   - `observation.frames.hand_right` 存在
   - 三列值均为非空 `bytes`
   - `meta/info.json` 含 `video_embedding` 与新增 features

### 验证方式与结果

#### 代码级验证

- `python -m pytest -q test/python/test_lerobot_video_embedding.py test/python/test_lerobot_array_fidelity.py test/python/test_raw_video_mapping.py test/python/test_main_entry.py`
- 结果：`12 passed`

- `python -m py_compile src/data_converter/main.py src/data_converter/backend.py src/data_converter/manifest.py src/data_converter/worker_shard_cli.py src/data_converter/converters/lerobot_runner.py test/python/test_lerobot_video_embedding.py`
- 结果：通过

#### 真实样例 smoke

输入样例：
- `D:\下载\windows-x64-agibot-isaac-downloader-真机真机\downloads\真机真机_2019353507798704130_20260306_181055\custom_task_pick_the_fruit_20260205181347.zip`

输出目录：
- `smoke-runs/2026-03-18-v30-embed-parquet-smoke-rerun-1/custom_task_pick_the_fruit_20260205181347__lerobot_v30`

结果：
- `SUMMARY {"total": 1, "success": 1, "failed": 0, "skipped": 0}`
- manifest 中 `embed_videos_in_parquet = true`

实际 parquet 文件：
- `smoke-runs/2026-03-18-v30-embed-parquet-smoke-rerun-1/custom_task_pick_the_fruit_20260205181347__lerobot_v30/data/chunk-000/file-000.parquet`
  - 文件大小：`680,477,109 bytes`
- `smoke-runs/2026-03-18-v30-embed-parquet-smoke-rerun-1/custom_task_pick_the_fruit_20260205181347__lerobot_v30/meta/tasks.parquet`
- `smoke-runs/2026-03-18-v30-embed-parquet-smoke-rerun-1/custom_task_pick_the_fruit_20260205181347__lerobot_v30/meta/episodes/chunk-000/file-000.parquet`

从主 parquet 读取验证：
- `observation.frames.head`
  - `2422 rows`，首项类型 `bytes`，首项大小 `164544`
- `observation.frames.hand_left`
  - `2422 rows`，首项类型 `bytes`，首项大小 `72268`
- `observation.frames.hand_right`
  - `2422 rows`，首项类型 `bytes`，首项大小 `79467`

metadata 验证：
- `meta/info.json` 中存在：
  - `video_embedding.enabled = true`
  - `video_embedding.encoding = "jpeg"`
  - 三个 `observation.frames.*` feature 定义

### 当前结论

截至 2026-03-18，这一轮已经不只是“标准 LeRobot 转换成功”，而是已经在真实样例 smoke 里把三路视频实际写入了主数据 parquet，满足“视频打到 `.parquet` 里”的核心要求。

### 下一步建议

1. 若后续要兼容 v2.1 / v2.0，也应分别做一轮带 `embed_videos_in_parquet=true` 的真实 smoke；
2. 当前首版采用 JPEG bytes + 比例重采样，是偏工程务实的实现；若后续对时序精度要求更高，应改成基于时间戳的显式对齐策略，而不是纯比例映射；
3. 如需交付给同事或打包验证，下一步应在 `full` 打包产物上重复同样的 embed smoke，确认 EXE 模式也能产出带 `observation.frames.*` 的 parquet。


## v2.1 / v2.0 带视频嵌入 smoke 与 16D 数组值复核

### 问题背景

在 v3.0 已经确认“视频已实际写入 parquet”之后，继续按用户要求验证两件事：

1. `v2.1` 与 `v2.0` 的真实样例转换，在开启 `embed_videos_in_parquet=true` 后是否也能成功生成带视频列的 parquet；
2. 转换后的 parquet 中，`joint.position` / `effector.position` 的 16D / 2D 数组值是否仍与原始数据一致，而不是只保留了形状。

### 验证方式与结果

#### 代码级回归

- `python -m pytest -q test/python/test_lerobot_video_embedding.py test/python/test_lerobot_array_fidelity.py test/python/test_main_entry.py test/python/test_backend_concurrency.py`
- 结果：`22 passed`

#### 真实样例 smoke

输入样例：
- `D:\下载\windows-x64-agibot-isaac-downloader-真机真机\downloads\真机真机_2019353507798704130_20260306_181055\custom_task_pick_the_fruit_20260205181347.zip`

输出目录：
- `smoke-runs/2026-03-18-v21-embed-parquet-smoke-rerun-1/custom_task_pick_the_fruit_20260205181347__lerobot_v21`
- `smoke-runs/2026-03-18-v20-embed-parquet-smoke-rerun-1/custom_task_pick_the_fruit_20260205181347__lerobot_v20`

运行结果：
- `v2.1`: `success=1, failed=0`
- `v2.0`: `success=1, failed=0`

manifest / metadata 复核：
- 两个版本的 `manifest.json` 都包含：`embed_videos_in_parquet = true`
- 两个版本的 `meta/info.json` 都包含：
  - `video_embedding.enabled = true`
  - `video_embedding.encoding = "jpeg"`
  - `keys = ["observation.images.head", "observation.images.hand_left", "observation.images.hand_right"]`

#### parquet 内视频列复核

两份真实产物的主数据 parquet 中均存在：
- `observation.frames.head`
- `observation.frames.hand_left`
- `observation.frames.hand_right`

并且：
- 每列 `rows = 2422`
- 每个单元格类型为 `bytes`
- 首项字节大小均大于 0

#### 16D / 2D 数组值复核

对照 `prepare_any4_source()` 适配后的 `proprio_stats.h5` 原始数组，分别读取 `v2.1` / `v2.0` 产物 parquet，验证如下字段：

- `observation.states.joint.position`
- `actions.joint.position`
- `observation.states.effector.position`
- `actions.effector.position`

结果：
- `v2.1`
  - `observation.states.joint.position`: shape=`(2422, 16)`, `allclose=True`
  - `actions.joint.position`: shape=`(2422, 16)`, `allclose=True`
  - `observation.states.effector.position`: shape=`(2422, 2)`, `allclose=True`
  - `actions.effector.position`: shape=`(2422, 2)`, `allclose=True`
- `v2.0`
  - `observation.states.joint.position`: shape=`(2422, 16)`, `allclose=True`
  - `actions.joint.position`: shape=`(2422, 16)`, `allclose=True`
  - `observation.states.effector.position`: shape=`(2422, 2)`, `allclose=True`
  - `actions.effector.position`: shape=`(2422, 2)`, `allclose=True`

首行样本值也与预期一致，例如 joint 前 4 维均为：
- `[-1.073663592338562, 0.610348641872406, 0.280805379152298, -1.2836918830871582]`

### 前端 / 后端链路复核

#### 前端按钮存在

已在 `src/data_converter/main.py` 中确认：
- 使用 `ft.Checkbox(...)` 定义 `embed_videos`
- 文案为：`将视频逐帧写入 parquet（体积会显著增大）`
- 仅在 `LeRobot` 且 `version != HDF5` 时显示
- 点击开始转换时，通过 `_build_opts()` 写入 `embed_videos_in_parquet=bool(embed_videos.value)`

#### 后端逻辑存在

已确认后端链路全部接通：
- `src/data_converter/models.py`
  - `ConversionOptions.embed_videos_in_parquet`
- `src/data_converter/backend.py`
  - `build_options()` 支持该参数
  - worker payload 透传该参数
- `src/data_converter/worker_shard_cli.py`
  - worker 反序列化该参数
- `src/data_converter/manifest.py`
  - manifest 写入该参数
- `src/data_converter/converters/lerobot_runner.py`
  - `if options.embed_videos_in_parquet: _embed_videos_in_parquet(runtime_output_dir)`
  - 实现对三路视频的 JPEG bytes 嵌入 parquet
  - 更新 `meta/info.json` 中的 embedding metadata

### 当前结论

截至本轮验证：

1. `v3.0` / `v2.1` / `v2.0` 三个版本都已完成真实样例 smoke；
2. 三个版本在开启 `embed_videos_in_parquet=true` 后，主数据 parquet 中都能实际看到 `observation.frames.*` 视频列；
3. `v2.1` / `v2.0` 的 `joint.position` 16 维与 `effector.position` 2 维数组值均与原始适配结果一致；
4. 前端已加“把视频写入 parquet”的可选按钮，后端也已实现完整透传与落盘逻辑。

### 下一步建议

代码侧目前已满足打包前的功能验证要求。下一步可以进入 `full` 打包并做 EXE 实测，重点关注：

1. EXE 模式下勾选该开关后，产物 parquet 是否仍包含 `observation.frames.*`；
2. 打包产物在同事机器上是否仍可稳定转换，不出现缺少 `cv2` / `pyarrow` / any4 依赖的问题；
3. 嵌入视频后主 parquet 体积明显增大，需同步观察生成时间和磁盘占用是否在可接受范围内。


## 二次转换残留 agibotworld 目录问题修复

### 问题背景

用户提供了实际产物目录：
- `D:\下载\windows-x64-agibot-isaac-downloader-手动进入下一轮采集\downloads\转换后__lerobot_v30`

现场现象是：
- 顶层已经存在 `data/`、`meta/`、`videos/`
- 同时又残留一个 `agibotworld/` 目录
- 其中 `agibotworld/task_xxx/...` 看起来像是第二次转换生成的内容

并要求确认该问题是否会影响 `v3.0` / `v2.1` / `v2.0`，并统一修复。

### 现场检查结论

对用户给出的真实目录扫描后，发现至少两个相关异常形态：

1. `1__lerobot_v30`
   - 顶层已有 `data/meta/videos`
   - 同时残留 `agibotworld/task_639911/...`
2. `1__lerobot_v21`
   - 顶层只有 `agibotworld/task_603457/...`
   - 没有被提升到根目录

这说明当前“扁平化 any4 输出布局”的逻辑并不稳，对不同版本/不同生成形态处理不一致。

### 根因分析

`src/data_converter/converters/lerobot_runner.py` 里的 `_flatten_generated_dataset_layout()` 有两个问题：

1. 只要顶层已经存在 `meta/info.json`，就立刻 `return`；
   - 因而对“顶层已平铺，但又多生成了 `agibotworld/`”这种二次转换残留完全漏掉
2. 仅对非常特定的目录形态做处理；
   - 当 `agibotworld/` 下的数据集根需要递归发现时，原逻辑不够鲁棒

此外，`_sync_tree()` 在 staged 模式下只会复制/合并，不会清理目标目录里旧的 `agibotworld/`，因此历史残留也可能被继续保留。

### 改动方案

#### 1. 强化扁平化逻辑

- 移除“顶层已有 `meta/info.json` 就直接返回”的短路逻辑
- 新增 `_find_dataset_roots_under_agibotworld()`：
  - 在 `agibotworld/` 下递归查找 `meta/info.json`
  - 若唯一定位到一个数据集根，则执行提升
- 扁平化时对 `data/`、`meta/`、`videos/` 等目标采用“替换当前目标”的策略：
  - 若目标已存在，先删除旧目标
  - 再把 `agibotworld/...` 下的新产物移动到顶层
- 完成后删除整个 `agibotworld/`

#### 2. staged 同步时清理旧残留

- 在 `_sync_tree(src, dst)` 中增加清理：
  - 若 `dst/agibotworld` 存在，而 `src/agibotworld` 不存在
  - 则先删除目标目录中的陈旧 `agibotworld/`

这样可同时覆盖：
- fresh run 后未正确扁平化
- rerun/overwrite 后保留旧 `agibotworld`
- staged 模式把历史残留继续带到最终输出目录

### 新增测试

更新：`test/python/test_lerobot_layout.py`

新增覆盖：

1. `test_flattens_agibotworld_even_when_root_already_has_dataset_dirs`
   - 模拟“顶层已有旧 `data/meta/videos`，`agibotworld/` 下又有一份新数据集”
   - 验证扁平化后：
     - 顶层保留新数据
     - 旧数据被替换
     - `agibotworld/` 被清掉

2. `test_sync_tree_removes_stale_agibotworld_when_source_is_flat`
   - 模拟 staged 同步时目标目录已有陈旧 `agibotworld/`
   - 验证 `_sync_tree()` 后该残留被删除

### 验证方式与结果

#### 代码级验证

- `python -m pytest -q test/python/test_lerobot_layout.py test/python/test_lerobot_video_embedding.py test/python/test_backend_concurrency.py`
- 结果：`21 passed`

- `python -m py_compile src/data_converter/converters/lerobot_runner.py test/python/test_lerobot_layout.py`
- 结果：通过

#### 真实转换验证

基于真实样例重新跑三种版本：

- `smoke-runs/2026-03-18-layout-v30-rerun-1/...__lerobot_v30`
- `smoke-runs/2026-03-18-layout-v21-rerun-1/...__lerobot_v21`
- `smoke-runs/2026-03-18-layout-v20-rerun-1/...__lerobot_v20`

结果：
- `v3.0`: `success=1, failed=0`, `HAS_AGIBOTWORLD=False`, `HAS_ROOT_META=True`
- `v2.1`: `success=1, failed=0`, `HAS_AGIBOTWORLD=False`, `HAS_ROOT_META=True`
- `v2.0`: `success=1, failed=0`, `HAS_AGIBOTWORLD=False`, `HAS_ROOT_META=True`

### 当前结论

`agibotworld/` 残留问题已在代码层统一修复，并已用真实转换确认：

- `v3.0` 不再出现“顶层已平铺 + 同时又有 agibotworld”的双布局
- `v2.1` / `v2.0` 也都能正确提升到输出根目录
- 这轮修复已经覆盖 direct / staged 两种路径的主要残留场景

### 下一步建议

代码侧目前已经同时满足：

1. 视频写入 parquet 可选开关存在且后端已实现；
2. `v3.0` / `v2.1` / `v2.0` 的 embed smoke 可通过；
3. 16D joint / 2D effector 数组值已验证正常；
4. `agibotworld/` 残留布局问题已修复。

可以进入 `full` 打包并做 EXE 实测。实测时建议优先挑一个样例做重复转换，确认不会再复现“第二次输出跑进 agibotworld/”的问题。
