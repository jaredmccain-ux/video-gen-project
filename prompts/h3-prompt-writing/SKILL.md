---
name: h3-prompt-writing
source: https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing
---

# MiniMax H3 Prompt Writing（运行时约束摘录）

本项目的 `short_drama/h3_prompt.py` 按官方 Skill 固化以下规则：

1. T2VA / I2VA / FL2VA 使用且只使用：
   `integrated_multimodal_description`、`overall_soundscape`、`non_diegetic_music`。
2. I2VA 使用官方 0.00 秒首帧对齐句；FL2VA 使用官方首尾图对齐句，并把有效时长格式化为两位小数。
3. Ref2VA 使用且只使用：
   `subject_definitions`、`summary`、`retention_analysis`、`detailed_description`、`overall_soundscape`、`non_diegetic_music`。
4. `<Picture i>`、`<Video k>`、`<Audio j>` 的顺序必须分别与实际送入 ComfyUI 的
   `ref_image_i`、`ref_video_k`、`ref_audio_j` 顺序相同；每项素材都必须在 Prompt 中声明用途，
   一个人物固定使用同一 `<Subject i>`。
5. 角色在 `retention_analysis` 中标为 `fully_preserved`，动作、背景和机位变化不得被误判为身份损失。
6. 对白使用稳定 `(S1)` 和 `<d>[Chinese] ...</d>`，原文只出现一次；声音摘要不得重复对白。
7. 每个生成单元只使用 `[Shot 1]`，避免模型在单个 4–8 秒片段中自行切镜。

完整英文规范以链接中的官方 `references/base-en.txt` 和 `references/ref-en.txt` 为准。本文件不是另一套 Prompt 标准，只记录本流水线实际编码的规则及来源。
