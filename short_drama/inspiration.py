"""Human inspiration intake and LLM-assisted story ideation for Studio stage I."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .azure_client import completion_text, create_text_completion
from .config import ProjectConfig
from .human_orchestration import resolve_asset_path
from .state import utc_now, write_json_atomic
from .validators import load_json


SYSTEM_PROMPT = """你是一名擅长中文竖屏与横屏短剧的资深编剧策划。
你的任务不是直接写分镜，而是把用户的起点整理成可继续开发的故事提案。
提案必须可拍、人物动机明确、前段有钩子、中段有升级、结尾有回收；不要依赖旁白解释剧情。
只返回 JSON 对象，不要 Markdown 或额外说明。"""


def inspiration_path(run_dir: Path) -> Path:
    return run_dir / "inputs/inspiration.json"


def load_inspiration(run_dir: Path) -> dict[str, Any]:
    path = inspiration_path(run_dir)
    if path.is_file():
        return load_json(path)
    return {
        "schema_version": "1.0",
        "updated_at": None,
        "active_source": None,
        "selected_images": [],
        "current_proposal": None,
        "history": [],
    }


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    value = json.loads(cleaned.strip())
    if not isinstance(value, dict):
        raise ValueError("LLM 灵感提案必须是 JSON 对象")
    required = ("title", "logline", "genre", "tone", "hook", "story_outline", "ending")
    missing = [key for key in required if not str(value.get(key) or "").strip()]
    if missing:
        raise ValueError("LLM 灵感提案缺少字段：" + ", ".join(missing))
    return value


def _proposal_markdown(proposal: dict[str, Any]) -> str:
    def render(value: Any) -> str:
        if isinstance(value, list):
            return "、".join(render(item) for item in value)
        if isinstance(value, dict):
            return "；".join(f"{key}：{render(item)}" for key, item in value.items())
        return str(value or "")

    return (
        f"# {render(proposal.get('title'))}\n\n"
        f"> {render(proposal.get('logline'))}\n\n"
        f"- 类型：{render(proposal.get('genre'))}\n"
        f"- 气质：{render(proposal.get('tone'))}\n\n"
        f"## 开场钩子\n\n{render(proposal.get('hook'))}\n\n"
        f"## 人物\n\n{render(proposal.get('characters'))}\n\n"
        f"## 故事提案\n\n{render(proposal.get('story_outline'))}\n\n"
        f"## 结尾\n\n{render(proposal.get('ending'))}\n\n"
        f"## 视觉意象\n\n{render(proposal.get('visual_motifs'))}\n"
    )


def select_inspiration_images(
    run_dir: Path,
    config: ProjectConfig,
    values: list[Any],
) -> dict[str, Any]:
    paths = [
        resolve_asset_path(value, run_dir=run_dir, config=config, required=True)
        for value in values
    ]
    paths = list(dict.fromkeys(path for path in paths if path is not None))
    if not paths:
        raise ValueError("请至少选择 1 张灵感图片")
    document = load_inspiration(run_dir)
    document["active_source"] = "images"
    document["selected_images"] = [str(path) for path in paths]
    document["updated_at"] = utc_now()
    write_json_atomic(inspiration_path(run_dir), document)
    return document


def generate_story_inspiration(
    run_dir: Path,
    config: ProjectConfig,
    *,
    mode: str,
    idea_text: str = "",
    genre: str = "现实情感",
    tone: str = "电影感",
) -> dict[str, Any]:
    if mode not in {"from_scratch", "polish"}:
        raise ValueError(f"未知灵感模式：{mode}")
    idea_text = idea_text.strip()
    if mode == "polish" and not idea_text:
        raise ValueError("请先填写你的剧情灵感")
    if len(idea_text) > 8000:
        raise ValueError("灵感文本不能超过 8000 字符")
    schema_hint = {
        "title": "短剧名",
        "logline": "一句话故事",
        "genre": "类型",
        "tone": "整体气质",
        "hook": "开场钩子",
        "characters": [{"name": "人物名", "role": "身份", "desire": "目标", "conflict": "阻力"}],
        "story_outline": "包含开端、升级、高潮的完整剧情提案，600-1000字",
        "ending": "结尾与情绪回收",
        "visual_motifs": ["可反复出现的视觉意象"],
    }
    if mode == "from_scratch":
        task = (
            "用户目前没有素材，也没有明确灵感。请从零提出一部约 120 秒、适合继续拆分成 4–8 秒镜头的中文短剧。"
            f"偏好类型：{genre}；画面气质：{tone}。避免陈词滥调，给出一个可视觉化的核心道具或意象。"
        )
    else:
        task = (
            "用户已经有故事灵感。必须保留其核心人物、主题和关键事件，不得擅自换题；请补足因果链、冲突升级、"
            "开场钩子和结尾回收，使其成为约 120 秒、可继续分镜的短剧提案。"
            f"偏好类型：{genre}；画面气质：{tone}。\n用户原始灵感：\n{idea_text}\n"
        )
    user_text = task + "\n严格按以下字段返回 JSON：\n" + json.dumps(schema_hint, ensure_ascii=False)
    started = time.monotonic()
    started_at = utc_now()
    response = create_text_completion(
        config,
        system_prompt=SYSTEM_PROMPT,
        user_text=user_text,
        max_completion_tokens=4096,
    )
    content = completion_text(response)
    proposal = _parse_json_object(content)
    item = {
        "proposal_id": uuid.uuid4().hex,
        "mode": mode,
        "idea_text": idea_text,
        "preferences": {"genre": genre, "tone": tone},
        "proposal": proposal,
        "created_at": utc_now(),
        "started_at": started_at,
        "elapsed_s": round(time.monotonic() - started, 3),
        "response_id": getattr(response, "id", None),
    }
    drafts_dir = run_dir / "02_story/studio_drafts"
    draft_name = f"{started_at.replace(':', '').replace('+', '-')}-{item['proposal_id'][:8]}"
    json_path = drafts_dir / f"{draft_name}.json"
    markdown_path = drafts_dir / f"{draft_name}.md"
    item["local_files"] = {
        "json": str(json_path.relative_to(run_dir)),
        "markdown": str(markdown_path.relative_to(run_dir)),
        "latest_json": "02_story/studio_drafts/latest.json",
        "latest_markdown": "02_story/studio_drafts/latest.md",
    }
    write_json_atomic(json_path, item)
    markdown = _proposal_markdown(proposal)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(markdown, encoding="utf-8")
    write_json_atomic(drafts_dir / "latest.json", item)
    (drafts_dir / "latest.md").write_text(markdown, encoding="utf-8")
    document = load_inspiration(run_dir)
    document["active_source"] = mode
    document["current_proposal"] = item
    document["history"] = [item, *(document.get("history") or [])][:20]
    document["updated_at"] = utc_now()
    write_json_atomic(inspiration_path(run_dir), document)
    return document
