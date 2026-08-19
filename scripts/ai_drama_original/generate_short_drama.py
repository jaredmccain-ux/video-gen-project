# -*- coding: utf-8 -*-
"""
generate_short_drama.py

Generate a structured short-drama screenplay from 1-3 reference images with Qwen3-VL.

The script runs a staged pipeline:
    images -> scene cards -> story outline -> screenplay -> shot list

No machine-specific paths are hard-coded. Configure the model, output directory,
and memory limits with command-line arguments or environment variables.

Examples
--------
Use a Hugging Face model ID::

    python generate_short_drama.py image1.jpg image2.jpg \
        --model Qwen/Qwen3-VL-8B-Instruct

Use a locally downloaded model::

    python generate_short_drama.py image1.jpg image2.jpg image3.jpg \
        --model /path/to/Qwen3-VL-8B-Instruct \
        --local-files-only \
        --output-dir ./outputs

GPU selection should be configured outside the script, for example::

    CUDA_VISIBLE_DEVICES=0 python generate_short_drama.py image1.jpg
"""

import argparse
import gc
import os
from datetime import datetime
from pathlib import Path

import torch
from transformers import AutoModelForImageTextToText, AutoProcessor, TextStreamer


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a structured AI short drama from 1-3 reference images."
    )
    parser.add_argument(
        "images",
        nargs="+",
        help="One to three reference image paths.",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("QWEN3VL_MODEL", "Qwen/Qwen3-VL-8B-Instruct"),
        help="Local model path or Hugging Face model ID. "
             "Default: env QWEN3VL_MODEL or Qwen/Qwen3-VL-8B-Instruct.",
    )
    parser.add_argument(
        "--output-dir",
        default=os.getenv("AI_DRAMA_OUTPUT_DIR", "./outputs"),
        help="Directory in which run folders are created. Default: ./outputs",
    )
    parser.add_argument(
        "--local-files-only",
        action="store_true",
        help="Only load model files already available locally.",
    )
    parser.add_argument("--image-size", type=int, default=384)
    parser.add_argument("--target-duration", type=int, default=150)
    parser.add_argument("--target-scenes", type=int, default=8)
    parser.add_argument("--target-clips", type=int, default=20)
    parser.add_argument(
        "--gpu-memory",
        default=os.getenv("QWEN3VL_GPU_MEMORY", "18GiB"),
        help="Maximum memory budget for logical CUDA device 0.",
    )
    parser.add_argument(
        "--cpu-memory",
        default=os.getenv("QWEN3VL_CPU_MEMORY", "200GiB"),
        help="Maximum CPU offload memory budget.",
    )
    parser.add_argument(
        "--dtype",
        choices=["auto", "bfloat16", "float16", "float32"],
        default=os.getenv("QWEN3VL_DTYPE", "bfloat16"),
    )
    return parser.parse_args()


def resolve_dtype(name):
    if name == "auto":
        return "auto"
    return {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }[name]


ARGS = parse_args()

MODEL_PATH = ARGS.model
OUTPUT_ROOT = Path(ARGS.output_dir).expanduser().resolve()
IMAGE_HEIGHT = ARGS.image_size
IMAGE_WIDTH = ARGS.image_size
TARGET_DURATION = ARGS.target_duration
TARGET_SCENES = ARGS.target_scenes
TARGET_CLIPS = ARGS.target_clips

SCENE_CARD_TOKENS = 700
OUTLINE_TOKENS = 1600
SCENE_CHUNK_TOKENS = 2200
SHOT_CHUNK_TOKENS = 2200

OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

selected_images = [Path(p).expanduser().resolve() for p in ARGS.images]

if len(selected_images) > 3:
    raise RuntimeError(
        f"At most 3 reference images are supported; received {len(selected_images)}."
    )

for p in selected_images:
    if not p.exists():
        raise FileNotFoundError(f"Image not found: {p}")

NUM_IMAGES = len(selected_images)

print("=" * 100)
print(f"Using {NUM_IMAGES} reference image(s):")
for i, p in enumerate(selected_images, 1):
    print(f"Image {i}: {p}")
print("=" * 100)

print("Loading processor...")
processor = AutoProcessor.from_pretrained(
    MODEL_PATH,
    local_files_only=ARGS.local_files_only,
)

max_memory = {"cpu": ARGS.cpu_memory}
if torch.cuda.is_available():
    max_memory[0] = ARGS.gpu_memory

print("Loading Qwen3-VL...")
model = AutoModelForImageTextToText.from_pretrained(
    MODEL_PATH,
    dtype=resolve_dtype(ARGS.dtype),
    device_map="auto",
    max_memory=max_memory,
    local_files_only=ARGS.local_files_only,
)

model.eval()
print("Model loaded.")


# ============================================================
# 4. 通用函数
# ============================================================

def cleanup():
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def generate(messages, max_new_tokens, title):
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)

    inputs = processor.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
    )

    inputs = inputs.to(model.device)

    streamer = TextStreamer(
        processor.tokenizer,
        skip_prompt=True,
        skip_special_tokens=True,
    )

    with torch.inference_mode():
        output_ids = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            streamer=streamer,
        )

    trimmed = [
        out[len(inp):]
        for inp, out in zip(inputs.input_ids, output_ids)
    ]

    text = processor.batch_decode(
        trimmed,
        skip_special_tokens=True,
        clean_up_tokenization_spaces=False,
    )[0].strip()

    del inputs, output_ids, trimmed
    cleanup()
    return text


def image_message(image_path: Path, prompt: str):
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "path": str(image_path),
                    "resized_height": IMAGE_HEIGHT,
                    "resized_width": IMAGE_WIDTH,
                },
                {
                    "type": "text",
                    "text": prompt,
                },
            ],
        }
    ]


def text_message(prompt: str):
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt}
            ],
        }
    ]


def save_text(path: Path, body: str):
    path.write_text(body.strip() + "\n", encoding="utf-8")
    print(f"已保存：{path}")


timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
RUN_DIR = OUTPUT_ROOT / f"drama_run_{timestamp}"
RUN_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# STAGE 1
# 每张图单独生成“场景卡”
# 重点：只提炼后续写剧本真正需要的信息，不写长分析
# ============================================================

scene_cards = []

for index, image_path in enumerate(selected_images, start=1):

    scene_card_prompt = f"""
你是一名影视编剧的视觉取材助手。

你现在只看“图片{index}”。

不要写长篇视觉分析。
不要输出构图理论。
不要输出可信度分类。
不要写剧本。
不要猜测真实人物身份。

只提炼后续编剧真正需要的素材。

严格输出：

# 图片{index} 场景卡

- 场景：
- 时间：
- 天气 / 光线：
- 可见人物：
- 人物A外观与服装：
- 人物A正在做什么：
- 人物B外观与服装：（没有则写无）
- 人物B正在做什么：（没有则写无）
- 关键物体：
- 可直接利用的动作：
- 可直接利用的环境：
- 可以成为剧情线索的可见元素：
- 不应该凭空增加的关键元素：
- 最适合承担的剧情功能：开场 / 发展 / 转折 / 高潮 / 结尾（可多选）
- 一句话视觉印象：

注意：
只写能帮助后续写故事的信息。
控制在500字以内。
"""

    card = generate(
        image_message(image_path, scene_card_prompt),
        SCENE_CARD_TOKENS,
        f"STAGE 1.{index} / 图片{index}场景卡",
    )

    scene_cards.append(card)


scene_cards_text = "\n\n".join(scene_cards)

scene_cards_file = RUN_DIR / "00_scene_cards.txt"
save_text(
    scene_cards_file,
    "# 参考图片场景卡\n\n" + scene_cards_text,
)


# ============================================================
# STAGE 2
# 场景卡 -> 故事骨架
# ============================================================

outline_prompt = f"""
你是一名专业短剧编剧。

下面是 {NUM_IMAGES} 张真实参考图片提炼出的场景卡。

【场景卡开始】
{scene_cards_text}
【场景卡结束】

现在设计一部约{TARGET_DURATION}秒的短剧。

这里只生成“故事骨架”，不要写正式剧本，不要写分镜。

最重要的要求：

1. 必须有真正的故事，不是环境描述。
2. 必须至少有2个真正参与剧情的人物。
3. 如果不同图片中的人物明显不同，不要强行让他们成为同一个人。
4. 不同图片可以对应：
   - 不同人物
   - 不同地点
   - 不同时间
   - 不同故事节点
5. 每次切换地点必须有明确原因。
6. 至少有5次信息变化。
7. 至少有一次阻碍或误判。
8. 至少有一次明确转折。
9. 高潮必须是人物主动做出的选择或行动。
10. 结尾必须是可见动作或一句真实对白。
11. 不允许“命运安排”“与过去有关”“突然明白一切”这种抽象句。
12. 不允许整部剧只是“寻找 -> 继续寻找 -> 再寻找”。
13. 可以合理增加普通小道具，但关键道具必须有明确来源。
14. 剧情必须适合后续AI视频生成，动作不要过于复杂。

严格输出：

# 短剧标题

# 类型

# 一句话梗概

# 主要角色

角色1：
- 姓名：
- 对应哪张参考图：
- 身份：
- 外观锚点：
- 性格：
- 目标：
- 与其他角色关系：

角色2：
...

# 故事核心因果链

1. 开场具体事件：
2. 主角目标：
3. 第一条线索：
4. 第一阻碍：
5. 第二条信息：
6. 误判或失败：
7. 关键转折：
8. 高潮选择：
9. 最终结局：

# 八场故事结构

## 场次01 0-15秒
- 地点：
- 人物：
- 具体发生什么：
- 关键对白：
- 本场新增信息：
- 下一场为什么发生：

## 场次02 15-35秒
...

一直写到：

## 场次08 140-150秒

每一场都必须新增事件或信息，不能重复。
"""

story_outline = generate(
    text_message(outline_prompt),
    OUTLINE_TOKENS,
    "STAGE 2 / 生成故事骨架",
)

outline_file = RUN_DIR / "01_story_outline.txt"
save_text(outline_file, story_outline)


# ============================================================
# STAGE 3
# 分4次生成正式剧本，每次2场
# ============================================================

scene_ranges = [
    (1, 2, "0-35秒"),
    (3, 4, "35-75秒"),
    (5, 6, "75-115秒"),
    (7, 8, "115-150秒"),
]

screenplay_chunks = []

for batch_idx, (start_scene, end_scene, time_range) in enumerate(scene_ranges, start=1):

    previous_screenplay = "\n\n".join(screenplay_chunks)

    continuity_context = ""
    if previous_screenplay:
        continuity_context = f"""
【已经完成的前文剧本】
{previous_screenplay}
【前文结束】

必须自然承接前文。
不要重写前面的场次。
不要让已经发生过的事件重新发生。
"""

    screenplay_prompt = f"""
你是一名专业影视编剧。

【总故事骨架】
{story_outline}
【故事骨架结束】

{continuity_context}

现在只写：
场次{start_scene:02d} 到 场次{end_scene:02d}
对应时间大约 {time_range}。

这次必须写“真正的剧本正文”，不是提纲。

==================================================
真正剧本必须有什么
==================================================

每一场都必须明确出现：

- 地点
- 时间
- 出场人物
- 真实环境
- 人物动作
- 人物之间的反应
- 人物实际说出的对白
- 一个推动剧情的场尾事件
- 转场

不要写：

“二人进行了交谈。”
“他十分焦虑。”
“他继续寻找。”
“他获得了一条线索。”

必须把这些实际写出来。

例如：

阿远冲到柜台前，把一张皱巴巴的小票放在台面上。

阿远：
“这个时间，有人送过东西来吗？”

店员低头看了一眼小票。

店员：
“东西没有。倒是有人问过失物招领。”

阿远：
“什么人？”

店员抬手指向东侧出口。

店员：
“戴灰帽子的，往老街去了。”

阿远抓起小票，转身跑出画面。

==================================================
对白硬性要求
==================================================

你本次写2场。

每场至少：
- 3句人物对白
- 2个具体人物动作
- 1次人物反应
- 1个新增信息

两场合计至少6句对白。

对白必须：
- 真实
- 简短
- 口语化
- 符合人物年龄
- 推动剧情

不要长篇独白。

==================================================
严格输出格式
==================================================

# 场次{start_scene:02d}

【时间轴：】
【地点：】
【时间：】
【参考图：图片X / 无】
【出场人物：】

## 场景描述
写2-4句环境、光线、重要物体、环境声音。

## 剧情表演
按照时间顺序写具体人物动作。

## 对白

角色：
“……”

动作 / 反应。

角色：
“……”

继续真实动作和对白。

## 场尾事件
写一个具体事件，直接推动下一场。

## 转场
CUT TO：……

然后继续：

# 场次{end_scene:02d}

同样完整写出。

禁止输出：
- 分镜
- JSON
- H3 Prompt
- Scene Bible
- Character Bible
- 图片分析

只输出正式剧本正文。
"""

    chunk = generate(
        text_message(screenplay_prompt),
        SCENE_CHUNK_TOKENS,
        f"STAGE 3.{batch_idx} / 正式剧本 场次{start_scene:02d}-{end_scene:02d}",
    )

    screenplay_chunks.append(chunk)

    chunk_file = RUN_DIR / f"{batch_idx + 1:02d}_screenplay_scene_{start_scene:02d}_{end_scene:02d}.txt"
    save_text(chunk_file, chunk)


full_screenplay = "\n\n".join(screenplay_chunks)

full_screenplay_file = RUN_DIR / "06_full_screenplay.txt"
save_text(
    full_screenplay_file,
    f"""# AI短剧正式剧本

# 参考图片
"""
    + "\n".join(
        f"图片{i}: {p}" for i, p in enumerate(selected_images, 1)
    )
    + "\n\n"
    + full_screenplay
)


# ============================================================
# STAGE 4
# 正式剧本 -> 20个剧情镜头
# 分两次，每次10个
# ============================================================

shot_ranges = [(1, 10), (11, 20)]
shot_chunks = []

for shot_start, shot_end in shot_ranges:

    previous_shots = "\n\n".join(shot_chunks)

    previous_context = ""
    if previous_shots:
        previous_context = f"""
【前10个镜头】
{previous_shots}
【前10个镜头结束】

从Clip 11自然承接Clip 10，不要重复已经表达的信息。
"""

    shot_prompt = f"""
你是一名电影分镜导演。

【正式短剧剧本】
{full_screenplay}
【剧本结束】

{previous_context}

现在只生成 Clip {shot_start:02d} - Clip {shot_end:02d}。

不要修改剧情。
不要重新创作故事。
必须保留剧本中的重要对白。

整个短剧最终固定20个Clip，总时长145-155秒。
平均每个Clip约7-8秒。
单Clip最长12秒。

每个Clip只安排一个主要动作。

严格格式：

### Clip XX
- 时长：X秒
- 对应场次：
- 使用参考图：图片X / 无
- 参考方式：人物+环境 / 仅环境 / 无
- 地点：
- 出场人物：
- 画面：
- 主要动作：
- 人物表情：
- 对白 / 旁白：
- 环境声音：
- 新增剧情信息：
- 剧情作用：
- 与下一镜头连接：

要求：
- 每个Clip都必须有实际画面。
- 有对白就把原句写出来。
- 不要连续重复寻找、走路、张望。
- 不生成H3 Prompt。
- 不生成JSON。
"""

    shot_chunk = generate(
        text_message(shot_prompt),
        SHOT_CHUNK_TOKENS,
        f"STAGE 4 / Clip {shot_start:02d}-{shot_end:02d}",
    )

    shot_chunks.append(shot_chunk)

    shot_file = RUN_DIR / (
        "07_shotlist_01_10.txt"
        if shot_start == 1
        else "08_shotlist_11_20.txt"
    )
    save_text(shot_file, shot_chunk)


full_shotlist = "\n\n".join(shot_chunks)

full_shotlist_file = RUN_DIR / "09_full_shotlist.txt"
save_text(full_shotlist_file, full_shotlist)


# ============================================================
# 5. 完成
# ============================================================

print("\n" + "=" * 100)
print("全部完成")
print("=" * 100)
print(f"运行目录：{RUN_DIR}")
print(f"场景卡：{scene_cards_file}")
print(f"故事骨架：{outline_file}")
print(f"正式剧本：{full_screenplay_file}")
print(f"完整20镜头：{full_shotlist_file}")
print("=" * 100)
print("最重要：先查看正式剧本：")
print(full_screenplay_file)
print("=" * 100)