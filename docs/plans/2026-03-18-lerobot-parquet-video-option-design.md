# LeRobot 视频嵌入 Parquet 与 16 维状态修复设计

日期：2026-03-18

## 背景

当前 AgiBot -> LeRobot 转换存在两个问题：

1. 原始 H5 中的 16 维动作/状态未被完整保留。当前适配器按 14 维 joint 处理，导致 `(N, 16)` 的 `joint.position` 被判定为不匹配并出现补零或丢失。
2. LeRobot 当前采用标准的 `parquet + 外置 mp4` 存储方式，而当前需求希望在 LeRobot 转换时允许用户选择是否将视频逐帧写入 parquet。

其中：
- 16 维完整保留属于数据正确性修复，应默认启用；
- 视频逐帧写入 parquet 属于定制存储策略，应作为可选项，不改变默认行为。

## 目标

1. 修复 AgiBot 原始数据到 LeRobot 的 state/action 表示，默认完整保留 16 维：
   - 左臂 7 关节（相对）
   - 右臂 7 关节（相对）
   - 左抓夹 1 维（绝对）
   - 右抓夹 1 维（绝对）
2. 保持 `effector.position` 字段兼容，同时保证其值等于 16 维 joint 向量最后两维。
3. 在 UI 中增加 LeRobot 专属开关，允许用户选择是否将 `head`、`hand_left`、`hand_right` 的视频逐帧写入 parquet。
4. 默认仍使用标准 LeRobot 存储方式（parquet + mp4），仅在用户开启时进入定制嵌入模式。

## 非目标

1. 首版不扩展到 fisheye、depth、tactile 等所有图像源。
2. 首版不追求与上游 any4lerobot 完全无差异；允许在本项目内做受控定制。
3. 首版不要求把所有图像统计流程都完全迁移到嵌入式 schema；可先保证数据可写、可读、可校验。

## 决策

### 1. 16 维状态/动作表示

采用“双保留”策略：

- `observation.states.joint.position`：改为 shape `(16,)`
- `actions.joint.position`：改为 shape `(16,)`
- `observation.states.effector.position`：继续保留 shape `(2,)`
- `actions.effector.position`：继续保留 shape `(2,)`

语义约定：

- 16 维 joint 的第 0-6 维：左臂 7 关节
- 第 7-13 维：右臂 7 关节
- 第 14 维：左抓夹
- 第 15 维：右抓夹

约束：

- `effector.position[0] == joint.position[14]`
- `effector.position[1] == joint.position[15]`

这样既满足“完整 16 维必须保留”，又兼容现有 any4/LeRobot 下游依赖 `effector.position` 的逻辑。

### 2. 视频逐帧写入 parquet 的产品策略

该能力作为 LeRobot 转换高级选项，不改变默认行为。

UI 文案建议：

- 主开关：`将视频逐帧写入 parquet（体积会显著增大）`
- 辅助说明：`关闭时按默认 LeRobot 方式外置保存 mp4；开启后会把 head、hand_left、hand_right 每帧图像写入 parquet。`

显示条件：

- 仅当 `target=lerobot` 且 `lerobot_version != HDF5` 时显示。

### 3. 嵌入模式的数据表示

不建议伪装成标准 LeRobot `video` 特征。原因：

- upstream LeRobot/any4 对 `video` 的语义是“parquet 中存 VideoFrame/path，真实画面在外置 mp4”；
- 若继续使用同名 `observation.images.xxx` 字段但塞入逐帧 RGB，会与现有 metadata、stats、读取器语义冲突。

首版建议在嵌入模式下引入定制列：

- `observation.frames.head`
- `observation.frames.hand_left`
- `observation.frames.hand_right`

每一行对应当前时间步的一帧图像。

编码建议：

- 优先存压缩后的 `bytes`（JPEG 优先，必要时 PNG）
- 不建议直接存原始 `H x W x 3 uint8`，否则体积和 IO 开销会进一步恶化

## 架构与落点

### UI / 选项层

涉及文件：

- `src/data_converter/main.py`
- `src/data_converter/models.py`

新增：

- `ConversionOptions.embed_videos_in_parquet: bool = False`

职责：

- UI 读取和展示用户选择
- 开始转换时把该选项传入后端与 manifest

### 数据适配层

涉及文件：

- `src/data_converter/adapters/raw_to_any4.py`
- `any4lerobot/agibot2lerobot/agibot_utils/config.py`

职责：

- 把 `(N,16)` 的 joint 正确识别为完整 joint 向量
- 从最后两维稳定派生 `effector.position`
- 禁止把 16 维 joint 因 schema 假设错误而补零

必要改动：

- `state_shapes["joint/position"]` 从 `(14,)` 改为 `(16,)`
- `action_shapes["joint/position"]` 从 `(14,)` 改为 `(16,)`
- `_derive_effector_from_joint()` 继续从 `[:, 14:16]` 派生
- vendored any4 配置中 joint schema 同步改为 16 维

### LeRobot 导出层

涉及文件：

- `src/data_converter/converters/lerobot_runner.py`
- `src/data_converter/manifest.py`
- 可能新增一个本项目自有 writer/postprocess 模块

职责：

- 默认模式：保持 any4/upstream 的 `parquet + videos/**/*.mp4`
- 嵌入模式：在导出后处理或自定义导出路径中，将三路视频转为逐帧图像列写入 parquet
- metadata/验证逻辑根据模式分流

推荐实现路径：

1. 先让 any4 正常产出标准 LeRobot 数据
2. 若 `embed_videos_in_parquet=False`，直接结束
3. 若 `embed_videos_in_parquet=True`，执行后处理：
   - 读取每个 episode parquet
   - 根据 `frame_index` / `timestamp` 从对应 mp4 解帧
   - 追加三路 frame 列
   - 重写 parquet
   - 更新 `meta/info.json` 中的 feature 定义
   - 放宽或修改视频存在性校验

这样比直接深改 any4 `save_episode()` 更稳，也更便于维护。

## 数据流

### 默认模式

1. 原始 AgiBot 数据进入 `raw_to_any4.py`
2. 输出 any4 结构
3. any4lerobot 生成标准 LeRobot：
   - `data/**/*.parquet`
   - `videos/**/*.mp4`
4. metadata 修复与校验

### 嵌入模式

1. 先执行默认模式生成标准 LeRobot 数据
2. 再进入嵌入后处理：
   - 解码 `head` / `hand_left` / `hand_right` mp4
   - 逐帧映射到 parquet 行
   - 写入 `observation.frames.*`
   - 更新 metadata
3. 校验改为：
   - 有 `videos/**/*.mp4`，或
   - parquet 中存在嵌入帧列
   - 两者满足其一即可

## 错误处理

1. 若嵌入模式下某一路视频缺失：
   - 默认报错并写 `any4_error.log`
   - 不 silent fallback，避免用户误以为已完整嵌入
2. 若视频帧数与 parquet 行数不一致：
   - 记录偏差原因
   - 采用严格模式直接失败，避免错位写入
3. 若 16 维 joint 不满足 shape `(N,16)`：
   - 继续保留现有诊断能力
   - 但错误信息明确指出“当前已要求完整 16 维”，而不是退回 14 维补零逻辑

## 测试

测试文件应放在 `test/python/`。

至少覆盖：

1. `raw_to_any4.py`
   - `(N,16)` joint 正确写入 state/action
   - effector 从最后两维派生正确
   - 不再因 `(N,16)` 被判不匹配补零
2. `main.py` / `models.py`
   - LeRobot 模式下 UI 开关能正确传递到 `ConversionOptions`
3. `lerobot_runner.py`
   - 默认模式仍要求存在有效 parquet，且兼容外置 mp4
   - 嵌入模式允许无外置视频校验通过（若设计上决定后处理后删除 mp4）
4. 嵌入模式集成测试
   - 生成 parquet 后可读出三路帧列
   - 行数与 episode 帧数一致
   - metadata 中包含定制帧字段
5. 回归测试
   - 现有 v3.0 / v2.1 / v2.0 流程不因默认模式而回归

## 参考与借鉴

### 用户提供脚本里值得借鉴的点

1. 使用图像帧直接构造 LeRobot `image` 特征，而不是强绑 `video` 语义
2. 通过时间戳做相机与关节对齐，而不是只按最短长度硬截断
3. 明确把 16 维动作拼成：左 7 + 右 7 + 左夹爪 + 右夹爪
4. 在长批处理里加入显式的内存回收和数据集重初始化

### 不建议直接照搬的点

1. 该脚本使用的是 LeRobot 原生 `image` 特征路径，更接近“采集时直接写图片”，和当前 any4 导出链路不完全同构
2. 该脚本的字段名与本仓库当前 any4/LeRobot 字段体系不一致，需要做命名适配
3. 若直接切换到 `image` 特征而不是后处理，改动面会比当前方案大很多

### 公开资料核查结论

已核查到以下公开信息：

- LeRobot 官方仓库明确说明其数据集格式支持 “Parquet + MP4 or images”
- LeRobot issue #1434 讨论了 `use_videos=False` 的图像写入路径，说明社区确实存在“图片直接入数据集、不走 mp4”的用法
- LeRobot issue #1919 说明“images in the parquet”是真实存在的场景，但 v21 -> v30 转换可能有 metadata bug

因此：

- “把图像直接写入 parquet” 并不是虚构路径，LeRobot 生态里确实有人这么做；
- 但在 any4/LeRobot v3.0 的标准 AgiBot 转换链里，这不是默认路径；
- 若我们引入该能力，需要把它视为项目定制能力，并补好 metadata/转换/校验的兼容逻辑。

## 风险

1. 数据体积显著膨胀，10 倍到数十倍是合理预期
2. parquet 重写和图像编码会明显增加转换耗时
3. 自定义帧列可能不被所有 LeRobot 工具直接识别
4. 上游版本转换脚本对 `images in parquet` 存在已知兼容问题，未来若要继续做 v21/v20 降级，需要额外验证

## 分阶段实施建议

### 第一阶段

- 修复 16 维 joint / effector 正确性
- 增加 UI 开关与参数传递
- manifest 记录该开关状态

### 第二阶段

- 为 v3.0 增加嵌入模式后处理器
- 完成 metadata 与校验分流
- 跑最小 smoke 数据验证

### 第三阶段

- 评估是否支持 v2.1 / v2.0 嵌入模式
- 评估是否保留外置 mp4 作为旁路产物
- 优化体积与性能策略（JPEG 质量、分块、并行解码）

## 当前结论

建议按以下原则落地：

- 16 维完整保留：默认修复，必须做
- 视频逐帧入 parquet：可选开关，只对 LeRobot 生效
- 首版只覆盖 `head`、`hand_left`、`hand_right`
- 首版优先以后处理方式实现，降低对 vendored any4 的侵入性
