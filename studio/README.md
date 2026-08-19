# SceneFlow 人工镜头编排台

本地人工编排与 MiniMax H3 生成控制台。每个镜头都由人决定：

- MiniMax H3 生成模式（T2VA / I2VA / FL2VA / Ref2VA）
- 每个镜头使用的首帧、末帧和身份参考图
- 最终提交给 H3 的 Prompt
- 是否批准并锁定镜头
- 素材准备页中的图片、视频和音频上传

第一阶段同时是“灵感准备”入口：

- 上传并选择任意数量的核心图片，交给下一阶段逐张做画面描述后发展剧情；
- 没有素材或灵感时，让项目配置的 LLM 从零生成一版 120 秒短剧提案；
- 有初步想法时，保留原意并由 LLM 补足钩子、冲突、因果链和结尾。

阶段 02–04 已接入同一个本地工作流：

- 画面理解按已选图片逐张调用多模态模型，图片数量不限，并支持人工修正后批准；
- 故事规划读取已批准的画面事实，生成约 120 秒剧情并支持本地编辑、批准；
- 分镜拆分读取已批准故事，生成 15–30 个、单镜 4–8 秒且总时长约 120 秒的镜头，再交给人工编排。

每次编辑或重新生成 `01_descriptions`、`02_story`、`03_shots` 产物前，旧版会自动保存在
对应阶段目录下的 `studio_versions/`，批准标记通过文件哈希自动失效，避免修改后沿用旧批准。

分镜批准后即可在人工编排页逐镜核对 Prompt 与提交生成，因此流程只保留 07 个阶段：
素材准备、画面理解、故事规划、分镜拆分、人工编排、字幕校对、合片验收。

## 字幕校对（阶段 06）

字幕文字始终来自已批准分镜的对白，识别结果只用来决定「哪一句落在哪个人声窗口」：

1. **生成计划字幕**按镜头对白和 `action_timeline` 里的按秒动作给出确定性时间轴，
   一个镜头有几句台词就产出几条字幕；
2. **按人声对齐**从成片抽 16 kHz 单声道音频，用本机 FSMN VAD 找人声窗口、
   SenseVoice 逐窗识别，再把剧本台词按文本相似度落到真实窗口上；窗口边缘的环境声
   由 RMS 包络裁掉，一个窗口内的多句台词在最安静的位置切开；
3. **硬字幕样式**按院线习惯预设（白字黑描边、底部居中、最多两行、无半透明底框），
   字号与边距按成片高度自动缩放，可在页面上覆盖后写入 `06_subtitles/style.json`；
4. **烧录硬字幕**用 libass 渲染 `06_subtitles/full.ass`，视频以 CRF 17 重编码，
   音频 `-c:a copy` 原样保留。

识别环境与 Studio 可以是不同的 Python：在项目配置的 `subtitles.asr` 里指定装有
`funasr` 的解释器与 SenseVoice / FSMN VAD 模型目录即可。未配置时页面仍可使用计划
时间轴与人工编辑。

灵感状态及最多 20 个历史提案保存在各 run 的 `inputs/inspiration.json`，不会覆盖
既有 `story.json`。Studio 会沿用项目配置的 LLM，并在本地开发环境中读取已有的
`.vscode/debug.env`（仅注入进程，不通过 API 返回密钥值）。

## 本地数据目录

Studio 不依赖云端数据库，用户数据直接写入部署机器上的当前 run：

```text
runs/<run_id>/
  inputs/
    inspiration.json                 # 当前灵感来源与提案历史
    studio_uploads/
      manifest.json                  # 上传素材索引（保存相对路径）
      images/                        # 用户图片，不限数量
      videos/                        # 用户视频
      audios/                        # 用户音频
  02_story/
    studio_drafts/
      <timestamp>-<id>.json          # 每次生成/润色的结构化剧情
      <timestamp>-<id>.md            # 可直接阅读的剧情稿
      latest.json                    # 当前剧情版本
      latest.md                      # 当前可读剧情版本
  06_subtitles/
    timeline.json                    # 字幕条目与成片时间轴、对齐来源
    full.ass                         # 烧录用硬字幕（院线样式）
    full.srt                         # 外挂字幕
    style.json                       # 人工覆盖过的 ASS 样式
    audio/
      film_16k.wav                   # 识别用音频
      asr_segments.json              # VAD 窗口 + 逐窗识别文本与能量包络
  07_final/
    studio_master.mp4                # 无字幕母版，烧录始终以它为输入
    studio_final.mp4                 # 合片输出
    studio_final_sub.mp4             # 烧录硬字幕后的成片
```

`configs/project.local.yaml` 使用相对于仓库的 `project_root: ..`，因此整个项目目录复制到
另一台机器后不需要修改 `/data/...` 一类绝对路径；新上传和新生成内容会落在那台机器自己的
`runs/` 目录中。API Key 仍由部署者通过环境变量或本地 `.vscode/debug.env` 提供。

在仓库根目录启动（无需新增 Web 框架依赖）：

```bash
python -m short_drama.studio_server \
  --config configs/project.local.yaml \
  --host 127.0.0.1 \
  --port 4173
```

然后访问 `http://127.0.0.1:4173/`。

网页会读取 `runs/*` 中已有的分镜和素材。人工选择保存到
`03_shots/human_orchestration.json`，不会改写原 `shots.json`；生成任务提交给配置中的
ComfyUI，输出到 `05_videos/studio_generations/<shot_id>/`，不会覆盖原流水线的
`05_videos/Sxxx.mp4`。

当前安装的 MiniMax H3 Ref2VA 节点限制为每个镜头最多 9 张参考图片、3 个参考视频和
3 个独立参考音频；参考视频必须为 2–15 秒。素材库中的图片、视频和音频均不限上传数量，
只有在逐镜编排时才执行 9 / 3 / 3 的模型输入上限。参考视频在提交 H3 前会生成 24 fps
的派生文件，原上传文件不会被改写。

运行前请确认 ComfyUI 已在配置的 `comfyui.base_url` 启动，并已安装 H3 节点和相应
FL2VA / Ref2VA checkpoint。网页中的任务卡会显示排队、预检、上传、生成、完成或失败状态。

页面保留 Figma 官方 capture helper，方便将后续页面修改再次同步到设计稿。
