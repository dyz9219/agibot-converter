# Repository Guidelines

## 项目结构与模块组织
- 核心代码位于 `src/data_converter/`：
  - UI 与入口：`main.py`
  - 转换编排：`backend.py`、`routing.py`、`precheck.py`
  - LeRobot 运行桥接：`any4_*`、`converters/lerobot_runner.py`
  - Rosbag 流程：`rosbag/`、`converters/rosbag_runner.py`
- 打包脚本在 `scripts/`（如 `build_exe.ps1`、`build_exe_onefile.ps1`、校验脚本）。
- 运行资源在 `assets/`。
- 本地验证与示例产物在 `test/`、`smoke-runs/`。

## 构建、测试与开发命令
- 本地启动（自动建虚拟环境并运行 UI）：`.\scripts\run.ps1`
- 手动可编辑安装：`pip install -e .`
- 运行测试：`pytest -q`
- 构建 Windows EXE：
  - `.\scripts\build_exe.ps1 -Profile fast`
  - `.\scripts\build_exe.ps1 -Profile full`
- 校验打包后的 any4 一致性：
  - `.\scripts\verify_packaged_any4.ps1 -DistRoot dist/DataConverterShell-full`

## 代码风格与命名规范
- Python 3.11+，4 空格缩进，UTF-8。
- 以 `pyproject.toml` 为准：`black`(100)、`isort`(black)、`ruff`(100)、`mypy`(strict)。
- 命名规则：模块/函数 `snake_case`，类 `PascalCase`，常量 `UPPER_SNAKE_CASE`。

## 测试规范
- 框架：`pytest`。
- 命名：`test_*.py`（如 `test_any4_health.py`、`test_exe.py`）。
- 所有正式测试脚本统一放在 `test/python/`，不要继续放在仓库根目录。
- 测试转换产物、临时输出和对比结果统一放在 `test/` 目录下按用途新建子目录，不要散落在仓库根目录。
- 根目录禁止保留测试生成的转换结果、临时 smoke 产物或一次性调试输出；测试结束后应优先清理。
- 清理测试产物时，仅处理仓库根目录内部的测试目录与临时目录，不要误删源码、文档或正式资源。
- 新功能需覆盖预检、转换与打包回归关键分支。
- 提交前至少运行：`pytest -q` 与相关 smoke 脚本。

## 提交与 PR 规范
- 建议使用 Conventional Commits：`fix(build): ...`、`ci(linux): ...`、`feat: ...`。
- PR 必须包含：问题背景、改动范围、验证命令与结果；UI 变更附截图；有任务号则关联。

## 日志记录规范
- 每次完成一轮实际工作后，必须将本轮的现象、分析、修改内容、验证命令与结果记录到 `.md` 日志文件中。
- 日志必须按天记录，默认放在 `docs/工作记录/` 目录下，并使用当天日期单独建文件，例如：`YYYY-MM-DD-*.md`。
- 同一天内的后续工作，优先继续更新当天对应的日志文件；跨天工作必须新建当天日期的新日志文件，不要继续写到前一天的日志中。
- 日志内容至少应包含：问题背景、根因分析、改动方案、量化结果、验证方式、当前结论与下一步建议。

## Markdown 文档规范
- 在本仓库中，后续新建或更新的 `.md` 文档默认使用中文编写，除非用户明确要求英文或原文保留英文。
- 适用范围包括但不限于：实施计划、设计文档、工作记录、分析说明、交付说明。
- 若文档需要保留固定英文标识（如命令、路径、字段名、代码片段、外部接口名称），应保持原样，不要强行翻译。

## 打包与发布注意事项
- 必须显式选择打包模式：`fast`（快速迭代）/`full`（完整分发）。
- 发布前必须做依赖一致性校验与干净路径 smoke 转换验证。

- **跨机器可运行性原则（必做）**  
  若出现“开发机可运行、同事机器失败”，默认优先排查**打包遗漏依赖或运行时不一致**，而非先判定数据问题。  
  必须执行：
  1. 用 `full` 模式重新打包；
  2. 对产物执行依赖校验（如 `verify_packaged_any4.ps1`）；
  3. 在干净环境机器做最小 smoke 测试并保留日志；
  4. 失败时优先检查 `manifest.json` / `any4_error.log` 的 `ModuleNotFoundError`、运行模式（bundled/external）与依赖收集参数。  
  验收标准：**以“打包产物可复现、可自包含运行”为准，不以开发机通过为准。**
