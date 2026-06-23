# LeRobot task 前缀清理

## 问题背景

用户反馈转换结果中 `episodes` metadata 的 `tasks` 形如：

```text
test_gaok_1_10_174727_93 | 把瓶子放进筐子里
```

其中 `test_gaok_1_10_174727_93 | ` 是平台样本名/任务名拼接前缀，训练语义上不需要；最终 task 应只保留真实指令：

```text
把瓶子放进筐子里
```

## 根因分析

any4 生成 LeRobot metadata 时会将 `task_name` 与 `init_scene_text` 拼接为
`{task_name} | {init_scene_text}`。前一轮修复保证了 `init_scene_text` 能来自
`annotation_result.json[action_text]`，但没有清理 any4 自动拼接的 `task_name` 前缀。

## 改动方案

在 LeRobot 后处理阶段新增 `_normalize_lerobot_task_text_metadata(...)`，对以下 metadata 统一清理
`左侧来源名 | ` 前缀：

- `meta/tasks.parquet`
- `meta/episodes/**/*.parquet`
- `meta/tasks.jsonl`
- `meta/episodes.jsonl`

清理规则保持保守：仅当字符串包含 ` | ` 且右侧真实指令非空时，才保留右侧文本；没有该分隔符的 task 不变。

## 验证方式与结果

新增回归测试：

```powershell
pytest -q test\python\test_lerobot_task_text.py
```

结果：

```text
1 passed in 1.37s
```

完整测试：

```powershell
pytest -q test\python
```

结果：

```text
61 passed, 6 skipped in 11.08s
```

真实 zip 源码入口复验：

```text
input=E:\Users\dyz\Documents\WXWork\1688858286666779\Cache\File\2026-06\task_2059925964389343234.zip
output=test\task-prefix-verify\source-run-output\task_2059925964389343234__lerobot_v30
```

验证结果：

```json
{
  "task_value": "\u628a\u74f6\u5b50\u653e\u8fdb\u7b50\u5b50\u91cc",
  "episode_value": "\u628a\u74f6\u5b50\u653e\u8fdb\u7b50\u5b50\u91cc",
  "prefix_removed": true,
  "contains_pipe": false
}
```

## 当前结论

源码转换输出的 task metadata 已只保留真实指令，不再包含 `test_gaok_... | ` 这类前缀。下一步若需要交付 Windows exe，应重新触发 GitHub Actions full 打包并用新 artifact 做同样复验。

## GitHub Actions Windows full 包复验

代码提交并推送：

```text
99c6b33 fix(lerobot): strip source prefix from task text
```

GitHub Actions run：

```text
run=28007631399
head_sha=99c6b33f090b2baf7baef80af8216e2d87fe094e
```

`build-windows` job 成功完成：

- `Build full package`: success
- `Verify packaged any4`: success
- `Verify build fingerprint`: success
- `Upload artifact`: success

下载 Windows full artifact：

```text
artifact_id=7813529823
name=DataConverterShell-Windows-full
size=455944835
zip=test\remote-package-verify-99c6b33-prefix\DataConverterShell-Windows-full.zip
```

从 zip 重新解压后读取 build info：

```json
{
  "profile": "full",
  "git_commit": "99c6b33f090b2baf7baef80af8216e2d87fe094e",
  "git_dirty": false
}
```

GUI 启动 smoke：启动 `from-zip\DataConverterShell-full.exe` 15 秒后进程仍在运行，未复现启动崩溃。

真实 zip 转换复验：

```text
input=E:\Users\dyz\Documents\WXWork\1688858286666779\Cache\File\2026-06\task_2059925964389343234.zip
output=test\remote-package-verify-99c6b33-prefix\from-zip-conversion-output\task_2059925964389343234__lerobot_v30
```

metadata 验证结果：

```json
{
  "manifest_status": "success",
  "runtime_mode": "bundled",
  "task_value": "\u628a\u74f6\u5b50\u653e\u8fdb\u7b50\u5b50\u91cc",
  "episode_arrow": "\u628a\u74f6\u5b50\u653e\u8fdb\u7b50\u5b50\u91cc",
  "episode_pandas": "\u628a\u74f6\u5b50\u653e\u8fdb\u7b50\u5b50\u91cc",
  "all_expected": true,
  "contains_pipe": false,
  "has_placeholder": false
}
```

最终结论：新 Windows full action 包已验证通过，LeRobot task metadata 不再带 `test_gaok_... | ` 或其他来源名前缀，只保留真实中文指令。
