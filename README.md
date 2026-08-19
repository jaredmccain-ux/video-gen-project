# SceneFlow · MiniMax H3 短剧生成流水线

从几张灵感图到一条带硬字幕的成片，中间七步，每一步的产出都摊开给人改、由人批准之后才继续。
视频生成走本机 ComfyUI 上的 MiniMax H3（Ref2VA / I2VA / FL2VA / T2VA 混合路由），规划与看图走
OpenAI 兼容的大模型接口，字幕时间轴用本机语音识别对齐成片里的真实人声。

`SceneFlow` 是这套系统的网页工作台名字：入口终端 + 七步制作台，服务本身就是 `short_drama.studio_server`。

三张锚点图来自 [lingbot-world-v2](https://github.com/kw9-21/lingbot-world-v2) 的 `runs/mvp_story_001/anchors/`。

## 三分钟演示片

- 仓库内（压缩版，约 45 MB，1080p，中文旁白 + 背景音乐）：`demo_video/out/sceneflow-demo-web.mp4`
- 原画质版（约 105 MB）在本仓库的 **Releases** 里下载
- 片中演示的项目就是仓库里的示例项目 `runs/20260819T210000Z-next-scene-together`，画面全部是系统真实产出，
  不是示意图；旁白由本机 TTS 合成，工程见 [`demo_video/`](demo_video)

## 七步流程

| 步骤 | 名称 | 产出 | 人工要做的事 |
| --- | --- | --- | --- |
| 01 | 素材准备 | `inputs/`、灵感记录 | 上传图片，或选题材风格让模型出提案，或写梗概让它扩写 |
| 02 | 画面理解 | `01_descriptions/` | 逐条核对可见事实、关键物体、不确定信息，然后批准 |
| 03 | 故事规划 | `02_story/` | 改剧名、一句话故事、角色、场景、段落节奏与正式剧本 |
| 04 | 分镜拆分 | `03_shots/` | 逐镜改镜头作用、对白、构图机位、逐秒动作、时长与生成方式 |
| 05 | 人工编排 | `05_videos/` | 逐镜定生成方式与参考图，提交 ComfyUI 生成 |
| 06 | 字幕校对 | `06_subtitles/` | 校对文字、对齐时间轴、定字幕样式 |
| 07 | 合片验收 | `07_final/` | 看拼接报告与缺失清单，预览成片 |

前六步都有 `*.approved` 闸门：没批准就进不到下一步，重跑上游会自动作废下游的批准。

## 快速开始

```bash
pip install -r requirements.txt          # 只有 5 个纯 Python 依赖
export ARK_API_KEY='你的-key'            # 配置文件里只写变量名，不存密钥

# 启动网页工作台
python -m short_drama.studio_server \
  --config configs/project.local.yaml \
  --host 127.0.0.1 --port 4173
```

- 入口页：`http://127.0.0.1:4173/` —— 终端风格的入口，开机自检会显示 ComfyUI 是否在线、本地有几个项目，敲 `start` 进工作台
- 工作台：`http://127.0.0.1:4173/studio` —— 七步制作台，刷新会留在当前步骤

另外需要本机就绪的外部依赖：

- **ComfyUI**（默认 `http://127.0.0.1:6006`）以及 MiniMax H3 权重，见下面「模型权重」
- **ffmpeg / ffprobe**：拼接、烧录字幕、抽音轨
- **中文字体**：硬字幕默认用 `Noto Sans CJK SC`
- **可选的语音识别环境**：字幕时间轴对齐需要一个装了 `funasr` 的 Python，见「字幕」

### 模型权重

权重不在仓库里，需要自己下载到实际启动的那个 ComfyUI 的 `models` 目录：

```bash
HF_ENDPOINT=https://hf-mirror.com hf download Comfy-Org/MiniMax-H3 \
  diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors \
  --local-dir /path/to/ComfyUI/models

# 下载完重启 ComfyUI，确认模型已注册
curl -s http://127.0.0.1:6006/object_info/UNETLoader
```

Ref2VA 用 `minimax_h3_ref2va_pruned_int8_convrot.safetensors`，FL2VA / I2VA / T2VA 用
`minimax_h3_fl2va_pruned_int8_convrot.safetensors`，文件名可在配置的 `comfyui` 段覆盖。

## 目录结构

```text
minimax_short_drama/
  short_drama/        # Python 包：CLI、各阶段实现、Studio 服务、字幕、ASR
  studio/             # 网页前端：入口页 gate.* + 工作台 index.html/app.js/styles.css
  workflows/          # 从 ComfyUI 导出的工作流参考（motion context）
  prompts/            # 规划阶段提示词
  schemas/            # 各阶段 JSON Schema
  configs/            # 项目配置（本机 / AutoDL），只写密钥的环境变量名
  assets/anchors/     # 三张示例输入图
  scripts/            # 辅助脚本（启动 ComfyUI、导入外部项目等）
  tests/              # unittest 用例
  demo_video/         # Remotion 演示片工程（截图 → 场景 → 渲染 → 配音）
  runs/               # 每个项目一个目录；仓库只保留一个示例项目
```

## 字幕：硬字幕 + 真实人声对齐

文字始终以**已批准的分镜对白**为准，时间轴则来自成片里的真实人声，避免"字幕和嘴不同步"：

1. 按分镜对白生成计划字幕
2. 抽出成片音轨，用 **FSMN VAD** 切句、**SenseVoice** 转写，再和剧本对白做相似度匹配
3. 用能量包络细修每句的起止边界
4. 输出 ASS/SRT，用 ffmpeg 烧成硬字幕

在配置的 `subtitles` 段指定识别环境（可以和主服务不是同一个 Python 环境）和字幕样式：

```yaml
subtitles:
  asr:
    python: /path/to/env-with-funasr/bin/python
    sensevoice_dir: /path/to/SenseVoiceSmall
    vad_dir: /path/to/speech_fsmn_vad_zh-cn-16k-common-pytorch
    device: cuda:0
  style:
    font_name: Noto Sans CJK SC
    max_chars_per_line: 18
```

不配 ASR 也能用：字幕会退回按对白时长排布，样式和烧录不受影响。

## 命令行全流程（不开网页也能跑）

```bash
export PYTHONPATH=$PWD

python -m short_drama.cli init --config configs/project.local.yaml
RUN=runs/<上面输出的 run_id>

python -m short_drama.cli prepare-images   --run "$RUN"
python -m short_drama.cli describe-images   --run "$RUN"
python -m short_drama.cli validate --run "$RUN" --stage descriptions
python -m short_drama.cli approve  --run "$RUN" --stage descriptions

python -m short_drama.cli plan-story --run "$RUN"
python -m short_drama.cli validate --run "$RUN" --stage story
python -m short_drama.cli approve  --run "$RUN" --stage story

python -m short_drama.cli plan-shots           --run "$RUN"
python -m short_drama.cli prepare-consistency  --run "$RUN"
python -m short_drama.cli check-continuity     --run "$RUN"
python -m short_drama.cli validate --run "$RUN" --stage shots
python -m short_drama.cli approve  --run "$RUN" --stage shots

python -m short_drama.cli render-prompts   --run "$RUN"
python -m short_drama.cli generate-videos  --run "$RUN"

python -m short_drama.cli generate-subtitles --run "$RUN"
python -m short_drama.cli assemble --run "$RUN" \
  --font /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
```

单镜 4–8 秒，整片二十镜左右总耗时很长，建议用 `screen` / `tmux` 跑 `generate-videos`。

测试：`python -m unittest discover -s tests -v`

## 示例项目

`runs/20260819T210000Z-next-scene-together` 是《下一场，还一起》这个项目的真实产物，保留了：

```text
inputs/                   参考图与首帧素材
01_descriptions/          画面理解结果
02_story/                 剧本、角色、场景、段落节奏
03_shots/                 16 张镜头卡与人工编排决策
04_prompts/               渲染出的生成提示词
05_videos/S001..S016.mp4  16 个分镜片段
06_subtitles/             计划字幕、ASR 对齐结果、ASS/SRT
07_final/studio_final_sub.mp4  122 秒带硬字幕成片
```

镜头片段和无字幕剪辑是外部工程产出的只读拷贝（源目录未被改动），由
`scripts/import_external_project.py` 按 `--source` / `--master` 拷进来；字幕是本系统自己跑出来的：
按已批准对白生成计划字幕，用本机 SenseVoice + FSMN VAD 对齐成片真实人声（24 个人声片段、
41 条字幕全部匹配），再烧成硬字幕。

为了控制仓库体积，中间生成物（`05_videos/studio_generations/`）、无字幕的
`studio_master.mp4` 和抽出来的音轨没有入库，规则见 `.gitignore`。

## 演示片工程（Remotion）

```bash
cd demo_video
npm install              # 需要 Node 16+，实测 Node 20
npm run capture          # 用 headless Chrome 抓工作台各步骤截图（需先启动服务）
npm run render           # 渲染 1080p / 30fps / 180 秒
npm run voice            # 重新合成旁白并混音（改 narration.json 后跑）
```

旁白是本机 [VoxCPM2](https://huggingface.co/openbmb/VoxCPM2) 离线合成的，文稿在 `narration.json`
（每条是时间点 + 文字）。`scripts/voice.sh` 会合成、按时间铺轨、和视频合流，并检查每句是否超格；
视频流是直接 copy 的，改旁白不需要重新渲染。合成好的整条旁白留了一份 `public/voice/narration.m4a`，
没有 GPU 也能直接重新合流。仓库里的 `out/sceneflow-demo-web.mp4` 是在此之上又叠了一层背景音乐的
交付版本，音乐素材不入库。

## 仓库有意不包含

- **模型权重**：H3 diffusion checkpoint、SenseVoice / FSMN VAD、VoxCPM2，都请按上面的说明自行下载
- **环境依赖**：`.venv/`、`demo_video/node_modules/`
- **任何密钥**：配置里只有 `api_key_env` 这类变量名，密钥走环境变量
- **其它项目**：`runs/` 下只保留上面那一个示例项目
- **大体积中间产物**：截图之外的渲染产物、无字幕成片、旁白 wav 原始文件

## 已知限制

- 故事阶段会给角色输出 `reference_image_ids`，把锚点图里真实可辨识的人物绑成视觉参考；不要把人物改成另一性别、年龄或服装
- `prepare-consistency` 做的是一致性预处理：角色正/侧/背参考注册、首尾帧分解、智能选参考图、中大变化镜头的关键帧生成与择优，并据此重算路由
- 重跑 `prepare-consistency` 会改写 `shots.json` 并作废 `shots.approved`，必须重新 validate / approve
- `validate --stage shots` 会同时做 story 交叉校验（含 `generation_mode` / `depends_on` / FL2VA 末帧）
- 场景首镜和周期性重锚点走 **Ref2VA**；连续动作镜头在两次重锚之间用上一镜末帧 **I2VA**，避免末帧链无限漂移
- medium/large 变化镜头在关键帧就绪后才走真正的 **FL2VA**，没有关键帧不会伪造 `last_frame`
- `image_generator.enabled=true` 时通过 OpenAI 兼容的 Images API 生成肖像与关键帧；关闭后仍可分解与选图
- 旧 run 的 `story.json` 缺字段时不会假装启用一致性，默认要求重新 `plan-story`
- 本期未接入 H3-Context-IR，也没有做视频生成后的 ArcFace / CLIP 自动一致性检测
