#!/usr/bin/env python3
"""端到端跑通短剧流水线（供 Cursor 单入口 Debug）。

流程：图片预处理 → 看图描述 → 故事 → 分镜 → H3 提示词 → Comfy 出片 → 字幕 → 合片。
人工门禁在本脚本中自动 --yes 跳过，方便断点调试。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from short_drama.approval import create_approval  # noqa: E402
from short_drama.assemble import assemble_run  # noqa: E402
from short_drama.cli import _validate_stage  # noqa: E402
from short_drama.config import load_config  # noqa: E402
from short_drama.consistency import prepare_consistency  # noqa: E402
from short_drama.describe import describe_run_images  # noqa: E402
from short_drama.h3_jobs import generate_run_videos  # noqa: E402
from short_drama.h3_prompt import render_run_prompts  # noqa: E402
from short_drama.image_prepare import prepare_run_images  # noqa: E402
from short_drama.shots import plan_shots  # noqa: E402
from short_drama.state import initialize_run, read_run, write_json_atomic  # noqa: E402
from short_drama.story import plan_story  # noqa: E402
from short_drama.subtitle_align import generate_planned_subtitles  # noqa: E402


def _approve(run_dir: Path, stage: str) -> None:
    artifact, errors = _validate_stage(run_dir, stage)
    if errors:
        raise ValueError(f"{stage} 校验失败：" + "; ".join(errors))
    create_approval(run_dir, stage, confirmed=True)
    print(f"[approve] {stage} ok → {artifact.name}")


def _patch_beat_durations_for_speakers(run_dir: Path) -> None:
    """给多说话人 beat 留出至少 12s，避免分镜硬约束无解。"""
    path = run_dir / "02_story/story.json"
    story = json.loads(path.read_text(encoding="utf-8"))
    three_speaker_hints = ("三人", "三名", "周叔、林川、林野", "许敏、林川、林野")
    beats = story.get("beats") or []
    if not beats:
        return
    heavy: set[str] = set()
    for beat in beats:
        notes = str(beat.get("dialogue_notes") or "")
        if any(h in notes for h in three_speaker_hints) or notes.count("、") >= 2 and "说话" in notes:
            # crude: if notes mention three names speaking
            speakers = 0
            for name in ("林野", "林川", "周叔", "许敏"):
                if name in notes and ("说话" in notes or "对白" in notes or "发问" in notes or "说" in notes):
                    speakers += 1
            if speakers >= 3 or any(h in notes for h in ("三人", "三名")):
                heavy.add(beat["beat_id"])
    # known pattern from MVP: prefer explicit B02/B07/B08/B11 style if 12 beats
    if len(beats) == 12 and not heavy:
        heavy = {"B02", "B07", "B08", "B11"}
    if not heavy:
        return
    for beat in beats:
        beat["duration_s"] = 12 if beat["beat_id"] in heavy else 9
    total = sum(float(b["duration_s"]) for b in beats)
    # keep ~120s
    if abs(total - 120) > 0.1 and len(beats) == 12:
        for beat in beats:
            beat["duration_s"] = 12 if beat["beat_id"] in heavy else 9
    path.write_text(json.dumps(story, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[story] patched beat durations heavy={sorted(heavy)} total={sum(b['duration_s'] for b in beats)}")


def _plan_shots_resilient(run_dir: Path, config) -> Path:
    try:
        return plan_shots(run_dir, config)
    except Exception as exc:  # noqa: BLE001
        print(f"[shots] plan_shots failed ({exc}); fallback to chunked planner")
        shots_dir = run_dir / "03_shots"
        for name in ("raw_response.txt", "error.json", "shots.json"):
            p = shots_dir / name
            if p.exists():
                p.unlink()
        import importlib.util

        chunked_path = ROOT / "scripts/plan_shots_chunked.py"
        spec = importlib.util.spec_from_file_location("plan_shots_chunked", chunked_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载 {chunked_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        old = sys.argv
        try:
            sys.argv = ["plan_shots_chunked.py", "--run", str(run_dir), "--chunk-size", "4"]
            code = mod.main()
        finally:
            sys.argv = old
        if code != 0:
            raise RuntimeError("chunked plan-shots failed")
        return run_dir / "03_shots/shots.json"


def run_pipeline(*, config_path: Path, font: Path, env_prefix: Path, max_shots: int | None) -> Path:
    config = load_config(config_path)
    run_dir = initialize_run(config)
    print(f"[init] {run_dir}")

    prepare_run_images(run_dir, config)
    print("[prepare-images] done")

    config = load_config(read_run(run_dir)["config_snapshot"])
    describe_run_images(run_dir, config)
    _approve(run_dir, "descriptions")

    plan_story(run_dir, config)
    _patch_beat_durations_for_speakers(run_dir)
    _approve(run_dir, "story")

    _plan_shots_resilient(run_dir, config)
    consistency_report_path = prepare_consistency(run_dir, config)
    consistency_report = json.loads(consistency_report_path.read_text(encoding="utf-8"))
    print(f"[prepare-consistency] done modes={consistency_report.get('mode_counts')}")
    blocking = consistency_report.get("blocking_errors") or []
    if blocking:
        raise ValueError(
            "prepare-consistency 存在阻断错误，无法批准分镜：" + "; ".join(blocking[:12])
        )
    _approve(run_dir, "shots")

    render_run_prompts(run_dir, config)
    print("[render-prompts] done")

    if max_shots is not None:
        # Trim prompt report so generate-videos only does first N ready chain shots.
        report_path = run_dir / "04_prompts/validation_report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["shots"] = report["shots"][:max_shots]
        write_json_atomic(report_path, report)
        print(f"[debug] limit generate-videos to first {max_shots} prompt entries")

    generate_run_videos(run_dir, config)
    print("[generate-videos] done")

    generate_planned_subtitles(run_dir)
    print("[subtitles] done")

    # assemble needs all shot videos; if max_shots truncated, skip assemble
    shots = json.loads((run_dir / "03_shots/shots.json").read_text(encoding="utf-8"))["shots"]
    missing = [s["shot_id"] for s in shots if not (run_dir / "05_videos" / f'{s["shot_id"]}.mp4').is_file()]
    if missing:
        print(f"[assemble] skip, missing videos: {missing[:8]}{'...' if len(missing)>8 else ''}")
    else:
        final = assemble_run(run_dir, env_prefix, font)
        print(f"[assemble] {final}")

    print(json.dumps({"run_dir": str(run_dir), "final": str(run_dir / "07_final/final.mp4")}, ensure_ascii=False, indent=2))
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="短剧全流程（Debug 入口）")
    parser.add_argument("--config", default=str(ROOT / "configs/project.local.yaml"))
    parser.add_argument("--font", default="/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    parser.add_argument("--env-prefix", default="/usr")
    parser.add_argument(
        "--max-shots",
        type=int,
        default=None,
        help="仅生成前 N 条 prompt 对应视频（快速调试用；不设则全量）",
    )
    args = parser.parse_args()
    if not os.environ.get("RIVO_API_KEY"):
        raise SystemExit("请先设置 RIVO_API_KEY（可用 .vscode/debug.env）")
    run_pipeline(
        config_path=Path(args.config),
        font=Path(args.font),
        env_prefix=Path(args.env_prefix),
        max_shots=args.max_shots,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
