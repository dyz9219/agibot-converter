# 2026-03-12 单任务压缩与并发矩阵分析

## 1. 背景

在 2026-03-11 的连续优化后，单任务真实样本端到端耗时已经降到：

- `12.55s`

当天遗留的核心问题有两个：

1. 是否还能继续把单任务压到 `10s` 以内
2. 为什么之前观察到：
   - `8` 并发似乎不如 `4`
   - 前端看起来像“一个一个完成”

因此今天的重点转成两条：

1. 继续压单任务热点
2. 给并发矩阵补任务级时间埋点

## 2. 继续优化：轻量运行时探测收尾

在继续 profile 后，发现本地源码模式下的 `any4_health` 轻量探测仍然会触发 `ray` 深层子模块 spec 探测，导致：

- 轻量检查并不真的“轻”
- `ray` 包初始化成本又被带回到单任务路径

### 根因

旧逻辑在 lightweight 模式下仍会做：

- `find_spec("ray.thirdparty_files.psutil")`
- `find_spec("ray.thirdparty_files.psutil._psutil_windows")`

这类探测会触发 `ray` 包级解析路径。

### 修复

修改文件：

- `src/data_converter/any4_health.py`
- `test_any4_health.py`

修复方式：

1. 仍检查：
   - `psutil`
   - `psutil._psutil_windows`

2. 对 `ray` 不再探测深层子模块 spec

3. 改为：
   - 获取顶层 `ray` 包目录
   - 直接检查磁盘上是否存在：
     - `thirdparty_files/psutil/`
     - `_psutil_windows.*`

### TDD

新增测试：

- `test_lightweight_psutil_probe_avoids_ray_submodule_spec_imports`

### 量化结果

同一真实样本：

- `custom_task_pick_the_fruit_20260205181347.zip`

优化前：

- `12.55s`

优化后：

- `12.16s`

本轮收益：

- 缩短 `0.39s`

## 3. 继续优化：PyAV 解码线程

继续拆分 `compute_episode_stats` 后发现：

- 统计计算本身不重
- 主要时间花在视频采样 `_sample_video_frames()`

### 真实拆分结果

对真实样本输出中的 3 路视频逐路计时：

- `observation.images.hand_left`
  - `sample=1.0581s`
  - `stats=0.0870s`
- `observation.images.hand_right`
  - `sample=0.8299s`
  - `stats=0.0965s`
- `observation.images.head`
  - `sample=2.0692s`
  - `stats=0.0653s`

结论：

- `get_feature_stats` 本身只有 `0.06s ~ 0.09s`
- 真正重的是 `PyAV` 视频解码采样

### 最小实验

对最慢的 `head` 路视频做实验，仅改 codec 线程参数：

- `thread_type = "AUTO"`
- `thread_count = 0`

实验结果：

- 默认解码：约 `1.98s ~ 2.15s`
- 开启 auto threading：约 `0.39s ~ 0.51s`

### 修复

修改文件：

- `any4lerobot/agibot2lerobot/agibot_utils/lerobot_utils.py`
- `test_any4_compute_stats.py`

新增 helper：

- `_configure_video_decoder(stream)`

在 `_sample_video_frames()` 中，打开视频后立即调用该 helper。

### 一致性判断

这次修改不会改变最终数据语义。

原因：

1. 采样的 frame index 没变
2. 统计公式没变
3. 归一化逻辑没变
4. 输出 parquet / meta / mp4 逻辑没变

改变的只是：

- 解码时 FFmpeg 如何使用 CPU 线程

### TDD

新增测试：

- `test_configure_video_decoder_enables_auto_threading`

### 量化结果

同一真实样本：

优化前：

- `12.16s`

优化后：

- `9.56s`

本轮收益：

- 缩短 `2.60s`

这也是第一次把单任务端到端真实 wall time 压进：

- `10s` 以内

## 4. 当前单任务结果

同一份真实样本端到端耗时演进如下：

1. 初始观测：
   - `73.5s`
2. 修正 any4 root + 列式路径后：
   - `25.77s`
3. 本地统计实现 + internal CLI 惰性加载后：
   - `21.78s`
4. 单任务 in-process any4 后：
   - `12.72s`
5. 运行前 health probe 轻量化后：
   - `12.16s`
6. PyAV 解码线程优化后：
   - `9.56s`

相对初始值累计收益：

- `73.5s -> 9.56s`
- 共缩短 `63.94s`
- 速度提升约 `7.69x`

## 5. 任务级时间埋点

为了确认并发矩阵中的真实执行形态，本轮给每个任务新增了 manifest 级时间字段。

### 修改文件

- `src/data_converter/models.py`
- `src/data_converter/backend.py`
- `src/data_converter/manifest.py`
- `test_backend_concurrency.py`

### 新增字段

- `started_at`
- `finished_at`
- `elapsed_seconds`

这些字段会写入每个任务的：

- `manifest.json`

### TDD

新增测试：

- `test_manifest_records_task_timing_fields`

验证：

- manifest 中必须出现上述 3 个字段

## 6. 并发矩阵复跑

基于新的任务级埋点，重新对真实 15 个 zip 数据执行矩阵测试。

输入目录：

- `D:\下载\windows-x64-agibot-isaac-downloader-真机真机\downloads\真机真机_2019353507798704130_20260306_181055`

### 总耗时结果

- `concurrency=4`: `60.29s`
- `concurrency=8`: `47.09s`
- `concurrency=16`: `43.47s`

所有配置下：

- `total=15 success=15 failed=0 skipped=0`

## 7. 任务级分布分析

### `concurrency=4`

- 总跨度：`59.22s`
- 峰值并发：`4`
- 单任务 `P50`: `15.54s`
- 单任务 `P95`: `16.53s`

观察：

- 典型 4 路分波执行
- 第一波 4 个任务同时启动
- 后续任务按空闲槽位补位

### `concurrency=8`

- 总跨度：`46.01s`
- 峰值并发：`8`
- 单任务 `P50`: `23.51s`
- 单任务 `P95`: `23.53s`

观察：

- 第一波 8 个任务同时启动
- 单任务时延明显高于 `4`
- 但总吞吐优于 `4`

### `concurrency=16`

- 总跨度：`42.29s`
- 峰值并发：`15`
- 单任务 `P50`: `41.57s`
- 单任务 `P95`: `41.86s`

观察：

- 15 个任务几乎同时启动
- 基本是一波全部跑完
- 总吞吐最好，但单任务时延最差

## 8. 结论

### 结论 A：当前版本下，`8` 并不比 `4` 慢

带埋点的最新真实数据表明：

- `8` 明显优于 `4`

因此之前“8 不如 4”的现象，不是当前版本的稳定结论。

### 结论 B：后端并发是真正生效的

manifest 时间线已经证明：

- `4`、`8`、`16` 都存在真实并发启动
- 不是串行执行

### 结论 C：现在是标准的吞吐/时延权衡

并发越高：

- 单任务 latency 越高
- 总吞吐越好

这不是“线程控制失效”，而是资源争用下的正常现象。

### 结论 D：前端“一个一个完成”的观感不代表后端串行

从 manifest 看，后端明显在并发执行。  
因此前端观感更可能来自：

- UI 刷新节奏
- 任务完成事件呈现方式
- 页面只按任务列表顺序刷新，而不是强调并发波次

## 9. 当前剩余问题

当前还值得继续探索的点：

1. 前端为什么看起来像逐个完成
2. 是否还存在可以继续削减的第三方导入成本：
   - `torch`
   - `datasets`
3. 是否需要按机器资源给出“推荐并发值”

## 10. 当前阶段总结

截至 2026-03-12：

1. 单任务目标
- 已达成：
  - `9.56s`

2. 并发控制
- 已确认真实生效

3. 并发矩阵
- 已确认：
  - `8` 优于 `4`
  - `16` 总吞吐最好

4. 当前最合理的下一步
- 排查前端进度呈现链路，而不是继续怀疑后端串行

## 11. 多 Worker 四并发标准单元实现

在方案确认后，本轮开始落地“多 worker 子进程 + 固定 4 并发标准单元”。

### 实现目标

1. 保留现有单任务转换链路，避免结果漂移
2. 把 `8/16/...` 从“一个大池子里的更高并发”改成“多个固定 4 并发 worker”
3. 前端只允许 `4` 的倍数

### 已完成改动

修改文件：

- `src/data_converter/models.py`
- `src/data_converter/backend.py`
- `src/data_converter/main.py`
- `test_backend_concurrency.py`

核心行为：

1. 前端并发下拉改成：
- `4, 8, 12, ..., 40`

2. 后端新增 worker shard 路径：
- 当 LeRobot 任务数大于 1 且并发大于等于 `8` 时
- 按 `4` 为标准单元切成多个 worker shard

3. 新增 internal worker CLI：
- `--internal-run-worker-shard`

4. 每个 worker 子进程处理自己的任务切片
- 单个 worker 内部固定按 `4` 并发运行

5. 任务切片策略：
- 按数量平均切片

### TDD 与回归

新增/更新测试：

- `test_lerobot_uses_worker_shards_for_multiple_of_four_concurrency`
- `test_non_multiple_of_four_concurrency_is_clamped_to_lower_worker_multiple`
- `test_frozen_bundled_lerobot_uses_worker_shards`

回归结果：

```powershell
python -m pytest -q test_any4_health.py test_backend_concurrency.py test_any4_batch_episode.py test_any4_compute_stats.py test_raw_video_mapping.py
```

结果：

- `26 passed`

## 12. 结果一致性验证

这是本轮最关键的验收点：调度变了，但输出结果不能变。

### 验证方法

取 4 个真实 zip 组成子集：

- `subset4-input`

分别运行：

- `concurrency=4`
- `concurrency=8`

然后逐任务比对：

- `meta/info.json`
- `meta/stats.json`
- parquet 文件数量与 SHA-256
- mp4 文件数量与 SHA-256

### 验证结果

4 个真实任务全部满足：

- `info_equal = True`
- `stats_equal = True`
- `parquet_sha_equal = True`
- `video_sha_equal = True`

结论：

- 当前多 worker 实现没有改变最终转换结果
- 本轮改动只改变了调度方式，没有破坏数据一致性

## 13. 实现后的最新矩阵

重新对真实 15 个 zip 跑矩阵：

- `concurrency=4`: `62.28s`
- `concurrency=8`: `47.83s`
- `concurrency=16`: `44.50s`

所有配置下：

- `total=15 success=15 failed=0 skipped=0`

### 当前结论

1. 多 worker 分片已经真正生效
2. `8` 仍然明显优于 `4`
3. `16` 仍然优于 `8`，但增益有限
4. 当前瓶颈已经从“并发语义错误”进一步收敛为：
- worker 间共享的 CPU / I/O / 导入成本

也就是说，本轮已经把架构从“一个大池子”推进到了“标准 4 并发单元复制”，并且没有破坏输出一致性。

## 14. Worker 级埋点补齐

为了继续判断“共享开销到底在 worker 启动，还是在任务内部”，本轮把埋点继续补齐到 worker 层。

### 修改文件

- `src/data_converter/backend.py`
- `src/data_converter/worker_shard_cli.py`
- `test_backend_concurrency.py`

### 新增内容

1. worker payload 增加：
- `worker_index`
- `summary_path`

2. worker 子进程执行完成后，额外写入：
- `.worker-results/worker_XX.json`

3. 主控在同步 manifest 时，补回：
- `stage_timings`

### TDD 与回归

新增/更新测试：

- `test_worker_payload_includes_summary_path`
- `test_sync_tasks_from_manifests_copies_stage_timings`

全量回归：

```powershell
python -m pytest -q test_any4_health.py test_backend_concurrency.py test_any4_batch_episode.py test_any4_compute_stats.py test_raw_video_mapping.py
```

结果：

- `29 passed`

## 15. Worker 启动成本与任务内部成本拆分

使用 4 个真实 zip 子集再次执行：

- `concurrency=8`

输出目录：

- `smoke-runs/subset4-c8-worker-metrics`

### Worker 级结果

- `worker_00 elapsed_seconds = 15.575640`
- `worker_01 elapsed_seconds = 15.794219`

每个 worker 都处理：

- `2` 个任务

### 任务级结果

4 个 manifest 的任务耗时大致为：

- `15.57s`
- `15.79s`
- `15.57s`
- `15.79s`

其中 `run_any4lerobot` 单段耗时大致为：

- `14.59s`
- `14.93s`
- `14.70s`
- `14.87s`

统计值：

- `elapsed_seconds P50 = 15.6815s`
- `run_any4lerobot P50 = 14.7887s`

### 结论

这轮结果非常明确：

1. worker 启动和收尾不是主要瓶颈
2. 任务总耗时与 worker 总耗时几乎重合
3. 当前 8 并发路径下，真正的大头仍然是：
- `run_any4lerobot`

也就是说，继续优化时不应该再优先怀疑：

- worker 外层调度
- worker 启动壳层
- manifest 同步

下一刀应该继续打在：

- any4 主转换内部
- 以及它带出的共享 CPU / I/O 成本

## 16. 热 Worker 进程池优化

为了继续压缩 8/16 并发下多出来的额外耗时，本轮继续把 worker 内部从“线程池 + 每任务再起 Python 子进程”推进到“长驻进程池 + 进程内直接跑 any4”。

### 设计目标

1. 每个 4 并发标准单元内部，尽量复用已加载的 any4 运行时
2. 避免每个任务都重新走一次 Python 子进程桥接
3. 不改变转换逻辑与最终输出内容

### 修改文件

- `src/data_converter/backend.py`
- `src/data_converter/any4lerobot_bridge.py`
- `test_backend_concurrency.py`
- `test_raw_video_mapping.py`

### 实现内容

1. worker 模式下，LeRobot 任务改走：
- `ProcessPoolExecutor(max_workers=4)`

2. 每个子进程启动时先执行：
- `preload_any4_runtime()`

3. 子进程内直接调用：
- `run_lerobot_task()`

4. 每个子进程任务执行完成后，直接写自己的 `manifest.json`

### 中途暴露的问题与修复

新热 worker 路径第一次真实运行时失败，根因不是转换逻辑，而是旧的临时视频目录实现存在并发竞态：

- 所有进程共享同一个 `.tmp-any4-video`
- 多进程同时创建/删除同名目录时会出现：
  - `[WinError 183] 当文件已存在时，无法创建该文件`
  - `No such file or directory`

修复方式：

1. 临时视频目录改成按进程隔离：
- `.tmp-any4-video/pid-<pid>/...`

2. 清理时只删除当前进程自己的临时目录

### TDD 与回归

新增/更新测试：

- `test_worker_mode_uses_process_pool_for_lerobot`
- `test_cleanup_any4_temp_video_dir_removes_temp_root`

全量回归：

```powershell
python -m pytest -q test_any4_health.py test_backend_concurrency.py test_any4_batch_episode.py test_any4_compute_stats.py test_raw_video_mapping.py
```

结果：

- `30 passed`

## 17. 热 Worker 优化后的真实结果

### 4 个真实 zip 子集，`concurrency=8`

旧实现结果：

- 总耗时约 `17.95s`
- 任务 `elapsed_seconds P50 ≈ 15.68s`
- 任务 `run_any4lerobot P50 ≈ 14.79s`

新实现结果：

- 总耗时约 `16.6s`
- 任务 `elapsed_seconds P50 ≈ 3.72s`
- 任务 `run_any4lerobot P50 ≈ 3.52s`

说明：

1. 单任务内部的真实 any4 执行时间被大幅压缩
2. 当前 4 包场景下，worker 自身仍有明显热启动成本
3. 4 包任务量太小，热启动收益还没有完全摊薄

### 15 个真实 zip，全量矩阵

旧实现：

- `concurrency=8`: `47.83s`
- `concurrency=16`: `44.50s`

新实现：

- `concurrency=8`: `28.3s`
- `concurrency=16`: `44.1s`

### 量化收益

对 `concurrency=8`：

- `47.83s -> 28.3s`
- 缩短 `19.53s`
- 速度提升约 `1.69x`
- 总耗时下降约 `40.8%`

对 `concurrency=16`：

- `44.50s -> 44.1s`
- 只有轻微改善

### 结论

1. 热 worker 进程池对 `8` 并发的大批量吞吐是有效的
2. 当前 `16` 并发没有继续显著提升，说明：
- 更高 worker 数带来的热启动成本
- 以及共享 CPU / I/O 成本
- 在 15 个任务规模下已经把收益吃掉了

3. 这也解释了为什么：
- `8` 现在变得非常值得
- `16` 还没有被一起打下来

## 18. 一致性验证

将新实现的 4 包输出与旧 worker 版本输出逐任务对比：

- `meta/info.json`
- `meta/stats.json`
- parquet SHA-256
- mp4 SHA-256

结果：

- 4 个任务全部一致

结论：

- 本轮热 worker 进程池优化仍然没有改变最终转换结果
- 修改只影响执行方式和性能，不影响数据内容

## 19. 16 并发进一步优化尝试：源码模式 direct process pool

在热 worker 方案下，`8` 并发已经显著下降到：

- `28.3s`

但 `16` 并发仍然停留在：

- `44.1s`

因此本轮又尝试了一条新的实验性路线：

- 在源码模式下跳过外层 worker shard 子进程
- 直接由主控创建一个全局 `ProcessPoolExecutor`
- 让所有 LeRobot 任务直接进入同一个预热进程池

### 预期

理论上，这可以减少：

- 外层 4 个 worker shard 进程的管理开销
- 每个 worker 再各自建池的嵌套启动成本

### 实测结果

真实 15 个 zip 数据：

- `direct process pool, concurrency=8`: `45.3s`
- `direct process pool, concurrency=16`: `61.5s`

对比现有稳定方案：

- 热 worker `concurrency=8`: `28.3s`
- 热 worker `concurrency=16`: `44.1s`

### 结论

这条路线明显更差，因此没有保留。

推断原因：

1. 全局直接进程池在当前机器上会触发更重的集中式 spawn / import 风暴
2. 相比“2 个或 4 个 worker shard 各自维护小池”，集中建大池在 Windows 上更容易放大启动争用

### 处理结果

1. 已回退 direct process pool 代码
2. 当前仓库保留的仍然是上一版稳定的热 worker 方案
3. 回退后验证：

```powershell
python -m pytest -q test\python\test_backend_concurrency.py test\python\test_any4_health.py test\python\test_any4_batch_episode.py test\python\test_any4_compute_stats.py test\python\test_raw_video_mapping.py
```

结果：

- `30 passed`
