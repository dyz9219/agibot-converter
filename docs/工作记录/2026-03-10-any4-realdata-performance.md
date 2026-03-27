# Any4 真机数据转换性能排查记录

日期：2026-03-10

## 一、问题背景

本次排查要回答三个问题：

- 当前 Agibot -> any4lerobot -> LeRobot 的转换耗时是否正常？
- 主要耗时点到底在哪里？
- 现在看到的慢，属于实现本身的成本，还是存在异常问题？

最初的现象是：

- 之前观察到单个大约 20 MB 的输入，转换可能需要约 57 秒
- 外部有一个更快的估算值，导致怀疑当前实现是否异常偏慢
- 因此需要先基于真实真机数据做本地复现，再决定是修 bug 还是做性能优化

## 二、使用的数据集

输入目录：

- `D:\下载\windows-x64-agibot-isaac-downloader-真机真机\downloads\真机真机_2019353507798704130_20260306_181055`

实际观察到的数据形态：

- 共 15 个 zip 文件
- 总大小约 0.13 GB
- 每个 zip 都是 Agibot 原始包
- 抽样看到的 zip 内容基本是：
  - 1 个 `aligned_joints.h5`
  - 1 个 `state.json`
  - 3 个 mp4

代表性样本：

- `custom_task_pick_the_fruit_20260205181347.zip`
- `custom_task_pick_the_fruit_20260205181422.zip`
- `custom_task_pick_the_fruit_20260205181449.zip`

## 三、Precheck 结果

对这批真机数据运行预检，结果如下：

- 发现任务数：15
- ready：15
- blocked：0
- skipped：0

预警信息：

- 运行时模式为 `bundled`
- 输入被识别为 Agibot 原始包，会先适配成 any4 结构，再执行 LeRobot 转换
- 15 个任务全部被识别为 Windows 长路径高风险任务，因此都会走 staged 短路径中转

这说明：

- 这批数据本身对当前转换流程是有效的
- 运行路径是可复现且稳定的
- 长路径规避策略对这批数据是预期行为，不是异常

## 四、先发现的环境问题

在正式下性能结论之前，先发现了一个真实的环境问题：

- `pip install -e .` 会把 `numpy` 升到 2.x
- 当前机器上的 `h5py` 是按 1.x ABI 编译的
- 导致导入 `h5py` 时直接报错：
  - `ValueError: numpy.dtype size changed, may indicate binary incompatibility`

根因：

- 项目依赖里写的是 `numpy>=1.26`
- 没有上限，导致在当前机器上安装出一个和 `h5py` 不兼容的组合

已修复：

- `pyproject.toml` 中将：
  - `numpy>=1.26`
- 改为：
  - `numpy>=1.26,<2`

这个修复和性能无关，但它是必要修复，因为不修的话，真机转换在进入流程前就可能直接失败。

## 五、端到端实测耗时

### 1. 单个样本 zip

样本：

- `custom_task_pick_the_fruit_20260205181347.zip`

实测耗时：

- 一次成功运行约 57.4 秒
- 多次重复本地运行大约落在 50.6 到 59.0 秒之间

结果：

- 转换成功
- `manifest.json` 正常生成
- LeRobot 输出正常写出

### 2. 整批 15 个 zip

运行配置：

- `concurrency=4`
- target = `lerobot`
- version = `v3.0`

实测耗时：

- 总耗时约 291.42 秒
- 即约 4 分 51 秒

结果：

- 15 / 15 全部成功
- 无失败任务

## 六、阶段级拆分结果

首先对外层流程做了分阶段计时。

针对单个样本 zip，外层包装的阶段耗时结论如下：

- `_materialize_source`：几乎可以忽略
- `prepare_any4_source`：几乎可以忽略
- 输出版本转换：几乎可以忽略
- flatten / repair / validate：几乎可以忽略
- 绝大多数时间都消耗在 any4 主体执行过程中

因此可以先排除以下方向：

- 不是解压 zip 本身太慢
- 不是 raw -> any4 适配太慢
- 不是输出目录扁平化和元数据修复太慢
- 不是视频复制回写本身太慢

## 七、Any4 内部热点定位

然后对 any4 内部流程继续做了细分计时。

针对同一个样本 zip，内部统计结果如下：

- `AgiBotDataset.add_frame`：35.76 秒
- `AgiBotDataset.save_episode`：16.40 秒
- `AgiBotDataset._save_episode_data`：9.01 秒
- `compute_episode_stats`：7.18 秒
- `AgiBotDataset._save_episode_video`：0.10 秒
- `load_local_dataset`：0.03 秒

逐帧明细：

- `AgiBotDataset.add_frame` 被调用了 2422 次
- 平均每帧约 14.77 ms
- `validate_frame` 本身几乎不耗时：
  - 总共约 0.11 秒
  - 平均每帧约 0.05 ms

本轮后续优化明确以你指定的 3 个热点为主：

- `AgiBotDataset.add_frame`：35.76 秒
- `AgiBotDataset.save_episode`：16.40 秒
- `AgiBotDataset._save_episode_data`：9.01 秒

优化原则保持不变：

- 先做真正可能带来数量级收益的路径调整
- 帧顺序、字段映射、统计逻辑、写盘逻辑不变
- 所有改动都必须经过真实样本一致性校验

## 八、核心结论

这次排查的核心结论是：

- 真正决定耗时的不是 zip 的 MB 大小
- 真正决定耗时的是帧数、特征处理、统计计算和 LeRobot 写盘逻辑

主要耗时点是：

1. Python 层逐帧将数据灌入 episode buffer
2. LeRobot 写 parquet 的 `_save_episode_data`
3. 统计计算 `compute_episode_stats`

也就是说：

- 这是一个“按帧数和特征复杂度计价”的流程
- 不是一个“按压缩包 MB 大小计价”的流程
- 不是 mp4 复制太慢
- 不是 zip 解压太慢
- 也不是视频编码太慢

这也解释了为什么“看起来只有 20MB 的 zip”仍然会跑到 50~60 秒：

- 这个样本里实际有 2422 个时间步
- 流程不是简单搬文件，而是在把原始数据重组为 LeRobot 数据集
- 工作负载主要是 CPU 和 Python 层处理，而不是磁盘拷贝

## 九、如何理解当前耗时是否正常

针对这份真机数据，结论要分成两层：

### 1. 整批数据

对于这 15 个 zip：

- 在当前机器上，`concurrency=4` 全跑完约 4 分 51 秒
- 这个量级是合理的

因此：

- 如果有人说这批同类数据要跑 1 小时，那就是明显不正常

### 2. 单个 zip

对于单个样本 zip：

- 当前实现下约 50~60 秒是可复现的
- 这不是“流程坏了”，而是当前实现确实存在较高的逐帧处理成本

因此当前状态可以概括为：

- 没有在这份真实数据上复现出功能失败
- 没有证据表明存在隐藏的解压、复制、路径问题导致 50~60 秒
- 有明确证据表明热点在 Python 逐帧处理和 LeRobot 数据写入

## 十、关于数据一致性的风险判断

我们也评估了后续优化会不会改变最终结果。

原则：

- 只要不改帧顺序、不改字段映射、不改统计逻辑、不改保存逻辑，就可以做到“语义等价”

可以安全尝试优化的部分：

- 内存中的缓冲方式
- 批量处理策略
- 重复的 Python 开销

不能随便改的部分：

- 帧顺序
- feature 映射关系
- `total_frames` / `episode_length` 的计算
- `compute_episode_stats`
- parquet 写盘语义
- 视频元数据生成方式

我们也验证过重复转换后的关键元数据字段是一致的，包括：

- `codebase_version`
- `total_episodes`
- `total_frames`
- `total_tasks`
- `fps`
- `robot_type`
- `features`

注意：

- “数据内容一致”不等于“所有文件字节级完全一致”
- 某些 metadata 文件可能会因为写入时间或序列化细节不同而导致哈希不同
- 但这不代表数据语义不一致

## 十一、尝试过的优化与结论

做过一版“安全快路径”实验：

- 把多次 `add_frame()` 改成批量把 frame 灌入内存缓冲

目的：

- 降低 Python 层循环开销
- 不改保存逻辑、统计逻辑和视频逻辑

结果：

- 等价性从原理上是可以做到的
- 但在这份样本数据上，没有拿到明显的速度收益

因此决策是：

- 不保留“没有实际收益”的优化
- 后续只继续保留能在同一份真机数据上测出明确收益的优化

之后又做了两轮更贴近真实热点的验证：

### 1. `_save_episode_data` 的 no-op 优化

做法：

- 在没有 image feature 的场景下，跳过 `embed_images()`

结果：

- 输出内容可以做到一致
- 但真实样本中 `_save_episode_data` 细分后只占约 `0.49 秒`
- 这条优化对端到端收益几乎没有意义

因此决策是：

- 不保留这条覆盖实现，避免引入额外维护成本

### 2. `compute_episode_stats` 的视频键并行统计

做法：

- 不改 `sample_images()`、`get_feature_stats()`、归一化逻辑
- 只把多个视频键的统计改为并行执行
- 非视频字段仍沿用原有统计公式
- 最终结果按原始字段顺序回填，保证输出 schema 顺序不变

真实样本验证结果：

- 原始 `compute_episode_stats`：约 `7.60 秒`
- 并行后：约 `4.38 秒`
- 统计结果：完全一致

端到端复跑结果：

- 优化后单样本端到端约 `45.48 ~ 46.99 秒`
- 相比原先单样本常见的 `50 ~ 59 秒`，有明确下降

一致性验证结果：

- `meta/info.json` 一致
- `meta/stats.json` 一致
- `data/chunk-000/file-000.parquet` 的 schema 和 row count 一致
- `meta/tasks.parquet` 的 schema 和 row count 一致
- `meta/episodes/chunk-000/file-000.parquet` 的 schema 和 row count 一致
- 三路 mp4 文件哈希一致

因此决策是：

- 保留这条优化
- 它满足“内容一致优先”的前提，同时在真实样本上有可测收益

### 3. `add_frame + save_episode + _save_episode_data` 的联动优化

这一轮是针对你点名的三个大热点做的真正主优化，分成两步：

#### 第一步：绕开逐帧 `add_frame()` 热点

做法：

- 新增批量构造 `episode_data` 的路径
- 在 `save_as_lerobot_dataset()` 中，不再对 2422 帧逐条调用 `add_frame()`
- 改为一次性把整段 `frames` 组织成 `episode_data`
- 然后继续走原有 `save_episode()` / 统计 / 写盘 / 视频落盘逻辑

关键点：

- 不改帧顺序
- 不改 feature 映射
- 不改 `save_episode()` 的核心语义
- 只是把“逐帧灌 buffer”改成“批量构造等价 buffer”

真实样本结果：

- 优化后端到端约 `13.44 秒`

对比原始基线：

- 原始单样本常见约 `50 ~ 59 秒`
- 仅靠这一条批量路径，已经拿到明显数量级收益

#### 第二步：重新启用 `_save_episode_data` 的安全短路

在批量路径落地后，热点重新收敛到了 `_save_episode_data()`。

复测发现：

- `build_episode_data` 约 `0.25 秒`
- `save_episode` 约 `13.05 秒`
- `_save_episode_data` 单独约 `8.63 秒`

说明：

- `add_frame` 热点已经基本被消掉
- 此时 `_save_episode_data` 重新变成新的主瓶颈

因此把之前验证过“内容一致”的安全优化重新启用：

- 当 `self.meta.image_keys` 为空时，跳过无实际作用的 `embed_images()`

真实样本结果：

- 批量路径 + `_save_episode_data` 安全短路叠加后
- 单样本端到端约 `5.26 秒`

#### 最终一致性验证

以原始基线输出和最终优化输出做对比，结果如下：

- `meta/info.json` 一致
- `meta/stats.json` 一致
- `data/chunk-000/file-000.parquet` 的 schema 和 row count 一致
- `meta/tasks.parquet` 的 schema 和 row count 一致
- `meta/episodes/chunk-000/file-000.parquet` 的 schema 和 row count 一致
- 三路 mp4 文件哈希一致

因此结论是：

- 这轮针对 `add_frame / save_episode / _save_episode_data` 的优化是成立的
- 它不是微调，而是直接绕开了最重的逐帧 Python 热点
- 同时保持了输出内容一致

## 十二、后续优化建议

下一步真正值得做的优化方向，应当集中在真实热点上：

1. `AgiBotDataset.add_frame`
   - 原始逐帧路径已经不再是当前主路径
   - 后续除非要兼容更多调用方，否则优先级可以下降

2. `compute_episode_stats`
   - 当前已经完成一轮安全优化
   - 如果继续优化，应优先继续关注视频采样阶段，而不是改统计公式本身

3. `_save_episode_data`
   - 在旧路径里不是主要瓶颈
   - 但在批量入缓冲后，它会重新成为热点
   - 当前已经通过跳过无效 `embed_images()` 做了一轮有效优化

## 十三、后续任何性能优化的验证准则

后续所有性能优化，都应继续用同一份真机数据做验证，至少检查以下项目：

1. 任务总数和成功数一致
2. `meta/info.json` 核心字段一致
3. parquet schema 一致
4. `row count` / `total_frames` 一致
5. `features` 一致
6. 视频文件数量一致
7. parquet 抽样行数据值一致

只有这些检查通过，才能接受某个性能优化。

## 十四、2026-03-10 当天优化总结

这一节只总结今天已经实际落地、已经验证过的优化，不包含未保留的实验。

### 1. 样本与基线

统一基准样本：

- `custom_task_pick_the_fruit_20260205181347.zip`

统一基准环境：

- 真实真机 zip
- LeRobot `v3.0`
- 同一台本机
- 同一条 any4 真实转换路径

原始基线耗时：

- 单样本常见约 `50 ~ 59 秒`
- 其中一次代表性测量约 `57.4 秒`

原始热点拆分：

- `AgiBotDataset.add_frame`：`35.76 秒`
- `AgiBotDataset.save_episode`：`16.40 秒`
- `AgiBotDataset._save_episode_data`：`9.01 秒`
- `compute_episode_stats`：`7.18 秒`

### 2. 今天实际保留的优化

#### 优化 A：`compute_episode_stats` 多视频键并行

实际方案：

- 不改 `sample_images()`
- 不改 `get_feature_stats()`
- 不改归一化逻辑
- 只把多个视频键的统计改为并行执行
- 结果按原始字段顺序写回，保持 schema 顺序不变

量化结果：

- 原始 `compute_episode_stats`：约 `7.60 秒`
- 优化后：约 `4.38 秒`
- 节省约：`3.22 秒`

#### 优化 B：批量构造 `episode_data`，绕开逐帧 `add_frame()`

实际方案：

- 在 `AgiBotDataset` 中增加 `build_episode_data()`
- 在 `save_as_lerobot_dataset()` 中，不再对每一帧调用 `add_frame()`
- 改为一次性把整段 `frames` 组装成等价的 `episode_data`
- 后续仍然走原有 `save_episode()` / 统计 / parquet 写盘 / 视频落盘逻辑

量化结果：

- 批量路径落地后，单样本端到端约 `13.44 秒`
- 相比原始基线 `57.4 秒`
- 节省约：`43.96 秒`

补充拆分：

- `build_episode_data`：约 `0.25 秒`
- 此时 `save_episode` 仍约 `13.05 秒`
- 此时 `_save_episode_data` 重新成为最主要剩余热点

#### 优化 C：`_save_episode_data()` 跳过无效 `embed_images()`

实际方案：

- 当前真实路径下没有 image keys，只有 video keys
- 因此在 `_save_episode_data()` 中，当 `self.meta.image_keys` 为空时，跳过无实际作用的 `embed_images()`

量化结果：

- 在批量路径基础上继续优化后
- 单样本端到端约 `5.26 秒`
- 相比批量路径阶段的 `13.44 秒`
- 再节省约：`8.18 秒`

### 3. 今日最终结果

最终保留优化全部叠加后的单样本结果：

- 最终单样本端到端：`5.26 秒`

相对原始代表性基线 `57.4 秒`：

- 总节省约：`52.14 秒`
- 速度提升约：`10.9 倍`

### 4. 优化后数据是否有差异

结论：

- 当前保留的优化，在已完成的真实样本验证中，与基线输出没有差异

已经完成的对比项：

- `meta/info.json` 一致
- `meta/stats.json` 一致
- `data/chunk-000/file-000.parquet` 的 schema 一致
- `data/chunk-000/file-000.parquet` 的 row count 一致
- `meta/tasks.parquet` 的 schema 一致
- `meta/tasks.parquet` 的 row count 一致
- `meta/episodes/chunk-000/file-000.parquet` 的 schema 一致
- `meta/episodes/chunk-000/file-000.parquet` 的 row count 一致
- `videos/observation.images.hand_left/chunk-000/file-000.mp4` 哈希一致
- `videos/observation.images.hand_right/chunk-000/file-000.mp4` 哈希一致
- `videos/observation.images.head/chunk-000/file-000.mp4` 哈希一致

因此可以明确说：

- 这些优化不是“牺牲数据一致性换速度”
- 在当前真实样本上，它们保持了输出内容一致
- 至少在目前已经验证的范围内，没有发现数据被改坏、字段漂移、帧顺序变化、统计结果变化或媒体内容变化

### 5. 今天没有保留的优化

以下方案虽然评估过，但没有保留到最终实现：

- 早期的 `add_frame()` 快路径实验
  - 原因：当时没有拿到稳定收益
- 早期的 `sample_images()` 解码策略调整实验
  - 原因：收益不明显

结论：

- 今天最终保留的都是“真实样本上测出明确收益，且内容一致验证通过”的方案

## 十五、线程控制失效问题排查与修复

### 1. 问题现象

现象描述：

- 前端已经提供了并发/线程数设置
- 后端外层也存在 `ThreadPoolExecutor`
- 但实际观察中，修改线程数后并没有体现出预期的 any4 并发行为

这说明：

- 问题不一定在 UI
- 也不一定是值没传过去
- 更可能是“值传到了，但在 deeper layer 被短路或错误解释”

### 2. 根因排查过程

本次按前端 -> 后端 -> any4 桥接 -> any4 主入口的链路逐段核对。

#### 第一层：前端是否传值

检查位置：

- `src/data_converter/main.py`
- `src/data_converter/backend.py`

结论：

- 前端并发值会进入 `build_options()`
- 后端 `ConversionBackend.run()` 的外层 `ThreadPoolExecutor(max_workers=options.concurrency)` 也确实会使用这个值

因此：

- 前端没有丢值
- 外层后端线程池也没有完全忽略该值

#### 第二层：any4 桥接是否正确传值

检查位置：

- `src/data_converter/converters/lerobot_runner.py`

排查结果：

- any4 参数构造中虽然会传 `--cpus-per-task`
- 但同时无条件追加了 `--debug`

这意味着：

- 即使并发值传入 any4
- 也可能被 any4 的 debug 分支直接绕开

#### 第三层：any4 主入口是否真正使用并发

检查位置：

- `any4lerobot/agibot2lerobot/agibot_h5.py`

原始行为：

- `if debug:` 时直接走 `save_as_lerobot_dataset(next(tasks), ...)`
- 这一条是单任务串行路径，不会进入 Ray 并发

因此真正的第一层根因是：

- `lerobot_runner.py` 无条件传 `--debug`
- 导致 any4 永远走串行 debug 路径
- 用户设置的线程/并发值虽然被传递，但不会生效

### 3. 第二层根因：并发参数语义错误

继续检查 any4 的非 debug 路径，发现还有第二个问题：

原始逻辑：

- `ray.init(runtime_env=...)`
- `remote_task = ray.remote(...).options(num_cpus=cpus_per_task)`

这会导致：

- `cpus_per_task` 被解释成“每个任务占多少 CPU”
- 而不是“允许多少个任务并发”

结果就是：

- 用户以为自己设置的是“最大线程数/最大并发数”
- 实际代码却把它当成“单任务资源占用”

因此第二层根因是：

- 并发参数语义实现错误
- 不是把用户的值当成“并发上限”来使用

### 4. 最终根因结论

线程控制失效，不是前端问题，而是后端 any4 集成层的两个问题叠加：

1. `lerobot_runner.py` 无条件加 `--debug`
   - 直接把 any4 固定到单任务串行路径

2. `agibot_h5.py` 对 `cpus_per_task` 的解释错误
   - 把它当成“每任务 CPU 数”
   - 而不是“最大并发任务数”

### 5. 修复方案

#### 修复 A：只在单任务 source 时保留 `--debug`

修改位置：

- `src/data_converter/converters/lerobot_runner.py`

修复方式：

- 新增 `_should_use_any4_debug_mode(source_dir)`
- 当 `task_info/*.json` 只有 1 个任务文件时，仍保留 `--debug`
- 当 source 是多任务时，不再强制传 `--debug`

这样做的原因：

- 单任务场景保留当前稳定行为
- 多任务场景才真正启用 any4 内部并发

#### 修复 B：把 `cpus_per_task` 语义改成“最大并发任务数”

修改位置：

- `any4lerobot/agibot2lerobot/agibot_h5.py`

修复方式：

- `ray.init(num_cpus=max(1, cpus_per_task), runtime_env=...)`
- `ray.remote(...).options(num_cpus=1)`

这样做之后：

- `cpus_per_task` 不再表示“每个任务占多少 CPU”
- 而是表示“Ray 最多可同时调度多少个任务”

这才和前端“并发/线程数”的语义一致

### 6. 修复后的行为

修复后：

- 前端并发值会传到后端
- 后端外层线程池仍然会使用该值
- 对多任务 any4 source，不再被 `--debug` 短路
- any4 内部会按“最大并发任务数”理解这个值

也就是说：

- 现在前端、后端、桥接层、any4 主入口的并发语义已经统一

### 7. 验证结果

新增/更新的测试点：

- 单任务 source 时，any4 参数仍然包含 `--debug`
- 多任务 source 时，any4 参数不再强制包含 `--debug`
- 后端外层 `ThreadPoolExecutor` 仍按前端并发值构造

已通过验证：

- `python -m pytest -q test_backend_concurrency.py test_any4_batch_episode.py test_any4_compute_stats.py test_any4_health.py test_raw_video_mapping.py`
- `python -m py_compile src\data_converter\converters\lerobot_runner.py any4lerobot\agibot2lerobot\agibot_h5.py`

结论：

- 这次线程控制失效的真正问题已经定位并修复
- 修复点在后端 any4 集成逻辑，不在前端传输层

## 八、打包后仍然串行执行的问题排查与修复

### 1. 现象

重新打包后，用户在前端选择：

- `10` 核时，整批任务耗时约 `1 分 30 秒`
- `4` 核时，整批任务耗时也约 `1 分 30 秒`

同时前端任务列表表现为：

- 每次只有 `1` 个任务完成
- 不再出现之前“4 个任务一批完成”的现象

这说明：

- 线程/并发参数在打包产物里没有真正转化为多任务并发执行
- 问题更可能出在打包态专用执行路径，而不是普通源码运行路径

### 2. 排查思路

按以下链路逐层排查：

1. 前端是否正确传递并发值
2. 后端 `ThreadPoolExecutor` 是否按该值构造
3. 打包态是否存在特殊降级逻辑
4. any4 在打包态到底是“进程内执行”还是“子进程执行”

### 3. 第一层结论：前端没有问题

检查位置：

- `src/data_converter/main.py`

结论：

- 前端下拉框的并发值仍然正确传入 `build_options(...)`
- 不是前端参数没有传到后端

### 4. 第二层结论：后端在打包态主动强制串行

检查位置：

- `src/data_converter/backend.py`

排查发现原有逻辑中存在以下降级：

- 当 `target == LEROBOT`
- 且不是 `HDF5`
- 且当前是 `frozen` 打包态
- 且没有找到外部 any4 Python 运行时

就会执行：

- `max_workers = 1`

这会直接导致：

- 前端即使选 `10` 核
- 后端线程池仍只会开 `1` 个 worker

这和用户看到的“一个一个完成”完全一致。

### 5. 第三层结论：之所以被强制串行，是因为打包态原来走的是进程内执行

检查位置：

- `src/data_converter/converters/lerobot_runner.py`
- `src/data_converter/any4lerobot_bridge.py`

原始行为：

- 非打包态：用 Python 子进程执行 any4 桥接
- 打包态且无外部 Python：直接调用 `run_any4lerobot_cli_result(args)`，在当前进程内执行

而 `run_any4lerobot_cli_result(...)` 内部会做这些事情：

- `redirect_stdout(...)`
- `redirect_stderr(...)`
- 动态修改 `sys.path`
- 动态 monkey patch any4 运行时行为

这些操作都不适合多个任务在线程内并发共用同一进程。

因此原作者才在 `backend.py` 里加了：

- 打包态 bundled 模式强制 `max_workers = 1`

所以这次问题的真正根因不是“参数没传到”，而是：

1. 打包态执行路径设计成了进程内运行 any4
2. 为了规避线程安全问题，后端又把外层线程池强制收缩到 `1`

### 6. 最终根因结论

这次“打包后 10 核和 4 核耗时一样”的根因在后端打包态执行路径，不在前端。

具体是两个条件叠加：

1. 打包态 bundled any4 走进程内执行
2. 后端因此强制 `max_workers = 1`

最终表现就是：

- UI 只能看到任务串行完成
- 用户调大并发值也没有效果

### 7. 修复方案

#### 修复 A：取消打包态的单 worker 限制

修改位置：

- `src/data_converter/backend.py`

修复内容：

- 删除打包态 bundled any4 时的 `max_workers = 1` 强制降级逻辑

#### 修复 B：把打包态 any4 改成“自拉起 EXE 子进程”执行

修改位置：

- `src/data_converter/converters/lerobot_runner.py`
- `src/data_converter/main.py`（复用已有入口，不新增参数协议）

修复内容：

- 打包态不再直接调用 `run_any4lerobot_cli_result(args)`
- 改为调用当前 EXE：
  - `DataConverterShell.exe --internal-run-any4lerobot ...`

也就是：

- 每个任务都在独立子进程里跑 bundled any4
- 不再共享当前主进程的 `stdout/stderr/sys.path/monkey patch` 状态

这样外层线程池就可以安全恢复并发。

### 8. 为什么这个修复方向是对的

这次不是简单粗暴地“删掉 `max_workers = 1`”。

如果只删这一行，而仍然保留“进程内执行 any4”：

- 多个任务会在同一进程内并发修改运行时状态
- 容易出现输出串扰、模块状态污染、临时目录冲突或不可预期异常

因此真正正确的修复顺序必须是：

1. 先把打包态 any4 切到独立子进程
2. 再放开外层 worker 并发

### 9. 本次验证结果

新增回归测试：

- `test_frozen_bundled_lerobot_keeps_outer_concurrency`
  - 验证打包态不再被强制降成 `1` 个 worker
- `test_frozen_any4_bridge_uses_internal_subprocess_entry`
  - 验证打包态 any4 改为通过 `--internal-run-any4lerobot` 子进程执行

已通过验证：

- `python -m pytest -q test_backend_concurrency.py`
  - `6 passed`
- `python -m pytest -q test_any4_batch_episode.py test_any4_compute_stats.py test_any4_health.py test_raw_video_mapping.py test_backend_concurrency.py`
  - `16 passed`
- `python -m py_compile src\data_converter\backend.py src\data_converter\converters\lerobot_runner.py test_backend_concurrency.py`

### 10. 结论

这次打包后的“线程控制失效”问题已经定位清楚：

- 不是前端传值问题
- 不是普通源码运行逻辑问题
- 是打包态 bundled any4 的执行模型导致后端被迫串行

修复后：

- 打包态 any4 改为独立子进程执行
- 后端外层并发重新按用户选择生效
- 代码级回归测试已通过

后续建议：

- 下一步应重新打包并在 EXE 上做一次真实 smoke 验证
- 重点观察：
  - 任务列表是否重新出现多任务并行完成
  - `10` 核是否明显快于 `4` 核

## 九、单任务很快，但并发后单任务变慢的原因与优化

### 1. 现象

用户观察到：

- 单个任务在优化后可以达到几秒级
- 但恢复多线程后，虽然任务可以并发执行
- 单个任务的完成时间又明显变慢

这看起来像“跷跷板”：

- 要单任务快，就难并发
- 要并发，就单任务变慢

### 2. 根因分析

这不是“目标天然无法同时满足”，而是当前实现里存在两类资源放大问题。

#### 根因 A：并发预算被重复使用

检查链路：

- 前端并发值进入 `backend.py`
- 外层 `ThreadPoolExecutor(max_workers=options.concurrency)`
- 同时同一个值又被传给 any4 的 `--cpus-per-task`
- any4 非 debug 路径再把这个值用于内部 Ray 调度

这会造成：

- 同一个“总并发预算”同时被外层和内层使用
- 资源被重复放大
- 一旦同时存在外层任务并发和内层 any4 并发，就会超卖 CPU、内存和磁盘带宽

也就是说，原先代码把：

- “用户想要的总并发上限”

错误实现成了：

- “外层任务并发数”
- 加上“每个任务内部并发数”

#### 根因 B：单任务 debug 路径仍然有重型子进程启动成本

进一步检查 `agibot_h5.py`，发现：

- 即使在 debug 单任务路径
- 模块导入时也会顶层导入 `ray`
- 同时还会顶层导入 `torch`

这会导致：

- 每个子进程一启动就做重型模块加载
- 单个任务独占资源时问题不太明显
- 一旦并发启动多个任务，模块导入、动态链接、磁盘读取和初始化开销会被同时放大

结果就是：

- 单任务时快
- 多任务一起跑时，每个任务都变慢

### 3. 修复方案

#### 修复 A：统一并发预算，避免内外层重复放大

修改位置：

- `src/data_converter/backend.py`
- `src/data_converter/models.py`
- `src/data_converter/converters/lerobot_runner.py`

修复内容：

- 新增 `TaskPlan.lerobot_inner_concurrency`
- 外层在调度前统一计算并发预算

当前策略：

1. 如果是普通多文件/多 zip 任务集：
   - 外层并发使用用户选择值
   - 每个任务内部 `lerobot_inner_concurrency = 1`

2. 如果只有一个 any4 数据集，并且内部含多个 `task_info/*.json`：
   - 外层 worker 固定为 `1`
   - 内层 any4 使用预算并发

这意味着：

- 不再同时把同一个并发值灌给两层
- 避免 `outer_workers * inner_workers` 的资源超卖

#### 修复 B：减少 debug 单任务路径的重型导入成本

修改位置：

- `any4lerobot/agibot2lerobot/agibot_h5.py`

修复内容：

1. `ray` 改为仅在非 debug 路径延迟导入
2. `torch` 改为按需延迟导入
3. 普通 numpy 帧数据不再为了类型判断强行触发 `torch` 导入

这样做的目的：

- 单任务 debug 路径避免无意义加载 `ray`
- 并发启动多个单任务子进程时，显著降低启动争用

### 4. 本次代码级验证

新增/更新验证点：

- 单一多任务 source 会切换为：
  - 外层 `1` worker
  - 内层使用预算并发
- any4 CLI 参数中的 `cpus-per-task`
  - 不再盲目等于前端总并发值
  - 改为使用每个任务实际分配到的内部并发预算
- 单任务 debug / 多任务非 debug 分支仍保持正确行为

已通过：

- `python -m pytest -q test_backend_concurrency.py test_any4_batch_episode.py test_any4_compute_stats.py test_any4_health.py test_raw_video_mapping.py`
  - `18 passed`
- `python -m py_compile src\data_converter\backend.py src\data_converter\converters\lerobot_runner.py src\data_converter\models.py any4lerobot\agibot2lerobot\agibot_h5.py test_backend_concurrency.py`

### 5. 当前结论

这次“单任务快、并发后慢”的问题，根因不是单纯线程数太大，而是：

1. 并发预算被内外层重复使用
2. debug 单任务路径仍携带重型模块启动成本

本次优化已经把这两点都收敛了。

### 6. 仍然存在的客观边界

即使修复后，也不能把并发性能简单理解成线性增长。

因为转换流程仍包含：

- 视频复制
- parquet 写入
- metadata 写入
- staged 短路径中转
- 磁盘和临时目录 I/O

因此更合理的目标应是：

- 单任务尽量压到 `10s` 级
- 总吞吐随并发提升明显改善
- 而不是要求 `16` 个任务并发时，每个任务仍完全保持单任务独占资源时的延迟
