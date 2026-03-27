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
- 打包验证产生的测试输出、smoke 结果和临时文件，统一放在仓库内 `test/` 目录下分类保存，不要再写到仓库根目录。
- 正式测试脚本统一放在 `test/python/`，不要再在仓库根目录新增 `test_*.py`。

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

## 本次回归问题说明：打包后 EXE 内置 any4 并发路径报错

### 现象

- 修改后，打包产物在执行内置 any4 转换时直接失败。
- 错误日志位于用户输出目录下的 `any4_error.log`，例如：
  - `D:\下载\windows-x64-agibot-isaac-downloader-真机真机\downloads\转换后\...\any4_error.log`
- 关键错误为：

```text
ModuleNotFoundError: No module named 'psutil._psutil_windows'
```

更准确地说，调用链是：

- `DataConverterShell-full.exe --internal-run-any4lerobot ...`
- `agibot_h5.py` 导入 `ray`
- `ray` 导入 `ray.thirdparty_files.psutil`
- `ray.thirdparty_files.psutil._pswindows` 再导入 `._psutil_windows`
- 打包产物里缺少这个 `.pyd`，因此 EXE 子进程启动失败

### 根因

这次并不是代码转换逻辑坏了，而是打包收集规则不完整。

之前打包脚本只显式收了顶层 `psutil` 的 Windows 扩展：

- `psutil._psutil_windows`

但 `ray` 在 Windows 上实际使用的是它自己 vendored 的那份 `psutil`：

- `ray.thirdparty_files.psutil`
- `ray.thirdparty_files.psutil._psutil_windows`

旧包中的实际情况已经核对过：

- 存在：`dist\\DataConverterShell-full\\_internal\\psutil\\_psutil_windows.pyd`
- 缺失：`dist\\DataConverterShell-full\\_internal\\ray\\thirdparty_files\\psutil\\_psutil_windows.pyd`

因此根因可以定性为：

- 打包时遗漏了 `ray.thirdparty_files.psutil._psutil_windows`
- 导致 EXE 自拉起子进程跑 any4 时，`ray` 初始化失败

### 已完成的代码修复

以下文件已经补好打包规则：

- `scripts/build_exe.ps1`
- `scripts/build_exe_onefile.ps1`
- `DataConverterShell.spec`
- `src/data_converter/any4_health.py`

具体补充内容：

1. 新增 hidden import：
   - `ray.thirdparty_files.psutil._psutil_windows`

2. 新增 collect：
   - `ray.thirdparty_files.psutil`

3. 健康检查扩展为同时探测：
   - `psutil._psutil_windows`
   - `ray.thirdparty_files.psutil._psutil_windows`

### 需要 Claude Code 重新执行的动作

请 Claude Code 不要只“重打包”，而是按下面顺序操作。

#### 1. 确认修复已在工作区

重点确认以下内容已经存在：

- `scripts/build_exe.ps1`
  - 包含：
    - `--hidden-import ray.thirdparty_files.psutil._psutil_windows`
    - `--collect-all ray.thirdparty_files.psutil`
- `scripts/build_exe_onefile.ps1`
  - 包含同样两项
- `DataConverterShell.spec`
  - `hiddenimports` 包含：
    - `ray.thirdparty_files.psutil._psutil_windows`
  - 并且有：
    - `collect_all('ray.thirdparty_files.psutil')`

#### 2. 重新 full 打包

在仓库根目录执行：

```powershell
Set-Location D:\workspace\work\bwy\agibot-converter
powershell -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1 -Profile full
```

#### 3. 打包完成后先验文件，不要直接交付

至少检查下面这个文件是否已经进入包中：

```powershell
Get-ChildItem -Recurse `
  D:\workspace\work\bwy\agibot-converter\dist\DataConverterShell-full\_internal\ray\thirdparty_files\psutil `
  | Select-Object Name, FullName
```

验收标准：

- 必须能看到：
  - `_psutil_windows.pyd`

如果这个文件还不存在，说明新包仍然不可用，不要继续交付。

#### 4. 再做一次 EXE 级健康检查

建议执行：

```powershell
& D:\workspace\work\bwy\agibot-converter\dist\DataConverterShell-full\DataConverterShell-full.exe `
  --internal-run-any4-health --version v3.0
```

预期：

- 退出码为 `0`

如果失败，需要把新的错误日志带回来看，不要跳过这一步。

#### 5. 再用真实数据做一次最小 smoke

建议优先用你之前复现问题的数据目录做一次，并将输出写到 `test/packaging/` 下的新目录：

```powershell
& D:\workspace\work\bwy\agibot-converter\dist\DataConverterShell-full\DataConverterShell-full.exe `
  --internal-run-conversion `
  --input-path "D:\下载\windows-x64-agibot-isaac-downloader-真机真机\downloads\真机真机_2019353507798704130_20260306_181055" `
  --output-path "D:\workspace\work\bwy\agibot-converter\test\packaging\full-smoke-v30-c8" `
  --target lerobot `
  --version v3.0 `
  --concurrency 8
```

观察点：

- 是否还报 `psutil._psutil_windows` 相关错误
- 输出目录下是否还有新的 `any4_error.log`
- 任务是否能正常完成

#### 6. 并发验收基线（当前机器）

基于当前源码实测，这台机器上：

- `8` 并发是当前最值得验证的基线
- `16` 并发不应作为“必须更快”的发布门槛

因此打包验证建议分两步：

1. 基线 smoke：
- `--concurrency 8`
- 作为当前机器的主要验收值

2. 可选压力测试：
- `--concurrency 16`
- 仅用于观察更高并发下是否出现崩溃、缺包、竞态或明显异常
- 不把“16 必须优于 8”作为当前机器的硬性验收标准

### 给 Claude Code 的一句话结论

这次要修的不是转换逻辑，而是 PyInstaller 收集规则。

请 Claude Code 按以下目标执行并验收：

- 让打包产物包含 `ray.thirdparty_files.psutil._psutil_windows`
- 重新 full 打包
- 验证 `_internal\\ray\\thirdparty_files\\psutil\\_psutil_windows.pyd` 已存在
- 再跑 EXE 级 health check 和最小 smoke
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
- 如果做了打包 smoke，保留到 `test/packaging/` 对应目录，不要把临时结果散落到仓库根目录

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
