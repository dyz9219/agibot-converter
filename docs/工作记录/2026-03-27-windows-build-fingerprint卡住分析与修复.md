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
