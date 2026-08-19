"""Render validated shot plans into prompts following the official MiniMax H3 guide."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .approval import approval_status
from .config import ProjectConfig
from .reference_assets import ReferenceBinding, resolve_shot_references
from .state import read_run, utc_now, write_json_atomic
from .validators import load_json, validate_document, validate_shots_against_story


FIELD_NAMES = (
    "integrated_multimodal_description:",
    "overall_soundscape:",
    "non_diegetic_music:",
)

REF_FIELD_NAMES = (
    "subject_definitions:",
    "summary:",
    "retention_analysis:",
    "detailed_description:",
    "overall_soundscape:",
    "non_diegetic_music:",
)

POSITION = {
    "screen-left": "screen left", "screen-center": "screen center", "screen-right": "screen right",
    "foreground": "the foreground", "midground": "the midground", "background": "the background",
    "camera": "the camera", "away-from-camera": "away from the camera",
    "toward-camera": "toward the camera",
}

POSITION_CN = {
    "screen-left": "画面左侧", "screen-center": "画面中央", "screen-right": "画面右侧",
    "foreground": "前景", "midground": "中景", "background": "背景",
    "screen-right": "画面右侧", "screen-left": "画面左侧",
    "camera": "镜头", "away-from-camera": "背向镜头",
    "toward-camera": "朝向镜头",
}

SOUND = {
    "商场人流声": "the steady murmur of the shopping-center crowd",
    "戏水人群环境声": "distant voices and splashes from people in the water",
    "桥面风声": "steady wind crossing the bridge",
    "海浪声": "waves washing against the shore",
    "海风声": "steady sea wind",
    "脚步声": "footsteps",
    "车辆声": "passing road traffic",
    "车辆驶过声": "vehicles passing nearby",
    "远处车辆声": "distant road traffic",
    "刹车声": "a brief braking sound",
    "布包被风吹开声": "the cloth bag snapping open in the wind",
    "布料拉扯声": "fabric pulling taut",
    "帽带撕开声": "the hatband seam tearing open",
    "急促脚步声": "rapid footsteps",
    "手机抬起声": "a soft handling sound as the phone is raised",
    "拉链声": "a zipper opening",
    "按键轻响": "a quiet button click",
    "照片落水声": "the photograph touching the shallow water",
    "自行车刹车声": "the bicycle brakes engaging",
    "自行车支架碰撞声": "the bicycle stand tapping the pavement",
    "自行车链条声": "the bicycle chain turning",
    "衣物摩擦声": "soft clothing rustle",
    "踩水声": "feet splashing through shallow water",
    "车轮减速声": "bicycle wheels slowing",
    "车轮滚动声": "bicycle wheels rolling",
    "车铃声": "a single bicycle-bell ring",
    "链条声": "the bicycle chain turning",
}


def _dialogue_exceptions(run_dir: Path) -> set[str]:
    path = run_dir / "03_shots/dialogue_pacing_exceptions.json"
    if not path.is_file():
        return set()
    return {item["shot_id"] for item in load_json(path).get("exceptions", [])}


def _voice_profile(character: dict[str, Any]) -> str:
    identity = character.get("identity", "")
    if "十一岁" in identity or "弟弟" in identity:
        return "清亮偏高的少年男声，语速较快但清楚"
    if "十九岁" in identity or "哥哥" in identity:
        return "清晰的中音青年男声，语气紧张，语速利落"
    if "母亲" in identity:
        return "稳定的中低音中年女声，语速克制"
    if "周婶" in character.get("name", "") or "邻居" in identity:
        return "略带喘息的中音中年女声，语速直接"
    return "清晰自然的中音声线，语速适中"


def _state_phrase(state: dict[str, Any]) -> str:
    if not state["visible"]:
        return "位于可见画面之外"
    return (
        f'位于{POSITION_CN[state["horizontal"]]}的{POSITION_CN[state["depth"]]}，'
        f'面朝{POSITION_CN[state["facing"]]}'
    )


def _character_opening(blocking: dict[str, Any], character: dict[str, Any]) -> str:
    return (
        f'{character["name"]}（{character["identity"]}；{character["appearance"]}）'
        f'开场时{_state_phrase(blocking["start"])}'
    )


def _character_ending(blocking: dict[str, Any], character: dict[str, Any]) -> str:
    movement = blocking["movement_direction"]
    movement_text = "保持画面位置稳定" if movement == "none" else f'运动方向为{POSITION_CN.get(movement, movement)}'
    return f'{character["name"]}在末帧{_state_phrase(blocking["end"])}，{movement_text}'


def _camera_sentence(camera: str) -> str:
    if "固定" in camera:
        return "The camera holds a static shot."
    if "环绕" in camera:
        return "The camera makes an arc shot with small amplitude at slow speed around the subjects."
    if "横摇" in camera:
        speed = "fast" if "快速" in camera else "slow"
        return f"The camera pans horizontally with small amplitude at {speed} speed to follow the action."
    if "横移" in camera:
        return "The camera trucks sideways with small amplitude at slow speed."
    if "推近" in camera or "推向" in camera:
        return "The camera pushes in with small amplitude at slow speed toward the focal subject."
    if "拉远" in camera or "拉回" in camera:
        speed = "fast" if "快速" in camera else "slow"
        return f"The camera pulls out with small amplitude at {speed} speed."
    if "跟拍" in camera or "跟进" in camera:
        speed = "fast" if "快速" in camera else "normal"
        return f"The camera uses a tracking shot at {speed} speed, keeping the moving subject framed."
    if "后撤" in camera or "向后" in camera:
        return "The camera pulls out at slow speed while keeping the subjects centered."
    if "俯拍" in camera:
        return "From a high angle, the camera descends with small amplitude at slow speed."
    if "上摇" in camera:
        return "The camera tilts up with small amplitude at slow speed."
    return "The camera moves gently with small amplitude at slow speed while preserving clear framing."


def _dialogue_sentence(
    shot: dict[str, Any], characters: dict[str, dict[str, Any]], blocking: dict[str, dict[str, Any]]
) -> str:
    dialogue = shot["dialogue"]
    if not dialogue:
        visible = [characters[item["character_id"]]["name"] for item in shot["blocking"] if item["start"]["visible"] or item["end"]["visible"]]
        names = ", ".join(visible) if visible else "Every person"
        return f"{names}全程闭紧嘴唇，不发出任何语言人声。"

    # Group consecutive lines by speaker so every planned line appears exactly once.
    groups: list[tuple[str, list[str]]] = []
    for line in dialogue:
        speaker_id = line["speaker_id"]
        if groups and groups[-1][0] == speaker_id:
            groups[-1][1].append(line["text"])
        else:
            groups.append((speaker_id, [line["text"]]))

    parts: list[str] = []
    all_speakers = {line["speaker_id"] for line in dialogue}
    for speaker_id, texts in groups:
        character = characters[speaker_id]
        mapping = next(item for item in shot["speaker_mappings"] if item["character_id"] == speaker_id)
        prompt_speaker_id = mapping["prompt_speaker_id"]
        state = blocking[speaker_id]["start"] if blocking[speaker_id]["start"]["visible"] else blocking[speaker_id]["end"]
        spoken = " ".join(f'<d>[Chinese] {text}</d>' for text in texts)
        if len(texts) == 1:
            verb = "说道"
        else:
            verb = "依次说道"
        parts.append(
            f'画面内可见的{character["name"]}（{character["identity"]}，{_voice_profile(character)}）'
            f'({prompt_speaker_id}) 在{_state_phrase(state)}时{verb}：{spoken}'
        )
    silent_names = [
        characters[item["character_id"]]["name"]
        for item in shot["blocking"]
        if item["character_id"] not in all_speakers
        and (item["start"]["visible"] or item["end"]["visible"])
    ]
    sentence = " ".join(parts)
    if silent_names:
        sentence += f' 对白期间，{", ".join(silent_names)}始终闭紧嘴唇，不发声。'
    return sentence


def _soundscape(audio: dict[str, Any]) -> str:
    items = [*audio["ambient_sounds"], *audio["action_sounds"]]
    phrases = [SOUND.get(item, f'the natural sound of {item}') for item in items]
    if not phrases:
        return "N/A"
    if len(phrases) == 1:
        return phrases[0].capitalize() + " remains synchronized with the visible scene."
    if len(phrases) == 2:
        joined = f"{phrases[0]} and {phrases[1]}"
    else:
        joined = ", ".join(phrases[:-1]) + f", and {phrases[-1]}"
    return joined.capitalize() + " remain synchronized with the visible scene and physical actions."


def _visual_description(
    shot: dict[str, Any], characters: dict[str, dict[str, Any]]
) -> str:
    blocking = {item["character_id"]: item for item in shot["blocking"]}
    opening = "; ".join(_character_opening(item, characters[item["character_id"]]) for item in shot["blocking"])
    ending = "; ".join(_character_ending(item, characters[item["character_id"]]) for item in shot["blocking"])
    if shot["generation_mode"] == "first_last_frame":
        image_anchor = (
            "开场构图严格保持 <Picture 1>，末帧严格收束到 <Picture 2>；"
            "中间运动必须自然连续，不瞬移、不换装、不改变人物身份。 "
        )
    elif shot["generation_mode"] == "first_frame":
        image_anchor = (
        "开场构图、人物身份、服装、颜色、道具和空间关系严格保持 <Picture 1>。 "
        )
    else:
        image_anchor = ""
    motion = str(shot.get("motion_desc") or "").strip()
    action = shot.get("action_timeline") or ""
    if motion and motion != action:
        action_sentence = (
            f'{shot["duration_s"]:g}秒内，可见动作严格按此顺序连续发生：{action} '
            f"运动细节补充：{motion}"
        )
    else:
        action_sentence = (
            f'{shot["duration_s"]:g}秒内，可见动作严格按此顺序连续发生：{action}'
        )
    return " ".join((
        "[Shot 1] 写实电影感短剧画面，采用一个连续镜头。",
        image_anchor,
        f"开场人物状态为：{opening}。",
        f'场景、景别与光线自然形成以下构图：{shot["composition"]}',
        action_sentence,
        _camera_sentence(shot["camera"]),
        _dialogue_sentence(shot, characters, blocking),
        f"末帧人物状态为：{ending}。",
        "全程保持人物身份、服装、道具、画面方向和单一地点连续一致；不增加字幕、文字条、标志、"
        "额外人物、镜头切换或计划外人声。",
    )).replace("  ", " ").strip()


def _reference_subjects(
    shot: dict[str, Any],
    story: dict[str, Any],
    bindings: list[ReferenceBinding],
) -> tuple[str, dict[str, dict[str, Any]]]:
    characters = {item["character_id"]: item for item in story["characters"]}
    rendered_characters = {key: dict(value) for key, value in characters.items()}
    lines: list[str] = []
    for subject_index, character_id in enumerate(
        shot.get("reference_character_ids") or [], start=1
    ):
        character = characters[character_id]
        pictures = [
            f"<Picture {index}>"
            for index, binding in enumerate(bindings, start=1)
            if character_id in binding.character_ids
        ]
        if not pictures:
            raise ValueError(
                f'{shot["shot_id"]}: {character_id} 没有可用的角色参考图'
            )
        # The story-level locator may mention several historical anchor images
        # (for example IMG01 + IMG03). Studio prompts must only describe the
        # pictures the human actually selected for this shot, so never copy
        # that cross-image locator into the runtime H3 prompt.
        visual_locator = "人工在当前镜头中指定的人物主体"
        lines.append(
            f'<Subject {subject_index}>: {character["name"]}，{character["identity"]}；'
            f'固定外观为“{character["appearance"]}”。身份和服装严格取自'
            f'{", ".join(pictures)} 中的“{visual_locator}”，跨帧不得改变脸型、年龄、'
            "发型、体型或服装。"
        )
        rendered_characters[character_id]["name"] = (
            f'<Subject {subject_index}>（{character["name"]}）'
        )
    if not lines:
        lines.append("N/A（本镜头没有把参考图片绑定为人物身份；图片只按各自声明的用途参与生成。）")
    return "\n".join(lines), rendered_characters


def _reference_picture_contract(index: int, binding: ReferenceBinding, note: str = "") -> str:
    roles = list(dict.fromkeys(role for role in binding.roles if role != "human_selected"))
    purposes = {
        "first_frame": (
            "first_frame - 首帧，必须作为目标视频 0.00 秒的起始画面；"
            "严格继承其构图、人物位置、姿态、镜头视角和可见场景"
        ),
        "last_frame": (
            "last_frame - 尾帧，必须作为目标视频结束时的收束画面；"
            "动作与镜头运动需从首帧自然连续过渡到该状态"
        ),
        "identity": (
            "identity - 人物参考，仅保持绑定人物的脸型、年龄、发型、体型、"
            "服装与主色，不复制背景或姿势"
        ),
        "scene": (
            "scene - 场景参考，必须用于地点、陈设、环境、光线与空间关系，"
            "不自动绑定人物身份"
        ),
        "style": (
            "style - 风格参考，仅参考色彩、光线、质感和摄影风格，"
            "不复制人物身份、原图动作或无关物体"
        ),
        "keyframe": (
            "keyframe - 参考关键动作状态、构图和空间关系（关键帧参考），"
            "不自动视为人物身份来源"
        ),
    }
    rendered = [purposes[role] for role in roles if role in purposes]
    if not rendered:
        rendered = [purposes["scene"]]
    suffix = f"；人工补充：{note.strip()}" if note and note.strip() else ""
    return f"<Picture {index}>: " + "；同时作为".join(rendered) + suffix + "。"


def _reference_video_contract(index: int, binding: dict[str, Any]) -> str:
    usage = str(binding.get("usage") or "motion")
    purposes = {
        "motion": "motion - 动作参考，提取主体动作、速度、节奏和物理运动，不复制无关人物身份",
        "camera": "camera - 运镜参考，提取机位、镜头运动、速度和幅度，不照搬无关场景",
        "style": "style - 视频风格参考，提取光线、色彩、质感和动态氛围，不复制无关人物",
        "continuity": "continuity - 连续性参考，承接前序动作、人物位置、道具状态和空间方向",
    }
    note = str(binding.get("note") or "").strip()
    suffix = f"；人工补充：{note}" if note else ""
    return f"<Video {index}>: {purposes.get(usage, purposes['motion'])}{suffix}。"


def _reference_audio_contract(index: int, binding: dict[str, Any]) -> str:
    usage = str(binding.get("usage") or "soundscape")
    purposes = {
        "voice": "voice - 人声/音色参考，仅用于剧本指定说话人的声线、语速与情绪，不新增台词或说话人",
        "soundscape": "soundscape - 环境声参考，用于空间氛围、环境声层次和响度关系",
        "action_sound": "action_sound - 动作音效参考，用于可见动作对应的材质、力度与同步时机",
        "rhythm": "rhythm - 声音节奏参考，仅用于动作节奏与音画同步，不作为非叙事背景音乐",
    }
    note = str(binding.get("note") or "").strip()
    suffix = f"；人工补充：{note}" if note else ""
    return f"<Audio {index}>: {purposes.get(usage, purposes['soundscape'])}{suffix}。"


def render_prompt(
    shot: dict[str, Any],
    story: dict[str, Any],
    reference_bindings: list[ReferenceBinding] | None = None,
) -> str:
    """Render one validated generation unit using the official H3 field layout."""
    characters = {item["character_id"]: item for item in story["characters"]}
    if shot["generation_mode"] == "ref2va":
        bindings = reference_bindings or []
        subject_definitions, rendered_characters = _reference_subjects(
            shot, story, bindings
        )
        visual = _visual_description(shot, rendered_characters)
        retention_lines = [
            f"<Subject {index}> (appears in [Shot 1]): fully_preserved - "
            f'{characters[character_id]["name"]}的脸型、年龄、发型、体型、'
            "固定服装和主色保持完整，不受动作或机位变化影响。"
            for index, character_id in enumerate(
                shot.get("reference_character_ids") or [], start=1
            )
        ]
        retention_lines.extend(
            _reference_picture_contract(
                index,
                binding,
                (shot.get("_picture_notes") or [""] * len(bindings))[index - 1],
            )
            for index, binding in enumerate(bindings, start=1)
        )
        retention_lines.extend(
            _reference_video_contract(index, binding)
            for index, binding in enumerate(shot.get("_video_bindings") or [], start=1)
        )
        retention_lines.extend(
            _reference_audio_contract(index, binding)
            for index, binding in enumerate(shot.get("_audio_bindings") or [], start=1)
        )
        return (
            f"subject_definitions:\n{subject_definitions}\n\n"
            f"summary: {shot['story_purpose']}；使用人工指定的多模态参考素材生成一个"
            f"{shot['duration_s']:g}秒连续写实短剧镜头。\n\n"
            "retention_analysis:\n"
            + "\n".join(retention_lines)
            + "\n\n"
            + f"detailed_description: {visual}\n\n"
            + f"overall_soundscape: {_soundscape(shot['audio_contract'])}\n\n"
            + "non_diegetic_music: N/A"
        )

    visual = _visual_description(shot, characters)
    bindings = reference_bindings or []
    picture_contract = " ".join(
        _reference_picture_contract(
            index,
            binding,
            (shot.get("_picture_notes") or [""] * len(bindings))[index - 1],
        )
        for index, binding in enumerate(bindings, start=1)
    )
    if picture_contract:
        visual = "图片用途硬约束（每张图必须按人工用途参与生成）：" + picture_contract + " " + visual
    body = (
        f"integrated_multimodal_description: {visual}\n\n"
        f"overall_soundscape: {_soundscape(shot['audio_contract'])}\n\n"
        "non_diegetic_music: N/A"
    )
    if shot["generation_mode"] in {"first_frame", "first_last_frame"}:
        instruction = (
            "For the target video, at 0.00 seconds into the target video, "
            "<Picture 1> (from [Shot 1]) is fully referenced."
        )
        if shot["generation_mode"] == "first_last_frame":
            instruction = (
                "How the reference pictures align with the target video — "
                "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the "
                "target video; Picture 2 (from Shot 1) aligns with the "
                f"{shot['duration_s']:.2f}-second mark of the target video."
            )
        return (
            instruction + "\n\n" + body
        )
    return body


def render_studio_prompt(
    shot: dict[str, Any],
    story: dict[str, Any],
    *,
    generation_mode: str,
    user_prompt: str = "",
    reference_paths: list[str] | None = None,
    reference_bindings: list[dict[str, Any]] | None = None,
    reference_video_bindings: list[dict[str, Any]] | None = None,
    reference_audio_bindings: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str]]:
    """Apply the official H3 prompt skill to one human-orchestrated shot.

    A structurally valid official prompt is preserved verbatim. Plain-language
    director notes are folded into the official detailed-description field so
    the final request always keeps MiniMax's field order and safety contracts.
    """
    normalized_shot = dict(shot)
    normalized_shot["generation_mode"] = generation_mode
    structured = reference_bindings if isinstance(reference_bindings, list) else []
    if not structured:
        # Preserve legacy decisions. New Studio selections provide a purpose
        # per image and therefore no longer bind every picture to every person.
        structured = [
            {
                "path": value,
                "usage": "identity",
                "character_ids": list(normalized_shot.get("characters") or []),
            }
            for value in (reference_paths or [])
        ]
    bindings: list[ReferenceBinding] = []
    picture_notes: list[str] = []
    identity_character_ids: list[str] = []
    for item in structured:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        usages = item.get("usages") or [item.get("usage") or "scene"]
        if isinstance(usages, str):
            usages = [usages]
        usages = list(dict.fromkeys(str(value).lower() for value in usages if value))
        usage = usages[0] if usages else "scene"
        character_ids = item.get("character_ids") or []
        if isinstance(character_ids, str):
            character_ids = [character_ids]
        if "identity" in usages and not character_ids:
            character_ids = list(normalized_shot.get("characters") or [])
        if "identity" not in usages:
            character_ids = []
        for character_id in character_ids:
            if character_id not in identity_character_ids:
                identity_character_ids.append(str(character_id))
        bindings.append(
            ReferenceBinding(
                path=Path(str(item["path"])),
                character_ids=tuple(str(value) for value in character_ids),
                roles=tuple([*usages, "human_selected"]),
            )
        )
        picture_notes.append(str(item.get("note") or "").strip())
    normalized_shot["_picture_usages"] = [
        [role for role in binding.roles if role != "human_selected"]
        for binding in bindings
    ]
    normalized_shot["_picture_notes"] = picture_notes
    normalized_shot["_video_bindings"] = list(reference_video_bindings or [])
    normalized_shot["_audio_bindings"] = list(reference_audio_bindings or [])
    if generation_mode == "ref2va":
        normalized_shot["reference_character_ids"] = identity_character_ids
    canonical = render_prompt(normalized_shot, story, bindings)
    source = str(user_prompt or "").strip()
    source_errors = validate_rendered_prompt(source, normalized_shot) if source else []
    if source and not source_errors:
        flattened_official = (
            "导演补充要求" in source
            and any(token in source for token in ("subject definitions", "retention analysis", "integrated multimodal description"))
        )
        return (canonical if flattened_official else source), []

    # A prompt structured for another H3 mode is replaced by the new mode's
    # canonical template. Only genuinely plain-language director notes should
    # be folded into the new official prompt.
    looks_like_official_prompt = source and (
        any(field in source for field in (*FIELD_NAMES, *REF_FIELD_NAMES))
        or source.startswith("For the target video")
        or source.startswith("How the reference pictures align")
    )
    if looks_like_official_prompt:
        source = ""

    # Do not let pasted field labels create duplicate official sections.
    if source:
        cleaned = source
        for field in (*FIELD_NAMES, *REF_FIELD_NAMES):
            cleaned = cleaned.replace(field, field.removesuffix(":").replace("_", " "))
        cleaned = re.sub(r"\[Shot\s+[2-9]\d*\]", "当前镜头", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned:
            marker = "overall_soundscape:"
            addition = f"导演补充要求（不得覆盖上述身份、连续性、对白与声音硬约束）：{cleaned}\n\n"
            canonical = canonical.replace(marker, addition + marker, 1)

    errors = validate_rendered_prompt(canonical, normalized_shot)
    return canonical, errors


def validate_rendered_prompt(prompt: str, shot: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    fields = REF_FIELD_NAMES if shot["generation_mode"] == "ref2va" else FIELD_NAMES
    for field in fields:
        if prompt.count(field) != 1:
            errors.append(f"{shot['shot_id']}: {field} 必须且只能出现一次")
    positions = [prompt.find(field) for field in fields]
    if positions != sorted(positions) or any(position < 0 for position in positions):
        errors.append(f"{shot['shot_id']}: 官方字段顺序错误")
    instruction = "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced."
    if shot["generation_mode"] == "first_frame":
        if not prompt.startswith(instruction + "\n\n"):
            errors.append(f"{shot['shot_id']}: 首帧对齐指令不符合官方固定格式")
    elif shot["generation_mode"] == "first_last_frame":
        fl_instruction = (
            "How the reference pictures align with the target video — "
            "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the "
            "target video; Picture 2 (from Shot 1) aligns with the "
            f"{shot['duration_s']:.2f}-second mark of the target video."
        )
        if not prompt.startswith(fl_instruction + "\n\n"):
            errors.append(f"{shot['shot_id']}: 首尾帧对齐指令不符合官方固定格式")
    elif shot["generation_mode"] == "ref2va":
        if not prompt.startswith("subject_definitions:"):
            errors.append(f"{shot['shot_id']}: Ref2VA Prompt 必须从 subject_definitions 开始")
        for index, _ in enumerate(shot.get("reference_character_ids") or [], start=1):
            if prompt.count(f"<Subject {index}>") < 2:
                errors.append(f"{shot['shot_id']}: <Subject {index}> 未贯穿角色定义和镜头描述")
    elif not prompt.startswith("integrated_multimodal_description:"):
        errors.append(f"{shot['shot_id']}: 无参考图 Prompt 必须直接从三字段开始")
    expected_picture_usages = shot.get("_picture_usages") or []
    for index, usages in enumerate(expected_picture_usages, start=1):
        marker = f"<Picture {index}>:"
        if prompt.count(marker) != 1:
            errors.append(f"{shot['shot_id']}: <Picture {index}> 缺少唯一的人工图片用途声明")
            contract = ""
        else:
            contract = prompt.split(marker, 1)[1].split("\n", 1)[0]
        for usage in usages:
            if f"{usage} -" not in contract:
                errors.append(
                    f"{shot['shot_id']}: <Picture {index}> 未写明人工指定用途 {usage}"
                )
    mentioned_picture_indices = {
        int(value) for value in re.findall(r"<Picture\s+(\d+)>", prompt)
    }
    if expected_picture_usages and any(
        index > len(expected_picture_usages) for index in mentioned_picture_indices
    ):
        errors.append(f"{shot['shot_id']}: Prompt 引用了未提交的 Picture 编号")
    expected_videos = shot.get("_video_bindings") or []
    for index, binding in enumerate(expected_videos, start=1):
        marker = f"<Video {index}>:"
        if prompt.count(marker) != 1:
            errors.append(f"{shot['shot_id']}: <Video {index}> 缺少唯一的参考视频用途声明")
        elif f"{binding.get('usage') or 'motion'} -" not in prompt.split(marker, 1)[1].split("\n", 1)[0]:
            errors.append(f"{shot['shot_id']}: <Video {index}> 未写明人工指定用途")
    expected_audios = shot.get("_audio_bindings") or []
    for index, binding in enumerate(expected_audios, start=1):
        marker = f"<Audio {index}>:"
        if prompt.count(marker) != 1:
            errors.append(f"{shot['shot_id']}: <Audio {index}> 缺少唯一的参考音频用途声明")
        elif f"{binding.get('usage') or 'soundscape'} -" not in prompt.split(marker, 1)[1].split("\n", 1)[0]:
            errors.append(f"{shot['shot_id']}: <Audio {index}> 未写明人工指定用途")
    for kind, expected in (("Video", expected_videos), ("Audio", expected_audios)):
        indices = {int(value) for value in re.findall(fr"<{kind}\s+(\d+)>", prompt)}
        if not expected and indices:
            errors.append(f"{shot['shot_id']}: Prompt 引用了未提交的 {kind} 素材")
        elif expected and any(index > len(expected) for index in indices):
            errors.append(f"{shot['shot_id']}: Prompt 引用了未提交的 {kind} 编号")
    if prompt.count("[Shot 1]") < 1 or re.search(r"\[Shot [2-9]", prompt):
        errors.append(f"{shot['shot_id']}: 单镜头编号不合法")
    for line in shot.get("dialogue") or []:
        if not isinstance(line, dict) or not line.get("text"):
            continue
        block = f'<d>[Chinese] {line["text"]}</d>'
        if prompt.count(block) != 1:
            errors.append(f"{shot['shot_id']}: 计划对白必须原样且只出现一次")
    prompt_speaker_ids: set[str] = set()
    mappings = shot.get("speaker_mappings") or []
    for line in shot.get("dialogue") or []:
        if not isinstance(line, dict) or not line.get("speaker_id"):
            continue
        mapping = next(
            (item for item in mappings if isinstance(item, dict) and item.get("character_id") == line["speaker_id"]),
            None,
        )
        if mapping is None:
            errors.append(f"{shot['shot_id']}: 缺少 {line['speaker_id']} 的 speaker_mappings")
            continue
        prompt_speaker_ids.add(str(mapping.get("prompt_speaker_id") or "S1"))
    for prompt_speaker_id in sorted(prompt_speaker_ids):
        if prompt.count(f"({prompt_speaker_id})") != 1:
            errors.append(
                f"{shot['shot_id']}: 官方 speaker ID ({prompt_speaker_id}) "
                "必须使用 ASCII 圆括号且只出现一次"
            )
    if "overall_soundscape:" in prompt and "non_diegetic_music:" in prompt:
        soundscape = prompt.split("overall_soundscape:", 1)[1].split("non_diegetic_music:", 1)[0]
        if any(isinstance(line, dict) and line.get("text") and line["text"] in soundscape for line in (shot.get("dialogue") or [])):
            errors.append(f"{shot['shot_id']}: overall_soundscape 不得重复对白")
    forbidden = ("voiceover", "off-screen voice", "background score", "non_diegetic_music: false")
    if any(term in prompt.lower() for term in forbidden):
        errors.append(f"{shot['shot_id']}: Prompt 含禁止的人声或配乐表达")
    if not prompt.endswith("non_diegetic_music: N/A"):
        errors.append(f"{shot['shot_id']}: non_diegetic_music 必须为 N/A")
    return errors


def _first_frame_path(
    run_dir: Path, shot: dict[str, Any], images: dict[str, Path]
) -> Path | None:
    if shot["generation_mode"] in {"t2va", "ref2va"}:
        return None
    # Prefer materialized keyframes over the abstract beat-anchor id.
    prepared = shot.get("prepared_first_frame")
    if prepared:
        path = Path(str(prepared))
        if path.is_file():
            return path.resolve()
    if shot.get("source_anchor_image"):
        return images[shot["source_anchor_image"]]
    if shot.get("depends_on"):
        return run_dir / "05_videos" / f'{shot["depends_on"]}.last_frame.png'
    raise ValueError(
        f'{shot["shot_id"]}: 首帧模式缺少 prepared_first_frame / source_anchor_image / depends_on'
    )


def _request(
    shot: dict[str, Any],
    prompt: str,
    first_frame: Path | None,
    last_frame: Path | None,
    references: list[ReferenceBinding],
    target: dict[str, Any],
    seed: int,
) -> dict[str, Any]:
    conditions: list[dict[str, Any]] = []
    if first_frame is not None:
        conditions.append(
            {
                "type": "image",
                "uri": first_frame.resolve().as_uri(),
                "role": "keyframe",
                "frame_index": 0,
            }
        )
    if last_frame is not None:
        conditions.append(
            {
                "type": "image",
                "uri": last_frame.resolve().as_uri(),
                "role": "keyframe",
                "frame_index": -1,
            }
        )
    for index, binding in enumerate(references, start=1):
        conditions.append(
            {
                "type": "image",
                "uri": binding.path.resolve().as_uri(),
                "role": "reference_picture",
                "picture_index": index,
                "character_ids": list(binding.character_ids),
                "reference_roles": list(binding.roles),
            }
        )
    task = {
        "t2va": "t2va",
        "first_frame": "fl2va",
        "first_last_frame": "fl2va",
        "ref2va": "ref2va",
    }[shot["generation_mode"]]
    return {
        "model": "MiniMaxAI/MiniMax-H3",
        "task": task,
        "prompt": prompt,
        "conditions": conditions,
        "target": {
            "short_edge": target["short_edge"], "aspect_ratio": target["aspect_ratio"],
            "duration_seconds": shot["duration_s"],
        },
        "num_outputs_per_prompt": 1, "num_inference_steps": 50,
        "flow_shift": 12.0, "audio_flow_shift": 3.0, "seed": seed, "quality": "lossless",
    }


def render_run_prompts(run_dir: Path, config: ProjectConfig) -> Path:
    run_dir = run_dir.resolve()
    if approval_status(run_dir, "shots") != "approved":
        status = approval_status(run_dir, "shots")
        raise ValueError(
            f"镜头尚未批准，或批准标记已失效（status={status}）。"
            "若刚执行过 prepare-consistency 或修改了 shots.json，请重新 validate/approve。"
        )

    shots_path = run_dir / "03_shots/shots.json"
    story_path = run_dir / "02_story/story.json"
    manifest_path = run_dir / "inputs/manifest.json"
    shots_doc, story, manifest = map(load_json, (shots_path, story_path, manifest_path))
    errors = validate_document("shots", shots_doc, dialogue_overflow_exceptions=_dialogue_exceptions(run_dir))
    errors.extend(validate_shots_against_story(shots_doc, story))
    if errors:
        raise ValueError("镜头数据校验失败：" + "; ".join(sorted(set(errors))))

    target = manifest["target"] | {"short_edge": int(config.data["short_edge"])}
    images = {item["image_id"]: Path(item["output_path"]) for item in manifest["images"]}
    output_dir = run_dir / "04_prompts"
    existing = [path for path in output_dir.iterdir() if path.is_file()]
    if existing:
        raise FileExistsError(f"Prompt 目录已有文件，拒绝覆盖：{output_dir}")

    report: dict[str, Any] = {
        "schema_version": "2.0", "generated_at": utc_now(),
        "official_guide": "https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing",
        "target": {
            "aspect_ratio": target["aspect_ratio"],
            "short_edge": target["short_edge"],
            "width": target["width"],
            "height": target["height"],
            "fps": 24,
        },
        "ready": [], "blocked": [], "shots": [], "static_validation": {"passed": True, "errors": []},
    }
    for index, shot in enumerate(shots_doc["shots"]):
        shot_id = shot["shot_id"]
        references = (
            resolve_shot_references(
                run_dir=run_dir,
                config=config,
                story=story,
                manifest=manifest,
                shot=shot,
            )
            if shot["generation_mode"] == "ref2va"
            else []
        )
        prompt = render_prompt(shot, story, references)
        prompt_errors = validate_rendered_prompt(prompt, shot)
        if prompt_errors:
            report["static_validation"]["passed"] = False
            report["static_validation"]["errors"].extend(prompt_errors)
        first_frame = _first_frame_path(run_dir, shot, images)
        last_frame = (
            Path(shot["source_last_frame"]).resolve()
            if shot.get("source_last_frame")
            else None
        )
        required_paths = [
            path
            for path in (
                [first_frame] if first_frame is not None else []
            )
            + ([last_frame] if last_frame is not None else [])
            + [binding.path for binding in references]
        ]
        missing = [path for path in required_paths if not path.is_file()]
        static_missing = [
            path
            for path in missing
            if path.parent != run_dir / "05_videos"
        ]
        if static_missing:
            raise FileNotFoundError(
                f"{shot_id}: 固定参考资产不存在："
                + ", ".join(str(path) for path in static_missing)
            )
        ready = not missing
        reason = (
            None
            if ready
            else f"等待依赖镜头 {shot['depends_on']} 的末帧："
            + ", ".join(str(path) for path in missing)
        )
        request = _request(
            shot,
            prompt,
            first_frame,
            last_frame,
            references,
            target,
            seed=2101 + index,
        )
        prompt_path = output_dir / f"{shot_id}.txt"
        request_path = output_dir / f"{shot_id}.request.json"
        prompt_path.write_text(prompt + "\n", encoding="utf-8")
        write_json_atomic(request_path, request)
        report["shots"].append({
            "shot_id": shot_id, "generation_mode": shot["generation_mode"],
            "depends_on": shot["depends_on"],
            "condition_path": str(first_frame) if first_frame else None,
            "first_frame_path": str(first_frame) if first_frame else None,
            "last_frame_path": str(last_frame) if last_frame else None,
            "reference_images": [str(binding.path) for binding in references],
            "ref_image_size": (
                (config.data.get("identity_consistency") or {}).get("ref_image_size")
                or "match"
            ),
            "status": "ready" if ready else "blocked_dependency", "reason": reason,
            "prompt": str(prompt_path.relative_to(run_dir)), "request": str(request_path.relative_to(run_dir)),
            "static_validation_passed": not prompt_errors,
        })
        report["ready" if ready else "blocked"].append(shot_id)

    if not report["static_validation"]["passed"]:
        raise ValueError("渲染后 Prompt 静态校验失败：" + "; ".join(report["static_validation"]["errors"]))
    report_path = output_dir / "validation_report.json"
    write_json_atomic(report_path, report)
    state = read_run(run_dir)
    state.update({"state": "PROMPTS_READY", "updated_at": utc_now(), "prompt_report": str(report_path)})
    write_json_atomic(run_dir / "run.json", state)
    return report_path
