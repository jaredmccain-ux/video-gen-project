#!/usr/bin/env python3
"""Copy a finished external production into a new SceneFlow run.

The source directory only needs a `shot_inputs.json` describing beats and shots
(per-shot视听描述、提示词、成品视频与首帧). Nothing outside `runs/` is written,
and the source tree is opened read-only.

The imported film must be the cut *without* subtitles: SceneFlow's own subtitle
pipeline (planned cues -> ASR alignment -> hard burn) runs afterwards, so
`06_subtitles/` and `07_final/studio_final_sub.mp4` stay consistent with the
approved dialogue.

    python scripts/import_external_project.py \
        --source /path/to/production \
        --master /path/to/cut_without_subtitles.mp4 \
        --story-seed /path/to/story.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from short_drama.approval import create_approval
from short_drama.config import load_config
from short_drama.state import initialize_run, utc_now, write_json_atomic
from short_drama.studio_finish import generate_studio_subtitles


ANCHOR_IMAGES = (
    "IMG01_bridge_bicycle.png",
    "IMG02_wanda_plaza.png",
    "IMG03_cinema.jpg",
    "C01_linchuan_white_tank.png",
    "C02_linye_orange_shirt.png",
)
MIN_SHOT_S = 4.0
MAX_SHOT_S = 8.0


def _probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        text=True,
    ).strip()
    return float(out)


def _copy(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def _wrap_prompt(visual: str, audio: str, sent: str) -> str:
    body = (sent or visual or "").strip()
    if "integrated_multimodal_description:" in body or "subject_definitions:" in body:
        return body
    sound = audio or "现场环境声与对白。"
    return (
        f"integrated_multimodal_description: {body}\n\n"
        f"overall_soundscape: {sound}\n\n"
        "non_diegetic_music: N/A"
    )


SPEECH_TAGS = {"speaker", "对白", "台词", "dialogue", "旁白"}
TAG_RE = re.compile(r"\[([^\]]{1,12})\]")
SPEECH_LINE_RE = re.compile(
    r"(?P<name>[\u4e00-\u9fff]{2,4})\s*(?:（[^）]*）|\([^)]*\))?\s*[：:]\s*"
    r"(?:“(?P<quoted>[^”]*)”|(?P<plain>[^；;“”]*))"
)


def _speech_blocks(audio: str) -> list[str]:
    """audio_desc mixes `[环境声]`, `[音乐]` and `[Speaker]` / `[对白]` blocks in one line."""
    text = str(audio or "")
    marks = list(TAG_RE.finditer(text))
    blocks = []
    for index, mark in enumerate(marks):
        if mark.group(1).strip().lower() not in SPEECH_TAGS:
            continue
        stop = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        blocks.append(text[mark.end() : stop])
    return blocks


def _dialogues(audio: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for block in _speech_blocks(audio):
        for match in SPEECH_LINE_RE.finditer(block):
            speaker = match.group("name").strip()
            body = match.group("quoted") if match.group("quoted") is not None else match.group("plain")
            line = str(body or "").strip().strip("；;“”\"\u3000 ")
            if speaker and line:
                found.append({"speaker_id": speaker, "text": line})
    return found


def _character_dialogue(audio: str) -> list[dict[str, str]]:
    """The source labels speakers by character name; SceneFlow shots use C01/C02 ids."""
    return [
        {"speaker_id": "C02" if "野" in line["speaker_id"] else "C01", "text": line["text"], "speaker_name": line["speaker_id"]}
        for line in _dialogues(audio)
    ]


def _load_shot_inputs(source: Path) -> dict:
    path = source / "shot_inputs.json"
    if not path.is_file():
        raise FileNotFoundError(f"源工程缺少 shot_inputs.json：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def refresh_dialogue(run_dir: Path, source: Path) -> dict[str, int]:
    """Re-read the source dialogue into an existing run, then rebuild planned subtitles."""
    inputs = _load_shot_inputs(source)
    by_origin_id = {
        shot["shot_id"]: (shot.get("text_inputs") or {}).get("audio_desc") or ""
        for beat in inputs["beats"]
        for shot in beat["shots"]
    }
    shots_path = run_dir / "03_shots/shots.json"
    document = json.loads(shots_path.read_text(encoding="utf-8"))
    lines = 0
    for shot in document["shots"]:
        dialogues = _character_dialogue(by_origin_id.get(shot.get("origin_shot_id"), ""))
        speakers = list(dict.fromkeys(line["speaker_id"] for line in dialogues))
        shot["dialogue"] = dialogues
        shot["subtitle_text"] = "".join(line["text"] for line in dialogues)
        shot["speaker_mappings"] = [
            {"character_id": value, "prompt_speaker_id": f"S{position}"}
            for position, value in enumerate(speakers, start=1)
        ]
        shot.setdefault("audio_contract", {})["allowed_speaker_ids"] = speakers
        lines += len(dialogues)
    document["dialogue_refreshed_at"] = utc_now()
    write_json_atomic(shots_path, document)
    payload = generate_studio_subtitles(run_dir)
    return {"shots": len(document["shots"]), "dialogue_lines": lines, "subtitle_cues": len(payload["cues"])}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", type=Path, required=True, help="外部工程目录，需含 shot_inputs.json")
    parser.add_argument("--assets", type=Path, help="角色与场景锚点图目录，默认 <source>/assets")
    parser.add_argument("--master", type=Path, help="导入的成片，必须是无字幕剪辑")
    parser.add_argument("--story-seed", type=Path, help="story.json 来源（角色、风格、配音设定）")
    parser.add_argument("--run-id", help="目标 run id，默认自动生成")
    parser.add_argument("--project-name", help="工作台显示的项目名，默认取配置里的 project_name")
    parser.add_argument("--config", default="configs/project.local.yaml")
    parser.add_argument("--refresh-dialogue", action="store_true", help="只重读源工程对白并重建计划字幕")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = args.source
    if not source.is_dir():
        raise NotADirectoryError(f"找不到源工程目录：{source}")
    assets = args.assets or (source / "assets")
    config = load_config(args.config)

    if args.refresh_dialogue:
        if not args.run_id:
            raise SystemExit("--refresh-dialogue 需要 --run-id")
        print(json.dumps(refresh_dialogue(config.run_root / args.run_id, source), ensure_ascii=False, indent=2))
        return

    for required, label in ((args.master, "--master"), (args.story_seed, "--story-seed")):
        if required is None:
            raise SystemExit(f"缺少参数 {label}")
    if not args.master.is_file():
        raise FileNotFoundError(f"找不到要导入的成片：{args.master}")

    inputs = _load_shot_inputs(source)
    if args.run_id and (config.run_root / args.run_id).exists():
        raise FileExistsError(f"run 已存在，拒绝覆盖：{config.run_root / args.run_id}")
    run_dir = initialize_run(config, run_id=args.run_id)
    if args.project_name:
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        state["project_name"] = " ".join(args.project_name.split())
        state["updated_at"] = utc_now()
        write_json_atomic(run_dir / "run.json", state)

    copied_assets: dict[str, Path] = {}
    for name in ANCHOR_IMAGES:
        src = assets / name
        if src.is_file():
            copied_assets[name] = _copy(src, run_dir / "inputs/studio_uploads/images" / name)

    # descriptions
    desc_images = []
    desc_map = {
        "IMG01": ("IMG01_bridge_bicycle.png", "临海桥面，白天，粉色自行车沿灰色金属护栏前进，远山与蓝色水面。", "暖日光、开阔、青春"),
        "IMG02": ("IMG02_wanda_plaza.png", "城市广场/万达外景，白天，商业建筑与人流。", "明亮、城市夏日"),
        "IMG03": ("IMG03_cinema.jpg", "电影院室内或入口相关画面。", "暗场、影院灯光"),
    }
    for image_id, (fname, facts, mood) in desc_map.items():
        path = copied_assets.get(fname)
        if not path:
            continue
        desc_images.append({
            "image_id": image_id,
            "source_path": str(path),
            "visible_facts": [facts],
            "setting": facts,
            "people": [
                {"label": "林川", "appearance": "白色背心、浅色短裤、短黑发哥哥", "pose_or_action": "骑车/行走", "screen_position": "画面前侧"},
                {"label": "林野", "appearance": "橙白拼接上衣、蓝色短裤弟弟", "pose_or_action": "后座/同行", "screen_position": "林川附近"},
            ] if image_id == "IMG01" else [],
            "objects": ["粉色自行车"] if image_id == "IMG01" else [],
            "mood_or_atmosphere": mood,
            "uncertainties": [],
            "story_affordances": [facts],
        })
    write_json_atomic(run_dir / "01_descriptions/image_descriptions.json", {
        "schema_version": "2.0",
        "generated_at": utc_now(),
        "images": desc_images,
        "origin": "imported_copy",
    })

    write_json_atomic(run_dir / "inputs/inspiration.json", {
        "schema_version": "1.0",
        "updated_at": utc_now(),
        "active_source": "images",
        "selected_images": [str(copied_assets[k]) for k in ("IMG01_bridge_bicycle.png", "IMG02_wanda_plaza.png", "IMG03_cinema.jpg") if k in copied_assets],
        "current_proposal": None,
        "history": [],
    })

    story = json.loads(args.story_seed.read_text(encoding="utf-8"))
    screenplay = "\n\n".join(
        f"# {beat['beat_id']}\n\n{beat.get('script') or ''}" for beat in inputs["beats"]
    )
    story["screenplay"] = screenplay
    story["outline"] = "\n".join(
        f"## {beat['beat_id']}\n{beat.get('script') or ''}" for beat in inputs["beats"]
    )
    write_json_atomic(run_dir / "02_story/story.json", story)
    (run_dir / "02_story/screenplay.md").write_text(screenplay + "\n", encoding="utf-8")
    (run_dir / "02_story/outline.md").write_text(story["outline"] + "\n", encoding="utf-8")

    flat = []
    for beat in inputs["beats"]:
        for shot in beat["shots"]:
            vg = shot.get("video_generation") or {}
            video_src = Path((vg.get("output") or {}).get("path") or "")
            if not video_src.is_file():
                video_src = source / "beats" / beat["beat_id"] / "shots" / str(shot["shot_idx"]) / "video.mp4"
            ff_src = Path(((shot.get("first_frame_image") or {}).get("output") or {}).get("path") or "")
            if not ff_src.is_file():
                ff_src = video_src.parent / "first_frame.png"
            prompt = vg.get("actual_prompt_sent") or vg.get("prompt") or ""
            text = shot.get("text_inputs") or {}
            duration = 7.5
            if video_src.is_file():
                try:
                    duration = min(8.0, max(4.0, round(_probe_duration(video_src), 2)))
                except Exception:
                    duration = 7.5
            flat.append({
                "origin_id": shot["shot_id"],
                "beat_id": beat["beat_id"],
                "video_src": video_src,
                "ff_src": ff_src,
                "prompt": prompt,
                "visual": text.get("visual_desc") or "",
                "audio": text.get("audio_desc") or "",
                "first_desc": text.get("first_frame_desc") or "",
                "last_desc": text.get("last_frame_desc") or "",
                "duration": duration,
                "script": beat.get("script") or "",
            })

    # Keep each shot's real length so the planned timeline tracks the imported cut,
    # only clamping to the 4–8s window the shot schema allows.
    for item in flat:
        item["duration_s"] = round(min(MAX_SHOT_S, max(MIN_SHOT_S, float(item["duration"]))), 2)

    shots = []
    orch: dict[str, dict] = {}
    jobs: dict[str, dict] = {}
    cursor = 0.0
    c01 = copied_assets.get("C01_linchuan_white_tank.png")
    c02 = copied_assets.get("C02_linye_orange_shirt.png")
    img01 = copied_assets.get("IMG01_bridge_bicycle.png")
    previous = None
    for index, item in enumerate(flat, start=1):
        shot_id = f"S{index:03d}"
        job_id = f"{shot_id}-{uuid.uuid4().hex[:10]}"
        _copy(item["video_src"], run_dir / "05_videos" / f"{shot_id}.mp4")
        dest_studio = _copy(item["video_src"], run_dir / "05_videos/studio_generations" / shot_id / f"{job_id}.mp4")
        dest_ff = None
        if item["ff_src"].is_file():
            dest_ff = _copy(item["ff_src"], run_dir / "inputs/studio_uploads/images" / f"{shot_id}_first_frame.png")
        dialogues = _character_dialogue(item["audio"])
        speakers = list(dict.fromkeys(line["speaker_id"] for line in dialogues))
        duration = float(item["duration_s"])
        shot = {
            "shot_id": shot_id,
            "beat_id": item["beat_id"],
            "scene_id": "L01" if item["beat_id"] in {"B01", "B02"} else "L02" if item["beat_id"] in {"B03", "B04"} else "L03",
            "planned_start_s": cursor,
            "planned_end_s": cursor + duration,
            "duration_s": duration,
            "story_purpose": item["visual"][:80] or item["script"].splitlines()[0],
            "composition": item["first_desc"][:120] or "16:9 暖色写实电影构图",
            "camera": "稳定跟拍 / 写实电影机位",
            "action_timeline": item["visual"][:400] or item["script"][:400],
            "continuity_in": "承接上一镜头人物位置与服装。",
            "continuity_out": "保持林川白背心、林野橙白上衣与粉色自行车连续。",
            "characters": ["C01", "C02"],
            "dialogue": dialogues,
            "subtitle_text": "".join(d["text"] for d in dialogues),
            "speaker_mappings": [
                {"character_id": value, "prompt_speaker_id": f"S{position}"}
                for position, value in enumerate(speakers, start=1)
            ],
            "audio_contract": {
                "allowed_speaker_ids": speakers,
                "offscreen_human_voice_allowed": False,
                "non_diegetic_music": False,
                "ambient_sounds": [],
                "action_sounds": [],
            },
            "generation_mode": "ref2va",
            "depends_on": previous,
            "first_frame_desc": item["first_desc"],
            "last_frame_desc": item["last_desc"],
            "origin_shot_id": item["origin_id"],
            "status": "completed",
            "attempt": 1,
        }
        shots.append(shot)
        refs = []
        bindings = []
        if dest_ff:
            refs.append(str(dest_ff))
            bindings.append({"path": str(dest_ff), "usage": "keyframe", "character_ids": [], "note": "镜头首帧"})
        if c01:
            refs.append(str(c01))
            bindings.append({"path": str(c01), "usage": "identity", "character_ids": ["C01"], "note": "林川身份参考"})
        if c02:
            refs.append(str(c02))
            bindings.append({"path": str(c02), "usage": "identity", "character_ids": ["C02"], "note": "林野身份参考"})
        if img01 and item["beat_id"] == "B01":
            refs.append(str(img01))
            bindings.append({"path": str(img01), "usage": "scene", "character_ids": [], "note": "桥面场景锚点"})
        orch[shot_id] = {
            "generation_mode": "ref2va",
            "mode_label": "多参考 Ref2VA",
            "prompt": _wrap_prompt(item["visual"], item["audio"], item["prompt"]),
            "origin_prompt": item["prompt"],
            "first_frame": str(dest_ff) if dest_ff else None,
            "last_frame": None,
            "reference_images": refs,
            "reference_image_bindings": bindings,
            "approved": True,
            "locked": True,
            "seed": 2101,
        }
        jobs[job_id] = {
            "job_id": job_id,
            "shot_id": shot_id,
            "status": "completed",
            "generation_mode": "ref2va",
            "video": str(dest_studio),
            "last_frame": str(dest_ff) if dest_ff else None,
            "completed_at": utc_now(),
            "origin": "imported_copy",
        }
        previous = shot_id
        cursor += duration

    write_json_atomic(run_dir / "03_shots/shots.json", {
        "schema_version": "2.0",
        "generated_at": utc_now(),
        "shots": shots,
        "origin": "imported_copy",
    })
    write_json_atomic(run_dir / "03_shots/human_orchestration.json", {
        "schema_version": "1.0",
        "updated_at": utc_now(),
        "policy": "imported_copy",
        "shots": orch,
    })
    write_json_atomic(run_dir / "05_videos/studio_jobs.json", {
        "schema_version": "1.0",
        "updated_at": utc_now(),
        "jobs": jobs,
    })

    # The clean cut lands as studio_master.mp4, which is exactly what the subtitle
    # step burns onto; studio_final_sub.mp4 then becomes the delivered film.
    dest_master = _copy(args.master, run_dir / "07_final/studio_master.mp4")
    write_json_atomic(run_dir / "07_final/studio_assemble.json", {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "shot_count": len(shots),
        "missing": [],
        "duration_s": _probe_duration(dest_master),
        "burned_subtitles": False,
        "outputs": {"master": str(dest_master)},
        "origin": "imported_copy",
    })

    create_approval(run_dir, "descriptions", confirmed=True)
    create_approval(run_dir, "story", confirmed=True)
    create_approval(run_dir, "shots", confirmed=True)
    generate_studio_subtitles(run_dir)

    (run_dir / "logs/IMPORT.txt").write_text(
        "镜头片段、首帧与无字幕成片为外部工程产出的只读拷贝，源目录未被改动。\n"
        "字幕由本系统生成：按已批准对白排计划字幕，再对齐成片人声并烧录。\n",
        encoding="utf-8",
    )
    print(json.dumps({"run_id": run_dir.name, "run_dir": str(run_dir), "shots": len(shots)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
