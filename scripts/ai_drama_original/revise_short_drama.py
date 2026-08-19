# -*- coding: utf-8 -*-
"""
revise_short_drama.py

Revise an existing short-drama project with Qwen3-VL and its reference images.

The script reads the project artifacts (scene cards, outline, segmented screenplay,
full screenplay, and shot list), analyzes up to three reference images, and rewrites
the screenplay scene by scene.

No server-specific paths, credentials, or GPU IDs are stored in this file.

Examples
--------
Automatic image discovery from project text::

    python revise_short_drama.py ./outputs/drama_run_YYYYMMDD_HHMMSS \
        --model Qwen/Qwen3-VL-8B-Instruct

Use a local model and explicit images::

    python revise_short_drama.py ./outputs/drama_run_YYYYMMDD_HHMMSS \
        --model /path/to/Qwen3-VL-8B-Instruct \
        --local-files-only \
        --images image1.jpg image2.jpg image3.jpg

GPU selection should be configured outside the script, for example::

    CUDA_VISIBLE_DEVICES=0 python revise_short_drama.py ./outputs/drama_run_xxx
"""

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import torch
from transformers import AutoModelForImageTextToText, AutoProcessor


# ============================================================
# 1. 配置
# ============================================================

DEFAULT_MODEL = os.getenv("QWEN3VL_MODEL", "Qwen/Qwen3-VL-8B-Instruct")

PREFERRED_ORDER = [
    "00_scene_cards.txt",
    "01_story_outline.txt",
    "02_screenplay_scene_01_02.txt",
    "03_screenplay_scene_03_04.txt",
    "04_screenplay_scene_05_06.txt",
    "05_screenplay_scene_07_08.txt",
    "06_full_screenplay.txt",
    "07_shotlist_01_10.txt",
    "08_shotlist_11_20.txt",
    "09_full_shotlist.txt",
]

MAIN_SCREENPLAY_NAME = "06_full_screenplay.txt"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

SCENE_HEADING_PATTERN = re.compile(
    r"(?m)^#{1,3}\s*"
    r"("
    r"(?:场次\s*0*(\d+)(?:[^\n]*))"
    r"|"
    r"(?:第([一二三四五六七八九十\d]+)场[：:]?[^\n]*)"
    r")"
    r"\s*$"
)

CHINESE_NUM = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


# ============================================================
# 2. 基础函数
# ============================================================

def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore").strip()


def save_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    print(f"已保存：{path}")


def collect_project_files(project_dir: Path) -> List[Path]:
    files = [
        p for p in project_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".txt", ".md", ".json"}
    ]

    name_map = {p.name: p for p in files}
    ordered = []

    for name in PREFERRED_ORDER:
        if name in name_map:
            ordered.append(name_map[name])

    used = {p.resolve() for p in ordered}

    for p in sorted(files, key=lambda x: x.name.lower()):
        if p.resolve() not in used:
            ordered.append(p)

    return ordered


def build_context_map(files: List[Path]) -> Dict[str, str]:
    return {p.name: read_text(p) for p in files}


def chinese_to_int(value: str) -> Optional[int]:
    value = value.strip()

    if value.isdigit():
        return int(value)

    if value in CHINESE_NUM:
        return CHINESE_NUM[value]

    if len(value) == 2 and value.startswith("十"):
        return 10 + CHINESE_NUM.get(value[1], 0)

    return None


def parse_scene_number(match: re.Match) -> Optional[int]:
    if match.group(2):
        return int(match.group(2))
    if match.group(3):
        return chinese_to_int(match.group(3))
    return None


def split_scenes(screenplay: str) -> List[Dict]:
    matches = list(SCENE_HEADING_PATTERN.finditer(screenplay))
    scenes = []

    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(screenplay)

        scenes.append({
            "scene_no": parse_scene_number(match),
            "heading": match.group(1).strip(),
            "body": screenplay[start:end].strip(),
        })

    return scenes


def extract_header(screenplay: str) -> str:
    match = SCENE_HEADING_PATTERN.search(screenplay)
    if not match:
        return screenplay.strip()
    return screenplay[:match.start()].strip()


def find_main_screenplay(project_dir: Path, files: List[Path]) -> Path:
    preferred = project_dir / MAIN_SCREENPLAY_NAME
    if preferred.exists():
        return preferred

    scored = []

    for p in files:
        name = p.name.lower()
        score = 0
        if "screenplay" in name:
            score += 5
        if "剧本" in name:
            score += 5
        if "full" in name:
            score += 2
        if "shotlist" in name:
            score -= 5
        if "outline" in name:
            score -= 4
        if score > 0:
            scored.append((score, p))

    if not scored:
        raise RuntimeError("没有找到主剧本，请确认目录中存在 06_full_screenplay.txt 或类似文件。")

    scored.sort(key=lambda x: (-x[0], x[1].name))
    return scored[0][1]


def parse_outline_scenes(outline_text: str) -> Dict[int, str]:
    result = {}
    pattern = re.compile(r"(?m)^#{1,3}\s*场次\s*0*(\d+)[^\n]*$")
    matches = list(pattern.finditer(outline_text))

    for i, m in enumerate(matches):
        scene_no = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(outline_text)
        result[scene_no] = outline_text[start:end].strip()

    return result


def parse_scene_cards(scene_cards_text: str) -> Dict[int, str]:
    """
    宽松解析 scene cards，如果抓不到就返回空字典。
    """
    result = {}
    pattern = re.compile(r"(?m)^#{1,3}\s*场景\s*0*(\d+)[^\n]*$|^#{1,3}\s*场次\s*0*(\d+)[^\n]*$")
    matches = list(pattern.finditer(scene_cards_text))

    for i, m in enumerate(matches):
        scene_no = int(m.group(1) or m.group(2))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(scene_cards_text)
        result[scene_no] = scene_cards_text[start:end].strip()

    return result


def collect_segmented_screenplay_scenes(project_dir: Path) -> Dict[int, str]:
    result = {}

    for path in sorted(project_dir.glob("*screenplay_scene_*.txt")):
        text = read_text(path)
        for scene in split_scenes(text):
            scene_no = scene["scene_no"]
            if scene_no is not None and scene_no not in result:
                result[scene_no] = scene["body"]

    return result


# ============================================================
# 3. 图片路径提取
# ============================================================

def extract_image_paths_from_text(text: str) -> List[Path]:
    pattern = re.compile(
        r'(/data/[^\s\'"]+\.(?:jpg|jpeg|png|webp|bmp))',
        flags=re.IGNORECASE
    )
    found = []

    for match in pattern.findall(text):
        p = Path(match)
        if p.exists() and p.suffix.lower() in IMAGE_EXTS:
            found.append(p)

    unique = []
    seen = set()

    for p in found:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(p)

    return unique


def auto_collect_images(project_dir: Path, file_texts: Dict[str, str]) -> List[Path]:
    found = []

    for _, text in file_texts.items():
        found.extend(extract_image_paths_from_text(text))

    # 也可顺带检查项目目录下是否有图片
    for ext in IMAGE_EXTS:
        found.extend(project_dir.glob(f"*{ext}"))

    unique = []
    seen = set()

    for p in found:
        if not p.exists():
            continue
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(p)

    return unique


# ============================================================
# 4. Qwen3-VL 推理
# ============================================================

def resolve_dtype(name: str):
    if name == "auto":
        return "auto"
    return {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }[name]


def load_qwen3vl(
    model_path: str,
    local_files_only: bool = False,
    gpu_memory: str = "20GiB",
    cpu_memory: str = "200GiB",
    dtype: str = "bfloat16",
):
    print("=" * 100)
    print("Loading processor...")
    processor = AutoProcessor.from_pretrained(
        model_path,
        local_files_only=local_files_only,
    )

    max_memory = {"cpu": cpu_memory}
    if torch.cuda.is_available():
        max_memory[0] = gpu_memory

    print("Loading Qwen3-VL...")
    model = AutoModelForImageTextToText.from_pretrained(
        model_path,
        dtype=resolve_dtype(dtype),
        device_map="auto",
        max_memory=max_memory,
        local_files_only=local_files_only,
    )
    model.eval()
    print("Model loaded.")
    print("=" * 100)
    return processor, model


def qwen_chat(
    processor,
    model,
    messages,
    max_new_tokens: int = 1024,
    do_sample: bool = False,
):
    inputs = processor.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
    )

    inputs = inputs.to(model.device)

    with torch.inference_mode():
        generated_ids = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=do_sample,
        )

    generated_ids_trimmed = [
        output_ids[len(input_ids):]
        for input_ids, output_ids in zip(inputs.input_ids, generated_ids)
    ]

    result = processor.batch_decode(
        generated_ids_trimmed,
        skip_special_tokens=True,
        clean_up_tokenization_spaces=False,
    )[0]

    return result.strip()


def analyze_single_image(processor, model, image_path: Path) -> str:
    prompt = """
请你作为短剧前期策划的视觉分析师，严格使用中文，分析这张图片。

目标：
为后续短剧剧本重写服务，因此请不要泛泛而谈，而要尽量提取能够用于
“人物、场景、动作、氛围、镜头、道具、情绪、剧情触发”的信息。

请按以下结构输出：

1. 场景与环境
- 地点环境
- 室内/室外
- 时间
- 天气
- 空间特征
- 背景元素

2. 人物
- 可见人物数量
- 每个可见人物的大致外貌、服装、年龄感、姿态、动作、表情
- 人物之间的互动关系（只能做谨慎判断）

3. 重要物体与道具
- 关键物体
- 物体状态
- 与人物的关系
- 哪些适合作为剧情道具

4. 画面风格
- 光线
- 色彩
- 氛围
- 景别/构图
- 是否有电影感

5. 可用于剧本的视觉线索
- 可以发展成剧情的细节
- 适合出现的动作
- 适合承接的情绪
- 适合的场景功能（开场/推进/冲突/转折/高潮/结尾）

6. 结论
- 用 5-8 条简洁 bullet，总结这张图最适合支持什么样的戏

要求：
- 全部用中文输出
- 不要虚构人名
- 不要把推测写成事实
- 以“后续写剧本可直接利用”为导向
""".strip()

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "path": str(image_path),
                    "resized_height": 384,
                    "resized_width": 384,
                },
                {
                    "type": "text",
                    "text": prompt,
                },
            ],
        }
    ]

    result = qwen_chat(
        processor=processor,
        model=model,
        messages=messages,
        max_new_tokens=1200,
        do_sample=False,
    )
    return result


def rewrite_single_scene(
    processor,
    model,
    scene_no: Optional[int],
    scene_heading: str,
    source_scene_text: str,
    image_analyses_text: str,
    outline_text: str,
    scene_card_text: str,
    segmented_text: str,
    shotlist_context: str,
    story_goal_hint: str,
) -> str:
    """
    核心：把某一场重写成“正式剧本”，而不是视觉分析。
    """

    prompt = f"""
你现在要做的是：重写正式短剧剧本，而不是做图片分析。

你将获得：
1. 参考图片的视觉分析
2. 当前场次的原始剧本文本
3. 本场可能相关的 outline / scene card / 分段剧本 / shotlist 信息

请你基于这些材料，输出“正式剧本场次”。

你的任务目标：
- 让输出真正像剧本，而不是分析报告
- 必须包含：场景、人物动作、对白、声音、场尾事件、转场
- 必须加入更具体的戏剧内容
- 必须把抽象表达改成可拍的内容
- 必须结合图片带来的具体视觉锚点
- 必须避免空话、套话、泛泛分析
- 允许你为了戏剧完整性做有限创作，但要保持与图片视觉一致
- 优先使用已有故事方向，不要完全推翻原剧情

请严格按以下格式输出，不要输出额外解释：

# {scene_heading}

【时间轴：用一句话标明本场在全片中的位置】
【地点：明确可拍地点】
【时间：白天 / 夜晚 / 黄昏等】
【出场人物：列出角色名】

## 场景描述
用2-5段描述镜头一开始看到的环境、人物状态、关键视觉元素、气氛。

## 剧情表演
按戏剧推进顺序，写清楚人物做了什么，发生了什么，冲突如何推进。
这一部分要写成“可拍摄”的动作与事件。

## 对白
必须写出人物对白。
格式示例：
林远（压低声音）：
“我刚才明明把手机放在这儿了。”
对白至少 6 轮，如果是重要场次可以更多。

## 环境声音
列出本场听得到的环境声、动作声、氛围声。

## 本场新增信息
写出本场给观众新增了什么信息，推进了什么剧情。

## 场尾事件
明确本场最后发生了什么，推动下一场。

## 转场
写一句明确转场方式，例如：
CUT TO：林远穿过斑马线，朝商场广场快步走去。

硬性要求：
1. 全部使用中文。
2. 不要输出“可见事实/合理推断/剧情潜力”这种分析型结构。
3. 不要写成视觉分析报告。
4. 一定要有对白，不能只有动作。
5. 一定要有场景，不能只写抽象概念。
6. 一定要有本场事件推进，不能只是人物张望或找东西。
7. 如果原文太空泛，请你主动补成“能拍的具体内容”。
8. 必须结合图片中的光线、环境、人物状态、道具等细节。
9. 如果图片与原剧情冲突，优先做合理融合，而不是忽视图片。
10. 输出的就是正式剧本，不要再解释你的思路。

全局故事方向参考：
{story_goal_hint}

================ 参考图片视觉分析 ================
{image_analyses_text}

================ 当前场次原始文本 ================
{source_scene_text}

================ 相关 Outline ================
{outline_text or "[无]"}

================ 相关 Scene Card ================
{scene_card_text or "[无]"}

================ 相关分段剧本 ================
{segmented_text or "[无]"}

================ 相关 Shotlist ================
{shotlist_context or "[无]"}
""".strip()

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": prompt,
                }
            ],
        }
    ]

    result = qwen_chat(
        processor=processor,
        model=model,
        messages=messages,
        max_new_tokens=1800,
        do_sample=False,
    )
    return result


# ============================================================
# 5. 从 Shotlist 提取对应场次上下文
# ============================================================

def extract_scene_shotlist_context(shotlist_text: str, scene_no: Optional[int]) -> str:
    if scene_no is None or not shotlist_text:
        return ""

    lines = shotlist_text.splitlines()
    blocks = []
    current = []

    for line in lines:
        if line.strip().startswith("### Clip "):
            if current:
                blocks.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)

    if current:
        blocks.append("\n".join(current).strip())

    related = []

    for block in blocks:
        # 宽松匹配：场次 01 / scene 1 / 剧情阶段里提到场次1
        if re.search(rf"(场次|scene|Scene)\s*0*{scene_no}\b", block):
            related.append(block)
            continue

        if re.search(rf"图片{scene_no}\b", block):
            related.append(block)

    return "\n\n".join(related[:4])


def build_story_goal_hint(outline_text: str, screenplay_header: str) -> str:
    parts = []

    if screenplay_header:
        parts.append(screenplay_header[:1200])

    if outline_text:
        parts.append(outline_text[:2500])

    hint = "\n\n".join([p for p in parts if p.strip()])

    if not hint:
        hint = "请保持原项目的故事主线，并把抽象设定改写成可拍的戏。"

    return hint


# ============================================================
# 6. 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="调用 Qwen3-VL，结合图片与项目文本，重写正式短剧剧本。"
    )
    parser.add_argument(
        "project_dir",
        type=str,
        help="drama_run_xxx 项目目录",
    )
    parser.add_argument(
        "--images",
        nargs="*",
        default=None,
        help="手动指定图片路径；如果不传，则自动从项目文本中提取。",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=str,
        default=None,
        help="输出目录；默认 project_dir/revision_qwen3vl_时间戳",
    )


    parser.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL,
        help="Local model path or Hugging Face model ID.",
    )
    parser.add_argument(
        "--local-files-only",
        action="store_true",
        help="Only load model files already available locally.",
    )
    parser.add_argument(
        "--gpu-memory",
        type=str,
        default=os.getenv("QWEN3VL_GPU_MEMORY", "20GiB"),
        help="Maximum memory budget for logical CUDA device 0.",
    )
    parser.add_argument(
        "--cpu-memory",
        type=str,
        default=os.getenv("QWEN3VL_CPU_MEMORY", "200GiB"),
        help="Maximum CPU offload memory budget.",
    )
    parser.add_argument(
        "--dtype",
        choices=["auto", "bfloat16", "float16", "float32"],
        default=os.getenv("QWEN3VL_DTYPE", "bfloat16"),
    )

    args = parser.parse_args()

    project_dir = Path(args.project_dir).expanduser().resolve()
    if not project_dir.exists():
        raise FileNotFoundError(f"项目目录不存在：{project_dir}")
    if not project_dir.is_dir():
        raise NotADirectoryError(f"输入不是目录：{project_dir}")

    files = collect_project_files(project_dir)
    if not files:
        raise RuntimeError("目录中没有找到 txt / md / json 文件。")

    file_texts = build_context_map(files)

    # 图片
    if args.images:
        image_paths = [Path(x).expanduser().resolve() for x in args.images]
    else:
        image_paths = auto_collect_images(project_dir, file_texts)

    image_paths = [p for p in image_paths if p.exists() and p.suffix.lower() in IMAGE_EXTS]

    if not image_paths:
        raise RuntimeError("没有找到可用图片。请用 --images 手动指定。")

    # 控制数量，避免上下文太长
    image_paths = image_paths[:3]

    print("=" * 100)
    print("本次使用的图片：")
    for i, p in enumerate(image_paths, 1):
        print(f"图片{i}: {p}")
    print("=" * 100)

    # 主剧本
    main_screenplay_file = find_main_screenplay(project_dir, files)
    main_screenplay_text = read_text(main_screenplay_file)
    scenes = split_scenes(main_screenplay_text)

    if not scenes:
        raise RuntimeError("主剧本中没有识别到场次。")

    header_text = extract_header(main_screenplay_text)

    # 其他文本
    outline_text = file_texts.get("01_story_outline.txt", "")
    scene_cards_text = file_texts.get("00_scene_cards.txt", "")
    shotlist_text = file_texts.get("09_full_shotlist.txt", "")
    if not shotlist_text:
        shotlist_text = "\n\n".join(
            [
                file_texts.get("07_shotlist_01_10.txt", ""),
                file_texts.get("08_shotlist_11_20.txt", ""),
            ]
        )

    outline_scenes = parse_outline_scenes(outline_text) if outline_text else {}
    scene_cards_scenes = parse_scene_cards(scene_cards_text) if scene_cards_text else {}
    segmented_scenes = collect_segmented_screenplay_scenes(project_dir)

    story_goal_hint = build_story_goal_hint(outline_text, header_text)

    # 输出目录
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else project_dir / f"revision_qwen3vl_{timestamp}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    # 加载模型
    processor, model = load_qwen3vl(
        args.model,
        local_files_only=args.local_files_only,
        gpu_memory=args.gpu_memory,
        cpu_memory=args.cpu_memory,
        dtype=args.dtype,
    )

    # Step 1: 逐图分析
    image_analysis_blocks = []
    for i, image_path in enumerate(image_paths, 1):
        print("=" * 100)
        print(f"正在分析图片{i}: {image_path}")
        analysis = analyze_single_image(processor, model, image_path)
        block = f"# 图片{i}\n路径：{image_path}\n\n{analysis}"
        image_analysis_blocks.append(block)

    image_analyses_text = "\n\n".join(image_analysis_blocks)
    save_text(output_dir / "00_image_analysis.txt", image_analyses_text)

    # Step 2: 逐场重写
    revised_scene_paths = []
    revised_scene_texts = []

    for idx, scene in enumerate(scenes, 1):
        scene_no = scene["scene_no"]
        scene_heading = scene["heading"]
        source_scene_text = scene["body"]

        outline_scene_text = outline_scenes.get(scene_no, "") if scene_no is not None else ""
        scene_card_text = scene_cards_scenes.get(scene_no, "") if scene_no is not None else ""
        segmented_scene_text = segmented_scenes.get(scene_no, "") if scene_no is not None else ""
        shotlist_context = extract_scene_shotlist_context(shotlist_text, scene_no)

        print("=" * 100)
        print(f"正在重写场次 {scene_heading}")

        revised_scene = rewrite_single_scene(
            processor=processor,
            model=model,
            scene_no=scene_no,
            scene_heading=scene_heading,
            source_scene_text=source_scene_text,
            image_analyses_text=image_analyses_text,
            outline_text=outline_scene_text,
            scene_card_text=scene_card_text,
            segmented_text=segmented_scene_text,
            shotlist_context=shotlist_context,
            story_goal_hint=story_goal_hint,
        )

        filename = f"01_revised_scene_{idx:02d}.txt"
        scene_path = output_dir / filename
        save_text(scene_path, revised_scene)

        revised_scene_paths.append(scene_path)
        revised_scene_texts.append(revised_scene)

    # Step 3: 合并完整剧本
    full_screenplay_parts = []
    if header_text:
        full_screenplay_parts.append(header_text)

    full_screenplay_parts.extend(revised_scene_texts)
    revised_full_screenplay = "\n\n".join(full_screenplay_parts).strip()

    save_text(output_dir / "02_revised_full_screenplay.txt", revised_full_screenplay)

    # Step 4: 输出总结
    summary_lines = []
    summary_lines.append("# Qwen3-VL 剧本重写总结")
    summary_lines.append("")
    summary_lines.append(f"- 项目目录：{project_dir}")
    summary_lines.append(f"- 主剧本：{main_screenplay_file.name}")
    summary_lines.append(f"- 参考图片数：{len(image_paths)}")
    summary_lines.append(f"- 场次数：{len(scenes)}")
    summary_lines.append("")
    summary_lines.append("## 使用图片")
    summary_lines.append("")
    for i, p in enumerate(image_paths, 1):
        summary_lines.append(f"- 图片{i}: {p}")

    summary_lines.append("")
    summary_lines.append("## 输出文件")
    summary_lines.append("")
    summary_lines.append(f"- 图片分析：{output_dir / '00_image_analysis.txt'}")
    for p in revised_scene_paths:
        summary_lines.append(f"- 分场剧本：{p}")
    summary_lines.append(f"- 完整剧本：{output_dir / '02_revised_full_screenplay.txt'}")

    save_text(output_dir / "03_revision_summary.txt", "\n".join(summary_lines))

    metadata = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "qwen3vl_with_images",
        "project_dir": str(project_dir),
        "main_screenplay": str(main_screenplay_file),
        "images": [str(p) for p in image_paths],
        "scene_count": len(scenes),
        "outputs": {
            "image_analysis": str(output_dir / "00_image_analysis.txt"),
            "revised_full_screenplay": str(output_dir / "02_revised_full_screenplay.txt"),
            "summary": str(output_dir / "03_revision_summary.txt"),
        },
    }

    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n" + "=" * 100)
    print("完成：已调用 Qwen3-VL，结合图片重写剧本。")
    print(f"输出目录：{output_dir}")
    print(f"完整剧本：{output_dir / '02_revised_full_screenplay.txt'}")
    print("=" * 100)


if __name__ == "__main__":
    main()