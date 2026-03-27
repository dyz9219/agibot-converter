# 2026-03-16 LeRobot parquet 与元数据不可用根因修复

## 问题背景
用户反馈转换后的 parquet 与元数据不可用，主要表现为：
- parquet 中关节数据缺失
- parquet 字段结构没有转成新版/目标版训练结构
- 元数据没有按训练格式组织

## 根因分析
### 根因 1：原始包适配器把真实关节数据错误清零
src/data_converter/adapters/raw_to_any4.py 的最小适配器把 joint.position / joint.current_value 等字段形状硬编码为 14 维。
当原始 aligned_joints.h5 中出现 16 维 joint 数组时，旧逻辑直接判定 shape mismatch，然后整列填零。
这会同时导致：
- observation.states.joint.position 丢失
- actions.joint.position 丢失
- effector.position 依赖的数据也没有被拆出，继续为零

### 根因 2：v3.0 -> v2.1 fallback 只是改版本号，没有真正重排数据
src/data_converter/converters/lerobot_runner.py 中旧的 _fallback_convert_v30_to_v21() 只做了：
- 改 meta/info.json 的 codebase_version
- 改 data_path / video_path 模板
- 创建空的 episodes.jsonl / episodes_stats.jsonl

但没有真正把：
- data/chunk-000/file-000.parquet
重排为：
- data/chunk-000/episode_000000.parquet

也没有从 meta/episodes/*.parquet 重建 legacy metadata。
结果是 metadata 指向的训练结构与磁盘真实结构不一致，训练侧读取失败。

### 根因 3：legacy metadata 缺失 tasks / episodes / episode stats 的可训练组织
旧 fallback 不会重建：
- meta/tasks.jsonl
- meta/episodes.jsonl
- meta/episodes_stats.jsonl
导致即使 data parquet 存在，也没有按 v2.1 训练侧预期组织元数据。

## 修改方案
### 1. 修 raw -> any4 proprio 适配
在 raw_to_any4.py 中：
- 为原始 H5 读取增加缓存，避免重复读相同 key
- 对 16 维 state/action joint.position 做训练结构重排：保留前 14 维 arm joints
- 对缺失的 state/action effector.position，从同源 16 维 joint 数组的后 2 维自动重建
- 对 1 维但训练期望 2 维的向量，做保守补齐而不是直接全零
- warning 从 填充零值 区分为 已重排到训练结构 与 缺失填零 两类，便于后续诊断

### 2. 把 v30 -> v2.1 fallback 改成真实结构重建
在 lerobot_runner.py 中：
- 从 meta/episodes/**/*.parquet 读取 episode records
- 重写 meta/info.json 为 v2.1 legacy 模板
- 从 meta/tasks.parquet 重建 meta/tasks.jsonl
- 将 v3 consolidated parquet 真正切分为 data/chunk-xxx/episode_xxxxxx.parquet
- 从 episode records 重建 meta/episodes.jsonl 和 meta/episodes_stats.jsonl
- 对单 episode 对应的 v3 chunked mp4，直接复制为 legacy 路径，覆盖 raw adapter 当前的一任务一 episode 场景

## 新增回归测试
- test/python/test_raw_to_any4_adapter.py
  - 验证 16 维 raw joint 输入不会再被清零
  - 验证 effector 会从 joint 后 2 维正确拆出
- test/python/test_lerobot_version_fallback.py
  - 验证 v3 fallback 会真正生成 episode_000000.parquet
  - 验证会生成 episodes.jsonl / episodes_stats.jsonl
  - 验证 info.json 的 v2.1 路径模板与输出结构一致

## 验证方式与结果
### 通过
命令：.venv\Scripts\python.exe -m unittest discover -s test\python -p test_raw_to_any4_adapter.py -v
结果：OK

命令：.venv\Scripts\python.exe -m unittest discover -s test\python -p test_lerobot_version_fallback.py -v
结果：OK

命令：对以下 4 个文件执行 py_compile
- src/data_converter/adapters/raw_to_any4.py
- src/data_converter/converters/lerobot_runner.py
- test/python/test_raw_to_any4_adapter.py
- test/python/test_lerobot_version_fallback.py
结果：4 个文件语法检查通过

### 发现的附带问题
命令：.venv\Scripts\python.exe -m unittest discover -s test\python -p test_lerobot_layout.py -v
结果：失败，但失败原因是仓库既有测试装配问题：data_converter 没有加入 sys.path，不是本轮修复引入的问题。

## 当前结论
本轮修复已经覆盖用户反馈的三类核心问题：
- 原始 joint 数据不再因 16 到 14 的 shape mismatch 被整列清零
- v3 fallback 会真正生成 v2.1 legacy parquet 结构
- legacy metadata 会按训练读取路径重建

## 下一步建议
- 用一份真实用户原始包做一次 v3.0 / v2.1 / v2.0 端到端 smoke，确认实际输出与训练脚本兼容
- 若后续发现单文件多 episode 的 v3 视频 chunk，需要把 fallback 视频处理从 单 episode 直接复制 扩展为 ffmpeg 精确切段


## 真实样本补充排查：相机与夹爪
### 样本
- 真机样本：D:\下载\windows-x64-agibot-isaac-downloader-真机真机\downloads\真机真机_2019353507798704130_20260306_181055
- 仿真样本：D:\下载\windows-x64-agibot-isaac-downloader-手动进入下一轮采集\downloads\手动进入下一轮采集_2016073913435938817_20260306_181116

### 结论 1：真机样本的相机数据没有丢，只是不在 data parquet 主表里
对真实样本执行 LeRobot v3.0 转换后确认：
- meta/info.json 包含 observation.images.head / hand_left / hand_right
- videos/ 目录下存在对应 mp4
- meta/episodes/*.parquet 中存在视频索引字段

因此 真机样本里“parquet 少了摄像头数据” 的现象不是视频丢失，而是 LeRobot v3.0 的正常存储方式：
- 机器人状态和动作在 data/*.parquet
- 图像数据在 videos/ 和 meta/episodes/*.parquet

### 结论 2：仿真样本夹爪缺失的根因在 rosbag 读取链路
src/data_converter/rosbag/source_reader.py 旧逻辑只读取：
- state/joint/position
- state/joint/velocity
- state/joint/effort

但没有利用：
- state/effector/position

同时，仿真样本 state.json 中夹爪关节名带 idx 前缀，例如：
- idx31_gripper_l_inner_joint1
- idx32_gripper_l_inner_joint2

旧逻辑会把这些原样写进 JointState，导致下游仿真侧很难按既有命名识别左右夹爪；而且即使 H5 已经提供双侧 effector.position，旧逻辑也完全没有追加显式 left_gripper / right_gripper。

### 本次修复
在 src/data_converter/rosbag/source_reader.py 中：
- 增加 _read_effector_array()，优先读取 state/effector/position
- 若 effector 缺失，则从 left_gripper_joint1 / right_gripper_joint1 回退推导
- 增加 _normalize_joint_name()，去掉 idxNN_ 前缀
- 增加 _augment_joint_state_with_effector_aliases()，在 JointState 尾部补出 left_gripper / right_gripper 两个显式别名通道

### 回归测试与真实样本验证
新增测试：test/python/test_rosbag_source_reader.py
- 验证 idx 前缀会被正确剥离
- 验证会追加 left_gripper / right_gripper
- 验证已有别名时不会重复追加

真实样本验证结果：
- 真机样本读取后 camera_videos = [hand_left, hand_right, head]
- 真机样本 joint_position shape = (2422, 18)，包含 left_gripper / right_gripper
- 仿真样本读取后 camera_videos = [hand_left, hand_right, head, whole_body]
- 仿真样本 joint_position shape = (444, 36)，包含 left_gripper / right_gripper
