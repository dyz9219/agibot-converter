## 问题背景

用户反馈 `1__lerobot_v21/data/chunk-000` 在“将视频以图片形式写入 parquet”后出现两个大小完全一样的 parquet 文件，怀疑转换重复写出。

## 根因分析

代码排查结果显示，视频嵌入逻辑不会额外复制 parquet，而是在现有 parquet 上追加 JPEG 二进制列：

- `src/data_converter/converters/lerobot_runner.py` 中 `_embed_videos_in_parquet()` 会遍历 `data/**/*.parquet`，逐个调用 `_embed_video_columns_into_parquet_file()` 原地重写文件。
- `any4lerobot/agibot2lerobot/agibot_h5.py` 的 `_save_episode_data()` 在 v3.0 阶段按 `data/file-{index}.parquet` 连续写入。
- 如果目标版本是 v2.1，`src/data_converter/converters/lerobot_runner.py` 中 `_convert_generated_output_to_target_version()` 会调用 v3.0 -> v2.1 转换；fallback 实现 `_split_v30_data_into_v21_files()` 会按 episode 记录把一个 v3.0 数据文件拆成多个 `episode_******.parquet`。

因此，`chunk-000` 下出现两个 parquet 的首要原因通常是“存在两个 episode”，不是“同一个 episode 被重复写了两次”。

两个 parquet 大小完全一样的原因通常有两类：

1. 两个 episode 帧数相同，且 schema 完全一致。
2. 视频被嵌入为 JPEG 二进制列后，每帧大小和总帧数接近，snappy 压缩后的总文件体积相同。

## 改动方案

本轮未修改代码，仅完成根因定位和调用链确认。

## 量化结果

- 定位到 v2.1 输出拆分逻辑：`_split_v30_data_into_v21_files()`
- 定位到嵌入逻辑：`_embed_videos_in_parquet()`
- 确认嵌入逻辑为“原地追加列并重写”，不是“复制出第二份 parquet”

## 验证方式

本轮采用静态代码验证，检查如下路径：

- `src/data_converter/converters/lerobot_runner.py`
- `any4lerobot/agibot2lerobot/agibot_h5.py`
- `any4lerobot/ds_version_convert/v30_to_v21/convert_dataset_v30_to_v21.py`
- `any4lerobot/ds_version_convert/v21_to_v30/convert_dataset_v21_to_v30.py`

## 当前结论

当前更符合“v2.1 每个 episode 一个 parquet，两个 episode 恰好大小相同”的正常现象，而不是重复写出。同大小不能单独证明内容重复。

## 下一步建议

如需最终确认，可对这两个 parquet 分别读取以下字段进行比对：

- `episode_index`
- `frame_index`
- `timestamp`
- 任一状态列如 `observation.states.joint.position`

若 `episode_index` 不同，则说明是两个独立 episode；若完全一致，再继续排查 metadata 切分是否异常。

---

## 追加工作：v2.1 转换后清理 v3.0 残留文件

### 问题背景

用户确认在 `...__lerobot_v21/data/chunk-000` 实际看到 `file-000.parquet` 与 `episode_000000.parquet` 同时存在，需要在 v2.1 转换后自动清理 v3.0 中间产物。

### 根因分析

此前实现只负责把 v3.0 数据拆成 v2.1 的 `episode_*.parquet`，但没有在成功后删除原始 `data/chunk-*/file-*.parquet`。因此最终目录会出现 v3.0 与 v2.1 两种布局混存。

### 改动方案

- 在 `src/data_converter/converters/lerobot_runner.py` 的 v2.1 版本后处理中增加 `_cleanup_v30_artifacts_from_v21_layout()`。
- 该清理同时覆盖：
  - `data/chunk-*/file-*.parquet`
  - `meta/episodes/chunk-*/file-*.parquet`
  - `meta/tasks*.parquet`
  - `meta/episodes_stats/chunk-*/file-*.parquet`
  - `videos/*/chunk-*/file-*.mp4`
- 为避免直接调用 fallback 时行为不一致，也在 `_fallback_convert_v30_to_v21()` 内补充同样清理。
- 新增回归断言：fallback 转换后 `file-000.parquet` 不应继续存在。

### 量化结果

- 新增 1 条回归断言，覆盖 v2.1 转换后数据目录不混入 v3.0 parquet。
- 目标测试通过 3 项。

### 验证方式

执行：

- `python -m pytest -q test\python\test_lerobot_version_fallback.py`
- `python -m pytest -q test\python\test_lerobot_video_embedding.py`

结果：

- `test_lerobot_version_fallback.py`: 1 passed
- `test_lerobot_video_embedding.py`: 2 passed

### 当前结论

v2.1 转换完成后会自动清理 v3.0 布局残留，避免 `file-000.parquet` 与 `episode_000000.parquet` 在最终输出中同时出现。

### 下一步建议

可在实际样本目录上再做一次 smoke 转换，确认 `...__lerobot_v21/data/chunk-000` 中只保留 `episode_*.parquet`。

---

## 追加工作：v2.1 扁平状态与图片 struct 需求计划

### 问题背景

用户提出新的 v2.1 parquet schema 目标：

- 将当前分散的 `observation.states.*` 合并为 `observation.state` 16 维向量
- 将 `actions` 作为同样的 16 维冗余向量单独保存
- 将当前视频转二进制列的逻辑改为“逐帧转图片，再将图片编码为字节”，并按 `observation.images.hand_left_color`、`observation.images.hand_right_color`、`observation.images.head_color` 三个 `struct<bytes, path>` 字段保存
- 其余转换链路尽量不改

### 根因分析

当前实现的目标 schema 与用户预期不一致，主要原因有两点：

1. 现有 v2.1 输出在后处理后仍保留了拆分状态字段体系，即 `observation.states.*` / `actions.*`。
2. 现有视频嵌入逻辑写入的是 `observation.frames.*` binary 列，而不是用户期望的 `observation.images.*_color` struct 列。

结合代码排查，最合适的改动入口在 `src/data_converter/converters/lerobot_runner.py`，因为 v2.1 fallback、parquet 重写、metadata 修复和校验都已集中在该文件中。

### 改动方案

- 新增中文实施计划文件：`docs/plans/2026-03-19-v21扁平状态与图片结构化存储实施计划.md`
- 计划采用“保持主链路不变、只在 v2.1 后处理阶段重写 schema”的策略：
  - 扁平化状态与动作列为 `observation.state` / 顶层 `actions`
  - 用逐帧 JPEG 字节 + 逻辑路径重写三路图片 struct 列
  - 同步更新 `meta/info.json` 和 v2.1 输出校验逻辑
- 参考用户提供脚本：`E:/Users/dyz/Documents/WXWork/1688858286666779/Cache/File/2026-03/convert_agibot_raw_to_lerobot.py`

### 量化结果

- 新增 1 份中文实施计划文档
- 明确拆分为 4 个任务阶段
- 明确列出目标字段、落点文件、回归测试与验收清单

### 验证方式

本轮采用静态分析与参考脚本对照：

- 检查当前仓库中的 v2.1 后处理入口与现有测试
- 阅读用户提供参考脚本中 `observation.state`、顶层 `actions`、`observation.images.*_color` 的写法
- 生成可执行的中文实施计划文档

### 当前结论

本次需求最稳妥的实现方式，是把改动集中在 `src/data_converter/converters/lerobot_runner.py` 的 v2.1 后处理阶段，而不是大范围改动 raw 适配主链。这样更符合“其余不做修改”的边界要求。

### 下一步建议

按计划先落测试，再实现 v2.1 parquet schema 重写与图片 struct 写入；执行时优先关注 metadata 与 parquet 一致性，以及视频帧数和 parquet 行数的对齐问题。

---

## 追加工作：统一 Markdown 文档默认使用中文

### 问题背景

用户要求后续在当前仓库中生成的 `.md` 文档统一默认使用中文书写，并希望将该规则写入 `AGENTS.md`，避免后续再次出现计划文档语言不一致的问题。

### 根因分析

此前仓库级规范中只约束了日志记录位置、内容结构和打包测试要求，但没有明确规定 Markdown 文档的默认语言。因此在生成计划文档时，容易出现标题或固定模板仍沿用英文的情况，与用户预期不一致。

### 改动方案

- 在 `AGENTS.md` 中新增“Markdown 文档规范”章节。
- 明确规定：本仓库后续新建或更新的 `.md` 文档默认使用中文编写，除非用户明确要求英文或原文保留英文。
- 同时补充例外说明：命令、路径、字段名、代码片段、外部接口名称等固定英文标识保持原样，不强行翻译。

### 量化结果

- 更新 1 个仓库规范文件：`AGENTS.md`
- 新增 3 条 Markdown 文档语言规则

### 验证方式

本轮采用静态校验：

- 读取并修改 `AGENTS.md`
- 确认新增章节已写入仓库规范文件
- 将本次改动同步记录到当天工作日志

### 当前结论

当前仓库已经具备明确的 Markdown 语言规范。后续生成实施计划、设计文档、工作记录和说明文档时，应默认使用中文。

### 下一步建议

后续若需要，我可以顺手把现有计划模板中的英文标题也逐步统一成中文风格，但命令、路径、字段名和代码标识建议继续保留英文原样。

---

## 追加工作：执行 v2.1 扁平状态与图片 struct 实施计划第一批

### 问题背景

开始执行 `docs/plans/2026-03-19-v21扁平状态与图片结构化存储实施计划.md` 的前 3 个任务，目标是先用测试锁定目标 schema，再实现 v2.1 parquet 的扁平状态重写与图片 struct 写入。

### 根因分析

原有实现存在两处与目标 schema 不一致的点：

1. v2.1 parquet 仍保留 `observation.states.*` / `actions.*` 的拆分字段。
2. 视频嵌入逻辑输出的是 `observation.frames.*` binary 列，而不是用户要求的 `observation.images.*_color` 的 `struct<bytes, path>`。

### 改动方案

- 新增测试文件 `test/python/test_lerobot_v21_schema_rewrite.py`，分别锁定：
  - `observation.state` / 顶层 `actions` 的扁平 16 维 schema
  - `observation.images.hand_left_color` / `hand_right_color` / `head_color` 的图片 struct schema
- 在 `src/data_converter/converters/lerobot_runner.py` 中新增并接入：
  - `_rewrite_v21_episode_parquet_schema()`
  - `_rewrite_v21_episode_parquet_file()`
  - `_build_v21_features()`
  - `_build_v21_image_struct_column_name()`
  - `_read_video_frames_as_image_structs()`
- 调整 v2.1 后处理流程，使其在转换完成后自动重写 episode parquet：
  - 将 `observation.states.joint.position` 压平为 `observation.state`
  - 将顶层 `actions` 写成与 `observation.state` 相同的 16 维冗余向量
  - 将三路视频逐帧编码为 JPEG 字节，并写入 `struct<bytes, path>` 图片列
  - 同步重写 `meta/info.json` 中的 feature 定义
- 更新 `test/python/test_lerobot_video_embedding.py`，补充 v2.1 图片 struct 回归测试。

### 量化结果

- 新增 1 个测试文件：`test/python/test_lerobot_v21_schema_rewrite.py`
- 扩展 1 个现有测试文件：`test/python/test_lerobot_video_embedding.py`
- 新增 6 个与本批次功能直接相关的验证用例通过

### 验证方式

执行：

- `python -m pytest -q test/python/test_lerobot_v21_schema_rewrite.py -k flat_state`
- `python -m pytest -q test/python/test_lerobot_v21_schema_rewrite.py -k image_struct`
- `python -m pytest -q test/python/test_lerobot_version_fallback.py`
- `python -m pytest -q test/python/test_lerobot_video_embedding.py`
- `python -m pytest -q test/python/test_lerobot_v21_schema_rewrite.py test/python/test_lerobot_version_fallback.py test/python/test_lerobot_video_embedding.py`

结果：

- 聚合验证 `6 passed in 1.14s`

### 当前结论

实施计划的前 3 个任务已经完成：

- 目标 schema 测试已建立
- v2.1 扁平状态/动作重写已实现
- 图片 struct 列写入已实现

当前代码已经能把目标 v2.1 parquet 重写成：

- `observation.state`
- `actions`
- `observation.images.hand_left_color`
- `observation.images.hand_right_color`
- `observation.images.head_color`

### 下一步建议

继续执行计划第 4 个任务，补上 v2.1 输出校验规则与更完整的端到端校验，确保运行真实转换链路时也以新 schema 作为成功标准。

---

## 追加工作：完成 v2.1 校验规则与完整验证

### 问题背景

在完成 v2.1 parquet schema 重写与图片 struct 写入后，还需要把正式输出校验逻辑同步切换到新 schema，避免真实转换完成后仍按旧字段体系判断成功与否。

### 根因分析

此前 `_validate_lerobot_output()` 对 v2.1 的判断只覆盖：

- 是否存在 parquet
- 是否存在 `episodes_stats.jsonl`

但并不会检查 parquet 是否真的已经是目标 schema，因此即使输出仍保留旧的 `observation.states.*` / `observation.frames.*`，也可能被错误放行。

### 改动方案

- 在 `src/data_converter/converters/lerobot_runner.py` 中新增 `_validate_v21_required_schema()`。
- 对 v2.1 输出增加 schema 校验，要求 `info.json` 与 parquet 至少同时具备以下目标字段：
  - `observation.state`
  - `actions`
  - `observation.images.hand_left_color`
  - `observation.images.hand_right_color`
  - `observation.images.head_color`
  - `timestamp`
  - `frame_index`
  - `episode_index`
  - `index`
  - `task_index`
- 在 `test/python/test_lerobot_v21_schema_rewrite.py` 中新增校验回归用例，直接调用 `_validate_lerobot_output(..., "v2.1")` 验证重写后的数据集能通过正式校验。

### 量化结果

- 新增 1 个 v2.1 schema 正式校验 helper
- 新增 1 条 v2.1 输出校验回归测试
- 相关聚合测试通过 7 项

### 验证方式

执行：

- `python -m pytest -q test/python/test_lerobot_v21_schema_rewrite.py test/python/test_lerobot_version_fallback.py test/python/test_lerobot_video_embedding.py`
- `python -m pytest -q`

结果：

- 聚合验证：`7 passed in 1.42s`
- 全量 `pytest -q`：存在 1 条与本次改动无关的历史收集错误

历史错误详情：

- `test/python/test_raw_video_mapping.py` 与仓库根目录 `test_raw_video_mapping.py` 同名，触发 pytest `import file mismatch`

### 当前结论

本次 v2.1 schema 改造已经完成并通过专项验证：

- 扁平 `observation.state`
- 顶层 `actions`
- 三路 `observation.images.*_color` 图片 struct 列
- v2.1 正式输出校验逻辑已同步切换到新 schema

当前唯一未通过的是仓库内既有的 pytest 收集冲突，不属于本次功能改动引入的问题。

### 下一步建议

如需让全量 `pytest -q` 也恢复通过，下一步可单独清理根目录与 `test/python/` 下的同名测试文件冲突，例如统一迁移或重命名仓库根目录的 `test_raw_video_mapping.py`。

---

## 追加工作：清理历史 pytest 冲突并恢复全量测试通过

### 问题背景

在完成 v2.1 schema 改造后，全量 `pytest -q` 先暴露出两类问题：

1. 历史测试收集冲突：仓库根目录与 `test/python/` 下同时存在 `test_raw_video_mapping.py`
2. 若干旧测试仍按旧 schema 断言，未同步到本次 16 维 joint 与 v2.1 扁平 schema 新行为

### 根因分析

- 根目录 `test_raw_video_mapping.py` 与 `test/python/test_raw_video_mapping.py` 同名，触发 pytest `import file mismatch`
- 根目录旧文件里还包含 3 条 `test/python/` 目录中缺失的测试，不能直接删而不合并
- `test/python/test_raw_to_any4_adapter.py` 仍按 14 维 joint 断言
- `test/python/test_lerobot_array_fidelity.py` 的 v2.1 断言仍按旧拆分列模型验证，而当前实现已切到 `observation.state` / 顶层 `actions` / 图片 struct 列
- 真实 v2.1 转换链路中，fallback 路径会先改写 `info.json`，导致后续图片列发现逻辑拿不到视频键，需要额外从文件系统回推视频源

### 改动方案

- 将根目录 `test_raw_video_mapping.py` 中缺失的 3 条测试合并到 `test/python/test_raw_video_mapping.py`
- 删除仓库根目录旧测试文件 `test_raw_video_mapping.py`
- 修正 `test/python/test_raw_to_any4_adapter.py` 的旧 14 维断言，改为 16 维 joint 断言
- 调整 `test/python/test_lerobot_array_fidelity.py`，使 v2.1 校验适配新 schema：
  - `observation.state`
  - 顶层 `actions`
  - 三路 `observation.images.*_color`
- 修正 `src/data_converter/converters/lerobot_runner.py`：
  - 真实 `v2.1` 转换默认执行 schema 重写，不依赖 `embed_videos_in_parquet` 开关
  - fallback 路径下通过 `_discover_v21_video_keys()` 从现有视频文件反推视频键，确保图片 struct 列能在真实转换中写出

### 量化结果

- 合并 3 条历史 raw adapter / video mapping 测试到正式测试目录
- 删除 1 个仓库根目录冲突测试文件
- 修正 2 个旧测试文件的 schema 断言
- 补强 1 条真实 v2.1 fallback 视频发现逻辑

### 验证方式

执行：

- `python -m pytest -q test/python/test_raw_video_mapping.py`
- `python -m pytest -q test/python/test_raw_to_any4_adapter.py test/python/test_lerobot_array_fidelity.py`
- `python -m pytest -q`

结果：

- `test/python/test_raw_video_mapping.py`: `9 passed`
- 全量 `pytest -q`: `52 passed, 4 skipped`

补充说明：

- 单独重复执行 `test_lerobot_array_fidelity.py::LerobotArrayFidelityTests::test_v21_preserves_arrays_after_conversion` 时，曾出现一次 any4 上游链路的偶发样本/路径错误：`there are some corrupted mp4s` / `[WinError 3] 系统找不到指定的路径`
- 但在完整测试套内重新执行时已通过，因此当前以全量测试结果为准，本次改动未阻断仓库测试主线

### 当前结论

当前仓库测试状态已恢复到可用水平：

- v2.1 扁平 schema 改造通过专项验证
- 历史 pytest 收集冲突已清理
- 全量 `pytest -q` 已通过

### 下一步建议

若后续还要继续增强稳定性，可单独为真实样本转换链路补一次“重复运行稳定性”测试，专门观察 any4 上游对临时视频路径的偶发失败是否需要额外兜底。

---

## 追加工作：v2.1 扁平状态与图片 struct 正式冒烟测试

### 问题背景

在完成代码实现和单元/集成测试后，需要补做一次基于真实样本的正式 smoke 测试，确认实际转换产物的 parquet 列名与类型已经符合目标 schema。

### 根因分析

此前已完成的主要是代码级验证与 pytest 回归验证，尚未形成一轮按仓库规范执行、可留档的真实样本 smoke 转换记录。

### 改动方案

- 选用仓库现有真实样本：
  - `D:\下载\windows-x64-agibot-isaac-downloader-真机真机\downloads\真机真机_2019353507798704130_20260306_181055\custom_task_pick_the_fruit_20260205181347.zip`
- 使用当前仓库代码执行一次完整 `v2.1` 转换
- 将产物写入 `test/smoke` 路径下的新 smoke 目录
- 读取生成的 `meta/info.json` 与 `data/chunk-000/episode_000000.parquet`，核对目标列名、列类型和行数

### 量化结果

- 成功完成 1 次真实样本 `v2.1` smoke 转换
- 产出 1 份 smoke 数据集
- 目标 parquet 行数：`2422`

### 验证方式

执行实际转换与结构检查，结果如下：

- 转换汇总：`total=1, success=1, failed=0, skipped=0`
- 生成 parquet 列：
  - `timestamp`
  - `frame_index`
  - `episode_index`
  - `index`
  - `task_index`
  - `observation.state`
  - `actions`
  - `observation.images.head_color`
  - `observation.images.hand_left_color`
  - `observation.images.hand_right_color`
- 类型检查：
  - `observation.state`: `fixed_size_list<element: float>[16]`
  - `actions`: `fixed_size_list<element: float>[16]`
  - 三路图片列均为：`struct<bytes: binary, path: string>`
- `meta/info.json` 中的 feature 集也已同步收敛为目标 schema：
  - `actions`
  - `episode_index`
  - `frame_index`
  - `index`
  - `observation.images.hand_left_color`
  - `observation.images.hand_right_color`
  - `observation.images.head_color`
  - `observation.state`
  - `task_index`
  - `timestamp`

### 当前结论

正式 smoke 测试通过。当前真实样本转换出的 v2.1 parquet 已符合目标结构：

- 扁平 `observation.state`
- 顶层 `actions`
- 三路 `observation.images.*_color` 的 `struct<bytes, path>`

### 下一步建议

如需发布前再补一道保障，可继续做一次“不同输出目录、重复运行两次”的稳定性 smoke，重点观察 any4 上游对临时路径和视频文件的偶发性问题是否还会出现。

---

## 追加工作：按期望格式对齐 info.json 并完成 test_case -> test_after 整体转换

### 问题背景

用户进一步要求 `meta/info.json` 不仅要通过当前实现，还要严格对齐到给定期望格式：

- `robot_type` 为 `agibot`
- `total_videos` 为 `0`
- 去掉 `video_embedding`
- `features` 中三路图片字段使用 LeRobot 风格的 `image` 元数据
- `observation.state` / `actions` 的 `names` 分别为 `joint_positions` / `joint_actions`

在确认对齐后，还需要使用 `test/test_case` 中的案例执行一次整体转换，输出到 `test/test_after`。

### 根因分析

此前实现虽然 parquet 实际内容已符合目标结构，但 `info.json` 仍停留在“与实际 Arrow struct 列一一对应”的描述方式：

- 图片特征写成 `struct<bytes, path>` 元数据
- `robot_type` 仍保留原值
- `total_videos` 按三路视频统计
- 附带 `video_embedding` 字段

这与用户期望的“对外表现为 image 特征的 metadata”不一致。

### 改动方案

- 修改 `src/data_converter/converters/lerobot_runner.py`：
  - `_rewrite_info_for_v21()` 中统一设置：
    - `robot_type = "agibot"`
    - `total_videos = 0`
    - 去掉 `video_embedding`
  - `_build_v21_features()` 改为输出期望的 image 风格 metadata：
    - `observation.images.hand_left_color`: `dtype=image`, `shape=[480, 848, 3]`
    - `observation.images.hand_right_color`: `dtype=image`, `shape=[480, 848, 3]`
    - `observation.images.head_color`: `dtype=image`, `shape=[720, 1280, 3]`
    - `observation.state.names = ["joint_positions"]`
    - `actions.names = ["joint_actions"]`
  - `_rewrite_v21_episode_parquet_schema()` 最终写回 metadata 时同步覆盖上述字段
- 重新执行真实样本 smoke，验证新 `info.json`
- 使用 `test/test_case/2.zip` 执行一次完整 `v2.1` 转换，输出到 `test/test_after`

### 量化结果

- 完成 1 次新的真实样本 smoke 转换
- 完成 1 次 `test/test_case -> test/test_after` 整体转换
- `test/test_after/2__lerobot_v21` 生成 parquet 行数：`589`

### 验证方式

1. 真实样本 smoke 验证：
- 转换成功：`total=1, success=1, failed=0`
- 新 `info.json` 已对齐为目标格式：
  - `robot_type = "agibot"`
  - `total_videos = 0`
  - 无 `video_embedding`
  - 图片特征为 `dtype=image`
  - `observation.state.names = ["joint_positions"]`
  - `actions.names = ["joint_actions"]`

2. 整体转换验证：
- 输入：`test/test_case/2.zip`
- 输出：`test/test_after/2__lerobot_v21`
- 转换成功：`total=1, success=1, failed=0, skipped=0`
- 输出 `info.json` 已符合目标格式
- 输出 parquet 列为：
  - `timestamp`
  - `frame_index`
  - `episode_index`
  - `index`
  - `task_index`
  - `observation.state`
  - `actions`
  - `observation.images.head_color`
  - `observation.images.hand_left_color`
  - `observation.images.hand_right_color`

### 当前结论

当前代码已经完成 metadata 对齐：

- `info.json` 对外格式已符合用户给定期望
- parquet 实际列结构与 metadata 同步满足当前目标
- `test/test_case/2.zip` 到 `test/test_after` 的整体转换已成功完成

### 下一步建议

如果后续需要对“多 episode、多 task”的批量目录继续验证，可直接在 `test/test_case` 中补更多样例 zip，再复用同样的 `test/test_after` 路径做批量回归。
