"""Short-drama writing craft from github_ready_ai_drama, via the configured Doubao API.

The original scripts loaded Qwen3-VL locally with transformers. SceneFlow already
talks to Volcengine Ark / Doubao, so this module keeps the staged prompts and
writes the same literary artifacts, then folds them back into Studio JSON.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .azure_client import completion_text, create_multimodal_completion, create_text_completion
from .config import ProjectConfig
from .state import utc_now, write_json_atomic


SCENE_RANGES_120S = (
    (1, 2, "0-30秒"),
    (3, 4, "30-60秒"),
    (5, 6, "60-90秒"),
    (7, 8, "90-120秒"),
)


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
        cleaned = cleaned.strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    errors: list[Exception] = []
    for candidate in (cleaned, _largest_json_object(cleaned)):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(exc)
            continue
        if isinstance(value, dict):
            return value
        errors.append(ValueError("模型响应必须是 JSON 对象"))
    raise errors[-1] if errors else ValueError("模型响应里没有 JSON 对象")


def _largest_json_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return ""
    return text[start : end + 1]


def describe_image_as_scene_card(
    config: ProjectConfig,
    *,
    image_path: Path,
    image_id: str,
    index: int,
) -> dict[str, Any]:
    system = (
        "你是影视编剧的视觉取材助手。只提炼后续写剧本真正需要的素材，"
        "不写构图理论，不猜测真实人物身份，不写剧本。只返回 JSON 对象。"
    )
    schema = {
        "image_id": image_id,
        "source_path": str(image_path),
        "visible_facts": ["可直接确认的画面事实"],
        "setting": "场景与时间环境",
        "people": [{"label": "人物标签", "appearance": "外观", "pose_or_action": "动作", "screen_position": "位置"}],
        "objects": ["关键物体"],
        "mood_or_atmosphere": "光线与构图氛围",
        "uncertainties": ["无法从画面确认的信息"],
        "story_affordances": ["可用于后续编剧的视觉元素"],
        "scene_card": {
            "scene": "场景",
            "time": "时间",
            "weather_or_light": "天气/光线",
            "visible_people": "可见人物",
            "usable_actions": ["可直接利用的动作"],
            "plot_clues": ["可以成为剧情线索的可见元素"],
            "do_not_invent": ["不应该凭空增加的关键元素"],
            "story_function": ["开场/发展/转折/高潮/结尾"],
            "one_line_impression": "一句话视觉印象",
        },
    }
    response = create_multimodal_completion(
        config,
        system_prompt=system,
        user_text=(
            f"你现在只看“图片{index}”。不要写长篇视觉分析。控制在500字以内的信息量。\n"
            "严格按下列结构返回 JSON：\n"
            + json.dumps(schema, ensure_ascii=False)
        ),
        image_paths=[image_path],
        max_completion_tokens=2048,
    )
    item = parse_json_object(completion_text(response))
    item["image_id"] = image_id
    item["source_path"] = str(image_path)
    for key in ("visible_facts", "people", "objects", "uncertainties", "story_affordances"):
        if not isinstance(item.get(key), list):
            item[key] = []
    return item


def write_story_outline(
    config: ProjectConfig,
    *,
    descriptions: dict[str, Any],
    proposal: dict[str, Any] | None = None,
    target_duration_s: int = 120,
) -> str:
    cards = []
    for item in descriptions.get("images") or []:
        card = item.get("scene_card") or {}
        cards.append(
            f"# {item.get('image_id')} 场景卡\n"
            f"- 场景：{item.get('setting') or card.get('scene')}\n"
            f"- 可见事实：{'；'.join(item.get('visible_facts') or [])}\n"
            f"- 人物：{json.dumps(item.get('people') or [], ensure_ascii=False)}\n"
            f"- 物体：{'；'.join(item.get('objects') or [])}\n"
            f"- 氛围：{item.get('mood_or_atmosphere')}\n"
            f"- 剧情功能：{card.get('story_function') or item.get('story_affordances')}\n"
        )
    proposal_text = json.dumps(proposal, ensure_ascii=False) if proposal else ""
    response = create_text_completion(
        config,
        system_prompt="你是专业短剧编剧。这里只生成故事骨架，不要写正式剧本，不要写分镜。",
        user_text=(
            f"下面是参考图片提炼出的场景卡。设计一部约{target_duration_s}秒的短剧骨架。\n"
            "必须有真正的故事，至少2个参与剧情的人物。不同图片中的人物明显不同时不要强行合并。\n"
            "每次切换地点必须有明确原因。至少有一次阻碍或误判、一次明确转折。\n"
            "高潮必须是人物主动做出的选择。结尾必须是可见动作或一句真实对白。\n"
            "不允许“命运安排”“突然明白一切”。不允许整部剧只是寻找。\n"
            "剧情必须适合后续 AI 视频生成，动作不要过于复杂。\n"
            + ("用户已采用的剧情提案：\n" + proposal_text + "\n" if proposal_text else "")
            + "【场景卡开始】\n" + "\n".join(cards) + "\n【场景卡结束】\n"
            "严格输出：\n# 短剧标题\n# 类型\n# 一句话梗概\n# 主要角色\n# 故事核心因果链\n"
            "# 八场故事结构\n## 场次01 到 ## 场次08，覆盖 0-120 秒。每一场都必须新增事件或信息。"
        ),
        max_completion_tokens=3200,
    )
    return completion_text(response).strip()


def write_screenplay_chunk(
    config: ProjectConfig,
    *,
    outline: str,
    start_scene: int,
    end_scene: int,
    time_range: str,
    previous_screenplay: str = "",
) -> str:
    continuity = ""
    if previous_screenplay:
        continuity = (
            "【已经完成的前文剧本】\n" + previous_screenplay + "\n【前文结束】\n"
            "必须自然承接前文。不要重写前面的场次。不要让已经发生过的事件重新发生。\n"
        )
    response = create_text_completion(
        config,
        system_prompt="你是专业影视编剧。这次必须写真正的剧本正文，不是提纲。只输出正式剧本。",
        user_text=(
            f"【总故事骨架】\n{outline}\n【故事骨架结束】\n{continuity}\n"
            f"现在只写场次{start_scene:02d} 到 场次{end_scene:02d}，大约 {time_range}。\n"
            "每一场必须明确出现：地点、时间、出场人物、真实环境、人物动作、人物反应、"
            "实际说出的对白、一个推动剧情的场尾事件、转场。\n"
            "不要写“二人进行了交谈”“他十分焦虑”“他继续寻找”。必须把这些写成可拍动作和对白。\n"
            "每场至少 3 句对白、2 个具体动作、1 次反应、1 个新增信息。对白简短口语化，不要长篇独白。\n"
            f"严格输出：\n# 场次{start_scene:02d}\n【时间轴：】\n【地点：】\n【时间：】\n"
            "【参考图：图片X / 无】\n【出场人物：】\n## 场景描述\n## 剧情表演\n## 对白\n"
            "## 场尾事件\n## 转场\n然后同样完整写出下一场。\n"
            "禁止输出分镜、JSON、H3 Prompt、图片分析。"
        ),
        max_completion_tokens=2800,
    )
    return completion_text(response).strip()


def write_full_screenplay(config: ProjectConfig, outline: str) -> str:
    chunks: list[str] = []
    for start, end, time_range in SCENE_RANGES_120S:
        chunk = write_screenplay_chunk(
            config,
            outline=outline,
            start_scene=start,
            end_scene=end,
            time_range=time_range,
            previous_screenplay="\n\n".join(chunks),
        )
        chunks.append(chunk)
    return "\n\n".join(chunks)


def _section(text: str, heading: str) -> str:
    pattern = re.compile(rf"(?im)^#+\s*{re.escape(heading)}\s*$")
    match = pattern.search(text)
    if not match:
        return ""
    rest = text[match.end():]
    nxt = re.search(r"(?m)^#+\s+\S", rest)
    return rest[: nxt.start() if nxt else None].strip()


def story_document_from_outline(outline: str, screenplay: str, image_ids: list[str]) -> dict[str, Any]:
    """Deterministic fallback so a broken JSON reply cannot drop a finished screenplay."""
    title = _section(outline, "短剧标题") or _section(outline, "标题")
    title = re.sub(r"[《》]", "", title.splitlines()[0] if title else "") or "未命名短剧"
    logline = _section(outline, "一句话梗概") or _section(outline, "梗概")
    logline = " ".join(logline.split())[:240]
    causal = _section(outline, "故事核心因果链")
    full_story = causal or logline or screenplay[:800]
    characters = []
    for index, line in enumerate(re.findall(r"(?m)^[-*]\s+\*?\*?([^*：:]+)\*?\*?[：:]?\s*(.*)$", _section(outline, "主要角色")), start=1):
        name, rest = line[0].strip(), line[1].strip()
        if name:
            characters.append({
                "character_id": f"C{index:02d}",
                "name": name,
                "identity": rest[:80] or "角色",
                "appearance": rest,
                "relationships": "",
                "reference_image_ids": image_ids[:1],
                "reference_subject_description": rest,
            })
    if not characters:
        characters = [{
            "character_id": "C01",
            "name": "主角",
            "identity": "待确认",
            "appearance": "",
            "relationships": "",
            "reference_image_ids": image_ids[:1],
            "reference_subject_description": "",
        }]
    beats = []
    scene_iter = re.finditer(r"(?m)^#+\s*场次\s*0*(\d+)[^\n]*\n(.*?)(?=^#+\s*场次|\Z)", outline, re.S)
    for match in scene_iter:
        body = match.group(2).strip()
        beats.append({
            "beat_id": f"B{int(match.group(1)):02d}",
            "duration_s": 15,
            "summary": body.splitlines()[0][:120] if body else f"场次{match.group(1)}",
            "events": [line.strip("- ").strip() for line in body.splitlines() if line.strip()][:4],
            "anchor_image_id": image_ids[0] if image_ids else None,
            "location_id": "L01",
            "dialogue_notes": "",
        })
    if not beats:
        beats = [{
            "beat_id": "B01",
            "duration_s": 120,
            "summary": logline or title,
            "events": [full_story[:80]],
            "anchor_image_id": image_ids[0] if image_ids else None,
            "location_id": "L01",
            "dialogue_notes": "",
        }]
    return {
        "schema_version": "2.0",
        "title": title,
        "logline": logline,
        "full_story": full_story,
        "image_order": image_ids,
        "characters": characters,
        "locations": [{"location_id": "L01", "name": "主场景", "visual_features": "以参考图为准"}],
        "beats": beats,
        "style_bible": "写实电影感，对白短，动作可拍，保持人物外观连续。",
        "outline": outline,
        "screenplay": screenplay,
        "structured_from": "outline_fallback",
    }


def story_document_from_screenplay(
    config: ProjectConfig,
    *,
    descriptions: dict[str, Any],
    outline: str,
    screenplay: str,
    image_ids: list[str],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema_hint = {
        "title": "短剧名",
        "logline": "一句话故事",
        "full_story": "不超过400字的连贯剧情摘要",
        "characters": [{"character_id": "C01", "name": "姓名", "identity": "身份", "appearance": "外观", "relationships": "关系", "reference_image_ids": image_ids[:1], "reference_subject_description": "图中主体"}],
        "locations": [{"location_id": "L01", "name": "地点", "visual_features": "视觉特征"}],
        "beats": [{"beat_id": "B01", "summary": "场次摘要", "events": ["事件"], "location_id": "L01"}],
        "style_bible": "画面与声音规则",
    }
    last_error: Exception | None = None
    document = None
    for attempt in range(2):
        try:
            response = create_text_completion(
                config,
                system_prompt="只返回一个合法 JSON 对象。不要 Markdown，不要剧本原文，不要对白引号 nested 在长文本里。",
                user_text=(
                    "根据故事骨架提取结构化字段。beats 对应场次01-08，每条 summary 不超过40字。\n"
                    "故事骨架：\n" + outline[:3500]
                    + "\n返回：\n" + json.dumps(schema_hint, ensure_ascii=False)
                ),
                max_completion_tokens=2048,
            )
            raw = completion_text(response)
            document = parse_json_object(raw)
            if not document.get("title") or not document.get("beats"):
                raise ValueError("结构化 JSON 缺少 title 或 beats")
            break
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            document = None
    if document is None:
        document = story_document_from_outline(outline, screenplay, image_ids)
        document["structure_error"] = str(last_error) if last_error else ""
    return complete_story_document(
        document,
        outline=outline,
        screenplay=screenplay,
        image_ids=image_ids,
        previous=previous,
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "；".join(_as_text(item) for item in value if _as_text(item))
    if isinstance(value, dict):
        return "；".join(f"{key}：{_as_text(item)}" for key, item in value.items() if _as_text(item))
    return str(value).strip()


def complete_story_document(
    document: dict[str, Any],
    *,
    outline: str = "",
    screenplay: str = "",
    image_ids: list[str] | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fill required story fields so Studio validation cannot reject a finished rewrite."""
    previous = previous or {}
    image_ids = image_ids or list(document.get("image_order") or previous.get("image_order") or [])
    fallback = story_document_from_outline(outline or _as_text(previous.get("outline")), screenplay, image_ids)
    result = dict(document)
    result["title"] = _as_text(result.get("title")) or _as_text(previous.get("title")) or fallback["title"]
    result["logline"] = _as_text(result.get("logline")) or _as_text(previous.get("logline")) or fallback["logline"] or result["title"]
    result["full_story"] = (
        _as_text(result.get("full_story"))
        or _as_text(previous.get("full_story"))
        or fallback["full_story"]
        or result["logline"]
    )
    prev_beats = previous.get("beats") if isinstance(previous.get("beats"), list) else []
    llm_beats = result.get("beats") if isinstance(result.get("beats"), list) else []
    if 3 <= len(llm_beats) <= 30:
        beats = llm_beats
    elif 3 <= len(prev_beats) <= 30:
        beats = prev_beats
    else:
        beats = fallback["beats"]
    result["beats"] = beats
    if not result.get("characters"):
        result["characters"] = previous.get("characters") or fallback["characters"]
    if not result.get("locations"):
        result["locations"] = previous.get("locations") or fallback["locations"]
    result.setdefault("style_bible", previous.get("style_bible") or fallback["style_bible"])
    result.setdefault("schema_version", "2.0")
    result["image_order"] = image_ids or result.get("image_order") or []
    result["outline"] = outline or _as_text(result.get("outline")) or _as_text(previous.get("outline"))
    result["screenplay"] = screenplay or _as_text(result.get("screenplay")) or _as_text(previous.get("screenplay"))
    return result


def revise_screenplay(
    config: ProjectConfig,
    *,
    screenplay: str,
    outline: str,
    descriptions: dict[str, Any],
    instruction: str = "",
) -> str:
    response = create_text_completion(
        config,
        system_prompt="你要重写正式短剧剧本，而不是做图片分析。输出正式剧本正文。",
        user_text=(
            "必须包含：场景、人物动作、对白、声音、场尾事件、转场。\n"
            "把抽象表达改成可拍内容，结合图片视觉锚点，不要完全推翻原剧情。\n"
            "全部使用中文。一定要有对白，不能只有动作。\n"
            + (f"人工修改要求：{instruction}\n" if instruction.strip() else "")
            + "画面描述：\n" + json.dumps(descriptions, ensure_ascii=False)
            + "\n故事骨架：\n" + (outline or "[无]")
            + "\n当前正式剧本：\n" + screenplay
        ),
        max_completion_tokens=8192,
    )
    return completion_text(response).strip()


def save_story_sidecars(run_dir: Path, *, outline: str, screenplay: str) -> None:
    story_dir = run_dir / "02_story"
    story_dir.mkdir(parents=True, exist_ok=True)
    (story_dir / "outline.md").write_text(outline.rstrip() + "\n", encoding="utf-8")
    (story_dir / "screenplay.md").write_text(screenplay.rstrip() + "\n", encoding="utf-8")
    write_json_atomic(
        story_dir / "writing_meta.json",
        {
            "schema_version": "1.0",
            "updated_at": utc_now(),
            "source": "github_ready_ai_drama prompts via configured Doubao/Ark API",
            "outline": "02_story/outline.md",
            "screenplay": "02_story/screenplay.md",
        },
    )


def load_screenplay(run_dir: Path) -> str:
    path = run_dir / "02_story/screenplay.md"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def load_outline(run_dir: Path) -> str:
    path = run_dir / "02_story/outline.md"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""
