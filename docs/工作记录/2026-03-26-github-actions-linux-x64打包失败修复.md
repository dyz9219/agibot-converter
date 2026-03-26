## 问题背景

GitHub Actions 运行 `Build Multi-Platform`（run id: `23536172346`）失败，用户要求定位失败原因并修复。失败链接：

- `https://github.com/dyz9219/agibot-converter/actions/runs/23536172346`

本次失败并非全部平台失败，只有 `build-linux-x64` 失败，`build-windows` 与 `build-linux-arm64` 均成功。

## 根因分析

通过 GitHub CLI 拉取失败日志后，确认真正报错为：

```text
struct.error: 'I' format requires 0 <= number <= 4294967295
```

调用栈位于 PyInstaller 的 `CArchiveWriter`，发生在 `Building PKG (CArchive) DataConverterShell.pkg` 阶段，说明 `--onefile` 模式下生成的单文件封包超过了 PyInstaller 单个 CArchive 的 4GB 上限。

进一步从日志中确认：

1. GitHub 上本次构建使用的提交里，`pyproject.toml` 仍然是旧的宽松依赖：
   - `torch>=2.0`
   - `lerobot>=0.1`
2. 因为依赖未 pin，`ubuntu-latest` x64 实际安装了更大的新版本组合：
   - `torch-2.10.0`
   - `torchvision-0.25.0`
   - `lerobot-0.5.0`
3. 同时 PyInstaller 又自动收集了大量 CUDA/NVIDIA 依赖：
   - `nvidia-cublas-cu12`
   - `nvidia-cudnn-cu12`
   - `nvidia-cusolver-cu12`
   - `nvidia-cusparse-cu12`
   - `nvidia-nccl-cu12`
   - 以及其它 `cu12` 组件
4. 这些大体积依赖在 `linux-x64` 的 `--onefile` 模式下合并进单个 `.pkg`，最终超出 4GB。

这也解释了为什么：

- `windows` 没炸：Windows 走的是自定义 full 打包流程，不是这里的 Linux onefile 路径。
- `linux-arm64` 没炸：ARM64 依赖集合与 x64 不同，未达到该 4GB 上限。

## 改动方案

本轮做了两部分修复：

1. 将 `linux-x64` workflow 从 `--onefile` 改为 `--onedir`
   - 避免把所有依赖塞进单个 CArchive
   - 保留 Linux 产物可执行，只是改为目录形式上传
2. 将本地已经验证过的依赖 pin 一并纳入提交
   - 避免 CI 再次漂移到更大的新依赖组合

## 修改内容

修改文件：

- `.github/workflows/build.yml`
- `pyproject.toml`

其中 `build-linux-x64` 的关键变化：

- `--onefile` -> `--onedir`
- `chmod +x dist/DataConverterShell` -> `chmod +x dist/DataConverterShell/DataConverterShell`
- artifact 上传路径调整为 `dist/DataConverterShell/`

## 验证方式

已完成的验证：

1. 使用 GitHub CLI 拉取失败 job 日志并确认报错栈。
2. 对比工作区 `pyproject.toml` 与失败提交中的 `pyproject.toml`，确认 GitHub 上次构建仍使用旧宽松依赖。
3. 结合日志确认安装了 CUDA/NVIDIA 依赖，并在 `Building PKG (CArchive)` 阶段失败。

建议验证：

1. 提交上述修复后重新触发 GitHub Actions。
2. 检查 `build-linux-x64` 是否成功上传目录型 artifact。
3. 如需进一步瘦身，再评估 Linux 是否切 CPU-only torch 或增加更细粒度的 PyInstaller excludes。

## 当前结论

本次 Actions 失败的直接原因不是业务代码，而是：

- GitHub 上使用了旧的宽松依赖元数据，导致 Linux x64 拉入大体积 CUDA 依赖；
- `PyInstaller --onefile` 在 `linux-x64` 上命中 4GB 单文件封包上限。

当前修复方案是先保证 CI 能稳定产物输出，再通过依赖 pin 降低后续漂移风险。

## 2026-03-26 第二轮修复：三平台依赖解算失败

### 现象
- 新 run `23583208914` 中，`build-windows`、`build-linux-x64`、`build-linux-arm64` 全部失败。
- 三个 job 都没有进入打包阶段，统一失败在安装依赖步骤。

### 根因
- 三个平台的失败日志一致，都是 pip 依赖解算冲突：

```text
The conflict is caused by:
  data-converter-shell depends on torchvision==0.17.2
  lerobot 0.4.4 depends on torchvision<0.26.0 and >=0.21.0
```

- 也就是说，前一轮把 `pyproject.toml` 直接 pin 到“本地已验证可工作版本”后，虽然运行时组合可用，但它和 `lerobot 0.4.4` 的官方 metadata 本身冲突，导致 CI 的 `pip install -e .[dev]` 在三平台全部提前失败。

### 修复方案
- 保留前一轮的 `linux-x64` `--onedir` 修复；
- 改为在 GitHub Actions 中显式安装“已验证可工作”的构建环境；
- 对 `lerobot==0.4.4` 与本项目自身使用 `--no-deps` 安装，绕开 pip 对冲突 metadata 的强制解算；
- 其余构建所需依赖由 workflow 先行显式安装。

### 实际修改
- 更新 `.github/workflows/build.yml` 三个平台的安装步骤：
  - 先安装 `setuptools<81`；
  - 先安装固定的构建依赖与运行依赖；
  - `python -m pip install --no-deps "lerobot==0.4.4"`
  - `python -m pip install --no-deps -e ".[dev]"`

### 当前状态
- 已完成 workflow 修改并通过本地静态检查；
- 准备提交并重新触发 GitHub Actions 继续验证。
