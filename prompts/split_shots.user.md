# 任务

把下方故事的 {{BEAT_COUNT}} 个剧情段落细分为 12–30 个短镜头，建议约 16–22 个。

## 一、剧情与时间轴

- 保持剧情段落的原始顺序和内容，不新增或删除关键事件。
- 同一剧情段落的镜头必须连续排列。
- 每个剧情段落内所有镜头的时长之和，必须严格等于该段落的 `duration_s`。
- 每个镜头时长为 4–8 秒。
- 全部镜头时间连续、没有空隙，总时长为 117–123 秒。
- `shot_id` 从 `S001` 开始连续递增。

## 二、单镜头复杂度

- 每个镜头只有一个地点、一个核心动作和一种主要运镜。
- 不在一个镜头内切换地点或时间。
- 骑车、奔跑、急转头、人物遮挡和大幅运镜尽量安排为无对白镜头。
- `action_timeline` 按秒描述动作顺序，并给对白前后留出稳定画面和反应时间。

## 三、相邻镜头连续性

- 同一 `scene_id` 内，相邻镜头必须形成连续画面。
- 下一镜头每个角色的 `blocking.start` 必须与上一镜头对应角色的 `blocking.end` 完全一致，包括：
  - `horizontal`
  - `depth`
  - `facing`
  - `visible`
- 新角色不能在下一镜头第一帧凭空出现：应先设为 `visible=false`，再在 `action_timeline` 中明确入场。
- 角色不能在镜头边界凭空消失：应在前一镜头内明确退场，并让其 `blocking.end.visible=false`。
- 站位、景别或朝向需要变化时，变化必须发生在镜头内部，并写入 `action_timeline`。
- 运动方向改变时，必须明确写出减速、停下、转身或反向移动。
- 新地点使用新的 `scene_id`，不要求继承上一地点的画面构图。

## 四、画面位置与朝向

- `horizontal` 只能使用 `screen-left`、`screen-center`、`screen-right`。
- `depth` 只能使用 `foreground`、`midground`、`background`。
- `facing` 只能使用 `screen-left`、`screen-right`、`camera`、`away-from-camera`。
- `movement_direction` 只能使用 Schema 中给定的枚举。
- 禁止使用“前面、后面、前方、后方、身前、身后、前边、后边”等词承担人物站位。
- `composition` 描述景别、场景、光线和构图，不重复或替代 `blocking`。

## 五、对白与嘴部动作

- 所有对白使用简体中文。
- 每个镜头最多一个说话人。
- 对白非空白字符目标上限为 `duration_s × 4`，中文、数字和标点都计数；确有必要时最多允许超出 3 个字符。
- `subtitle_text` 必须等于 `dialogue[].text` 按顺序直接拼接的结果。
- 有对白时：
  - `allowed_speaker_ids` 只包含实际说话人的角色 ID；
  - `speaker_mappings` 只包含该角色，并将其映射为 `S1`；
  - 说话人在说话阶段必须可见，开场或结束至少一处 `mouth_state=speaking`；
  - 其他角色的开场和结束均为 `mouth_state=closed`。
- 无对白时：
  - `dialogue`、`allowed_speaker_ids` 和 `speaker_mappings` 都为空数组；
  - 所有角色开场和结束均为 `mouth_state=closed`。

## 六、声音范围

- 禁止旁白、内心独白、画外解说、离屏人物语音和设备播放的人声。
- `offscreen_human_voice_allowed` 固定为 `false`。
- `non_diegetic_music` 固定为 `false`。
- `ambient_sounds` 只列当前场景自然存在的环境声。
- `action_sounds` 只列画面中可见动作产生的短音效。

## 七、自检顺序

输出前逐项检查：

1. 镜头数是否为 12–30。
2. 每个剧情段落的镜头时长之和是否等于原时长。
3. 总时间轴是否连续且为 117–123 秒。
4. 同场景相邻镜头的 `blocking.end` 与 `blocking.start` 是否完全一致。
5. 是否存在人物在边界凭空出现、消失、换边、改变景深或突然转向。
6. 每个对白镜头是否只有一个画面内说话人。
7. 对白字数是否不超过目标上限 3 个字符，字幕、声音白名单和嘴部状态是否一致。
8. 是否只使用 Schema 允许的枚举值。

# 输入故事

{{STORY_JSON}}

# 输出 JSON Schema

{{SCHEMA_JSON}}
