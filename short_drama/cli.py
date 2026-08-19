"""Command-line entry point for the short-drama workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .approval import STAGES, approval_status, create_approval, stage_paths
from .config import ConfigError, load_config
from .consistency import prepare_consistency
from .describe import describe_run_images
from .image_prepare import prepare_run_images
from .state import initialize_run, read_run, utc_now, write_json_atomic
from .story import plan_story
from .shots import plan_shots
from .h3_prompt import render_run_prompts
from .subtitle_align import generate_planned_subtitles
from .assemble import assemble_run
from .h3_jobs import generate_run_videos
from .validators import (
    load_json,
    validate_adjacent_shot_continuity,
    validate_document,
    validate_shots_against_story,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="short-drama")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="创建不可覆盖的新 run")
    init.add_argument("--config", required=True)
    init.add_argument("--run-id")
    status = sub.add_parser("status", help="显示 run 状态和审核门")
    status.add_argument("--run", required=True)
    prepare = sub.add_parser("prepare-images", help="按三图共同方向生成 16:9 或 9:16 版本")
    prepare.add_argument("--run", required=True)
    describe = sub.add_parser("describe-images", help="调用 Azure GPT 生成三图结构化描述")
    describe.add_argument("--run", required=True)
    story = sub.add_parser("plan-story", help="根据已批准描述调用 Azure GPT 规划短剧故事")
    story.add_argument("--run", required=True)
    shots = sub.add_parser("plan-shots", help="根据已批准故事调用 Azure GPT 切分镜头")
    shots.add_argument("--run", required=True)
    consistency = sub.add_parser(
        "prepare-consistency",
        help="角色参考注册、镜头首尾分解、智能选图、关键帧生成与 FL2VA/Ref2VA 路由",
    )
    consistency.add_argument("--run", required=True)
    approve = sub.add_parser("approve", help="校验并批准阶段产物")
    approve.add_argument("--run", required=True)
    approve.add_argument("--stage", required=True, choices=sorted(STAGES))
    approve.add_argument("--yes", action="store_true", help="跳过交互确认（调试用）")
    validate = sub.add_parser("validate", help="校验阶段 JSON，不创建批准")
    validate.add_argument("--run", required=True)
    validate.add_argument("--stage", required=True, choices=sorted(STAGES))
    render = sub.add_parser("render-prompts", help="将已批准镜头确定性渲染为 H3 请求")
    render.add_argument("--run", required=True)
    generate = sub.add_parser("generate-videos", help="通过 ComfyUI 按末帧链生成 H3 镜头视频")
    generate.add_argument("--run", required=True)
    continuity = sub.add_parser("check-continuity", help="生成相邻末帧依赖连续性报告")
    continuity.add_argument("--run", required=True)
    subtitles = sub.add_parser("generate-subtitles", help="从批准的计划对白生成确定性 SRT 时间轴")
    subtitles.add_argument("--run", required=True)
    assemble = sub.add_parser("assemble", help="规范化镜头、硬切拼接并烧录字幕")
    assemble.add_argument("--run", required=True)
    assemble.add_argument("--env-prefix", required=True)
    assemble.add_argument("--font", required=True)
    return parser


def cmd_init(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    run_dir = initialize_run(config, args.run_id)
    print(json.dumps({"run_id": run_dir.name, "run_dir": str(run_dir), "state": "CREATED"}, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    state = read_run(run_dir)
    result = {"run_id": state["run_id"], "state": state["state"], "approvals": {stage: approval_status(run_dir, stage) for stage in STAGES}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_prepare_images(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    state = read_run(run_dir)
    config = load_config(state["config_snapshot"])
    manifest = prepare_run_images(run_dir, config)
    print(f"图片预处理完成：{manifest}")
    print(f"人工预览：{run_dir / 'inputs/processed/contact_sheet.jpg'}")
    return 0


def cmd_describe_images(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    state = read_run(run_dir)
    config = load_config(state["config_snapshot"])
    artifact = describe_run_images(run_dir, config)
    print(f"图片描述完成：{artifact}")
    print("下一步请人工检查，然后执行 validate 和 approve。")
    return 0


def cmd_plan_story(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    state = read_run(run_dir)
    config = load_config(state["config_snapshot"])
    artifact = plan_story(run_dir, config)
    print(f"故事规划完成：{artifact}")
    print("下一步请人工检查，然后执行 validate 和 approve。")
    return 0


def cmd_plan_shots(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    state = read_run(run_dir)
    config = load_config(state["config_snapshot"])
    artifact = plan_shots(run_dir, config)
    print(f"镜头规划完成：{artifact}")
    print(f"时间线摘要：{run_dir / '03_shots/timeline.md'}")
    print("建议下一步：prepare-consistency → validate/approve shots。")
    return 0


def cmd_prepare_consistency(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    state = read_run(run_dir)
    config = load_config(state["config_snapshot"])
    report_path = prepare_consistency(run_dir, config)
    report = load_json(report_path)
    print(f"一致性预处理完成：{report_path}")
    print(f"模式统计：{report.get('mode_counts')}")
    if report.get("shots_approval_invalidated"):
        print("已使 shots.approved 失效，请重新 validate/approve。")
    blocking = report.get("blocking_errors") or []
    if blocking:
        print(f"阻断错误 {len(blocking)} 条：")
        for item in blocking[:20]:
            print(f"  - {item}")
    warnings = report.get("warnings") or []
    if warnings:
        print(f"警告 {len(warnings)} 条：")
        for warning in warnings[:20]:
            print(f"  - {warning}")
        if len(warnings) > 20:
            print(f"  ... 另有 {len(warnings) - 20} 条")
    print("下一步请人工检查 03_shots/ 产物，然后执行 validate 和 approve。")
    return 2 if blocking else 0


def cmd_render_prompts(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    state = read_run(run_dir)
    config = load_config(state["config_snapshot"])
    report_path = render_run_prompts(run_dir, config)
    report = load_json(report_path)
    print(f"H3 Prompt 渲染完成：{report_path}")
    print(f"当前 ready：{', '.join(report['ready']) or '无'}")
    print(f"等待上一镜头末帧：{len(report['blocked'])} 个")
    print("本命令未启动 SGLang，也未提交视频推理。")
    return 0


def cmd_generate_videos(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    state = read_run(run_dir)
    config = load_config(state["config_snapshot"])
    jobs_path = generate_run_videos(run_dir, config)
    jobs = load_json(jobs_path)
    completed = [sid for sid, item in jobs.get("shots", {}).items() if item.get("status") == "completed"]
    blocked = [sid for sid, item in jobs.get("shots", {}).items() if item.get("status") == "blocked_dependency"]
    failed = [sid for sid, item in jobs.get("shots", {}).items() if item.get("status") == "failed"]
    print(f"阶段 VII 视频任务：{jobs_path}")
    print(f"已完成：{len(completed)}；仍阻塞：{len(blocked)}；失败：{len(failed)}")
    if blocked:
        print("阻塞镜头：" + ", ".join(blocked))
    if failed:
        print("失败镜头：" + ", ".join(failed))
        for sid in failed[:5]:
            item = jobs["shots"][sid]
            print(f"  - {sid}: {item.get('error_type')}: {item.get('error')}")
    return 0 if not failed else 2


def cmd_check_continuity(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    shots_path = run_dir / "03_shots/shots.json"
    document = load_json(shots_path)
    issues = validate_adjacent_shot_continuity(document)
    dependency_edges = sum(1 for shot in document.get("shots", []) if shot.get("depends_on"))
    report_path = run_dir / "03_shots/continuity_report.json"
    report = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "shots": len(document.get("shots", [])),
        "dependency_edges": dependency_edges,
        "passed": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "scope": [
            "blocking.end to dependent blocking.start",
            "character appearance and disappearance",
            "screen movement direction with explicit transition actions",
        ],
        "note": "This report validates the plan only; it does not inspect generated pixels.",
    }
    write_json_atomic(report_path, report)
    print(f"连续性报告：{report_path}")
    print(f"依赖边：{dependency_edges}；问题：{len(issues)}；通过：{'是' if not issues else '否'}")
    return 0 if not issues else 2


def cmd_generate_subtitles(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    report_path = generate_planned_subtitles(run_dir)
    report = load_json(report_path)
    print(f"阶段 VIII 字幕时间轴完成：{report_path}")
    print(f"字幕条目：{report['subtitle_cue_count']}；无对白镜头：{report['silent_shot_count']}")
    print(f"全片 SRT：{report['full_srt']}")
    print("本阶段未使用 ASR，也未烧录字幕。")
    return 0


def cmd_assemble(args: argparse.Namespace) -> int:
    report_path = assemble_run(
        Path(args.run).expanduser().resolve(),
        Path(args.env_prefix).expanduser().resolve(),
        Path(args.font).expanduser().resolve(),
    )
    report = load_json(report_path)
    print(f"阶段 IX 完成：{report['outputs']['final']}")
    print(f"无字幕版：{report['outputs']['without_burned_subtitles']}")
    print(f"独立字幕：{report['outputs']['subtitle']}")
    print(f"时长：{report['duration_s']:.3f} 秒；技术验收：通过")
    return 0


def _validate_stage(run_dir: Path, stage: str) -> tuple[Path, list[str]]:
    artifact, _ = stage_paths(run_dir, stage)
    if not artifact.is_file():
        raise FileNotFoundError(f"阶段文件不存在：{artifact}")
    exceptions: set[str] | None = None
    document = load_json(artifact)
    if stage == "shots":
        exception_path = run_dir / "03_shots/dialogue_pacing_exceptions.json"
        if exception_path.is_file():
            payload = load_json(exception_path)
            exceptions = {item["shot_id"] for item in payload.get("exceptions", [])}
    errors = validate_document(stage, document, dialogue_overflow_exceptions=exceptions)
    if stage == "shots":
        story_path = run_dir / "02_story/story.json"
        if story_path.is_file():
            errors.extend(validate_shots_against_story(document, load_json(story_path)))
        else:
            errors.append("缺少 02_story/story.json，无法做镜头与故事交叉校验")
        for shot in document.get("shots", []):
            shot_id = shot.get("shot_id", "<unknown>")
            mode = shot.get("generation_mode")
            if mode == "first_last_frame":
                last_frame = shot.get("source_last_frame")
                if not last_frame:
                    errors.append(f"{shot_id}: first_last_frame 缺少 source_last_frame")
                elif not Path(str(last_frame)).is_file():
                    errors.append(f"{shot_id}: source_last_frame 文件不存在：{last_frame}")
                prepared = shot.get("prepared_first_frame")
                if prepared and not Path(str(prepared)).is_file():
                    errors.append(f"{shot_id}: prepared_first_frame 文件不存在：{prepared}")
            if mode == "ref2va":
                selected = shot.get("selected_references") or []
                if not selected and not shot.get("selected_reference_paths"):
                    errors.append(
                        f"{shot_id}: ref2va 缺少 selected_references；请先执行 prepare-consistency"
                    )
                for item in selected:
                    if not item.get("path"):
                        errors.append(f"{shot_id}: selected_references 缺少 path")
                    elif not Path(str(item["path"])).is_file():
                        errors.append(
                            f"{shot_id}: selected_references 文件不存在：{item['path']}"
                        )
                required_ids = list(shot.get("reference_character_ids") or [])
                covered: set[str] = set()
                for item in selected:
                    for character_id in item.get("character_ids") or []:
                        covered.add(str(character_id))
                missing = [character_id for character_id in required_ids if character_id not in covered]
                if missing and selected:
                    errors.append(
                        f"{shot_id}: ref2va 缺少角色参考覆盖：{', '.join(missing)}"
                    )
        # Require consistency artifacts when identity pipeline is expected.
        cons_report = run_dir / "03_shots/consistency_report.json"
        config_snapshot = run_dir / "project.config.yaml"
        if config_snapshot.is_file():
            try:
                snapshot = load_config(config_snapshot, require_images=False).data
                identity = snapshot.get("identity_consistency") or {}
                cons = snapshot.get("consistency_pipeline") or {}
                consistency_enabled = bool(cons.get("enabled", identity.get("enabled", False)))
            except Exception:  # noqa: BLE001
                consistency_enabled = False
            if consistency_enabled and not cons_report.is_file():
                errors.append(
                    "已启用 consistency_pipeline，但缺少 03_shots/consistency_report.json；"
                    "请先执行 prepare-consistency"
                )
            if cons_report.is_file():
                report = load_json(cons_report)
                for item in report.get("blocking_errors") or []:
                    errors.append(str(item))
    return artifact, sorted(set(errors))


def cmd_validate(args: argparse.Namespace) -> int:
    artifact, errors = _validate_stage(Path(args.run).expanduser().resolve(), args.stage)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 2
    print(f"校验通过：{artifact}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    artifact, errors = _validate_stage(run_dir, args.stage)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 2
    print(f"即将批准：{artifact}")
    if not args.yes:
        answer = input("输入 APPROVE 确认：")
        if answer != "APPROVE":
            print("未批准。")
            return 1
    approval = create_approval(run_dir, args.stage, confirmed=True)
    print(f"已创建批准标记：{approval}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    commands = {
        "init": cmd_init, "status": cmd_status, "prepare-images": cmd_prepare_images,
        "describe-images": cmd_describe_images,
        "plan-story": cmd_plan_story,
        "plan-shots": cmd_plan_shots,
        "prepare-consistency": cmd_prepare_consistency,
        "render-prompts": cmd_render_prompts,
        "generate-videos": cmd_generate_videos,
        "check-continuity": cmd_check_continuity,
        "generate-subtitles": cmd_generate_subtitles,
        "assemble": cmd_assemble,
        "validate": cmd_validate, "approve": cmd_approve,
    }
    try:
        return commands[args.command](args)
    except (ConfigError, FileNotFoundError, FileExistsError, ValueError, json.JSONDecodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
