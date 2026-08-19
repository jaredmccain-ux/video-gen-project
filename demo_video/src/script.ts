// Every piece of copy and timing for the demo lives here so the narrative can
// be retuned without touching the scene components.

export type Focus = { x: number; y: number; w: number; h: number; label: string };
export type Plate = { src: string; focus?: Focus };

export type StageSpec = {
  id: string;
  index: string;
  name: string;
  headline: string;
  purpose: string;
  bullets: string[];
  plates: Plate[];
  durationInFrames: number;
};

export const STAGE_NAMES = ["素材准备", "画面理解", "故事规划", "分镜拆分", "人工编排", "字幕校对", "合片验收"];

export const stages: StageSpec[] = [
  {
    id: "assets",
    index: "01",
    name: "素材准备",
    headline: "你的故事，从哪里开始？",
    purpose: "三种起点，最终都汇入同一条流水线。",
    bullets: [
      "有图片素材：直接上传，从画面反推故事",
      "没有灵感：选题材与风格，让 LLM 先给剧情提案",
      "已有想法：写一段梗概，交给 LLM 扩写成剧情",
      "图片 / 视频 / 音频分开入库，后面每一镜都从这里取素材",
    ],
    plates: [
      { src: "shots/assets-a.png", focus: { x: 0.14, y: 0.235, w: 0.85, h: 0.45, label: "三种创作起点，任选其一" } },
      { src: "shots/assets-b.png", focus: { x: 0.14, y: 0.4, w: 0.85, h: 0.42, label: "本地素材库：上传即可复用" } },
    ],
    durationInFrames: 450,
  },
  {
    id: "descriptions",
    index: "02",
    name: "画面理解",
    headline: "先确认事实，再让故事发生",
    purpose: "多模态模型只写画面里看得见的东西，不替你编剧。",
    bullets: [
      "逐张读图，输出场景事实、光线与氛围",
      "可见事实一条一行，可以逐条增删改",
      "关键物体、不确定信息分开登记，避免模型瞎猜",
      "点「批准并进入故事规划」才会往下走",
    ],
    plates: [
      { src: "shots/descriptions-a.png", focus: { x: 0.545, y: 0.47, w: 0.44, h: 0.38, label: "可见事实：逐条可编辑" } },
    ],
    durationInFrames: 510,
  },
  {
    id: "story",
    index: "03",
    name: "故事规划",
    headline: "把画面连接成一条因果链",
    purpose: "先写骨架和正式剧本，再收敛到可拍摄的时长。",
    bullets: [
      "骨架：剧名、一句话故事、角色与场景一次写清",
      "段落节奏按时长排成时间轴，总时长一眼可见",
      "完整剧情、正式剧本、风格与声音规则分页签编辑",
      "结构数据以 JSON 折叠保存，供后面保持一致性",
    ],
    plates: [
      { src: "shots/story-a.png", focus: { x: 0.155, y: 0.5, w: 0.82, h: 0.19, label: "段落节奏：8 个 Beat 共 120 秒" } },
      { src: "shots/story-b.png", focus: { x: 0.155, y: 0.3, w: 0.82, h: 0.45, label: "剧本正文可直接改写" } },
    ],
    durationInFrames: 540,
  },
  {
    id: "shots",
    index: "04",
    name: "分镜拆分",
    headline: "把剧情变成可执行镜头",
    purpose: "每个镜头控制在 4–8 秒，写明构图、机位、动作与对白。",
    bullets: [
      "剧本自动拆成 16 张镜头卡，编号与 Beat 对齐",
      "镜头作用、对白、构图机位、逐秒动作分区呈现",
      "时长与建议生成模式可以在卡头直接改",
      "批准后镜头表锁定，成为后面生成的唯一依据",
    ],
    plates: [
      { src: "shots/shots-a.png", focus: { x: 0.16, y: 0.345, w: 0.83, h: 0.35, label: "一张镜头卡包含全部拍摄信息" } },
      { src: "shots/shots-b.png", focus: { x: 0.16, y: 0.25, w: 0.83, h: 0.5, label: "16 镜依次排好" } },
    ],
    durationInFrames: 510,
  },
  {
    id: "orchestration",
    index: "05",
    name: "人工编排",
    headline: "逐镜决定，机器执行",
    purpose: "生成方式、输入图片和最终 Prompt 都要人工确认。",
    bullets: [
      "四种生成方式：文本生成 / 单图 / 首尾帧 / 多图参考",
      "编排输入素材：首帧、尾帧、参考图，缺图可 AI 补",
      "右侧给出镜头上下文与硬约束，避免跑偏",
      "确认后提交 ComfyUI，队列进度实时可见",
    ],
    plates: [
      { src: "shots/orchestration-a.png", focus: { x: 0.145, y: 0.4, w: 0.74, h: 0.23, label: "选择生成方式，系统只给建议" } },
      { src: "shots/orchestration-b.png", focus: { x: 0.145, y: 0.3, w: 0.74, h: 0.45, label: "首帧 / 尾帧逐镜编排" } },
    ],
    durationInFrames: 420,
  },
  {
    id: "subtitles",
    index: "06",
    name: "字幕校对",
    headline: "人声对齐、样式与烧录",
    purpose: "时间轴对齐真实人声，文字仍以已批准的对白为准。",
    bullets: [
      "先按分镜对白生成计划字幕",
      "本机 SenseVoice + FSMN VAD 识别成片人声",
      "一键按人声对齐时间轴，逐条可微调",
      "字体、描边、位置可调，支持 ASS / SRT 与硬字幕烧录",
    ],
    plates: [
      { src: "shots/subtitles-a.png", focus: { x: 0.16, y: 0.28, w: 0.53, h: 0.36, label: "每条字幕都能对齐到镜头" } },
      { src: "shots/subtitles-b.png", focus: { x: 0.685, y: 0.25, w: 0.3, h: 0.5, label: "硬字幕样式实时预览" } },
    ],
    durationInFrames: 480,
  },
  {
    id: "assemble",
    index: "07",
    name: "合片验收",
    headline: "拼接、烧录与检查",
    purpose: "按镜头顺序合片，缺镜会在报告里点出来。",
    bullets: [
      "一键合片：按镜头顺序拼接并烧录硬字幕",
      "报告给出可拼接镜头数、成片时长与缺失清单",
      "16 / 16 全部就位，128.1 秒成片可直接预览",
    ],
    plates: [
      { src: "shots/assemble-a.png", focus: { x: 0.625, y: 0.22, w: 0.36, h: 0.3, label: "镜头与缺失报告" } },
    ],
    durationInFrames: 390,
  },
];

export const gateBullets = [
  "开机自检：ComfyUI 是否在线、有几个项目",
  "help 看命令，flow 看流程，runs 看项目",
  "敲 start 或点「制作台」，进入七步工作台",
];

export const overviewItems = [
  ["01", "素材准备", "灵感图片、视频与音频入库"],
  ["02", "画面理解", "只写画面里看得见的事实"],
  ["03", "故事规划", "骨架、节奏与正式剧本"],
  ["04", "分镜拆分", "构图、机位、动作与对白"],
  ["05", "人工编排", "逐镜确认生成方式与输入"],
  ["06", "字幕校对", "人声对齐与硬字幕烧录"],
  ["07", "合片验收", "拼接、烧录与技术检查"],
];
