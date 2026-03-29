## 问题背景

用户要求继续分析 GitHub Actions 新 run 中 Windows 构建长期停在 `Verify build fingerprint` 的问题。

相关 run：

- `https://github.com/dyz9219/agibot-converter/actions/runs/23583587627`

## 现象

最新 run 中：

- `build-linux-x64` 成功
- `build-linux-arm64` 成功
- `build-windows` 最终被取消

其中 Windows job 的时间线为：

- `Verify build fingerprint` 于 `2026-03-26 08:06:15 UTC` 开始
- 长时间未结束
- job 于 `2026-03-26 14:02:55 UTC` 被取消

## 根因分析

通过拉取完整 job 日志并对照脚本，确认有两个独立问题：

### 1. `verify_build_fingerprint.ps1` 缺少超时保护

该脚本使用：

```powershell
Start-Process -FilePath $ExePath -ArgumentList @("--internal-build-info") -Wait ...
```

一旦打包后的 GUI EXE 在 CI runner 上没有按预期及时退出，脚本就会无限等待，最终把整个 Windows job 拖到超时或被取消。

本地现有产物执行 `--internal-build-info` 可以正常退出，因此当前更合理的判断是：

- 这一步在 CI 环境下存在阻塞风险；
- 脚本没有任何超时与强制结束机制，导致问题不可恢复。

### 2. `scripts/build_exe.ps1` 内部仍然执行了冲突安装

虽然 workflow 外层已经改成了 curated install，但 `scripts/build_exe.ps1` 内部仍然有：

- `pip install -e .`
- `pip install pyinstaller`

而日志显示在 `Build full package` 阶段，这个内部 `pip install -e .` 仍然触发了与 `lerobot 0.4.4 / torchvision 0.17.2` 相关的冲突。只是脚本没有及时中止，后续还继续打包。

这说明：

- Windows 打包脚本与 workflow 的依赖安装策略不一致；
- 旧安装逻辑仍可能污染 venv，增加后续验证步骤的不确定性。

## 改动方案

本轮修复分两部分：

1. 新增统一的 curated 安装脚本 `scripts/install_curated_env.ps1`
   - 固定安装当前已验证可工作的依赖集合
   - 使用 `--no-deps` 安装 `lerobot==0.4.4`
   - 使用 `--no-deps` 安装本项目
2. 收口所有 Windows 相关脚本的安装逻辑并补超时保护
   - `build_exe.ps1`
   - `build_exe_onefile.ps1`
   - `run.ps1`
   - `smoke_lerobot_exe.ps1`
   - `verify_build_fingerprint.ps1`

## 具体修改

### 新增

- `scripts/install_curated_env.ps1`

### 更新

- `scripts/build_exe.ps1`
  - 改为复用 `install_curated_env.ps1`
  - 显式启用 `$PSNativeCommandUseErrorActionPreference = $true`
  - 修正构建完成提示路径
- `scripts/build_exe_onefile.ps1`
  - 改为复用 `install_curated_env.ps1`
- `scripts/run.ps1`
  - 改为复用 `install_curated_env.ps1`
- `scripts/smoke_lerobot_exe.ps1`
  - 改为复用 `install_curated_env.ps1`
- `scripts/verify_build_fingerprint.ps1`
  - 移除无限等待模式
  - 增加 60 秒超时
  - 超时后强制结束子进程并报错

## 验证方式

已完成：

1. 拉取 GitHub Actions Windows job 全量日志，确认卡点在 `Verify build fingerprint`。
2. 本地直接执行现有打包产物：
   - `dist\DataConverterShell\DataConverterShell.exe --internal-build-info`
   - 结果可正常退出并输出 JSON。
3. 使用 PowerShell Parser 校验以下脚本语法：
   - `scripts/install_curated_env.ps1`
   - `scripts/build_exe.ps1`
   - `scripts/build_exe_onefile.ps1`
   - `scripts/run.ps1`
   - `scripts/smoke_lerobot_exe.ps1`
   - `scripts/verify_build_fingerprint.ps1`
   - 结果均为 `PARSE_OK`

说明：

- 本地对旧产物运行 `verify_build_fingerprint.ps1` 返回 mismatch 是预期现象，因为旧产物对应的是旧 commit，不代表脚本故障。

## 当前结论

Windows 卡住不是单一业务代码问题，而是：

- 指纹验证脚本缺少超时保护，导致 CI 上一旦 EXE 不退出就会无限挂住；
- Windows 打包脚本内部仍沿用旧的冲突安装逻辑，和 workflow 外层策略不一致。

当前已完成针对性修复，下一步应提交并重新触发 Actions，重点观察 Windows job 是否：

1. 不再卡死在 `Verify build fingerprint`；
2. 内部不再出现旧的 `pip install -e .` 依赖冲突日志。

## 同日补充：linux-x64 产物体积异常偏大

### 问题背景

用户继续反馈最新成功 run 中 `DataConverterShell-Linux-x64` artifact 体积约 `2.87 GB`，明显高于 `Linux-ARM64` 与 `Windows`，要求确认缩包后仍能正常使用且不影响功能。

相关 run：

- `https://github.com/dyz9219/agibot-converter/actions/runs/23626585022`

### 现象

该 run 的 artifact 体积为：

- `DataConverterShell-Linux-x64`: `3,077,489,913 bytes`（约 `2.87 GB`）
- `DataConverterShell-Linux-ARM64`: `311,780,214 bytes`（约 `297 MB`）
- `DataConverterShell-Windows-full`: `451,826,095 bytes`（约 `431 MB`）

其中 `linux-x64` 不是构建失败，而是“能产出但明显异常偏大”。

### 根因分析

通过拉取 `build-linux-x64` job 全量日志，确认体积暴涨来自 Linux x64 默认安装的 GPU 版 PyTorch 依赖：

- `torch==2.2.2` 与 `torchvision==0.17.2` 在 `ubuntu-latest` 上默认解算到了带 CUDA 的 wheel；
- pip 额外安装了一整套 `nvidia-*` 包，例如：
  - `nvidia-cublas-cu12`
  - `nvidia-cudnn-cu12`
  - `nvidia-cusparse-cu12`
  - `nvidia-nccl-cu12`
- PyInstaller 在分析阶段又识别并打包了这些 CUDA 运行时库。

因此：

- 当前 `2.87 GB` 不是随机异常，也不是用户数据导致；
- 它本质上是“把 Linux GPU 运行时一起打进了桌面分发包”。

### 改动方案

本轮对 CI workflow 做两项修正：

1. `linux-x64` 改为安装 CPU-only PyTorch
   - 保留其余 curated 依赖不变；
   - 将 `torch/torchvision` 单独从 `https://download.pytorch.org/whl/cpu` 安装；
   - 避免继续把 CUDA/NVIDIA 动态库带入分发包。
2. 给两个 Linux job 都补上打包后 smoke test
   - `--internal-build-info`
   - `--internal-run-rosbag-health --bag-type MCAP`
   - `--internal-run-any4-health --version v3.0`
   - `--internal-run-any4-health --version v2.1`
   - `--internal-run-any4-health --version v2.0`

### 具体修改

更新文件：

- `.github/workflows/build.yml`

修改内容：

- `build-linux-x64`
  - 拆分 Python 依赖安装；
  - 将 `torch==2.2.2` / `torchvision==0.17.2` 改为 CPU-only index 安装；
  - 新增 Linux 二进制 smoke test 步骤。
- `build-linux-arm64`
  - 新增 Linux 二进制 smoke test 步骤。

### 验证方式

已完成：

1. 拉取 `23626585022` 的 artifact 元数据，确认 `linux-x64` 体积约 `2.87 GB`。
2. 拉取 `build-linux-x64` job 全量日志，确认默认安装了多项 `nvidia-*` CUDA 依赖。
3. 对照 `src/data_converter/main.py`，确认以下内部 CLI 在进入 Flet GUI 前即可直接执行：
   - `--internal-build-info`
   - `--internal-run-rosbag-health`
   - `--internal-run-any4-health`
4. 更新 workflow，补入 CPU-only 安装与 Linux smoke test。

待下一次 Actions 验证：

1. `linux-x64` artifact 体积应显著下降；
2. `linux-x64` 与 `linux-arm64` 都必须通过新增 smoke test；
3. 在 smoke test 通过前，不将“缩包后功能不受影响”视为已证实结论。

### 当前结论

当前 `linux-x64` 包过大确实不正常，但根因已明确，不是业务功能损坏，而是 CI 误装了 GPU 版 PyTorch 及其 CUDA 运行时。

本轮已经把 workflow 改为：

- `linux-x64` 使用 CPU-only PyTorch，优先解决体积异常；
- Linux 产物增加打包后基础健康检查，避免只缩体积、不验证可用性。

下一步应提交并触发新 run，以“体积下降 + smoke test 全绿”作为新的验收标准。

## 同日补充二：Linux smoke test 失败根因与继续修复

### 新现象

提交 `fix(ci): shrink linux x64 artifact and smoke test binaries` 后，新的 run：

- `build-linux-x64` 构建步骤成功，但 `Smoke test binary` 失败
- `build-linux-arm64` 构建步骤成功，但 `Smoke test binary` 失败

说明新的 Linux smoke test 已经生效，并且成功把打包后运行问题拦了出来。

### 新根因分析

进一步查看 Linux job 日志后，确认两点：

1. `linux-x64` 已经切换到 CPU-only PyTorch 安装
   - 日志中明确执行：
     - `python -m pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.2.2" "torchvision==0.17.2"`
   - 这说明“误装 CUDA/NVIDIA 大包”的方向已经被修正。
2. Linux smoke test 的第一个检查 `--internal-build-info` 直接失败
   - `src/data_converter/main.py` 中该命令依赖 `assets/build_meta.json`
   - Windows `scripts/build_exe.ps1` 会先生成 `build/build-meta/build_meta.json`，再通过 `--add-data "$metaPath;assets"` 打进产物
   - 但 Linux workflow 之前的 PyInstaller 命令没有打入这份 metadata 文件

因此，这一轮失败的直接原因不是转换能力损坏，而是：

- Linux 打包链路缺少与 Windows 对齐的 `build_meta.json` 注入步骤；
- smoke test 正好把这个遗漏暴露出来。

### 继续改动方案

在原有修复基础上继续补齐：

1. 为 `build-linux-x64` / `build-linux-arm64` 增加 `Generate build metadata` 步骤
   - 在 `build/build-meta/build_meta.json` 生成最小可用 metadata
2. 更新 Linux PyInstaller 命令
   - 增加 `--add-data "$PWD/build/build-meta/build_meta.json:assets"`
3. 调整 Linux smoke test 输出
   - 将 `--internal-build-info >/tmp/build-info.json` 改为 `| tee /tmp/build-info.json`
   - 这样若后续再次失败，可直接从 Actions 日志看到具体输出，而不是只看到 exit code

### 当前结论更新

截至目前，可以确认：

- `linux-x64` 体积异常的主因已经改到正确方向，即改用 CPU-only PyTorch；
- 但“缩包后功能不受影响”还不能宣称已完成验证，因为新增 smoke test 已证明 Linux 打包链路还有 metadata 漏打包问题；
- 本轮已继续补齐 Linux metadata 注入，下一次 run 应以：
  - `linux-x64` 体积下降
  - `linux-x64` smoke test 通过
  - `linux-arm64` smoke test 通过
  作为最终验收标准。

## 同日补充三：Linux any4 健康检查误判失败

### 新现象

在补齐 Linux `build_meta.json` 打包后，新的 run 中：

- `build-linux-x64` 通过 `--internal-build-info`
- `build-linux-x64` 通过 `--internal-run-rosbag-health --bag-type MCAP`
- 但 `--internal-run-any4-health --version v3.0` 失败

`linux-arm64` 也出现相同模式。

### 根因分析

过滤 Linux job 日志后，关键输出为：

- `ROSBAG_HEALTH_OK`
- `ANY4_HEALTH_FAIL`
- `missing=psutil_runtime`
- `bundled_error=ModuleNotFoundError: No module named 'psutil._psutil_windows'`
- 同时日志显示包内实际存在的是 `psutil_files=_psutil_linux.abi3.so`

这说明：

- 打包后的 Linux 程序已经能启动、能读 `build_meta.json`、能完成 rosbag 健康检查；
- 真正失败的是 `src/data_converter/any4_health.py` 中 `_probe_psutil_runtime()` 的平台判断写死成了 Windows 私有扩展：
  - `psutil._psutil_windows`
  - `ray.thirdparty_files.psutil._psutil_windows`
- 在 Linux 产物中，正确的目标应当是：
  - `psutil._psutil_linux`
  - `ray.thirdparty_files.psutil._psutil_linux`

因此，这是一个 bundled any4 运行时探针的跨平台误判问题，而不是“缩包后 any4 真不能用”的直接证据。

### 本轮修复

更新文件：

- `src/data_converter/any4_health.py`

修改内容：

1. 为 psutil runtime 探针增加按平台选择私有扩展名的逻辑
   - Windows: `_psutil_windows`
   - Linux: `_psutil_linux`
   - macOS: `_psutil_osx`
   - 其他 POSIX: `_psutil_posix`
2. bundled 轻量探针与 frozen 导入探针都改为使用平台对应模块名
3. 失败诊断中额外输出：
   - `private_module`
   - `ray_private_module`
   - `ray_psutil_files`

这样若后续仍失败，日志会直接告诉我们它实际找的是哪个平台模块、包里又有哪些文件。

### 本轮验证

已完成：

1. 读取 Linux x64 / arm64 job 日志，确认失败点一致为 `psutil._psutil_windows` 误判。
2. 本地执行：
   - `python -m py_compile src/data_converter/any4_health.py`
   - 结果通过。

### 当前结论更新

截至当前，关于“缩包以后这个包能不能正常用”的判断应更新为：

- 已证实的部分：
  - `linux-x64` 已切换到 CPU-only PyTorch 安装路径；
  - Linux 包能启动；
  - `build_meta.json` 已成功打入；
  - rosbag 健康检查可通过。
- 尚未最终证实的部分：
  - any4 bundled runtime 还需要经过这次平台修正后的下一轮 CI 验证。

也就是说，当前剩下的是一个明确、可复现、已修补的跨平台探针问题，不再是之前那种“包太大且没有可用性证明”的状态。

## 同日补充四：Linux any4 健康检查中的 ray 误判

### 新现象

在修正平台私有扩展名后，最新 run 中 Linux smoke test 仍失败，但日志已经收敛到新的明确原因：

- `private_module=psutil._psutil_linux`
- `psutil_files=_psutil_linux.abi3.so`
- `ray_psutil_dir_missing`
- `bundled_error=ModuleNotFoundError: No module named 'ray'`

说明：

- Linux 包内的 `psutil` 私有扩展已经能被正确识别；
- 当前失败是因为 `_probe_psutil_runtime()` 仍把 `ray.thirdparty_files.psutil` 当成硬依赖；
- 但当前 Linux PyInstaller 配置并没有打入 `ray`，因此这是 another false negative，而不是主程序必然不可用。

### 本轮修复

继续更新：

- `src/data_converter/any4_health.py`

调整内容：

1. `lightweight` 探针中：
   - 仅在 `ray` 包实际存在时，才进一步检查 `ray.thirdparty_files.psutil`
2. frozen 导入探针中：
   - 先判断 `ray.thirdparty_files.psutil` 是否存在；
   - 存在才导入并校验其私有扩展；
   - 不存在时不再误报失败

这样探针就与当前 Linux 打包现实保持一致：

- `psutil` 是必验项；
- `ray` 只在被实际打包时才参与校验。

### 本轮验证

已完成：

- `python -m py_compile src/data_converter/any4_health.py`
- 结果通过。

### 当前结论更新

截至本次修复，Linux 侧已连续排除三类误判：

1. `build_meta.json` 未打包
2. `psutil._psutil_windows` 平台写死
3. `ray.thirdparty_files.psutil` 被当成 Linux 硬依赖

下一轮 CI 的意义就非常集中：

- 如果通过，才能正式确认“缩包后 Linux 包仍可正常使用，且当前功能未被破坏”；
- 如果仍失败，剩下的就会是更接近真实运行依赖的问题，而不是 smoke test 本身的误判。

## 同日补充五：AgiBot 转 LeRobot 核心代码链路梳理

### 问题背景

需要快速定位仓库内 “AgiBot 转 LeRobot” 的核心实现位置，明确：

- 预检入口在哪里；
- 任务编排在哪里；
- 原始 AgiBot 包如何适配到 any4 结构；
- 最终如何调用 any4lerobot 并回写 LeRobot 产物。

### 根因分析

此前仓库文件较多，入口既有 UI、也有后端编排、适配层和 any4 桥接层。如果只看 `main.py` 或打包脚本，容易误判“核心转换逻辑”所在位置。

本轮梳理后确认，AgiBot 转 LeRobot 的主链路并不在 UI，而是在以下顺序中：

1. `src/data_converter/precheck.py`
   - 负责输入发现、运行时依赖校验、源数据类型识别、任务构建与输出目录冲突检查。
2. `src/data_converter/backend.py`
   - 负责执行计划调度、并发策略、任务状态流转，以及把单任务派发到 LeRobot runner。
3. `src/data_converter/adapters/raw_to_any4.py`
   - 当输入不是标准 any4 结构，而是 AgiBot 原始包（如 `aligned_joints.h5`、`state.json`、`*.mp4`）时，负责适配成最小 any4 数据集结构。
4. `src/data_converter/converters/lerobot_runner.py`
   - 负责单个任务的真实执行，包括解包、短路径 staging、适配、运行 any4lerobot、版本后处理、parquet/video/metadata 修复与最终校验。
5. `src/data_converter/any4lerobot_bridge.py`
   - 负责把仓库内的执行参数桥接到 `agibot2lerobot.agibot_h5`，并做运行时 patch，例如动态视频键与临时视频目录处理。

### 改动方案

本轮未修改业务代码，仅完成源码定位与链路确认，提炼出以下核心入口：

- `precheck.py:16` `run_precheck`
- `backend.py:36` `ConversionBackend`
- `backend.py:40` `ConversionBackend.run`
- `backend.py:141` `ConversionBackend._run_task`
- `raw_to_any4.py:26` `detect_source_kind`
- `raw_to_any4.py:34` `prepare_any4_source`
- `raw_to_any4.py:114` `_build_min_any4_dataset`
- `lerobot_runner.py:79` `run_lerobot_task`
- `lerobot_runner.py:210` `_build_any4lerobot_args`
- `lerobot_runner.py:316` `_convert_generated_output_to_target_version`
- `any4lerobot_bridge.py:26` `preload_any4_runtime`
- `any4lerobot_bridge.py:41` `run_any4lerobot_cli_result`
- `any4lerobot_bridge.py:154` `_patch_any4_image_config`

### 量化结果

- 已确认 5 个核心模块、12 个关键入口/函数位置。
- 已明确主执行链路为：
  `run_precheck -> ConversionBackend.run -> run_lerobot_task -> prepare_any4_source/run_any4lerobot_cli_result -> 后处理与校验`

### 验证方式

本轮执行的主要定位命令：

- `Get-ChildItem -Recurse -File src\data_converter`
- `Get-Content src\data_converter\backend.py -TotalCount 260`
- `Get-Content src\data_converter\precheck.py -TotalCount 260`
- `Get-Content src\data_converter\converters\lerobot_runner.py -TotalCount 320`
- `Get-Content src\data_converter\adapters\raw_to_any4.py -TotalCount 320`
- `Get-Content src\data_converter\any4lerobot_bridge.py -TotalCount 260`
- `Select-String` 定位关键函数行号

结果：

- 所有目标文件可正常读取；
- 已能稳定定位 AgiBot 转 LeRobot 的核心实现，不依赖 UI 层猜测。

### 当前结论与下一步建议

当前可以明确：

- 如果要看 “AgiBot 转 LeRobot 真正做转换” 的代码，优先看 `lerobot_runner.py`；
- 如果要看 “为什么某个输入能/不能转”，优先看 `precheck.py`；
- 如果要看 “原始包怎么被包装成 any4”，优先看 `raw_to_any4.py`；
- 如果要看 “最终怎么调用 any4 上游”，优先看 `any4lerobot_bridge.py`。

下一步若需要继续深入，建议按以下顺序看：

1. `run_precheck`
2. `ConversionBackend.run`
3. `run_lerobot_task`
4. `prepare_any4_source`
5. `run_any4lerobot_cli_result`

## 同日补充六：AgiBot 转 LeRobot 对 16 维 joint 与左右手拆分字段的兼容性判断

### 问题背景

需要确认当前转换器是否兼容两类原始 AgiBot 数据：

- 旧结构：16 维数据全部位于 `joint`；
- 新结构：原本 joint 中的两个关键维度被拆出，改为独立的 `right` / `left` 字段。

### 根因分析

本轮检查的核心对象是 LeRobot 转换链路中的原始包适配层 `src/data_converter/adapters/raw_to_any4.py`。

结论如下：

1. 当前适配器显式支持的核心键是：
   - `state/joint/current_value` -> 目标 16 维
   - `state/joint/position` -> 目标 16 维
   - `action/joint/position` -> 目标 16 维
   - `state/effector/position` -> 目标 2 维
   - `action/effector/position` -> 目标 2 维
2. 当输入存在 16 维 `joint.position` 时：
   - 适配器会直接保留 16 维 joint；
   - 同时将 `joint[:, 14:16]` 派生为 `effector/position`。
3. 当输入只有 14 维 joint，且存在显式 `effector/position` 时：
   - 适配器会把 joint 补齐到 16 维目标结构；
   - 同时保留原始 `effector/position`。
4. 当前 `raw_to_any4.py` 中没有直接读取独立 `left` / `right` / `gripper` 原始键的逻辑。
   - 代码检索 `left|right|gripper` 在该文件中无命中。

因此，当前转换器对 “拆成 left/right 独立字段” 的兼容性不是自动成立的，是否兼容取决于新数据是否仍同步提供旧适配器可识别的 `joint` 或 `effector` 键。

### 改动方案

本轮未修改代码，仅完成兼容性核查与证据确认。

### 量化结果

- 定位关键实现函数：
  - `raw_to_any4.py:208` `_build_proprio_stats`
  - `raw_to_any4.py:304` `_normalize_raw_array`
  - `raw_to_any4.py:335` `_derive_effector_from_joint`
- 运行针对性回归测试：
  - `python -m pytest -q test\python\test_raw_to_any4_adapter.py test\python\test_raw_video_mapping.py`
  - 结果：`10 passed in 1.95s`

### 验证方式

主要证据：

1. 16 维 joint 兼容测试通过：
   - `test_raw_to_any4_adapter.py:20`
   - 断言 `state/joint/position`、`action/joint/position` 原样保留；
   - 断言 `state/effector/position`、`action/effector/position` 等于 `joint[:, 14:16]`。
2. 14 维 joint + 显式 effector 兼容测试通过：
   - `test_raw_video_mapping.py:46`
   - 断言 joint 前 14 维保留；
   - 断言 effector 使用显式输入值。
3. 16 维 joint 优先于原始 effector 的兼容测试通过：
   - `test_raw_video_mapping.py:84`
   - 断言 effector 最终取自 `joint[:, 14:16]`，而不是外部 bogus effector。
4. 对 `raw_to_any4.py` 搜索 `left|right|gripper` 无结果，说明 LeRobot 原始适配链路当前没有直接消费新式 left/right 独立字段。

### 当前结论与下一步建议

当前可以明确：

- 兼容：
  - 旧 16 维全在 joint 的数据；
  - 14 维 joint + 2 维 `effector/position` 的数据。
- 暂不证实兼容：
  - 如果新数据把两个关键维度从 joint 拆走后，只保留独立 `left` / `right` 字段，而不再提供可识别的 `effector/position`，当前转换器大概率不兼容。

建议下一步：

1. 抽一个新格式样本，确认 H5 的真实键名；
2. 若键名确为类似 `state/left`、`state/right`、`action/left`、`action/right`，则需要在 `raw_to_any4.py` 中新增映射逻辑，把它们组装成 2 维 `effector/position`，必要时再补回 16 维 joint；
3. 增加对应回归测试后再正式放行新格式数据。

## 同日补充七：AgiBot 转 LeRobot 中 effector 左右手处理方式核查

### 问题背景

需要确认当前 AgiBot 转 LeRobot 核心代码中：

- 是否存在显式 `left_effector` / `right_effector` 处理；
- `effector` 到目标结构的映射是写死索引，还是按字段名灵活适配。

### 根因分析

本轮重点检查：

- `src/data_converter/adapters/raw_to_any4.py`
- `src/data_converter/rosbag/source_reader.py`

确认结果：

1. LeRobot 原始适配链路只处理统一的二维键：
   - `state/effector/position`
   - `action/effector/position`
2. 在 LeRobot 链路中，没有发现：
   - `left_effector`
   - `right_effector`
   - 基于左右手键名的动态拼接逻辑
3. LeRobot 链路里从 `joint` 派生 `effector` 的逻辑是写死的：
   - `_derive_effector_from_joint()` 直接返回 `joint_arr[:, 14:16]`
4. LeRobot 链路对 effector 的目标形状也写死为 `(2,)`：
   - `_build_proprio_stats()` 中 `state_shapes` / `action_shapes` 都把 `effector/position` 定义为二维。
5. Rosbag 链路存在额外的左右手别名逻辑，但这是另一条路径，不属于 LeRobot 核心转换：
   - `_read_effector_array()` 会尝试用 `left_gripper_joint1` / `right_gripper_joint1` 从 joint 中抽取二维 effector；
   - `_augment_joint_state_with_effector_aliases()` 会补充 `left_gripper` / `right_gripper` alias。

### 改动方案

本轮未修改代码，仅完成核心源码核查。

### 量化结果

关键证据位置：

- `raw_to_any4.py:210` / `221`
  - `effector/position` 目标形状定义为 `(2,)`
- `raw_to_any4.py:272` / `314`
  - 只识别 `state/effector/position` 与 `action/effector/position`
- `raw_to_any4.py:345`
  - 从 joint 派生 effector 使用固定切片 `joint_arr[:, 14:16]`
- `rosbag/source_reader.py:137-140`
  - Rosbag 路径按 `left_gripper_joint1` / `right_gripper_joint1` 查索引
- `rosbag/source_reader.py:156-161`
  - Rosbag 路径补 `left_gripper` / `right_gripper` alias

### 验证方式

本轮执行：

- 全仓检索 `left_effector|right_effector|effector/position|left_gripper|right_gripper|14:16`
- 读取 `raw_to_any4.py` effector 核心逻辑
- 读取 `rosbag/source_reader.py` 对照左右手 alias 逻辑

结果：

- LeRobot 路径未发现 `left_effector` / `right_effector` 支持；
- LeRobot 路径确认存在固定切片 `14:16`；
- Rosbag 路径存在左右手别名逻辑，但不能代表 LeRobot 路径已兼容。

### 当前结论与下一步建议

当前可明确：

- AgiBot 转 LeRobot 的 effector 处理是偏写死的，不是按左右手字段名灵活适配；
- 当前假设是：目标 effector 永远是 2 维，且在 16 维 joint 场景下，这两维固定来自索引 14 和 15；
- 如果后续原始数据改成新的显式左右手 effector 字段，需要在 `raw_to_any4.py` 新增字段映射，不能指望现有逻辑自动兼容。

## 同日补充八：对 test/agibot_test 两个 H5 样本进行 v2.1 实转验证

### 问题背景

需要用当前仓库中的现有 AgiBot 转 LeRobot 代码，实际验证 `test/agibot_test` 下两个 H5 样本的转换表现，并把结果直接放到该目录下，便于对比差异。

输入样本：

- `test/agibot_test/aligned_joints(1).h5`
- `test/agibot_test/aligned_joints(5)(1).h5`

### 根因分析

先对输入做结构核查后发现：

- 该目录下仅有两个 `.h5` 文件；
- 不包含现有正式预检要求的 `state.json`；
- 也不包含视频文件。

直接对 `test/agibot_test` 跑现有 LeRobot v2.1 预检时，结果为：

- `ok=False`
- 全局错误：
  `当前输入既不满足 any4lerobot AgiBotWorld 结构（task_info/*.json + observations/*），也不是可适配的原始包结构（aligned_joints.h5 + state.json）。`

因此，若不补足最小原始包结构，现有正式入口无法直接转换这两个 H5。

为测试“现有代码”在最小可接受输入下的真实表现，本轮在同目录下为每个 H5 创建了最小包装目录：

- 复制原始 H5 为 `aligned_joints.h5`
- 补一个最小 `state.json`
- 生成最小 `head.mp4` / `hand_left.mp4` / `hand_right.mp4`

然后使用仓库现有 `ConversionBackend` + `ConversionOptions(target=lerobot, lerobot_version=v2.1)` 正式跑转换。

### 改动方案

本轮未修改源码，仅在 `test/agibot_test` 下新增最小包装输入目录与实际转换输出目录。

新增包装输入：

- `test/agibot_test/aligned_joints(1)__rawpkg`
- `test/agibot_test/aligned_joints(5)(1)__rawpkg`

新增转换输出：

- `test/agibot_test/aligned_joints(1)__rawpkg__lerobot_v21`
- `test/agibot_test/aligned_joints(5)(1)__rawpkg__lerobot_v21`

### 量化结果

#### 1. 输入 H5 结构差异

`aligned_joints(1).h5`：

- `state/joint/position`: `(2352, 16)`
- `action/joint/position`: `(2352, 16)`
- `state/effector/position`: `(0,)`
- `action/effector/position`: `(0,)`

`aligned_joints(5)(1).h5`：

- `state/joint/position`: `(624, 14)`
- `action/joint/position`: `(624, 14)`
- `state/left_effector/position`: `(624, 1)`
- `state/right_effector/position`: `(624, 1)`
- `action/left_effector/position`: `(624, 1)`
- `action/right_effector/position`: `(624, 1)`
- 不存在标准 `state/effector/position` / `action/effector/position`

#### 2. 实际 v2.1 转换结果

样本一：`aligned_joints(1)__rawpkg__lerobot_v21`

- 转换成功：`success=1 failed=0`
- 输出 parquet 行数：`2352`
- `observation.state` 形状：`(2352, 16)`
- `actions` 形状：`(2352, 16)`
- 最后两维前 3 帧示例：
  - `observation.state[:, -2:]` -> `[[34.95, 34.92], ...]`
  - `actions[:, -2:]` -> `[[34.95, 34.92], ...]`
- 结论：最后两维被保留下来，符合“16 维 joint -> 末两维作为 effector” 的当前实现假设。

样本二：`aligned_joints(5)(1)__rawpkg__lerobot_v21`

- 转换成功：`success=1 failed=0`
- 输出 parquet 行数：`624`
- `observation.state` 形状：`(624, 16)`
- `actions` 形状：`(624, 16)`
- 最后两维前 3 帧示例：
  - `observation.state[:, -2:]` -> `[[0.0, 0.0], ...]`
  - `actions[:, -2:]` -> `[[0.0, 0.0], ...]`
- 结论：最后两维被补零，没有从 `left_effector/right_effector` 迁移到 v2.1 输出状态向量中。

### 验证方式

本轮执行的关键步骤与命令：

1. 目录检查：
   - `Get-ChildItem -Recurse test\agibot_test`
2. H5 结构检查：
   - Python + `h5py` 遍历两个样本所有 dataset
3. 正式预检验证：
   - Python 调 `ConversionBackend().precheck(...)`
   - 确认 H5-only 输入被现有入口拒绝
4. 生成最小包装输入目录：
   - Python 创建 `state.json` 与最小 mp4
5. 实际转换：
   - Python 调 `ConversionBackend().run(...)`
6. 结果比对：
   - 读取 v2.1 parquet 中 `observation.state` 与 `actions`
   - 对比最后两维是否为零

### 当前结论与下一步建议

当前可以明确：

- 现有代码对 `16 维 joint` 样本兼容；
- 对 `14 维 joint + left_effector/right_effector` 样本，虽然整体 v2.1 转换可成功完成，但左右手 effector 信息没有进入输出状态向量，最终被补零；
- 这说明当前 LeRobot 适配链路并不会自动把 `left_effector/right_effector` 合并成标准二维 `effector/position`。

建议下一步：

1. 在 `raw_to_any4.py` 中新增对：
   - `state/left_effector/position` + `state/right_effector/position`
   - `action/left_effector/position` + `action/right_effector/position`
   的识别与拼接；
2. 将其组装成标准二维 `state/effector/position` / `action/effector/position`；
3. 增加一个针对该新结构的回归测试，再重新跑这两个样本做 A/B 对比。

## 同日补充九：修复 left/right effector 到 LeRobot v2.1 的兼容性问题

### 问题背景

用户确认新格式样本并非标准 `state/effector/position` / `action/effector/position`，而是：

- `state/left_effector/position`
- `state/right_effector/position`
- `action/left_effector/position`
- `action/right_effector/position`

此前实际验证表明：

- 旧 16 维 joint 样本转换正常；
- 新 split-effector 样本虽然能完成 v2.1 转换，但输出 `observation.state` / `actions` 最后两维被补零，没有保留左右手 effector 信息。

### 根因分析

问题集中在 `src/data_converter/adapters/raw_to_any4.py`：

1. 适配器只识别统一键：
   - `state/effector/position`
   - `action/effector/position`
2. 从 joint 派生 effector 的逻辑写死为：
   - `joint_arr[:, 14:16]`
3. 当 raw 数据只有：
   - `left_effector/position`
   - `right_effector/position`
   而缺少统一 `effector/position` 时，适配器没有重建逻辑；
4. 当 joint 只有 14 维时，适配器只是零填充到 16 维，没有把 split effector 补到 joint 尾部。

因此，新结构数据在 LeRobot v2.1 输出中丢失了关键的左右手 effector 值。

### 改动方案

本轮修改了 `raw_to_any4.py`，核心策略如下：

1. 新增 split-effector 识别：
   - 从 `left_effector/position` 和 `right_effector/position` 组装标准二维 effector；
2. 保持原有优先级：
   - 若存在可用 16 维 joint，则仍优先从 `joint[:, 14:16]` 派生 effector；
   - 否则尝试标准 `effector/position`；
   - 再否则尝试 split left/right effector；
3. 对 14 维 joint 场景：
   - 在补齐到 16 维时，把解析出的二维 effector 写入 joint 的最后两维，而不是继续补零。

同时新增回归测试，覆盖：

- `14d joint + left/right effector -> 16d joint tail + 2d effector`

### 量化结果

#### 1. 回归测试

执行：

- `python -m pytest -q test\python\test_raw_to_any4_adapter.py test\python\test_raw_video_mapping.py`

结果：

- `11 passed in 0.60s`

其中新增失败用例先红后绿，验证修复生效。

#### 2. 用户样本复跑

重新生成输出：

- `test/agibot_test/aligned_joints(1)__rawpkg__lerobot_v21`
- `test/agibot_test/aligned_joints(5)(1)__rawpkg__lerobot_v21`

样本一（16d joint）：

- `observation.state[:, -2:]` 非零，保持原行为不变。

样本二（14d joint + split effector）：

- 修复后 `observation.state[:, -2:]` 非零；
- 修复后 `actions[:, -2:]` 非零；
- 与源 H5 中 `left/right_effector/position` 直接对比：
  - `state match = True`
  - `action match = True`

即：输出最后两维已正确等于源数据的左右手 effector 值，不再是零填充。

### 验证方式

本轮验证步骤：

1. 新增 split-effector 回归测试，并确认初始失败；
2. 修改 `raw_to_any4.py` 后回跑单测，确认转绿；
3. 跑完整 adapter 相关测试集；
4. 删除旧的 `__lerobot_v21` 目录后，重新对两个样本做 v2.1 转换；
5. 读取生成 parquet 中 `observation.state` / `actions` 最后两维；
6. 直接对比第二个源样本 H5 中 left/right effector 与输出向量尾部，确认逐值一致。

### 当前结论与下一步建议

当前可以明确：

- 现有仓库已修复 `left_effector/right_effector` 到 LeRobot v2.1 的兼容问题；
- 旧 16 维 joint 行为未回退；
- 新 split-effector 样本现在会把左右手 effector 正确写入输出 16 维状态向量尾部。

建议下一步：

1. 如需长期保留这两个样本作为验收基准，可把当前包装输入与输出目录保留在 `test/agibot_test`；
2. 若后续还会出现其他命名变体（如 `left_gripper` / `right_gripper`），可以继续在同一适配层补命名别名映射。
## 2026-03-27 GitHub 推送与 Actions 打包触发

### 问题背景

用户要求将当前仓库代码上传到 GitHub，并通过仓库中的 GitHub Actions 自动执行打包。

### 根因分析

检查后确认：

1. 仓库已配置 `origin = https://github.com/dyz9219/agibot-converter.git`；
2. 已存在 `.github/workflows/build.yml`，且在 `push main`、`push master`、`tag v*` 与手动触发时会执行多平台构建；
3. 当前工作区除源码改动外，还混有本地缓存、下载日志、临时输出目录等不应入库的产物，需要先收敛提交范围；
4. 根目录已有若干旧测试文件删除记录，同时 `test/python/` 下已有对应正式测试文件，符合仓库规范。

### 改动方案

本轮处理如下：

1. 先执行全量测试，确认当前代码可提交；
2. 更新 `.gitignore`，补充忽略：
   - `.hf-local/`
   - `out_*/`
   - `*.html`
   - `*.json`
   - `*.zip`
3. 提交时排除本地配置与一次性调试/下载产物，只提交源码、脚本、测试迁移及工作记录；
4. 推送到 `origin/main`，让 GitHub Actions 按现有 `build.yml` 自动启动打包。

### 量化结果

#### 1. 本地验证

执行：

- `pytest -q`

结果：

- `56 passed, 4 skipped in 75.50s`

#### 2. Actions 触发条件确认

检查 `.github/workflows/build.yml`，确认：

- `push` 到 `main` 会自动触发；
- 会执行：
  - `build-windows`
  - `build-linux-x64`
  - `build-linux-arm64`
- 构建完成后会上传对应 artifact。

### 验证方式

本轮验证步骤：

1. 检查 `git remote -v`，确认 GitHub 远程存在；
2. 检查 `.github/workflows/build.yml`，确认推送触发条件；
3. 执行 `pytest -q`，确认提交前测试通过；
4. 收敛 `.gitignore` 与提交范围，避免上传本地缓存和调试文件；
5. 推送后检查远端是否成功触发 Actions。

### 当前结论与下一步建议

当前结论：

- 仓库已具备“推送即自动打包”的 GitHub Actions 配置；
- 当前代码在本地测试通过，可以安全推送；
- 推送后应以 GitHub Actions 产物和日志作为最终打包验收依据。

建议下一步：

1. 若某个平台构建失败，优先查看对应 job 日志中的依赖安装与 PyInstaller 收集阶段；
2. 若需要对外发布，待 Actions artifact 生成后再做一次下载 smoke 验证。
## 2026-03-27 Linux any4 健康检查对缺失 ray 的最终修复

### 问题背景

用户在最新 GitHub Actions run `23638396726` 中发现：

- `build-linux-x64` 失败；
- 日志显示失败发生在 `Smoke test binary`；
- 用户要求尽快修复，并避免继续在同一问题上来回误判。

### 根因分析

本轮直接使用已登录的 GitHub CLI 拉取失败 job 的完整日志，确认不是构建失败，而是打包后二进制运行健康检查失败。

`build-linux-x64` 关键日志为：

- `--internal-build-info` 通过；
- `--internal-run-rosbag-health --bag-type MCAP` 通过；
- `--internal-run-any4-health --version v3.0` 失败；
- 失败诊断中包含：
  - `ANY4_HEALTH_FAIL`
  - `missing=psutil_runtime`
  - `bundled_error=ModuleNotFoundError:No module named 'ray'`

`build-linux-arm64` 拉取失败日志后，确认是完全相同的报错模式。

因此可以明确：

1. 失败不是 Linux x64 专属，而是两个 Linux job 共用的 bundled runtime 探针问题；
2. 失败不是 `psutil` 真缺失，因为日志同时给出：
   - `private_module=psutil._psutil_linux`
   - `psutil_files=_psutil_linux.abi3.so`
3. 真正的问题在 `src/data_converter/any4_health.py` 的 frozen 分支：
   - 代码直接执行 `find_spec("ray.thirdparty_files.psutil")`
   - 当顶层 `ray` 根本未打包时，这一步会抛 `ModuleNotFoundError: No module named 'ray'`
   - 该异常被上层错误归类为 `psutil_runtime` 缺失

换言之，本次 Linux CI 失败的本质是：

- `ray` 在当前 Linux 打包产物中本来就是可选/未打入；
- 但 any4 健康检查把“缺少顶层 ray 包”误判成 bundled runtime 不健康。

### 改动方案

本轮采用 TDD 方式修复：

1. 先在 `test/python/test_any4_health.py` 新增回归测试；
2. 构造 frozen 模式下：
   - `psutil` 与 `psutil._psutil_linux` 可导入；
   - `find_spec("ray.thirdparty_files.psutil")` 抛 `ModuleNotFoundError("No module named 'ray'")`
3. 先确认测试失败，再做最小修复；
4. 修复后补跑针对性测试和全量测试；
5. 通过后再推送触发新一轮 Actions。

### 实际修改

修改文件：

- `test/python/test_any4_health.py`
- `src/data_converter/any4_health.py`

具体修复：

1. 新增回归测试：
   - `test_frozen_psutil_probe_tolerates_missing_ray_package`
2. 在 `any4_health.py` 中新增 `_safe_find_spec(name)`：
   - 对 `importlib.util.find_spec(name)` 做 `ModuleNotFoundError` 保护；
   - 缺父包时返回 `None`，而不是直接异常中断。
3. 将 frozen 分支中的：
   - `find_spec("ray.thirdparty_files.psutil")`
   - `find_spec(ray_private_module)`
   改为 `_safe_find_spec(...)`

这样在 Linux bundled 包中若根本没有 `ray`：

- 健康检查会把它当作“ray 未打包，因此无需继续检查其私有 psutil 扩展”；
- 不会再把这个情况误报成 `psutil_runtime` 缺失。

### 量化结果

#### 1. TDD 红绿验证

执行：

- `pytest -q test/python/test_any4_health.py -k frozen_psutil_probe_tolerates_missing_ray_package`

结果：

- 修复前：失败，明确返回 `ModuleNotFoundError: No module named 'ray'`
- 修复后：通过

#### 2. 针对性测试

执行：

- `pytest -q test/python/test_any4_health.py`
- `pytest -q test/python/test_main_entry.py`
- `python -m py_compile src/data_converter/any4_health.py src/data_converter/main.py`

结果：

- `test_any4_health.py`: `5 passed`
- `test_main_entry.py`: `2 passed`
- `py_compile`: 通过

#### 3. 全量回归

执行：

- `pytest -q`

结果：

- `57 passed, 4 skipped in 95.60s`

### 验证方式

本轮验证链路：

1. 使用 `gh run view 23638396726 --job 68852796634 --log-failed` 拉取 `build-linux-x64` 完整失败日志；
2. 使用 `gh run view 23638396726 --job 68852796626 --log-failed` 确认 `build-linux-arm64` 同根因；
3. 新增 frozen 模式缺失 `ray` 的回归测试并先看红灯；
4. 做最小修复后，看该测试转绿；
5. 跑 `test_any4_health.py`、`test_main_entry.py` 与全量 `pytest -q`；
6. 通过后再进入提交流程。

### 当前结论与下一步建议

当前结论：

- 本次 Linux CI 失败根因已被精确定位为 any4 健康检查对缺失 `ray` 的误判；
- 修复已用回归测试锁住，并通过全量测试验证；
- 下一步应立即推送并观察新的 GitHub Actions run，重点确认：
  - `build-linux-x64` smoke test 通过；
  - `build-linux-arm64` smoke test 通过；
  - Windows 构建不受影响。

建议下一步：

1. 若新 run 仍失败，优先继续抓完整 `--log-failed`，不再依赖网页摘要；
2. 若 Linux 全绿，再考虑把 `gh run view --log-failed` 纳入固定排障流程，减少反复猜测。

## 2026-03-29 Linux any4 健康检查第二阶段：补齐动态导入依赖打包

### 问题背景

在提交 `fix(any4): tolerate missing ray in linux health probe` 后，新 run `23639550604` 中：

- `build-windows` 成功；
- `build-linux-x64` 仍失败；
- `build-linux-arm64` 仍失败；
- 两条 Linux 线依然都卡在 `Smoke test binary`。

这说明上一轮修复确实消除了一个误判点，但 Linux bundled 包里还存在下一层真实运行时问题。

### 根因分析

本轮继续沿“构建成功、smoke 失败”的思路排查，重点比较：

1. Windows 打包链路使用的 `DataConverterShell.spec`
2. Linux workflow 里直接写死的 `python -m PyInstaller ...` 命令

对比后确认一个关键差异：

- Windows spec 已显式收集：
  - `ray`
  - `torch`
  - `lerobot`
  - `psutil`
  - `ray.thirdparty_files.psutil`
- 但 Linux workflow 之前只收集：
  - `flet`
  - `flet_desktop`
  - `rosbags`
  - `tkinter`
  - 以及原样打入 `any4lerobot` 目录

而当前 any4 运行链路是：

- 主程序在 bundled 模式下把 `any4lerobot` 目录加入 `sys.path`
- 然后运行时动态导入 `agibot2lerobot.agibot_h5`
- `agibot_h5.py` 又会导入：
  - `lerobot.datasets.*`
  - `torch`
  - 以及相关运行时依赖

这类“运行时动态导入 but 主分析入口没有静态 import 到”的包，如果不在 PyInstaller 阶段显式收集，就很容易出现：

- 源码文件在包里；
- 但其真实依赖不在包里；
- 结果是健康检查从前一轮的 `ray` 误判继续推进后，进入真实导入阶段再失败。

因此，本轮新的高概率根因是：

- Linux workflow 没有像 Windows spec 那样补齐 any4 动态导入链所需的 PyInstaller 收集项；
- bundled smoke test 在继续往下执行 any4 健康检查时，遇到真实缺包。

### 改动方案

本轮不再继续单改探针代码，而是直接补齐 Linux 打包命令中的依赖收集项，使其与 Windows spec 的依赖覆盖级别对齐。

### 实际修改

修改文件：

- `.github/workflows/build.yml`

调整内容：

1. 对 `build-linux-x64` 的 PyInstaller 命令新增：
   - `--collect-all torch`
   - `--collect-all lerobot`
   - `--collect-all ray`
   - `--collect-all psutil`
   - `--collect-all ray.thirdparty_files.psutil`
   - `--hidden-import psutil._psutil_linux`
   - `--hidden-import ray.thirdparty_files.psutil._psutil_linux`
2. 对 `build-linux-arm64` 的 PyInstaller 命令同步新增相同收集项。

这样 Linux 构建时会把 any4 在 bundled 模式下运行所需的动态依赖一并打入包内，而不只是打入 `any4lerobot` 源码目录。

### 量化结果

本轮本地静态验证：

执行：

- 读取 `.github/workflows/build.yml` 关键命令行
- Python 断言 workflow 文本中包含新增的 `collect-all` / `hidden-import` 参数

结果：

- `workflow-check-ok`

### 验证方式

本轮验证步骤：

1. 查询 run `23639550604` 的 jobs，确认 Windows 成功、Linux 仍在 smoke 阶段失败；
2. 对比 Windows spec 与 Linux workflow 的 PyInstaller 收集项；
3. 读取 `any4lerobot/agibot2lerobot/agibot_h5.py` 的真实导入链；
4. 将 Linux workflow 的动态依赖收集项补齐；
5. 用本地脚本断言 workflow 文字内容确实包含新增参数。

### 当前结论与下一步建议

当前结论：

- Linux 失败已从“探针误判”进入“动态导入依赖未打包”的更真实阶段；
- 本轮修复方向是把 Linux 的 PyInstaller 依赖收集能力补齐到接近 Windows spec 的覆盖水平；
- 下一步应重新推送并观察新的 run，重点确认 Linux smoke 是否继续向后推进。

建议下一步：

1. 若下一轮仍失败，继续优先抓失败日志，而不是再根据网页摘要猜测；
2. 若 Linux 通过，可考虑后续再评估是否要把 Linux 打包逻辑进一步收敛到统一 spec 或共享参数生成方式，减少平台间漂移。
