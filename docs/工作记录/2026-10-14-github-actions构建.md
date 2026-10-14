# 2026-10-14 GitHub Actions 构建

## 问题背景

用户要求在 GitHub 上触发 Actions 构建。当前仓库默认推送目标已切换至 GitLab，本次显式使用 GitHub 的 `origin` 远端和 `dyz9219/agibot-converter` 仓库。

## 版本核对与处理

- 构建前主工作区干净，当前分支为 `main`。
- 本地与 GitLab 的 `main` 为 `3de5686d23c340aee5a986969ea6275b0e46a089`。
- GitHub 的 `main` 为 `11ad4451a49fc96d28830e16ef1a507ffd580a53`，与本地仅相差上一轮 GitLab 同步工作记录。
- 执行 `git push origin main`，将 GitHub 更新到与本地一致的提交；GitLab 默认推送配置保持生效。
- 执行 `gh workflow run build.yml --repo dyz9219/agibot-converter --ref main`，手动触发工作流。

## 构建范围

工作流：`.github/workflows/build.yml`，名称 `Build Multi-Platform`。

| 任务 | 平台与模式 | 产物名称 |
| --- | --- | --- |
| `build-windows` | Windows，`-Profile full -Clean` | `DataConverterShell-Windows` |
| `build-linux-x64` | Linux x64，PyInstaller `--onefile` | `DataConverterShell-Linux-x64` |
| `build-linux-arm64` | Linux ARM64，PyInstaller `--onefile` | `DataConverterShell-Linux-ARM64` |

## 验证命令与量化结果

- `.\.venv\Scripts\python.exe -m pytest -q`：`69 passed, 6 skipped in 14.00s`。
- `gh run list --repo dyz9219/agibot-converter --workflow build.yml --event workflow_dispatch --limit 3 --json databaseId,status,conclusion,headSha,createdAt,url`：确认本次运行已创建，构建版本与本地提交一致。
- 运行 ID：`38539524136`。
- 构建提交：`3de5686d23c340aee5a986969ea6275b0e46a089`。
- 运行地址：[本次 Actions 构建](https://github.com/dyz9219/agibot-converter/actions/runs/38539524136)。
- `gh run watch 38539524136 --repo dyz9219/agibot-converter --interval 30 --exit-status`：持续检查三个平台的结果。

## 构建结果

本次运行状态为 `completed`，结论为 `success`，三个平台均成功完成。

| 任务 | 耗时 | 结论 | GitHub artifact 大小 |
| --- | --- | --- | --- |
| `build-windows` | 7 分 14 秒 | `success` | 301,039,474 bytes |
| `build-linux-x64` | 7 分 22 秒 | `success` | 444,794,019 bytes |
| `build-linux-arm64` | 4 分 52 秒 | `success` | 316,732,294 bytes |

最终日志和产物元数据核对：

- Windows：`OK: packaged any4 matches repo source.`、`FINGERPRINT_CHECK_OK`，构建指纹对应提交 `3de5686d23c340aee5a986969ea6275b0e46a089`。
- Linux x64 与 ARM64：均打印 `ROSBAG_HEALTH_OK`，LeRobot `v3.0`、`v2.1`、`v2.0` 的检查均打印 `ANY4_HEALTH_OK`。
- 两个 Linux 任务的 `Package Linux artifact` 步骤均通过，确认 `.tar.gz` 解压后的程序保留执行权限且可运行 `--internal-build-info`。
- GitHub API 返回 `total_count=3`，三个产物均为 `expired=false`，名称与构建范围一致。
- `gh run watch` 正常结束，退出码为 `0`。

本轮复用了工作流内的打包验证和健康检查，未另外下载产物执行真实数据转换。

## 当前结论与下一步建议

GitHub Actions 三个平台已全部构建成功，可从本次运行页面的 Artifacts 下载对应产物。本轮未修改转换代码或构建工作流，工作记录使用带 `[skip ci]` 的文档提交同步到 GitHub 和 GitLab，避免重复触发构建。后续若需要验证实际转换场景，应使用本次产物和对应真实数据开展单独的 smoke 验证。
