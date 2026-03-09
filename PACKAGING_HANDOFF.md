# Data Converter 打包协同手册（Fast/Full 双方案）

## Claude Code 执行协议（强制）
- 任何打包动作前，**必须先向用户提问**：
  - `你要 fast 还是 full 打包模式？`
- 如果用户没有明确回答 `fast` 或 `full`，**不得开始打包**。
- 打包命令必须显式带 `-Profile`，禁止无 profile 默认打包。

## 目标
- 解决两个问题：
  - 日常改代码后打包太慢；
  - 担心“改了代码但包里没有更新”。
- 通过 `fast/full` 双档 + 构建指纹门禁，兼顾速度和可验证性。

## 关键规则（必须遵守）
- `build_exe.ps1` 支持 `-Profile fast|full`。
- **如果打包命令没有显式指定 `-Profile`，必须先由 Claude Code 询问用户选择 fast/full。**
- 脚本层面也会拒绝无 `-Profile` 执行。
- 不允许默认走某个 profile。

## 两套打包方案

### Fast（推荐用于日常迭代）
- 目的：快速验证“代码已打进包 + UI/预检/HDF5 链路可用”。
- 依赖范围：轻依赖（不收集 `ray/torch/lerobot/agibot_utils/rosbags`）。
- 产物路径：`dist/DataConverterShell-fast/DataConverterShell-fast.exe`
- 适用场景：频繁改代码、快速回归、确认包内容最新。

### Full（推荐用于对外发布/联调）
- 目的：完整功能交付（含 LeRobot 非 HDF5 和 Rosbag）。
- 依赖范围：全量收集（`ray/torch/lerobot/agibot_utils/rosbags`）。
- 产物路径：`dist/DataConverterShell-full/DataConverterShell-full.exe`
- 适用场景：发同事、发测试、发布候选包。

## 标准命令

### 1) 未指定 profile（预期行为：直接失败）
```powershell
Set-Location D:\workspace\work\bwy\agibot-converter
./scripts/build_exe.ps1
```
预期输出：`缺少 -Profile，请使用 -Profile fast 或 -Profile full。`

### 2) 显式 fast
```powershell
Set-Location D:\workspace\work\bwy\agibot-converter
./scripts/build_exe.ps1 -Profile fast
```

### 3) 显式 full
```powershell
Set-Location D:\workspace\work\bwy\agibot-converter
./scripts/build_exe.ps1 -Profile full
```

### 4) 清理后打包
```powershell
./scripts/build_exe.ps1 -Profile fast -Clean
./scripts/build_exe.ps1 -Profile full -Clean
```

## 构建指纹门禁（防“改了没打进去”）

### 门禁脚本
```powershell
Set-Location D:\workspace\work\bwy\agibot-converter
./scripts/verify_build_fingerprint.ps1 -ExePath "dist\DataConverterShell-fast\DataConverterShell-fast.exe"
./scripts/verify_build_fingerprint.ps1 -ExePath "dist\DataConverterShell-full\DataConverterShell-full.exe"
```

### 通过标准
- 输出 `FINGERPRINT_CHECK_OK` 才允许继续后续验证或发包。
- 任一 mismatch（`git_commit` / `source_fingerprint`）都视为不可发包。

## 验证门禁矩阵

### Fast 包门禁（最小必过）
1. 指纹门禁通过：`verify_build_fingerprint.ps1`
2. 基础健康：EXE 能启动，`--internal-build-info` 可返回 JSON
3. HDF5/预检路径可用（按你的最小 smoke 数据）

### Full 包门禁（发布必过）
1. 指纹门禁通过：`verify_build_fingerprint.ps1`
2. any4 健康检查：
```powershell
dist\DataConverterShell-full\DataConverterShell-full.exe --internal-run-any4-health --version v3.0
dist\DataConverterShell-full\DataConverterShell-full.exe --internal-run-any4-health --version v2.1
dist\DataConverterShell-full\DataConverterShell-full.exe --internal-run-any4-health --version v2.0
```
3. rosbag 健康检查：
```powershell
dist\DataConverterShell-full\DataConverterShell-full.exe --internal-run-rosbag-health --bag-type MCAP
```
4. LeRobot 四版本 smoke（建议）

## 交付物清单（发同事前）
- fast/full 对应 dist 目录
- 打包日志（建议 `build_exe.log`）
- 指纹门禁通过输出
- full 包健康检查输出（any4 + rosbag）
- （建议）smoke summary

## 本机复现同事环境（强制 bundled）
- 背景：你本机常有 Python/依赖，转换器可能走 external python 回退路径，导致“本机正常、同事失败”难复现。
- 配置名称：`DATA_CONVERTER_FORCE_BUNDLED`（环境变量，调试开关，兼容旧名 `AGIBOT_FORCE_BUNDLED`）
- 启用值：`1`（等价真值：`true/yes/on`）
- 新增调试开关示例：`DATA_CONVERTER_FORCE_BUNDLED=1`
- 作用范围：仅 LeRobot 非 HDF5（`v3.0/v2.1/v2.0`）强制禁用 external python，统一走 bundled 路径。
- 默认行为（重要）：打包 EXE（`sys.frozen=True`）默认即 `force_bundled=1`，无需手动设置环境变量。
- 如需临时关闭强制 bundled（仅调试）：设置 `DATA_CONVERTER_FORCE_BUNDLED=0`。
- 用法（当前 PowerShell 会话）：
```powershell
$env:DATA_CONVERTER_FORCE_BUNDLED = "1"
dist\DataConverterShell-full\DataConverterShell-full.exe
```
- 预期诊断特征（manifest/runtime_diagnostic）：
  - `force_bundled=1`
  - `mode=bundled`（或失败时 `fallback` 仍显示 `force_bundled=1`）
- 结束后可恢复：
```powershell
Remove-Item Env:DATA_CONVERTER_FORCE_BUNDLED -ErrorAction SilentlyContinue
```

## 常见问题
- Q: 为什么 fast 包不能覆盖完整 LeRobot/Rosbag？
  - A: fast 目标是加速迭代和验证“代码已进包”，完整功能验证留给 full，避免每次都付出全量收集成本。
- Q: 未指定 profile 为什么必须先问？
  - A: 防止误打错包，确保打包模式可追溯、可复现。
