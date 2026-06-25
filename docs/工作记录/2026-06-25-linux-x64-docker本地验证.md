# 2026-06-25 Linux x64 Docker 本地验证

## 问题背景

用户要求使用本机 Docker Desktop 验证 Linux x64 修复，而不是只依赖本地 Windows 测试或 GitHub Actions。

本轮验证目标是尽量复现 Linux x64 Actions 链路：

- 在 Docker Desktop 的 Linux x86_64 容器内构建 PyInstaller onefile；
- 运行构建后的 `DataConverterShell` smoke；
- 将最终交付物打包为 `.tar.gz`；
- 解包后检查执行权限；
- 解包后的二进制再次运行 `--internal-build-info`。

## 现象与环境处理

Docker Desktop 初始 daemon 未运行，启动后 server 显示：

- Docker Server：`27.0.3`
- OS/Arch：`linux/x86_64`

首次容器内 `apt-get update` 失败，错误指向 Docker Desktop 的透明代理：

- `connecting to 127.0.0.1:7890`
- 宿主机 `127.0.0.1:7890` 当前无服务监听

排查 `C:\Users\dyz\AppData\Roaming\Docker\settings.json` 后确认 Docker Desktop 配置了：

- `proxyHttpMode: manual`
- `overrideProxyHttp: http://127.0.0.1:7890`
- `vpnKitTransparentProxy: true`

为完成验证，本轮临时备份并清空 Docker Desktop 失效代理配置，重启 Docker 后确认容器内 `apt-get update` 成功。验证结束后已恢复原始 `settings.json`，并再次重启 Docker Desktop。

## 根因分析

本轮验证关注的核心根因仍是 Linux artifact 直接以 GitHub zip 形式分发时可能丢失执行位。修复后的方案是：

- 不直接上传裸 `dist/DataConverterShell`；
- 先打 `DataConverterShell-Linux-x64.tar.gz`；
- CI 中解包并执行 `test -x`；
- 解包后的 `DataConverterShell` 再跑一次 `--internal-build-info`。

Docker 本地验证还发现两个本机 Ubuntu 容器特有的构建前置依赖：

- PyInstaller Linux 分析需要 `binutils` 提供 `objdump`；
- 使用 Ubuntu apt 的 system Python 3.12 venv 构建 onefile 时，需要 `libpython3.12` 提供 `libpython3.12.so.1.0`。

GitHub Actions 使用 `actions/setup-python` 的托管 Python，上一轮 Actions 日志已显示能找到 Python shared library，因此这两个前置依赖属于本地 Docker 验证环境补齐项，不是当前 workflow 必需改动。

## 验证命令与结果

在 Docker Desktop Linux x86_64 容器内完成：

- 安装系统依赖：`python3`、`python3-venv`、`python3-pip`、`python3-tk`、`git`、`tar`、`ca-certificates`、`libgl1`、`libglib2.0-0`、`binutils`、`libpython3.12`
- 安装 Python 依赖：
  - CPU `torch==2.2.2`
  - `torchvision==0.17.2`
  - `flet`、`flet-desktop`、`rosbags`、`ray`、`lerobot==0.4.4` 等 workflow 同级依赖
- 运行 Linux x64 PyInstaller onefile 构建命令；
- 构建后执行：
  - `./dist/DataConverterShell --internal-build-info`
  - `./dist/DataConverterShell --internal-run-rosbag-health --bag-type MCAP`
  - `./dist/DataConverterShell --internal-run-any4-health --version v3.0`
  - `./dist/DataConverterShell --internal-run-any4-health --version v2.1`
  - `./dist/DataConverterShell --internal-run-any4-health --version v2.0`
- 执行 tar 打包与解包验证：
  - `tar -czf dist/DataConverterShell-Linux-x64.tar.gz -C dist DataConverterShell`
  - `tar -xzf dist/DataConverterShell-Linux-x64.tar.gz -C /tmp/agibot-linux-x64-package-check`
  - `test -x /tmp/agibot-linux-x64-package-check/DataConverterShell`
  - `/tmp/agibot-linux-x64-package-check/DataConverterShell --internal-build-info`

关键输出：

- `ROSBAG_HEALTH_OK`
- `ANY4_HEALTH_OK` for `v3.0`
- `ANY4_HEALTH_OK` for `v2.1`
- `ANY4_HEALTH_OK` for `v2.0`
- 解包后的 `DataConverterShell` 可执行并能输出 build info

量化结果：

- `dist/DataConverterShell`：`418M`
- `dist/DataConverterShell-Linux-x64.tar.gz`：`414M`
- 解包后权限：`-rwxr-xr-x`

## 当前结论

Linux x64 修复已通过本机 Docker Desktop 的真实 Linux x86_64 构建与运行验证。修复后的 `.tar.gz` 分发方式可以保留执行权限，解包后的 `DataConverterShell` 能直接运行，并且 bundled rosbag 与 any4 健康检查均通过。

## 清理与恢复

- 已删除临时容器：`codex-agibot-linux-x64-verify`
- 已删除临时镜像：`codex/agibot-linux-x64-verify:deps`
- 已恢复 Docker Desktop 原始 `settings.json`
- 已重启 Docker Desktop 使原配置生效
- 未将 Linux 构建产物写入仓库工作区；构建发生在容器内部 `/work`

# 2026-06-25 GitHub Actions 推送验证

## 问题背景

用户追问是否已经上传代码到 GitHub Actions 构建。前一轮只完成了本地 Docker 验证，还没有推送远端触发 CI。

## 改动与推送

提交并推送到 `main`：

- commit：`2719b20`
- subject：`fix(ci): preserve linux artifact executable bit`
- push：普通推送到 `origin/main`

本次提交包含：

- Linux x64 / ARM64 artifact 改为上传 `.tar.gz`；
- CI 中新增解包后 `test -x` 与 `--internal-build-info` 验证；
- packaging 测试约束；
- 2026-06-25 Docker 本地验证记录。

## Actions 验证结果

GitHub Actions run：

- run id：`28159363515`
- workflow：`Build Multi-Platform`
- 触发方式：push
- head sha：`2719b20907fea07492f2a033e24293a1b5ce0113`
- 结果：`success`
- URL：`https://github.com/dyz9219/agibot-converter/actions/runs/28159363515`

Job 结果：

- `build-linux-x64`：success，耗时约 `7m56s`
- `build-linux-arm64`：success，耗时约 `5m20s`
- `build-windows`：success，耗时约 `6m59s`

Linux x64 关键验证：

- `ROSBAG_HEALTH_OK`
- `ANY4_HEALTH_OK` for `v3.0`
- `ANY4_HEALTH_OK` for `v2.1`
- `ANY4_HEALTH_OK` for `v2.0`
- `Package Linux artifact` 步骤通过
- 该步骤实际执行：
  - `tar -czf dist/DataConverterShell-Linux-x64.tar.gz -C dist DataConverterShell`
  - `tar -xzf dist/DataConverterShell-Linux-x64.tar.gz -C /tmp/agibot-linux-x64-package-check`
  - `test -x /tmp/agibot-linux-x64-package-check/DataConverterShell`
  - `/tmp/agibot-linux-x64-package-check/DataConverterShell --internal-build-info`

Linux x64 artifact：

- 上传路径：`dist/DataConverterShell-Linux-x64.tar.gz`
- artifact name：`DataConverterShell-Linux-x64`
- artifact id：`7873802265`
- final size：`446,979,282` bytes

## 当前结论

修复已经推送到 GitHub 并通过完整多平台 Actions 验证。Linux x64 不再上传裸 ELF，而是上传包含可执行位的 `.tar.gz`，并且 CI 已验证下载前的最终压缩包解包后仍可执行。

# 2026-06-25 Linux x64 artifact 下载后真数据转换验证

## 问题背景

用户要求确认是否已经把 `build-linux-x64` artifact 下载到本地验证，并明确要求使用以下目录下的 zip 包测试数据转换功能：

- `E:\Users\dyz\Documents\WXWork\1688858286666779\Cache\File\2026-06\task_2059925964389343234`

## 验证输入

样本目录中存在 50 个 zip。本轮先选取一个代表性 zip 做真实转换 smoke：

- `test_gaok_1_1_172311_86.zip`
- 原始大小：约 `13 MB`

## artifact 下载与解包

最初使用 `gh run download 28159363515 -n DataConverterShell-Linux-x64` 下载超过 10 分钟超时，并留下 `gh` 进程。已停止残留进程后改用 GitHub artifact API 下载：

```powershell
$artifactDir = Join-Path $env:TEMP "agibot-linux-x64-artifact-28159363515"
$zipPath = Join-Path $artifactDir "DataConverterShell-Linux-x64-artifact.zip"
$token = gh auth token
curl.exe -L --fail --retry 3 --retry-delay 5 `
  -H "Authorization: Bearer $token" `
  -H "Accept: application/vnd.github+json" `
  -o $zipPath `
  "https://api.github.com/repos/dyz9219/agibot-converter/actions/artifacts/7873802265/zip"
```

下载结果：

- artifact zip：`446,979,282` bytes
- 解开 GitHub artifact zip 后包含：
  - `DataConverterShell-Linux-x64.tar.gz`
  - 大小：`446,845,199` bytes

## Docker 验证命令

使用下载到本地的 artifact，而不是本地构建产物。Docker 挂载：

- artifact：`%TEMP%\agibot-linux-x64-artifact-28159363515\artifact-zip-expanded`
- 输入数据：`E:\Users\dyz\Documents\WXWork\1688858286666779\Cache\File\2026-06\task_2059925964389343234`
- 输出目录：`%TEMP%\agibot-linux-x64-realdata-verify`

执行命令核心如下：

```bash
tar -xzf /artifact/DataConverterShell-Linux-x64.tar.gz -C /work/bin
ls -l /work/bin/DataConverterShell
test -x /work/bin/DataConverterShell
/work/bin/DataConverterShell --internal-build-info
AGIBOT_FORCE_BUNDLED_ANY4=1 /work/bin/DataConverterShell \
  --internal-run-conversion \
  --input-path /data/test_gaok_1_1_172311_86.zip \
  --output-path /out \
  --target lerobot \
  --version v3.0 \
  --concurrency 1
```

## 验证结果

artifact 解包后权限：

- `-rwxr-xr-x`
- 文件大小：`450,999,472` bytes

下载 artifact 的 build info：

- `profile`: `linux-x64`
- `git_commit`: `2719b20907fea07492f2a033e24293a1b5ce0113`
- `git_dirty`: `false`
- `source_fingerprint`: `linux-ci`

真实转换命令输出：

```text
RUN_SUMMARY total=1 success=1 failed=0 skipped=0
```

生成产物包括：

- `any4_input_diag.json`
- `data/chunk-000/file-000.parquet`
- `manifest.json`
- `meta/info.json`
- `meta/stats.json`
- `meta/tasks.parquet`
- `videos/.../*.mp4`

`manifest.json` 关键结果：

- `status`: `success`
- `target`: `lerobot`
- `lerobot_version`: `v3.0`
- `conversion_mode`: `lerobot_real`
- `input_kind`: `raw`
- `adapter_used`: `true`
- `runtime_mode`: `bundled`
- `elapsed_seconds`: `46.409091`

`meta/info.json` 关键结果：

- `codebase_version`: `v3.0`
- `total_episodes`: `1`
- `total_frames`: `460`
- `total_tasks`: `1`

## 当前结论

已完成“下载 GitHub Actions 的 `build-linux-x64` artifact 到本地，再用用户指定目录下 zip 包进行真实数据转换”的验证。验证对象是单个代表性 zip：`test_gaok_1_1_172311_86.zip`。结果为转换成功，且运行模式为 bundled runtime。

本轮尚未对该目录下全部 50 个 zip 做全量批处理验证；若需要全量验证，应单独运行目录级输入并预留较长时间。
