"""Data Converter UI shell using downloader-like proportions."""

from __future__ import annotations

import argparse
import importlib.util
import json
import multiprocessing
import shutil
import sys
from pathlib import Path

ft = None

try:
    from .any4_health import check_any4_runtime
    from .any4lerobot_bridge import run_any4lerobot_cli
    from .backend import ConversionBackend, build_options
    from .models import MAX_CONCURRENCY, TaskStatus
    from .process_tracker import terminate_all_children
    from .worker_shard_cli import run_worker_shard_cli
except ImportError:
    from data_converter.any4_health import check_any4_runtime
    from data_converter.any4lerobot_bridge import run_any4lerobot_cli
    from data_converter.backend import ConversionBackend, build_options
    from data_converter.models import MAX_CONCURRENCY, TaskStatus
    from data_converter.process_tracker import terminate_all_children
    from data_converter.worker_shard_cli import run_worker_shard_cli

BG = "#f5f5f7"
PANEL = "#FFFFFF"
BORDER = "#E5E5E7"
TEXT = "#1D1D1F"
MUTED = "#86868B"
PRIMARY = "#007AFF"
SUCCESS = "#34C759"
ERROR = "#FF3B30"

TITLE_SIZE = 15
BODY_SIZE = 13
SUB_SIZE = 12
BADGE_SIZE = 11
FONT_FAMILY = "Microsoft YaHei UI"
WIDTH_S = 96
WIDTH_M = 146
WIDTH_L = 146

CTRL_HEIGHT = 34
BTN_HEIGHT = 34
RADIUS = 6


def _resolve_asset_path(name: str) -> str:
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "assets" / name)
    here = Path(__file__).resolve()
    candidates.append(here.parents[2] / "assets" / name)
    candidates.append(Path.cwd() / "assets" / name)
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def _card(content: ft.Control) -> ft.Container:
    return ft.Container(
        bgcolor=PANEL,
        border_radius=8,
        border=ft.border.all(1, BORDER),
        padding=ft.padding.symmetric(horizontal=12, vertical=10),
        content=content,
    )


def _badge(label: str, color: str, bg: str) -> ft.Container:
    return ft.Container(
        bgcolor=bg,
        border_radius=10,
        padding=ft.padding.symmetric(horizontal=8, vertical=2),
        content=ft.Text(label, size=BADGE_SIZE, color=color, weight=ft.FontWeight.W_500),
    )


def _button(label: str, on_click=None, primary: bool = False) -> ft.ElevatedButton:
    return ft.ElevatedButton(
        label,
        height=BTN_HEIGHT,
        on_click=on_click,
        style=ft.ButtonStyle(
            bgcolor=PRIMARY if primary else "#E5E5E7",
            color="#FFFFFF" if primary else TEXT,
            padding=ft.padding.symmetric(horizontal=14, vertical=7),
            shape=ft.RoundedRectangleBorder(radius=RADIUS),
            text_style=ft.TextStyle(size=BODY_SIZE, weight=ft.FontWeight.W_500, font_family=FONT_FAMILY),
            side=ft.BorderSide(1, PRIMARY if primary else "#D1D1D3"),
        ),
    )


def _path_selector(title: str, value_field: ft.TextField, on_pick=None) -> ft.Column:
    return ft.Column(
        spacing=6,
        controls=[
            ft.Text(title, size=BODY_SIZE, color=TEXT, weight=ft.FontWeight.W_500),
            ft.Container(
                bgcolor=BG,
                border_radius=RADIUS,
                padding=ft.padding.symmetric(horizontal=12, vertical=8),
                content=ft.Row(
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Text("📁", size=14),
                        value_field,
                        _button("选择", on_pick),
                    ],
                ),
            ),
        ],
    )


def _task_row(
    name: str,
    status: str,
    progress: float,
    color: str,
    detail: str = "",
    expanded: bool = False,
    on_toggle=None,
) -> ft.Container:
    detail_view = (
        ft.Text(
            detail,
            size=SUB_SIZE,
            color=ERROR if status == "失败" else MUTED,
            selectable=True,
        )
        if expanded and detail
        else None
    )
    title_suffix = " ▲" if expanded and detail else (" ▼" if detail else "")
    return ft.Container(
        bgcolor=BG,
        border_radius=RADIUS,
        padding=ft.padding.symmetric(horizontal=10, vertical=8),
        on_click=on_toggle if detail else None,
        content=ft.Column(
            spacing=6,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Text(f"{name}{title_suffix}", size=BODY_SIZE, color=TEXT, weight=ft.FontWeight.W_500),
                        ft.Text(status, size=SUB_SIZE, color=color, weight=ft.FontWeight.W_500),
                    ],
                ),
                ft.ProgressBar(value=progress, color=PRIMARY, bgcolor="#DADCE0", bar_height=6),
                detail_view if detail_view is not None else ft.Container(visible=False),
            ],
        ),
    )


def _field(label: str, control: ft.Control) -> ft.Column:
    return ft.Column(
        spacing=6,
        expand=True,
        controls=[ft.Text(label, size=BODY_SIZE, color=TEXT, weight=ft.FontWeight.W_500), control],
    )


def _inline_labeled_field(label: str, control: ft.Control) -> ft.Row:
    return ft.Row(
        spacing=8,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Text(label, size=BODY_SIZE, color=TEXT, weight=ft.FontWeight.W_500),
            control,
        ],
    )


def _uniform_input_shell(control: ft.Control, width: int) -> ft.Container:
    return ft.Container(
        width=width,
        height=CTRL_HEIGHT,
        bgcolor="#FFFFFF",
        border=ft.border.all(1, "#D1D1D3"),
        border_radius=RADIUS,
        padding=ft.padding.only(left=8, right=8, top=0, bottom=0),
        alignment=ft.Alignment(-1, 0),
        content=control,
    )


def _build(page: ft.Page) -> ft.Control:
    backend = ConversionBackend()
    state = {
        "target": "LeRobot",
        "plans": [],
        "expanded": set(),
        "show_failed_only": False,
        "starting": False,
        "prechecking": False,
        "running": False,
        "paused": False,
        "cancelled": False,
        "stopping": False,
        "start_context": None,
    }
    task_total_text = ft.Text("总进度 0%", size=SUB_SIZE, color=MUTED)
    task_rows = ft.Column(spacing=6)
    badge_text = ft.Text("0 个任务", size=BADGE_SIZE, color=MUTED, weight=ft.FontWeight.W_500)
    failed_filter_btn = ft.TextButton(
        "仅看失败: 关",
        style=ft.ButtonStyle(
            color=PRIMARY,
            text_style=ft.TextStyle(size=SUB_SIZE, weight=ft.FontWeight.W_500),
            padding=ft.padding.symmetric(horizontal=6, vertical=0),
        ),
    )
    picker = ft.FilePicker()
    page.services.append(picker)

    source = ft.TextField(
        value="",
        hint_text="未选择输入路径",
        dense=True,
        border_width=0,
        text_size=SUB_SIZE,
        read_only=True,
        expand=True,
        content_padding=ft.padding.only(left=0, right=0, top=8, bottom=8),
    )
    output = ft.TextField(
        value="",
        hint_text="未选择输出路径",
        dense=True,
        border_width=0,
        text_size=SUB_SIZE,
        read_only=True,
        expand=True,
        content_padding=ft.padding.only(left=0, right=0, top=8, bottom=8),
    )
    detect = ft.Text("待识别", size=SUB_SIZE, color=MUTED)
    preflight = ft.Text("未预检", size=SUB_SIZE, color=MUTED, weight=ft.FontWeight.W_500)

    version = ft.DropdownM2(
        value="v3.0",
        dense=True,
        height=CTRL_HEIGHT - 2,
        width=88,
        text_size=BODY_SIZE,
        border_width=0,
        content_padding=ft.padding.only(left=0, right=0, top=8, bottom=8),
        options=[
            ft.dropdownm2.Option("v3.0"),
            ft.dropdownm2.Option("v2.1"),
            ft.dropdownm2.Option("v2.0"),
            ft.dropdownm2.Option("HDF5"),
        ],
    )
    fps = ft.TextField(
        value="30",
        dense=True,
        height=CTRL_HEIGHT - 2,
        width=88,
        text_size=BODY_SIZE,
        border_width=0,
        content_padding=ft.padding.only(left=0, right=0, top=8, bottom=8),
    )
    version_shell = _uniform_input_shell(version, WIDTH_M)
    fps_shell = _uniform_input_shell(fps, WIDTH_S)

    bag_type = ft.DropdownM2(
        value="MCAP",
        dense=True,
        height=CTRL_HEIGHT - 2,
        width=112,
        text_size=BODY_SIZE,
        border_width=0,
        content_padding=ft.padding.only(left=0, right=0, top=8, bottom=8),
        options=[
            ft.dropdownm2.Option("MCAP"),
            ft.dropdownm2.Option("ROS2 .db3"),
            ft.dropdownm2.Option("ROS1 .bag"),
        ],
    )
    bag_type_shell = _uniform_input_shell(bag_type, WIDTH_M)
    bag_type.visible = False

    embed_videos = ft.Checkbox(
        value=False,
        label="将视频逐帧写入 parquet（体积会显著增大）",
        label_style=ft.TextStyle(size=BODY_SIZE, color=TEXT, font_family=FONT_FAMILY),
        active_color=PRIMARY,
    )
    embed_hint = ft.Text(
        "关闭时按默认 LeRobot 方式外置保存 mp4；开启后会把 head、hand_left、hand_right 每帧图像写入 parquet。",
        size=SUB_SIZE,
        color=MUTED,
    )

    concurrent = ft.DropdownM2(
        value="4",
        dense=True,
        height=CTRL_HEIGHT - 2,
        width=88,
        text_size=BODY_SIZE,
        border_width=0,
        content_padding=ft.padding.only(left=0, right=0, top=8, bottom=8),
        options=[ft.dropdownm2.Option(str(i)) for i in range(4, MAX_CONCURRENCY + 1, 4)],
    )
    concurrent_shell = _uniform_input_shell(concurrent, WIDTH_S)

    seg_left = ft.Container(width=170, border_radius=RADIUS, padding=ft.padding.symmetric(horizontal=14, vertical=7), alignment=ft.Alignment(0, 0))
    seg_right = ft.Container(width=170, border_radius=RADIUS, padding=ft.padding.symmetric(horizontal=14, vertical=7), alignment=ft.Alignment(0, 0))

    row_le = ft.Row(
        spacing=10,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            _inline_labeled_field("版本", version_shell),
            _inline_labeled_field("FPS", fps_shell),
        ],
    )
    row_rb = ft.Row(spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER, controls=[_inline_labeled_field("类型", bag_type_shell)])
    row_rb.visible = False

    def _status_view(status: TaskStatus) -> tuple[str, float, str]:
        if status is TaskStatus.RUNNING:
            return "运行中", 0.5, PRIMARY
        if status is TaskStatus.SUCCESS:
            return "完成", 1.0, SUCCESS
        if status is TaskStatus.FAILED:
            return "失败", 1.0, ERROR
        if status is TaskStatus.SKIPPED:
            return "跳过", 1.0, MUTED
        if status is TaskStatus.BLOCKED:
            return "阻断", 1.0, ERROR
        return "待执行", 0.0, MUTED

    def _refresh_task_panel() -> None:
        plans = state["plans"]
        badge_text.value = f"{len(plans)} 个任务"
        if not plans:
            task_total_text.value = "总进度 0%"
            task_rows.controls = [ft.Text("暂无任务", size=BODY_SIZE, color=MUTED, text_align=ft.TextAlign.CENTER)]
            return

        rows = []
        progress_sum = 0.0
        visible_count = 0
        for p in plans:
            if state["show_failed_only"] and p.status is not TaskStatus.FAILED:
                continue
            status_label, progress_value, status_color = _status_view(p.status)
            progress_sum += progress_value
            visible_count += 1
            detail = "; ".join(p.reasons).strip()
            is_expanded = p.task_id in state["expanded"]

            def _toggle(_: ft.ControlEvent, task_id: str = p.task_id) -> None:
                expanded = state["expanded"]
                if task_id in expanded:
                    expanded.remove(task_id)
                else:
                    expanded.add(task_id)
                _refresh_task_panel()
                page.update()

            rows.append(
                _task_row(
                    f"{p.source.name} -> {p.output_dir.name}",
                    status_label,
                    progress_value,
                    status_color,
                    detail=detail,
                    expanded=is_expanded,
                    on_toggle=_toggle,
                )
            )

        task_rows.controls = rows if rows else [ft.Text("暂无失败任务", size=BODY_SIZE, color=MUTED, text_align=ft.TextAlign.CENTER)]
        if state["show_failed_only"]:
            task_total_text.value = f"失败视图 {visible_count} 项"
        else:
            total = int((progress_sum / max(visible_count, 1)) * 100)
            task_total_text.value = f"总进度 {total}%"

    def _on_task_progress(_: object) -> None:
        _refresh_task_panel()
        page.update()

    def _toggle_failed_filter(_: ft.ControlEvent) -> None:
        state["show_failed_only"] = not state["show_failed_only"]
        failed_filter_btn.text = f"仅看失败: {'开' if state['show_failed_only'] else '关'}"
        _refresh_task_panel()
        page.update()

    failed_filter_btn.on_click = _toggle_failed_filter

    async def _pick_source(_: ft.ControlEvent) -> None:
        selected = await picker.get_directory_path(dialog_title="选择输入路径", initial_directory=source.value or None)
        if selected:
            source.value = selected
            page.update()

    async def _pick_output(_: ft.ControlEvent) -> None:
        selected = await picker.get_directory_path(dialog_title="选择输出路径", initial_directory=output.value or None)
        if selected:
            output.value = selected
            page.update()

    def _sync_target() -> None:
        is_le = state["target"] == "LeRobot"
        seg_left.bgcolor = "#EAF3FF" if is_le else PANEL
        seg_left.border = ft.border.all(1, PRIMARY if is_le else "#D1D1D3")
        seg_right.bgcolor = "#EAF3FF" if not is_le else PANEL
        seg_right.border = ft.border.all(1, PRIMARY if not is_le else "#D1D1D3")
        row_le.visible = is_le
        row_rb.visible = not is_le
        bag_type.visible = not is_le
        show_embed = is_le and (version.value or "v3.0") != "HDF5"
        embed_videos.visible = show_embed
        embed_hint.visible = show_embed

    def _build_opts():
        return build_options(
            input_path=source.value,
            output_path=output.value,
            target=state["target"],
            version=version.value or "v3.0",
            fps=fps.value or "30",
            bag_type=bag_type.value or "MCAP",
            concurrency=concurrent.value or "4",
            embed_videos_in_parquet=bool(embed_videos.value),
        )

    def _precheck(_: ft.ControlEvent) -> None:
        if not source.value.strip() or not output.value.strip():
            preflight.value = "预检失败：请先选择输入路径和输出路径"
            preflight.color = ERROR
            page.update()
            return
        preflight.value = "预检中..."
        preflight.color = PRIMARY
        page.update()

        def _work() -> None:
            opts = _build_opts()
            result = backend.precheck(opts)
            state["plans"] = result.tasks
            detect.value = f"识别: 就绪 {result.ready}, 跳过 {result.skipped}, 阻断 {result.blocked}"
            _refresh_task_panel()
            _sync_action_buttons()
            if result.ok:
                preflight.value = "预检通过"
                preflight.color = SUCCESS
            else:
                reason = result.global_errors[0] if result.global_errors else "无可执行任务"
                preflight.value = f"预检失败：{reason}"
                preflight.color = ERROR
            page.update()

        page.run_thread(_work)

    def _show_partial_continue_dialog(opts, ready: int, skipped: int, blocked: int) -> None:
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("预检结果"),
            content=ft.Text(f"可执行 {ready}，跳过 {skipped}，阻断 {blocked}。\n是否继续转换可执行任务？"),
            actions=[],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        def _cancel(_: ft.ControlEvent) -> None:
            dlg.open = False
            state["starting"] = False
            state["start_context"] = None
            preflight.value = "已取消：未开始转换"
            preflight.color = MUTED
            _sync_action_buttons()
            page.update()

        def _ok(_: ft.ControlEvent) -> None:
            dlg.open = False
            _run_convert_with_plans(opts)

        dlg.actions = [_button("取消", _cancel), _button("继续转换", _ok, primary=True)]
        page.dialog = dlg
        dlg.open = True
        page.update()

    def _show_conflict_dialog(opts, conflict_tasks: list[object]) -> None:
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("检测到重复输出"),
            content=ft.Text(f"检测到 {len(conflict_tasks)} 个输出目录已存在。\n请选择本次统一处理策略。"),
            actions=[],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        def _cancel(_: ft.ControlEvent) -> None:
            dlg.open = False
            state["starting"] = False
            state["start_context"] = None
            preflight.value = "已取消：未开始转换"
            preflight.color = MUTED
            _sync_action_buttons()
            page.update()

        def _apply_and_continue(overwrite: bool) -> None:
            for task in state["plans"]:
                is_conflict = task in conflict_tasks
                if not is_conflict:
                    continue
                if overwrite:
                    if task.output_dir.exists():
                        shutil.rmtree(task.output_dir, ignore_errors=True)
                    task.status = TaskStatus.PENDING
                    task.reasons = [r for r in task.reasons if "目标目录已存在" not in r]
                    task.reasons.append("用户选择覆盖已有目录")
                else:
                    task.status = TaskStatus.SKIPPED
                    if "用户选择跳过已有目录" not in task.reasons:
                        task.reasons.append("用户选择跳过已有目录")

            ready = len([p for p in state["plans"] if p.status is TaskStatus.PENDING])
            skipped = len([p for p in state["plans"] if p.status is TaskStatus.SKIPPED])
            blocked = len([p for p in state["plans"] if p.status is TaskStatus.BLOCKED])
            detect.value = f"识别: 就绪 {ready}, 跳过 {skipped}, 阻断 {blocked}"
            _refresh_task_panel()
            _sync_action_buttons()
            page.update()

            if ready <= 0:
                state["starting"] = False
                state["start_context"] = None
                preflight.value = "预检失败：无可执行任务"
                preflight.color = ERROR
                _sync_action_buttons()
                page.update()
                return

            if blocked > 0 or skipped > 0:
                _show_partial_continue_dialog(opts, ready, skipped, blocked)
            else:
                _run_convert_with_plans(opts)

        def _overwrite(_: ft.ControlEvent) -> None:
            dlg.open = False
            _apply_and_continue(True)

        def _skip(_: ft.ControlEvent) -> None:
            dlg.open = False
            _apply_and_continue(False)

        dlg.actions = [_button("取消", _cancel), _button("跳过全部", _skip), _button("覆盖全部", _overwrite, primary=True)]
        page.dialog = dlg
        dlg.open = True
        page.update()

    def _run_convert_with_plans(opts) -> None:
        state["cancelled"] = False
        state["paused"] = False
        state["stopping"] = False
        state["starting"] = False
        state["prechecking"] = False
        state["running"] = True
        state["start_context"] = None
        try:
            page.window.prevent_close = True
        except Exception:
            pass
        preflight.value = "转换中..."
        preflight.color = PRIMARY
        _sync_action_buttons()
        page.update()

        def _work() -> None:
            try:
                summary = backend.run(
                    opts,
                    state["plans"],
                    on_progress=_on_task_progress,
                    should_pause=lambda: bool(state["paused"]),
                    should_cancel=lambda: bool(state["cancelled"]),
                )
                if state["cancelled"]:
                    preflight.value = f"已取消：成功 {summary.success}，失败 {summary.failed}，跳过 {summary.skipped}"
                    preflight.color = MUTED
                elif summary.failed > 0:
                    preflight.value = f"完成：成功 {summary.success}，失败 {summary.failed}，跳过 {summary.skipped}"
                    preflight.color = ERROR
                else:
                    preflight.value = f"完成：成功 {summary.success}，跳过 {summary.skipped}"
                    preflight.color = SUCCESS
                _refresh_task_panel()
                page.update()
            finally:
                state["running"] = False
                state["paused"] = False
                state["stopping"] = False
                state["prechecking"] = False
                state["starting"] = False
                state["start_context"] = None
                try:
                    page.window.prevent_close = False
                except Exception:
                    pass
                _sync_action_buttons()
                page.update()

        page.run_thread(_work)

    def _start_convert(_: ft.ControlEvent) -> None:
        if state["running"] or state["starting"] or state["prechecking"] or state["stopping"]:
            return
        if not source.value.strip() or not output.value.strip():
            preflight.value = "预检失败：请先选择输入路径和输出路径"
            preflight.color = ERROR
            page.update()
            return
        opts = _build_opts()
        state["starting"] = True
        state["prechecking"] = True
        state["cancelled"] = False
        state["paused"] = False
        state["stopping"] = False
        state["start_context"] = {"opts": opts}
        preflight.value = "预检中..."
        preflight.color = PRIMARY
        _sync_action_buttons()
        page.update()

        def _work() -> None:
            try:
                result = backend.precheck(opts)
                state["plans"] = result.tasks
                detect.value = f"识别: 就绪 {result.ready}, 跳过 {result.skipped}, 阻断 {result.blocked}"
                _refresh_task_panel()
                _sync_action_buttons()

                if result.ready <= 0:
                    reason = result.global_errors[0] if result.global_errors else "无可执行任务"
                    preflight.value = f"预检失败：{reason}"
                    preflight.color = ERROR
                    state["prechecking"] = False
                    state["starting"] = False
                    state["start_context"] = None
                    _sync_action_buttons()
                    page.update()
                    return

                conflict_tasks = [
                    p for p in state["plans"] if p.status is TaskStatus.BLOCKED and any("目标目录已存在" in r for r in p.reasons)
                ]
                state["prechecking"] = False
                _sync_action_buttons()
                page.update()

                if conflict_tasks:
                    _show_conflict_dialog(opts, conflict_tasks)
                    return

                if result.blocked > 0 or result.skipped > 0:
                    _show_partial_continue_dialog(opts, result.ready, result.skipped, result.blocked)
                else:
                    _run_convert_with_plans(opts)
            finally:
                if state["prechecking"]:
                    state["prechecking"] = False
                    _sync_action_buttons()
                    page.update()

        page.run_thread(_work)

    def _choose_l(_: ft.ControlEvent) -> None:
        state["target"] = "LeRobot"
        _sync_target()
        page.update()

    def _choose_r(_: ft.ControlEvent) -> None:
        state["target"] = "Rosbag"
        _sync_target()
        page.update()

    start_btn = _button("开始转换", primary=True)
    start_btn.width = 160
    start_btn.on_click = _start_convert
    pause_resume_btn = _button("暂停")
    pause_resume_btn.width = 120
    stop_menu_item = ft.PopupMenuItem(content=ft.Text("停止"))
    precheck_menu_item = ft.PopupMenuItem(content=ft.Text("预检"))
    retry_menu_item = ft.PopupMenuItem(content=ft.Text("重试失败项"))
    more_btn = ft.PopupMenuButton(
        icon=ft.Icons.MORE_HORIZ,
        icon_size=18,
        tooltip="更多",
        items=[stop_menu_item, precheck_menu_item, retry_menu_item],
        menu_position=ft.PopupMenuPosition.UNDER,
    )

    def _sync_action_buttons() -> None:
        starting = bool(state["starting"])
        prechecking = bool(state["prechecking"])
        running = bool(state["running"])
        paused = bool(state["paused"])
        stopping = bool(state["stopping"])
        has_failed = any(p.status is TaskStatus.FAILED for p in state["plans"])

        start_btn.disabled = running or stopping or starting or prechecking
        pause_resume_btn.disabled = (not running) or stopping or starting or prechecking
        pause_resume_btn.text = "继续" if paused else "暂停"

        stop_menu_item.disabled = (not running) or stopping
        precheck_menu_item.disabled = stopping or starting or prechecking
        retry_menu_item.disabled = running or starting or prechecking or (not has_failed)
        more_btn.disabled = stopping or starting or prechecking

    def _pause_convert(_: ft.ControlEvent) -> None:
        if not state["running"]:
            return
        if state["paused"]:
            return
        state["paused"] = True
        preflight.value = "转换已暂停（当前任务完成后停止派发）"
        preflight.color = MUTED
        _sync_action_buttons()
        page.update()

    def _resume_convert(_: ft.ControlEvent) -> None:
        if not state["running"]:
            return
        state["paused"] = False
        preflight.value = "已继续转换"
        preflight.color = PRIMARY
        _sync_action_buttons()
        page.update()

    def _toggle_pause_resume(_: ft.ControlEvent) -> None:
        if not state["running"] or state["stopping"]:
            return
        if state["paused"]:
            _resume_convert(_)
        else:
            _pause_convert(_)

    def _stop_convert(_: ft.ControlEvent) -> None:
        if not state["running"]:
            return
        state["cancelled"] = True
        state["paused"] = False
        state["stopping"] = True
        terminate_all_children()
        preflight.value = "停止中..."
        preflight.color = MUTED
        _sync_action_buttons()
        page.update()

    def _retry_failed(_: ft.ControlEvent) -> None:
        if state["running"] or state["starting"] or state["prechecking"] or state["stopping"]:
            return
        failed = [p for p in state["plans"] if p.status is TaskStatus.FAILED]
        if not failed:
            preflight.value = "无失败任务可重试"
            preflight.color = MUTED
            page.update()
            return
        for p in failed:
            p.status = TaskStatus.PENDING
            p.reasons.clear()
            p.attempts = 0
        _refresh_task_panel()
        opts = _build_opts()
        _run_convert_with_plans(opts)

    def _menu_precheck(_: ft.ControlEvent) -> None:
        if state["stopping"]:
            return
        if state["running"] and (not state["paused"]):
            _pause_convert(_)
        _precheck(_)

    pause_resume_btn.on_click = _toggle_pause_resume
    stop_menu_item.on_click = _stop_convert
    precheck_menu_item.on_click = _menu_precheck
    retry_menu_item.on_click = _retry_failed
    _sync_action_buttons()

    seg_left.content = ft.Text("AgiBot 转 LeRobot", size=BODY_SIZE, color=TEXT, weight=ft.FontWeight.W_500, no_wrap=True)
    seg_left.on_click = _choose_l
    seg_right.content = ft.Text("AgiBot 转 Rosbag", size=BODY_SIZE, color=TEXT, weight=ft.FontWeight.W_500, no_wrap=True)
    seg_right.on_click = _choose_r
    def _on_version_change(_: ft.ControlEvent) -> None:
        _sync_target()
        page.update()

    version.on_change = _on_version_change
    _sync_target()

    header = ft.Container(
        bgcolor="#FFFFFF",
        border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
        padding=ft.padding.symmetric(horizontal=16, vertical=10),
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Row(
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[ft.Text("文件转换器", size=TITLE_SIZE, color=TEXT, weight=ft.FontWeight.W_600), _badge_text_container(badge_text)],
                ),
                ft.Container(),
            ],
        ),
    )

    def _close_window_now() -> None:
        try:
            page.window.prevent_close = False
        except Exception:
            pass
        if hasattr(page.window, "close"):
            page.window.close()
        elif hasattr(page.window, "destroy"):
            page.window.destroy()

    def _force_stop_then_close() -> None:
        state["cancelled"] = True
        state["stopping"] = True
        terminate_all_children()
        _sync_action_buttons()
        _close_window_now()

    def _confirm_stop_and_close() -> None:
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("确认关闭"),
            content=ft.Text("当前正在转换，是否直接停止并关闭？"),
            actions=[],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        def _no(_: ft.ControlEvent) -> None:
            dlg.open = False
            page.update()

        def _yes(_: ft.ControlEvent) -> None:
            dlg.open = False
            _force_stop_then_close()

        dlg.actions = [_button("取消", _no), _button("停止并关闭", _yes, primary=True)]
        page.dialog = dlg
        dlg.open = True
        page.update()

    def _on_window_event(e: ft.WindowEvent) -> None:
        data = str(getattr(e, "data", "")).lower()
        normalized = data.replace("-", "_")
        is_close_event = (
            normalized in {"close", "close_request", "window_close", "window_close_request"}
            or "close" in normalized
        )
        if not is_close_event:
            return
        is_busy = bool(state["running"] or state["starting"] or state["prechecking"])
        if not is_busy:
            _close_window_now()
            return

        # Paused state should close immediately with force stop, no extra prompt.
        if bool(state["paused"]):
            _force_stop_then_close()
            return

        # Active execution path: ask for confirmation before force stop + close.
        _confirm_stop_and_close()

    try:
        page.window.prevent_close = False
    except Exception:
        pass
    try:
        page.window.on_event = _on_window_event
    except Exception:
        # Backward compatibility for older flet versions.
        page.on_window_event = _on_window_event

    task_panel = _card(
        ft.Column(
            spacing=8,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Text("任务列表", size=BODY_SIZE, color=TEXT, weight=ft.FontWeight.W_600),
                        ft.Row(spacing=8, controls=[task_total_text, failed_filter_btn]),
                    ],
                ),
                task_rows,
            ],
        )
    )
    config_panel = _card(
        ft.Column(
            spacing=10,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Text("转换配置", size=BODY_SIZE, color=TEXT, weight=ft.FontWeight.W_600),
                        ft.Row(
                            spacing=8,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[detect, _inline_labeled_field("并发", concurrent_shell)],
                        ),
                    ],
                ),
                _path_selector("输入路径", source, _pick_source),
                _path_selector("输出路径", output, _pick_output),
                ft.Row(spacing=8, controls=[seg_left, seg_right]),
                row_le,
                embed_videos,
                embed_hint,
                row_rb,
                ft.Row(
                    spacing=10,
                    controls=[start_btn, pause_resume_btn, more_btn],
                ),
                preflight,
            ],
        )
    )

    main_area = ft.Container(
        expand=True,
        padding=12,
        content=ft.Column(
            spacing=10,
            scroll=ft.ScrollMode.AUTO,
            controls=[config_panel, task_panel],
        ),
    )

    return ft.Column(expand=True, spacing=0, controls=[header, main_area])


def _badge_text_container(text_control: ft.Text) -> ft.Container:
    return ft.Container(
        bgcolor="#E5E5E7",
        border_radius=10,
        padding=ft.padding.symmetric(horizontal=8, vertical=2),
        content=text_control,
    )


def main(page: ft.Page) -> None:
    page.title = "Converter"
    page.window.width = 560
    page.window.height = 800
    page.window.min_width = 480
    page.window.min_height = 600
    page.padding = 0
    page.bgcolor = BG
    page.theme_mode = ft.ThemeMode.LIGHT
    page.theme = ft.Theme(font_family=FONT_FAMILY)
    icon_path = _resolve_asset_path("pku_logo.ico") or _resolve_asset_path("pku_logo.png")
    if icon_path:
        page.window.icon = icon_path
    page.add(_build(page))


def _run_internal_conversion_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="data-converter-internal")
    parser.add_argument("--input-path", required=True)
    parser.add_argument("--output-path", required=True)
    parser.add_argument("--target", default="lerobot", choices=["lerobot", "rosbag"])
    parser.add_argument("--version", default="v3.0")
    parser.add_argument("--fps", default="30")
    parser.add_argument("--bag-type", default="MCAP")
    parser.add_argument("--concurrency", default="4")
    parser.add_argument("--embed-videos-in-parquet", action="store_true")
    args = parser.parse_args(argv)

    backend = ConversionBackend()
    options = build_options(
        input_path=args.input_path,
        output_path=args.output_path,
        target=args.target,
        version=args.version,
        fps=args.fps,
        bag_type=args.bag_type,
        concurrency=args.concurrency,
        embed_videos_in_parquet=args.embed_videos_in_parquet,
    )
    result = backend.precheck(options)
    if not result.ok:
        print("PRECHECK_FAILED")
        for err in result.global_errors:
            print(err)
        return 2

    summary = backend.run(options, result.tasks, on_progress=None)
    print(
        "RUN_SUMMARY",
        f"total={summary.total}",
        f"success={summary.success}",
        f"failed={summary.failed}",
        f"skipped={summary.skipped}",
    )
    return 0 if summary.failed == 0 else 1


def _run_internal_rosbag_health_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="data-converter-rosbag-health")
    parser.add_argument("--bag-type", default="MCAP", choices=["MCAP", "ROS2 .db3", "ROS1 .bag"])
    args = parser.parse_args(argv)

    required = ["rosbags.rosbag2", "rosbags.serde.primitives"]
    if args.bag_type in {"MCAP", "ROS2 .db3"}:
        required.append("rosbags.typesys.stores.ros2_humble")
    else:
        required.append("rosbags.typesys.stores.ros1_noetic")

    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        print("ROSBAG_HEALTH_FAIL")
        print(",".join(missing))
        return 1

    print("ROSBAG_HEALTH_OK")
    print(args.bag_type)
    return 0


def _run_internal_any4_health_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="data-converter-any4-health")
    parser.add_argument("--version", default="v3.0", choices=["v3.0", "v2.1", "v2.0"])
    args = parser.parse_args(argv)

    result = check_any4_runtime(args.version)
    if result.ok:
        print("ANY4_HEALTH_OK")
        print(result.diagnostic)
        return 0

    print("ANY4_HEALTH_FAIL")
    print(result.diagnostic)
    return 1


def _run_internal_build_info_cli(argv: list[str]) -> int:
    del argv
    info_path = _resolve_asset_path("build_meta.json")
    if not info_path:
        print("BUILD_INFO_FAIL")
        print("missing assets/build_meta.json")
        return 1
    try:
        info = json.loads(Path(info_path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print("BUILD_INFO_FAIL")
        print(str(exc))
        return 1
    print(json.dumps(info, ensure_ascii=False))
    return 0


def _ensure_flet() -> None:
    global ft
    if ft is None:
        import flet as _ft

        ft = _ft


def run_cli_entry(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--internal-run-any4lerobot":
        return run_any4lerobot_cli(args[1:])
    if args and args[0] == "--internal-run-conversion":
        return _run_internal_conversion_cli(args[1:])
    if args and args[0] == "--internal-run-worker-shard":
        return run_worker_shard_cli(args[1:])
    if args and args[0] == "--internal-run-rosbag-health":
        return _run_internal_rosbag_health_cli(args[1:])
    if args and args[0] == "--internal-run-any4-health":
        return _run_internal_any4_health_cli(args[1:])
    if args and args[0] == "--internal-build-info":
        return _run_internal_build_info_cli(args[1:])
    _ensure_flet()
    ft.app(target=main)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli_entry())
