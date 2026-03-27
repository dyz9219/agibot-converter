# 2026-03-11 并发预算与启动开销优化记录

## 1. 背景

在前一轮修复后，线程控制已经恢复生效：

- 打包态不再被强制串行
- 前端选择的并发值可以真正传到后端执行链路

但新的现象是：

- 单个任务独占资源时，转换可以达到几秒级
- 一旦恢复多线程，多任务虽然能同时执行
- 但每个任务的完成时间又明显变慢

用户提出的核心疑问是：

- 这是不是“跷跷板问题”
- 是否存在方案，既保持单任务低延迟，又支持多任务并发提升总吞吐

## 2. 现象描述

当前表现并不是“线程没生效”，而是：

1. 单任务性能已经足够好
2. 多任务并发也已经能跑起来
3. 但并发后单任务延迟恶化

也就是说，问题从“并发失效”转移成了：

- 并发是否被正确调度
- 资源是否被错误放大

## 3. 根因分析

### 根因 A：并发预算被重复使用

排查链路：

- 前端并发值进入 `ConversionOptions.concurrency`
- 后端外层 `ThreadPoolExecutor` 使用该值
- 同时同一个值又被传给 any4 的 `--cpus-per-task`
- any4 非 debug 路径再将该值用于内部 Ray 并发

这会导致：

- 外层任务并发使用 `N`
- 内层 any4 并发也使用 `N`
- 实际资源使用接近 `N x N`

这不是正确的“总并发预算”，而是预算重复透支。

### 根因 B：单任务 debug 路径仍然携带重型模块启动成本

继续检查 `any4lerobot/agibot2lerobot/agibot_h5.py`，发现：

- 模块顶层直接导入 `ray`
- 模块顶层直接导入 `torch`

即使当前任务最终走的是 debug 单任务路径，这两个重型模块也会在子进程启动时被加载。

单任务独占资源时，这个成本相对可接受。  
但多个任务同时启动时，会放大为：

- 模块导入争用
- 磁盘读取争用
- 动态链接和初始化争用

最终表现就是：

- 单任务快
- 并发启动多个任务时，每个任务都变慢

## 4. 结论

这不是“天然跷跷板”，而是当前实现方式导致的结果。

真正的问题不是线程数本身，而是：

1. 同一个并发预算被外层和内层重复使用
2. 单任务路径仍承担不必要的重型启动成本

## 5. 本次优化方案

### 优化 A：统一并发预算

修改文件：

- `src/data_converter/models.py`
- `src/data_converter/backend.py`
- `src/data_converter/converters/lerobot_runner.py`

实现方式：

1. 为 `TaskPlan` 新增：
   - `lerobot_inner_concurrency`

2. 在后端执行前统一分配并发预算：

- 如果是普通多文件 / 多 zip 任务集：
  - 外层并发 = 用户设置值
  - 每个任务内部并发 = `1`

- 如果只有一个 any4 数据集，并且内部含多个 `task_info/*.json`：
  - 外层并发 = `1`
  - 内层 any4 并发 = `min(用户预算, 实际 task_info 数量)`

3. any4 参数构造时，不再无条件使用：
   - `options.concurrency`

而是改为使用：

- `task.lerobot_inner_concurrency`

这样就不会再出现：

- 外层 `10`
- 内层也 `10`
- 导致整体资源超卖

### 优化 B：减少 debug 单任务路径的启动成本

修改文件：

- `any4lerobot/agibot2lerobot/agibot_h5.py`

实现方式：

1. `ray` 改为仅在非 debug 路径延迟导入
2. `torch` 改为按需延迟导入
3. 普通 numpy 数据不再因为类型检查触发 `torch` 导入

优化目的：

- 单任务 debug 路径不再无意义加载 `ray`
- 多个任务并发启动时，减少重型模块初始化争用

## 6. 当前收益

这次优化的直接收益是：

1. 并发语义变得自洽
- 用户设置的是“总并发预算”
- 不再被错误解释为“每层都各自拿一份预算”

2. 单任务启动更轻
- debug 单任务路径不再默认背负 `ray` 顶层导入成本

3. 更接近用户预期目标
- 单任务尽量保持在低延迟
- 多任务并发时提升总吞吐
- 避免“线程越高，单任务越慢得离谱”的放大效应

## 7. 代码级验证

本次新增/更新验证点：

- 单一多任务 any4 source 时：
  - 外层 worker 会收敛到 `1`
  - 内层并发会按 task 数和总预算计算

- any4 CLI 的 `--cpus-per-task`：
  - 不再盲目等于前端总并发值
  - 改为使用每个任务实际分配到的内部并发预算

- 单任务 debug / 多任务非 debug 分支：
  - 仍保持正确分支行为

已通过验证：

```powershell
python -m pytest -q test_backend_concurrency.py test_any4_batch_episode.py test_any4_compute_stats.py test_any4_health.py test_raw_video_mapping.py
```

结果：

- `18 passed`

已通过编译检查：

```powershell
python -m py_compile src\data_converter\backend.py src\data_converter\converters\lerobot_runner.py src\data_converter\models.py any4lerobot\agibot2lerobot\agibot_h5.py test_backend_concurrency.py
```

## 8. 尚未完成但值得继续优化的点

当前还有一个明显热点没有在本轮直接改动：

- `any4lerobot/agibot2lerobot/agibot_utils/agibot_utils.py`
- `load_local_dataset()`

这个函数仍然会：

- 先把整集数据展开成 `frames: list[dict]`
- 产生大量 Python 对象
- 带来额外内存分配和对象构造开销

后续如果要进一步逼近：

- 单任务稳定 `10s` 内
- 并发下吞吐继续提升

这个位置是下一刀最值得优化的热点之一。

## 9. 当前结论

今天这轮优化已经把问题从“线程数表面调参”推进到“并发预算正确分配”。

结论是：

- 可以同时追求“单任务更快”和“多任务并发更高吞吐”
- 但前提不是盲目增大线程数
- 而是统一总预算、避免内外层重复放大、降低单任务启动成本

下一步建议：

1. 用真实数据做并发矩阵测试：
   - `1 / 4 / 8 / 16`
2. 分别记录：
   - 单任务耗时
   - 总耗时
   - 吞吐量
3. 根据结果再决定：
   - 是否继续优化 `load_local_dataset()`
   - 是否还要进一步拆分 I/O 热点

## 10. 后续继续优化的实际发现

在继续优化过程中，又定位到两个比预期更关键的问题。

### 发现 A：端到端路径最初没有实际使用仓库内的 any4 代码

排查发现：

- `find_any4lerobot_root()` 的候选根目录顺序中
- `here.parents[3] / "any4lerobot"`（仓库外同级目录）
- 竟然排在仓库内 `agibot-converter/any4lerobot` 之前

导致实际运行时拿到的是：

- `D:\workspace\work\bwy\any4lerobot`

而不是当前仓库里的：

- `D:\workspace\work\bwy\agibot-converter\any4lerobot`

这直接解释了一个看起来很反常的现象：

- 仓库里已经做了性能优化
- 但端到端耗时几乎没有改善

根因并不是优化无效，而是运行时根本没吃到新代码。

### 修复：调整 any4 根目录优先级

修改文件：

- `src/data_converter/any4lerobot_locator.py`

修复内容：

优先使用当前仓库内的：

- `D:\workspace\work\bwy\agibot-converter\any4lerobot`

再考虑仓库外同级目录：

- `D:\workspace\work\bwy\any4lerobot`

这样后续性能优化才会真正作用到真实执行路径。

### 发现 B：`frames: list[dict]` 中间态仍然造成明显额外开销

原始链路是：

1. `load_local_dataset()` 把整集展开为 `frames: list[dict]`
2. `build_episode_data()` 再把这些 frame 重新 `stack` 回列式数组

这会产生：

- 大量 Python dict / list 对象
- 额外的逐帧对象构造
- 再次堆叠回 numpy 的双重开销

### 修复：改为列式 episode_data 构造

修改文件：

- `any4lerobot/agibot2lerobot/agibot_utils/agibot_utils.py`
- `any4lerobot/agibot2lerobot/agibot_h5.py`

修复内容：

1. 新增 `load_local_episode_columns()`
   - 直接返回列式数组
   - 不再强制先构造 `frames: list[dict]`

2. 新增 `AgiBotDataset.build_episode_data_from_columns()`
   - 直接从列式数组构建 `episode_data`
   - 避免“先拆散再 stack 回来”

3. `save_as_lerobot_dataset()` 改为走列式路径

### 代码级回归

新增等价性验证：

- `test_build_episode_data_from_columns_matches_frame_path`

已通过：

```powershell
python -m pytest -q test_any4_health.py test_backend_concurrency.py test_any4_batch_episode.py test_any4_compute_stats.py test_raw_video_mapping.py
```

结果：

- `20 passed`

### 真实样本端到端量化结果

测试样本：

- `custom_task_pick_the_fruit_20260205181347.zip`

实测结果：

- 在修 locator 之前，端到端耗时约 `73.5s`
- 修复 locator 并使用仓库内优化代码后，端到端耗时约 `25.77s`

量化收益：

- 总计缩短约 `47.73s`
- 速度提升约 `2.85x`

## 11. 继续优化：视觉统计、本地统计实现与 internal CLI 启动

在把端到端耗时压到 `25.77s` 之后，又继续做了三轮更细的优化。

### 优化 A：把视频采样从 `torchvision.VideoReader` 改为 `pyav`

修改文件：

- `any4lerobot/agibot2lerobot/agibot_utils/lerobot_utils.py`

作用：

- 降低视觉统计链路的额外依赖和转换成本
- 避免“全量堆叠所有帧再抽样”的浪费

### 优化 B：把项目使用到的统计逻辑本地化

修改文件：

- `any4lerobot/agibot2lerobot/agibot_utils/lerobot_utils.py`

目的：

- 避免为了计算统计值而引入整条 `lerobot.datasets.compute_stats -> torch` 重型链路
- 保留统计公式语义，减少启动成本

### 优化 C：把 `flet` 从 internal CLI 路径中剥离

修改文件：

- `src/data_converter/main.py`

修改后：

- 顶层不再直接导入 `flet`
- 仅在真正启动 UI 时才导入 `flet`

## 12. 3 月 11 日阶段性结果

截至 2026-03-11，单任务端到端耗时演进为：

1. 初始观测：
   - `73.5s`
2. 修正 any4 root 选择 + 列式 episode_data 路径后：
   - `25.77s`
3. 本地统计实现 + internal CLI 惰性加载 `flet` 后：
   - `21.78s`
4. 单任务本地源码路径允许 in-process any4 后：
   - `12.72s`
5. 再将运行前 any4 health probe 改为本地源码轻量检查后：
   - `12.55s`

相对初始值的总收益：

- `73.5s -> 12.55s`
- 共缩短约 `60.95s`
- 速度提升约 `5.86x`

截至 3 月 11 日的结论：

1. 之前的大部分慢，不是单一点瓶颈
2. 单任务端到端已经从一分钟级压到十几秒级
3. 后续是否还能稳定进 `10s` 内，需要继续看：
   - `compute_episode_stats`
   - `_save_episode_data`
   - parquet / arrow 落盘路径
   - staged 相关 I/O
