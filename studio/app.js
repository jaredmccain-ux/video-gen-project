const UI_BUILD = "20260819-10";

const MODE_LABELS = {
  t2va: "T2VA",
  first_frame: "I2VA",
  first_last_frame: "FL2VA",
  ref2va: "Ref2VA",
  T2VA: "T2VA",
  I2VA: "I2VA",
  FL2VA: "FL2VA",
  Ref2VA: "Ref2VA",
};

const MODE_HINTS = {
  T2VA: "T2VA 当前只提交文本；已选图片仍会保留，切换模式后可继续使用。",
  I2VA: "I2VA 提交 1 张人工指定首帧；尾帧与参考图可预先保存但当前不提交。",
  FL2VA: "FL2VA 提交人工指定的首帧和尾帧；其他参考输入会保留但当前不提交。",
  Ref2VA: "Ref2VA 最多提交 9 张图片和 3 个音频；所选源视频会自动抽取 1–3 张连续性帧计入图片额度，不再加载整段视频 latent。",
};

const state = {
  runs: [],
  runId: null,
  shots: [],
  assets: [],
  jobs: [],
  uploadLimits: { image: null, video: null, audio: null },
  uploadCounts: { image: 0, video: 0, audio: 0 },
  materialFilter: "all",
  inspiration: null,
  llm: null,
  imageGenerator: null,
  workflow: { descriptions: null, story: null, shots: null },
  activeDescriptionIndex: 0,
  inspirationImagePaths: [],
  lastInspirationRequest: null,
  activeIndex: 0,
  activeFilter: "all",
  assetRole: "first",
  subtitles: null,
  subtitleFormat: "ass",
  online: false,
};

const $ = (selector) => document.querySelector(selector);
const shotList = $("#shotList");
const promptText = $("#promptText");
let toastTimer;
let pollTimer;
let aiProgressTimer;
let aiProgressHideTimer;
let buildReloading = false;
const aiProgressState = { value: 0, steps: [], jobIds: [] };
const draftSaveChains = new Map();

const AI_PROGRESS_TASKS = {
  inspiration: ["整理创作偏好", "调用剧情模型", "检查故事结构", "保存本地提案"],
  descriptions: ["读取灵感图片", "逐张进行视觉理解", "整理可见事实", "保存画面描述"],
  story: ["读取已批准事实", "构建人物与因果链", "编排 120 秒剧情", "校验并保存故事"],
  shots: ["读取已批准故事", "拆分 4–8 秒镜头", "检查对白与连续性", "保存分镜计划"],
  prompt: ["读取分镜剧本与图片用途", "LLM 生成镜头导演初稿", "应用 MiniMax 官方 Skill", "校验字段与图片顺序", "保存最终 Prompt"],
  image: ["整理镜头与画面要求", "提交 AI 生图模型", "生成高质量静帧", "保存到本地素材库", "绑定当前镜头"],
  video: ["提交生成请求", "检查 H3 环境", "上传镜头输入", "MiniMax H3 推理", "保存视频与末帧"],
};

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    cache: "no-store",
    headers: { "Content-Type": "application/json", "X-SceneFlow-UI-Build": UI_BUILD, ...(options.headers || {}) },
  });
  let body = {};
  try { body = await response.json(); } catch { body = {}; }
  if (body.reload_required && !buildReloading) {
    buildReloading = true;
    window.location.reload();
  }
  if (!response.ok) throw new Error(body.message || `请求失败 (${response.status})`);
  return body;
}

async function ensureCurrentBuild() {
  if (buildReloading) return false;
  const response = await fetch(`/api/version?t=${Date.now()}`, { cache: "no-store" });
  const body = await response.json();
  if (body.ui_build && body.ui_build !== UI_BUILD) {
    buildReloading = true;
    window.location.reload();
    return false;
  }
  return true;
}

function showToast(message, error = false) {
  const toast = $("#toast");
  toast.querySelector("p").textContent = message;
  toast.querySelector("span").textContent = error ? "!" : "✓";
  toast.classList.toggle("is-error", error);
  toast.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 3000);
}

function paintAIProgress(value, step, hint) {
  const safeValue = Math.max(0, Math.min(100, Math.round(value)));
  aiProgressState.value = safeValue;
  $("#aiProgressPercent").textContent = `${safeValue}%`;
  $("#aiProgressBar").style.width = `${safeValue}%`;
  if (step) $("#aiProgressStep").textContent = step;
  if (hint) $("#aiProgressHint").textContent = hint;
}

function startAIProgress(kind, title, hint = "预计进度，会随实际返回自动完成") {
  clearInterval(aiProgressTimer);
  clearTimeout(aiProgressHideTimer);
  const panel = $("#aiProgress");
  aiProgressState.value = 4;
  aiProgressState.steps = AI_PROGRESS_TASKS[kind] || ["正在处理"];
  aiProgressState.jobIds = [];
  $("#aiProgressTitle").textContent = title;
  panel.classList.remove("is-error");
  panel.classList.add("is-visible");
  panel.setAttribute("aria-hidden", "false");
  paintAIProgress(4, aiProgressState.steps[0], hint);
  aiProgressTimer = setInterval(() => {
    const next = Math.min(93, aiProgressState.value + Math.max(1, Math.round((94 - aiProgressState.value) * 0.07)));
    const index = Math.min(aiProgressState.steps.length - 1, Math.floor(next / (94 / aiProgressState.steps.length)));
    paintAIProgress(next, aiProgressState.steps[index]);
  }, 900);
}

function finishAIProgress(message = "处理完成，本地结果已更新") {
  clearInterval(aiProgressTimer);
  paintAIProgress(100, message, "已完成");
  aiProgressHideTimer = setTimeout(() => {
    $("#aiProgress").classList.remove("is-visible");
    $("#aiProgress").setAttribute("aria-hidden", "true");
  }, 1400);
  aiProgressState.jobIds = [];
}

function failAIProgress(message) {
  clearInterval(aiProgressTimer);
  $("#aiProgress").classList.add("is-error");
  paintAIProgress(aiProgressState.value, "处理未完成", message || "请检查错误信息后重试");
  aiProgressState.jobIds = [];
}

function syncVideoProgress() {
  if (!aiProgressState.jobIds.length) return;
  const jobs = aiProgressState.jobIds.map((id) => state.jobs.find((job) => job.job_id === id)).filter(Boolean);
  if (!jobs.length) return;
  const weights = { queued: 8, preflight: 18, uploading_inputs: 34, running: 62, completed: 100, failed: 100 };
  const failed = jobs.find((job) => job.status === "failed");
  if (failed) return failAIProgress(`${failed.shot_id}：${failed.error || "生成失败"}`);
  const completed = jobs.filter((job) => job.status === "completed").length;
  if (completed === jobs.length) return finishAIProgress(`${jobs.length} 个镜头生成完成`);
  const value = jobs.reduce((sum, job) => sum + (weights[job.status] || 5), 0) / jobs.length;
  const active = jobs.find((job) => job.status !== "completed") || jobs[0];
  const labels = { queued: "已进入生成队列", preflight: "正在检查 H3 环境", uploading_inputs: "正在上传图片、视频和音频", running: "MiniMax H3 正在生成视频" };
  paintAIProgress(Math.max(aiProgressState.value, value), `${active.shot_id} · ${labels[active.status] || "正在处理"}`, `已完成 ${completed} / ${jobs.length} 个镜头`);
}

function setConnection(online, text) {
  state.online = online;
  const node = $("#connectionState");
  node.classList.toggle("is-online", online);
  node.classList.toggle("is-offline", !online);
  node.lastChild.textContent = text;
}

function currentShot() { return state.shots[state.activeIndex]; }

function draftFor(shot) {
  if (!shot.draft) {
    const decision = shot.decision || {};
    const legacyReferences = [...(decision.reference_images || [])];
    const bindings = Array.isArray(decision.reference_image_bindings)
      ? decision.reference_image_bindings.map((item) => ({
        path: item.path,
        usage: item.usage || "scene",
        character_ids: [...(item.character_ids || [])],
        note: item.note || "",
      }))
      : legacyReferences.map((path) => ({ path, usage: "identity", character_ids: [], note: "" }));
    const videoBindings = Array.isArray(decision.reference_video_bindings)
      ? decision.reference_video_bindings.map((item) => ({ path: item.path, usage: item.usage || "motion", note: item.note || "" }))
      : (decision.reference_videos || []).map((path) => ({ path, usage: "motion", note: "" }));
    const audioBindings = Array.isArray(decision.reference_audio_bindings)
      ? decision.reference_audio_bindings.map((item) => ({ path: item.path, usage: item.usage || "soundscape", note: item.note || "" }))
      : (decision.reference_audios || []).map((path) => ({ path, usage: "soundscape", note: "" }));
    const videoFrameBindings = Array.isArray(decision.video_frame_bindings)
      ? decision.video_frame_bindings.map((item) => ({ ...item }))
      : [];
    shot.draft = {
      generation_mode: MODE_LABELS[decision.generation_mode || decision.mode_label || shot.suggested_mode] || "T2VA",
      first_frame: decision.first_frame || null,
      last_frame: decision.last_frame || null,
      reference_images: bindings.map((item) => item.path),
      reference_image_bindings: bindings,
      reference_videos: videoBindings.map((item) => item.path),
      reference_audios: audioBindings.map((item) => item.path),
      reference_video_bindings: videoBindings,
      reference_audio_bindings: audioBindings,
      video_frame_bindings: videoFrameBindings,
      reference_video_strategy: decision.reference_video_strategy || "sampled_frames",
      // A saved human decision is always the source of truth.  Older saves can
      // legitimately contain a Skill-optimized prompt without prompt_skill
      // metadata, so never replace decision.prompt with the generic suggestion.
      prompt: decision.prompt || shot.official_prompt || shot.suggested_prompt || "",
      approved: Boolean(decision.approved),
      locked: Boolean(decision.locked),
      seed: Number(decision.seed || 2101),
      prompt_llm_draft: decision.prompt_llm_draft || "",
      prompt_skill: decision.prompt_skill || "",
      prompt_pipeline: decision.prompt_pipeline || decision.pipeline || [],
      prompt_optimized_at: decision.prompt_optimized_at || null,
      // Decisions saved before the sampled-frame migration still contain a
      // source video but no durable Picture frames.  Mark them stale on load
      // so the user cannot mistake the old <Video> prompt for a runnable one.
      promptStale: Boolean(decision.prompt_inputs_stale)
        || !decision.prompt_skill
        || (videoBindings.length > 0 && videoFrameBindings.length === 0),
      dirty: false,
    };
  }
  return shot.draft;
}

function shotStatus(shot) {
  if (shot.draft && shot.draft.dirty) return "review";
  if (shot.decision && shot.decision.approved) return "approved";
  if (shot.decision) return "review";
  return "pending";
}

function compactText(value, limit = 54) {
  const text = String(value == null ? "" : value).replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

function contextShotCard(shot, position) {
  if (!shot) {
    const isPrevious = position === "previous";
    return `<div class="mini-shot placeholder"><span>${isPrevious ? "START" : "END"}</span><strong>${isPrevious ? "故事从当前镜头开始" : "当前镜头为最后一镜"}</strong><small>${isPrevious ? "无上一镜头" : "无下一镜头"}</small></div>`;
  }
  const detail = position === "previous"
    ? (shot.continuity_out || shot.action_timeline || shot.title)
    : position === "next"
      ? (shot.continuity_in || shot.composition || shot.title)
      : (shot.action_timeline || shot.composition || shot.title);
  return `<button class="mini-shot ${position === "current" ? "current" : ""}" data-context-index="${state.shots.indexOf(shot)}">
    <span>${escapeHtml(shot.id)} · ${escapeHtml(shot.scene || "未命名场景")}</span>
    <strong>${escapeHtml(shot.title || "未命名镜头")}</strong>
    <small>${escapeHtml(compactText(detail, 62) || "暂无连续性说明")}</small>
  </button>`;
}

function renderShotContext() {
  const shot = currentShot();
  if (!shot) return;
  const previous = state.shots[state.activeIndex - 1] || null;
  const next = state.shots[state.activeIndex + 1] || null;
  const incoming = compactText(shot.continuity_in || (previous && previous.continuity_out) || "承接上一镜", 18);
  const outgoing = compactText(shot.continuity_out || (next && next.continuity_in) || "衔接下一镜", 18);
  $("#shotContextFlow").innerHTML = [
    contextShotCard(previous, "previous"),
    `<div class="connector"><i></i><span title="${escapeHtml(incoming)}">${escapeHtml(incoming)}</span><i></i></div>`,
    contextShotCard(shot, "current"),
    `<div class="connector"><i></i><span title="${escapeHtml(outgoing)}">${escapeHtml(outgoing)}</span><i></i></div>`,
    contextShotCard(next, "next"),
  ].join("");
  document.querySelectorAll("[data-context-index]").forEach((button) => button.addEventListener("click", () => selectShot(Number(button.dataset.contextIndex))));
}

function renderShotConstraints(shot = currentShot(), draft = shot && draftFor(shot)) {
  if (!shot || !draft) return;
  const audio = shot.audio_contract || {};
  const dialogue = Array.isArray(shot.dialogue) ? shot.dialogue : [];
  const characters = Array.isArray(shot.characters) ? shot.characters.filter(Boolean) : [];
  const inputs = draft.generation_mode === "T2VA"
    ? "文本生成：不得提交图片、视频或音频参考"
    : draft.generation_mode === "I2VA"
      ? "单首帧：必须且只能指定 1 张起始图片"
      : draft.generation_mode === "FL2VA"
        ? "首尾帧：必须分别指定 1 张首帧和末帧"
        : "多模态参考：首帧、尾帧、参考图及视频抽帧合计最多 9 张；另可使用 3 个源视频、3 个音频";
  const rules = [
    `镜头时长固定为 ${shot.duration} 秒（允许范围 4–8 秒）`,
    inputs,
    characters.length ? `在镜人物：${characters.join("、")}；身份与服装需连续` : "本镜头无人角色，不得额外生成主体人物",
    dialogue.length
      ? `仅允许剧本对白：${dialogue.map((line) => `${line.speaker_id || "角色"}「${line.text || ""}」`).join("；")}`
      : "本镜头无对白，画面人物必须保持闭口",
    audio.offscreen_human_voice_allowed ? "允许剧本指定的离屏人声" : "禁止离屏人声、旁白和画外对白",
    audio.non_diegetic_music ? "允许使用非叙事音乐" : "禁止旁白和非叙事背景音乐",
  ];
  const usageNames = { identity: "人物身份", scene: "场景空间", style: "视觉风格", keyframe: "关键帧动作/构图" };
  if (draft.reference_image_bindings.length) {
    const usageCounts = draft.reference_image_bindings.reduce((result, item) => ({ ...result, [item.usage]: (result[item.usage] || 0) + 1 }), {});
    rules.push(`人工图片用途：${Object.entries(usageCounts).map(([usage, count]) => `${usageNames[usage] || usage} ${count} 张`).join("、")}；仅 Ref2VA 提交这些参考图`);
  }
  if (draft.reference_video_bindings.length) rules.push(`源视频：${draft.reference_video_bindings.length} 个；生成 Prompt 时抽为 ${draft.video_frame_bindings.length || "待生成"} 张 <Picture i> 连续性参考，不提交视频 latent`);
  if (draft.reference_audio_bindings.length) rules.push(`参考音频：${draft.reference_audio_bindings.length} 个；将按 <Audio j> 编号提交给 Ref2VA 并参与 LLM Prompt 生成`);
  if (shot.continuity_in) rules.push(`入镜连续性：${shot.continuity_in}`);
  if (shot.continuity_out) rules.push(`出镜连续性：${shot.continuity_out}`);
  if (shot.depends_on) rules.push(`依赖镜头：${shot.depends_on}，其人物、道具与空间方向必须保持一致`);
  if (shot.source_anchor_image) rules.push(`原分镜视觉锚点：${shot.source_anchor_image}；最终迁移用途以本页人工分类为准`);
  $("#constraintCount").textContent = rules.length;
  $("#constraintList").innerHTML = rules.map((rule) => `<li><span>✓</span><p>${escapeHtml(rule)}</p></li>`).join("");
}

function renderCounts() {
  const approved = state.shots.filter((shot) => shotStatus(shot) === "approved").length;
  const review = state.shots.filter((shot) => shotStatus(shot) === "review").length;
  const pending = state.shots.length - approved - review;
  $("#approvedCount").textContent = approved;
  $("#reviewCount").textContent = review;
  $("#pendingCount").textContent = pending;
  const filterButtons = document.querySelectorAll(".segmented button");
  if (filterButtons[0]) filterButtons[0].textContent = `全部 ${state.shots.length}`;
  if (filterButtons[1]) filterButtons[1].textContent = `待处理 ${review + pending}`;
  if (filterButtons[2]) filterButtons[2].textContent = `已批准 ${approved}`;
}

function renderShots() {
  const query = $("#shotSearch").value.trim().toLowerCase();
  shotList.innerHTML = state.shots.length ? state.shots.map((shot, index) => {
    const status = shotStatus(shot);
    const visibleByFilter = state.activeFilter === "all" || (state.activeFilter === "approved" ? status === "approved" : status !== "approved");
    const visibleByQuery = !query || `${shot.id}${shot.title}${shot.scene}${(shot.characters || []).join(" ")}`.toLowerCase().includes(query);
    return `<button class="shot-row ${index === state.activeIndex ? "is-active" : ""} ${visibleByFilter && visibleByQuery ? "" : "is-hidden"}" data-index="${index}">
      <span class="shot-thumb">${escapeHtml(shot.id.slice(1))}</span>
      <span class="shot-row-copy"><strong>${escapeHtml(shot.title)}</strong><small>${escapeHtml(shot.scene)} · ${escapeHtml(shot.duration)}s</small><em>${escapeHtml(draftFor(shot).generation_mode)}</em></span>
      <span class="shot-row-status ${status}"></span>
    </button>`;
  }).join("") : '<div class="shot-list-empty"><span>01</span><strong>等待生成分镜</strong><small>请从素材准备开始完成创作流程</small></div>';
  shotList.querySelectorAll(".shot-row").forEach((row) => row.addEventListener("click", () => selectShot(Number(row.dataset.index))));
  renderCounts();
}

function assetByPath(path) { return state.assets.find((asset) => asset.path === path); }

function mediaUrlForPath(path) {
  const asset = assetByPath(path);
  if (asset) return asset.url;
  return `/api/media?${new URLSearchParams({ run: state.runId, path })}`;
}

function openImageLightbox(url, title, meta = "") {
  const dialog = $("#imageLightbox");
  $("#imageLightboxPreview").src = url;
  $("#imageLightboxPreview").alt = `${title || "图片"}大图预览`;
  $("#imageLightboxTitle").textContent = title || "图片预览";
  $("#imageLightboxMeta").textContent = meta || "点击遮罩或按 Esc 关闭";
  dialog.showModal();
}

function closeImageLightbox() {
  const dialog = $("#imageLightbox");
  if (dialog.open) dialog.close();
  $("#imageLightboxPreview").removeAttribute("src");
}

function openVideoPreview(path, title, meta = "") {
  const dialog = $("#videoPreviewDialog");
  const player = $("#videoPreviewPlayer");
  player.src = mediaUrlForPath(path);
  $("#videoPreviewTitle").textContent = title || "视频预览";
  $("#videoPreviewMeta").textContent = meta || "可播放、暂停和拖动时间轴查看具体内容";
  dialog.showModal();
  player.focus();
}

function closeVideoPreview() {
  const dialog = $("#videoPreviewDialog");
  const player = $("#videoPreviewPlayer");
  player.pause();
  player.removeAttribute("src");
  player.load();
  if (dialog.open) dialog.close();
}

function renderAsset(role, path) {
  const prefix = role === "first" ? "first" : "last";
  const preview = $(`#${prefix}AssetPreview`);
  const asset = assetByPath(path);
  preview.querySelectorAll("img, .empty-asset").forEach((node) => node.remove());
  const content = document.createElement(asset ? "img" : "div");
  if (asset) {
    content.src = asset.url;
    content.alt = asset.label || asset.name;
    content.classList.add("is-zoomable");
    content.title = "点击放大查看";
    content.addEventListener("click", () => openImageLightbox(asset.url, asset.label || asset.name, `${role === "first" ? "首帧" : "尾帧"} · ${formatBytes(asset.size_bytes)}`));
  } else {
    content.className = "empty-asset";
    content.textContent = `点击选择${role === "first" ? "首帧" : "末帧"}`;
  }
  preview.prepend(content);
  $(`#${prefix}AssetSlot`).classList.toggle("is-filled", Boolean(asset));
  $(`#${prefix}AssetName`).textContent = (asset && (asset.label || asset.name)) || "尚未选择";
  $(`#${prefix}AssetMeta`).textContent = asset ? `${asset.role} · ${formatBytes(asset.size_bytes)}` : "从素材库选择";
  const clearButton = document.querySelector(`[data-clear-frame="${role}"]`);
  if (clearButton) clearButton.disabled = !path;
}

function crossShotCandidates(shot) {
  const currentIndex = state.shots.indexOf(shot);
  if (currentIndex <= 0) return [];
  const firstShotId = state.shots[0].id;
  const candidates = [];
  const firstLastFrameJob = state.jobs
    .filter((job) => job.shot_id === firstShotId && job.status === "completed" && job.last_frame)
    .sort((a, b) => String(b.completed_at || b.updated_at || "").localeCompare(String(a.completed_at || a.updated_at || "")))[0];
  if (firstLastFrameJob) {
    candidates.push({
      path: firstLastFrameJob.last_frame,
      name: firstLastFrameJob.last_frame.split("/").pop(),
      label: `${firstShotId} · 第一镜生成末帧`,
      role: "continuity",
      media_kind: "image",
      source_shot_id: firstShotId,
      asset_origin: "video_last_frame",
      url: mediaUrlForPath(firstLastFrameJob.last_frame),
    });
  }
  const priorShotIds = new Set(state.shots.slice(0, currentIndex).map((item) => item.id));
  state.assets
    .filter((asset) => asset.media_kind === "image" && asset.asset_origin === "ai_still" && priorShotIds.has(asset.source_shot_id))
    .sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0))
    .forEach((asset) => candidates.push(asset));
  const unique = new Map();
  candidates.forEach((asset) => unique.set(asset.path, asset));
  return [...unique.values()];
}

function renderCrossShotAssets() {
  const shot = currentShot();
  const section = $("#crossShotAssets");
  if (!shot || state.activeIndex <= 0) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  const candidates = crossShotCandidates(shot);
  $("#crossShotAssetsList").innerHTML = candidates.length ? candidates.map((asset, index) => `
    <div class="cross-shot-asset-card">
      <button type="button" class="cross-shot-preview" data-cross-preview="${index}"><img src="${escapeHtml(asset.url || mediaUrlForPath(asset.path))}" alt="${escapeHtml(asset.label || asset.name)}" loading="lazy" /></button>
      <div><strong>${escapeHtml(asset.label || asset.name)}</strong><small>${escapeHtml(asset.source_shot_id || "前序镜头")} · ${asset.asset_origin === "video_last_frame" ? "生成末帧" : "AI 生成图"}</small></div>
      <div class="cross-shot-actions"><button type="button" data-cross-role="first" data-cross-index="${index}">设为首帧</button><button type="button" data-cross-role="reference" data-cross-index="${index}">加入参考</button></div>
    </div>`).join("") : `<div class="cross-shot-empty">第一镜尚无已完成末帧，前序镜头也暂无 AI 生成图片；生成后会自动出现在这里。</div>`;
  document.querySelectorAll("[data-cross-preview]").forEach((button) => button.addEventListener("click", () => {
    const asset = candidates[Number(button.dataset.crossPreview)];
    openImageLightbox(asset.url || mediaUrlForPath(asset.path), asset.label || asset.name, "跨镜头承接素材");
  }));
  document.querySelectorAll("[data-cross-role]").forEach((button) => button.addEventListener("click", async () => {
    const asset = candidates[Number(button.dataset.crossIndex)];
    state.assetRole = button.dataset.crossRole;
    await chooseAsset(asset);
  }));
}

function formatBytes(value) {
  if (!value) return "图片素材";
  return value > 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(value / 1024)} KB`;
}

function uploadRemaining(kind) {
  const limit = state.uploadLimits[kind];
  return limit == null ? Infinity : Math.max(0, limit - state.uploadCounts[kind]);
}

function syncReferencePaths(draft) {
  draft.reference_images = draft.reference_image_bindings.map((item) => item.path);
}

function syncMediaPaths(draft) {
  if (!Array.isArray(draft.reference_video_bindings)) draft.reference_video_bindings = [];
  if (!Array.isArray(draft.reference_audio_bindings)) draft.reference_audio_bindings = [];
  draft.reference_videos = draft.reference_video_bindings.map((item) => item.path);
  draft.reference_audios = draft.reference_audio_bindings.map((item) => item.path);
}

function effectivePictureInputs(draft) {
  const items = [];
  const byPath = new Map();
  const add = (path, usage, metadata = {}) => {
    if (!path) return;
    let item = byPath.get(path);
    if (!item) {
      item = { path, usages: [], ...metadata };
      byPath.set(path, item);
      items.push(item);
    }
    Object.assign(item, metadata);
    if (!item.usages.includes(usage)) item.usages.push(usage);
  };
  if (["I2VA", "FL2VA", "Ref2VA"].includes(draft.generation_mode)) add(draft.first_frame, "first_frame");
  if (["FL2VA", "Ref2VA"].includes(draft.generation_mode)) add(draft.last_frame, "last_frame");
  if (draft.generation_mode === "Ref2VA") {
    draft.reference_image_bindings.forEach((item) => add(item.path, item.usage || "scene"));
    (draft.video_frame_bindings || []).forEach((item) => add(item.path, "keyframe", {
      sourceVideoFrame: true,
      sourceVideoIndex: Number(item.source_video_index || 0),
      sampleRatio: Number(item.sample_ratio || 0),
    }));
  }
  return items.map((item, index) => ({ ...item, picture: `<Picture ${index + 1}>` }));
}

function renderPromptPictureContract(draft) {
  const node = $("#promptPictureContract");
  if (!node) return;
  const labels = { first_frame: "首帧", last_frame: "尾帧", identity: "人物参考", scene: "场景参考", style: "风格参考", keyframe: "关键帧参考" };
  const items = effectivePictureInputs(draft);
  const videoLabels = { motion: "动作节奏", camera: "运镜", continuity: "连续性", style: "动态风格" };
  const audioLabels = { soundscape: "环境声", voice: "人声音色", action_sound: "动作音效", rhythm: "声音节奏" };
  const videos = draft.generation_mode === "Ref2VA" ? draft.reference_video_bindings : [];
  const audios = draft.generation_mode === "Ref2VA" ? draft.reference_audio_bindings : [];
  node.classList.toggle("is-empty", !items.length && !videos.length && !audios.length);
  node.innerHTML = items.length || videos.length || audios.length
    ? `<div class="picture-contract-head"><strong>本次提交的多模态素材顺序</strong><span>Prompt 与 H3 输入共用此编号</span></div><div class="picture-contract-items">${items.map((item) => {
      const asset = assetByPath(item.path);
      const name = (asset && (asset.label || asset.name)) || item.path.split("/").pop();
      const purpose = item.sourceVideoFrame
        ? `视频 ${item.sourceVideoIndex} 抽帧 · 约 ${Math.round(item.sampleRatio * 100)}% · 连续性参考`
        : item.usages.map((usage) => labels[usage] || usage).join(" + ");
      return `<div class="picture-contract-chip ${item.sourceVideoFrame ? "is-video" : ""}"><b>${escapeHtml(item.picture)}</b><span>${escapeHtml(purpose)}</span><small>${escapeHtml(name)}</small></div>`;
    }).join("")}${videos.map((item, index) => `<div class="picture-contract-chip is-video"><b>源视频 ${index + 1}</b><span>${escapeHtml(videoLabels[item.usage] || item.usage)}${item.note ? ` · ${escapeHtml(item.note)}` : ""}</span><small>仅用于抽帧，不直接提交 H3 · ${escapeHtml(item.path.split("/").pop())}</small></div>`).join("")}${audios.map((item, index) => `<div class="picture-contract-chip is-audio"><b>&lt;Audio ${index + 1}&gt;</b><span>${escapeHtml(audioLabels[item.usage] || item.usage)}${item.note ? ` · ${escapeHtml(item.note)}` : ""}</span><small>${escapeHtml(item.path.split("/").pop())}</small></div>`).join("")}</div>`
    : `<div class="picture-contract-empty">当前模式没有多模态输入；Prompt 不应引用 Picture、Video 或 Audio。</div>`;
}

function renderReferenceBinding(item, index) {
  const asset = assetByPath(item.path);
  const url = asset ? asset.url : mediaUrlForPath(item.path);
  const name = (asset && (asset.label || asset.name)) || item.path.split("/").pop();
  const usageOptions = item.usage === "keyframe" ? "" : `
    <select data-ref-usage="${index}" aria-label="参考图用途">
      <option value="identity" ${item.usage === "identity" ? "selected" : ""}>人物身份</option>
      <option value="scene" ${item.usage === "scene" ? "selected" : ""}>场景空间</option>
      <option value="style" ${item.usage === "style" ? "selected" : ""}>视觉风格</option>
    </select>`;
  return `<div class="reference-binding-chip ${item.usage === "keyframe" ? "is-keyframe" : ""}">
    <button class="reference-preview" type="button" data-preview-ref="${index}" title="点击放大查看"><img src="${escapeHtml(url)}" alt="${escapeHtml(name)}" loading="lazy" /><span>⌕</span></button>
    <div><strong>${escapeHtml(name)}</strong>${usageOptions || "<small>关键帧 · 动作与构图</small>"}</div>
    <button type="button" data-remove-ref="${index}" aria-label="移除参考图">×</button>
  </div>`;
}

function renderReferences(draft) {
  if (!Array.isArray(draft.reference_image_bindings)) draft.reference_image_bindings = [];
  syncReferencePaths(draft);
  syncMediaPaths(draft);
  const regular = draft.reference_image_bindings.map((item, index) => ({ item, index })).filter(({ item }) => item.usage !== "keyframe");
  const keyframes = draft.reference_image_bindings.map((item, index) => ({ item, index })).filter(({ item }) => item.usage === "keyframe");
  $("#referenceChips").innerHTML = regular.map(({ item, index }) => renderReferenceBinding(item, index)).join("");
  $("#keyframeReferenceChips").innerHTML = keyframes.map(({ item, index }) => renderReferenceBinding(item, index)).join("");
  const pictureCount = effectivePictureInputs(draft).length;
  $("#imageRefCount").textContent = draft.generation_mode === "Ref2VA" ? `${pictureCount} / 9 总图片` : `${draft.reference_image_bindings.length} 张已保留`;
  $("#keyframeRefCount").textContent = `${keyframes.length}`;
  document.querySelectorAll("[data-preview-ref]").forEach((button) => button.addEventListener("click", () => {
    const item = draft.reference_image_bindings[Number(button.dataset.previewRef)];
    const asset = assetByPath(item.path);
    const usageNames = { identity: "人物身份参考", scene: "场景空间参考", style: "视觉风格参考", keyframe: "关键帧动作/构图参考" };
    openImageLightbox(
      asset ? asset.url : mediaUrlForPath(item.path),
      (asset && (asset.label || asset.name)) || item.path.split("/").pop(),
      `${usageNames[item.usage] || "参考图"}${asset ? ` · ${formatBytes(asset.size_bytes)}` : ""}`,
    );
  }));
  document.querySelectorAll("[data-remove-ref]").forEach((button) => button.addEventListener("click", () => {
    const shot = currentShot();
    beginDraftEdit(shot, draft);
    draft.reference_image_bindings.splice(Number(button.dataset.removeRef), 1);
    syncReferencePaths(draft);
    renderReferences(draft);
    renderShotConstraints(shot, draft);
    markPromptStale(draft);
    persistDraft(shot, draft, { quiet: true });
  }));
  document.querySelectorAll("[data-ref-usage]").forEach((select) => select.addEventListener("change", () => {
    const shot = currentShot();
    beginDraftEdit(shot, draft);
    draft.reference_image_bindings[Number(select.dataset.refUsage)].usage = select.value;
    renderReferences(draft);
    renderShotConstraints(shot, draft);
    markPromptStale(draft);
    persistDraft(shot, draft, { quiet: true });
  }));
  renderMediaReferences(draft, "video");
  renderMediaReferences(draft, "audio");
  renderPromptPictureContract(draft);
}

function renderMediaReferences(draft, kind) {
  const key = kind === "video" ? "reference_video_bindings" : "reference_audio_bindings";
  const target = kind === "video" ? "#videoReferenceChips" : "#audioReferenceChips";
  const count = kind === "video" ? "#videoRefCount" : "#audioRefCount";
  const sampledCount = kind === "video" ? (draft.video_frame_bindings || []).length : 0;
  $(count).textContent = kind === "video"
    ? `${draft[key].length} / 3 · ${sampledCount ? `已抽 ${sampledCount} 帧` : "待抽帧"}`
    : `${draft[key].length} / 3`;
  const options = kind === "video"
    ? [["motion", "动作节奏"], ["camera", "运镜"], ["continuity", "连续性"], ["style", "动态风格"]]
    : [["soundscape", "环境声"], ["voice", "人声音色"], ["action_sound", "动作音效"], ["rhythm", "声音节奏"]];
  $(target).innerHTML = draft[key].map((item, index) => {
    const asset = assetByPath(item.path);
    const name = (asset && (asset.label || asset.name)) || item.path.split("/").pop();
    const preview = kind === "video"
      ? `<button class="media-preview-button" type="button" data-preview-video="${index}" aria-label="预览 ${escapeHtml(name)}"><span>▷</span><small>预览</small></button>`
      : `<span class="media-kind-mark">♫</span>`;
    const placeholder = kind === "video"
      ? "描述这个视频在当前镜头中的作用，例如：参考前 3 秒推镜和人物起身动作"
      : "描述这段音频在当前镜头中的作用，例如：沿用海浪环境声，降低人声权重";
    return `<div class="media-binding-chip ${kind === "video" ? "is-video" : "is-audio"}">${preview}<div class="media-binding-main"><strong>${escapeHtml(name)}</strong><div class="media-binding-controls"><select data-${kind}-usage="${index}" aria-label="${kind === "video" ? "参考视频" : "参考音频"}用途">${options.map(([value, label]) => `<option value="${value}" ${item.usage === value ? "selected" : ""}>${label}</option>`).join("")}</select><input data-${kind}-note="${index}" value="${escapeHtml(item.note || "")}" maxlength="500" placeholder="${placeholder}" aria-label="${kind === "video" ? "视频" : "音频"}作用描述" /></div></div><button class="media-remove-button" type="button" data-remove-${kind}="${index}" aria-label="移除">×</button></div>`;
  }).join("");
  if (kind === "video") document.querySelectorAll("[data-preview-video]").forEach((button) => button.addEventListener("click", () => {
    const item = draft.reference_video_bindings[Number(button.dataset.previewVideo)];
    const asset = item && assetByPath(item.path);
    if (item) openVideoPreview(item.path, (asset && (asset.label || asset.name)) || item.path.split("/").pop(), item.note || "当前镜头参考视频");
  }));
  document.querySelectorAll(`[data-remove-${kind}]`).forEach((button) => button.addEventListener("click", () => {
    const shot = currentShot();
    beginDraftEdit(shot, draft);
    draft[key].splice(Number(button.dataset[`remove${kind[0].toUpperCase()}${kind.slice(1)}`]), 1);
    syncMediaPaths(draft);
    renderMediaReferences(draft, kind);
    markPromptStale(draft);
    persistDraft(shot, draft);
  }));
  document.querySelectorAll(`[data-${kind}-usage]`).forEach((select) => select.addEventListener("change", () => {
    const shot = currentShot();
    beginDraftEdit(shot, draft);
    draft[key][Number(select.dataset[`${kind}Usage`])].usage = select.value;
    syncMediaPaths(draft);
    renderMediaReferences(draft, kind);
    markPromptStale(draft);
    persistDraft(shot, draft, { quiet: true });
  }));
  document.querySelectorAll(`[data-${kind}-note]`).forEach((input) => input.addEventListener("change", () => {
    const shot = currentShot();
    const item = draft[key][Number(input.dataset[`${kind}Note`])];
    if (!item) return;
    beginDraftEdit(shot, draft);
    item.note = input.value.trim();
    syncMediaPaths(draft);
    renderPromptPictureContract(draft);
    renderShotConstraints(shot, draft);
    markPromptStale(draft);
    persistDraft(shot, draft, { quiet: true });
    showToast(`${kind === "video" ? "视频" : "音频"}作用描述已保存；重新生成并优化后会写入 Prompt`);
  }));
  renderPromptPictureContract(draft);
}

function updateInputVisibility(mode) {
  $("#inputHint").textContent = MODE_HINTS[mode];
  const firstActive = ["I2VA", "FL2VA", "Ref2VA"].includes(mode);
  const lastActive = ["FL2VA", "Ref2VA"].includes(mode);
  const referenceActive = mode === "Ref2VA";
  $("#firstAssetSlot").classList.toggle("is-inactive-input", !firstActive);
  $("#lastAssetSlot").classList.toggle("is-inactive-input", !lastActive);
  $("#referenceStrip").classList.toggle("is-inactive-input", !referenceActive);
  $("#keyframeReferenceStrip").classList.toggle("is-inactive-input", !referenceActive);
  $("#videoReferenceStrip").classList.toggle("is-inactive-input", !referenceActive);
  $("#audioReferenceStrip").classList.toggle("is-inactive-input", !referenceActive);
  $("#firstAssetRequirement").textContent = mode === "Ref2VA" ? "可选 · 作为 0 秒首帧参考提交" : firstActive ? "当前模式必需" : "已保留 · 当前不提交";
  $("#lastAssetRequirement").textContent = mode === "Ref2VA" ? "可选 · 作为结束帧参考提交" : lastActive ? "当前模式必需" : "可选保存 · 当前不提交";
  $("#frameModeBadge").textContent = mode === "Ref2VA" ? "Ref2VA 会按人工用途提交时序帧" : firstActive ? `当前 ${mode} 会提交${lastActive ? "首帧与尾帧" : "首帧"}` : `当前 ${mode} 不提交时序帧`;
  $("#referenceModeBadge").textContent = referenceActive ? "当前 Ref2VA 会提交" : `已保留 · ${mode} 不提交`;
  $(".flow-arrow").style.display = "flex";
  document.querySelector(".asset-layout").style.gridTemplateColumns = "minmax(0,1fr) 68px minmax(0,1fr)";
}

function beginDraftEdit(shot, draft) {
  if (!shot || !draft) return;
  draft.locked = false;
  draft.approved = false;
  draft.dirty = true;
  draft.editVersion = Number(draft.editVersion || 0) + 1;
  $("#lockShot").checked = false;
  $("#confirmInputs").checked = false;
  $("#confirmContinuity").checked = false;
  promptText.contentEditable = "true";
  document.querySelectorAll(".mode-card").forEach((button) => { button.disabled = false; });
}

function markPromptStale(draft) {
  draft.promptStale = true;
  // Extracted frames belong to the exact source-video selection and usage
  // notes that produced the current prompt.  Any input edit invalidates them.
  draft.video_frame_bindings = [];
  draft.reference_video_strategy = "sampled_frames";
  const badge = $(".official-skill-badge");
  badge.textContent = "输入已变更";
  badge.classList.add("is-stale");
  renderPromptPictureContract(draft);
}

async function refreshOfficialPromptForMode(shot, draft) {
  const requestedMode = draft.generation_mode;
  const pendingAutoSave = draftSaveChains.get(shot.id);
  if (pendingAutoSave) await pendingAutoSave;
  const inputEditVersion = Number(draft.editVersion || 0);
  syncReferencePaths(draft);
  syncMediaPaths(draft);
  startAIProgress("prompt", `正在生成 ${shot.id} 最终 Prompt`, "LLM 将先理解分镜剧本与图片用途");
  try {
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/shots/${encodeURIComponent(shot.id)}/prompt/optimize`, {
      method: "POST",
      body: JSON.stringify({
        generation_mode: requestedMode,
        user_prompt: "",
        first_frame: draft.first_frame,
        last_frame: draft.last_frame,
        reference_images: draft.reference_images,
        reference_image_bindings: draft.reference_image_bindings,
        reference_videos: draft.reference_videos,
        reference_audios: draft.reference_audios,
        reference_video_bindings: draft.reference_video_bindings,
        reference_audio_bindings: draft.reference_audio_bindings,
        video_frame_bindings: draft.video_frame_bindings,
        reference_video_strategy: "sampled_frames",
      }),
    });
    if (
      currentShot() !== shot
      || draft.generation_mode !== requestedMode
      || Number(draft.editVersion || 0) !== inputEditVersion
    ) {
      return showToast("生成 Prompt 期间输入发生了变化，本次结果未覆盖当前编辑；请重新优化", true);
    }
    draft.prompt = body.prompt;
    draft.prompt_llm_draft = body.llm_draft || "";
    draft.prompt_skill = body.prompt_skill || "MiniMax H3 / h3-prompt-writing";
    draft.prompt_pipeline = body.pipeline || [];
    draft.prompt_optimized_at = body.prompt_optimized_at || new Date().toISOString();
    draft.video_frame_bindings = Array.isArray(body.video_frame_bindings) ? body.video_frame_bindings.map((item) => ({ ...item })) : [];
    draft.reference_video_strategy = body.reference_video_strategy || "sampled_frames";
    draft.promptStale = false;
    $(".official-skill-badge").textContent = "已优化";
    $(".official-skill-badge").classList.remove("is-stale");
    promptText.textContent = body.prompt;
    renderReferences(draft);
    renderShotConstraints(shot, draft);
    updateCount();
    await persistDraft(shot, draft, { quiet: true });
    finishAIProgress(`${shot.id} · LLM 初稿与官方 Skill 优化已完成`);
  } catch (error) {
    if (currentShot() !== shot || draft.generation_mode !== requestedMode) return;
    failAIProgress(error.message);
    showToast(`Prompt 重新生成并优化失败：${error.message}`, true);
  }
}

function setMode(mode, announce = true) {
  const shot = currentShot();
  if (!shot) return;
  const draft = draftFor(shot);
  const nextMode = MODE_LABELS[mode] || mode;
  const changed = nextMode !== draft.generation_mode;
  if (announce) beginDraftEdit(shot, draft);
  draft.generation_mode = nextMode;
  document.querySelectorAll(".mode-card").forEach((card) => card.classList.toggle("is-selected", card.dataset.mode === draft.generation_mode));
  updateInputVisibility(draft.generation_mode);
  renderAsset("first", draft.first_frame);
  renderAsset("last", draft.last_frame);
  renderReferences(draft);
  renderShotConstraints(shot, draft);
  if (announce) {
    showToast(`${shot.id} 已${changed ? "切换" : "重新打开"}为 ${draft.generation_mode} 编辑`);
    markPromptStale(draft);
    persistDraft(shot, draft, { quiet: true });
  }
  renderShots();
}

function selectShot(index) {
  if (!state.shots.length) return;
  state.activeIndex = Math.max(0, Math.min(state.shots.length - 1, index));
  const shot = currentShot();
  const draft = draftFor(shot);
  $("#activeShotId").textContent = shot.id;
  $("#activeShotTitle").textContent = shot.title;
  $("#activeScene").textContent = shot.scene;
  $("#activeDuration").textContent = `${shot.duration} 秒`;
  document.querySelector(".shot-meta span:last-child").textContent = `Beat ${shot.beat || "—"}`;
  const statusName = shotStatus(shot);
  const status = $("#activeStatus");
  status.textContent = statusName === "approved" ? "已批准" : statusName === "review" ? "待确认" : "未编排";
  status.className = `status-pill ${statusName === "approved" ? "status-approved" : "status-review"}`;
  $("#approveShot").textContent = `批准 ${shot.id} 并进入下一镜`;
  promptText.textContent = draft.prompt;
  promptText.contentEditable = draft.locked ? "false" : "true";
  $("#lockShot").checked = draft.locked;
  $("#confirmInputs").checked = draft.approved;
  $("#confirmContinuity").checked = draft.approved;
  $("#recommendationTitle").textContent = `系统建议：${MODE_LABELS[shot.suggested_mode] || shot.suggested_mode || "未提供"}`;
  $("#recommendationCopy").textContent = "这是原流水线的建议，仅用于辅助判断；人工选择会单独保存并优先执行。";
  $("#adoptRecommendation").dataset.mode = MODE_LABELS[shot.suggested_mode] || "T2VA";
  $(".official-skill-badge").textContent = draft.promptStale ? "输入已变更" : "已优化";
  $(".official-skill-badge").classList.toggle("is-stale", draft.promptStale);
  document.querySelectorAll(".mode-card").forEach((button) => { button.disabled = false; });
  setMode(draft.generation_mode, false);
  renderCrossShotAssets();
  updateCount();
  renderShots();
  renderShotContext();
  renderShotConstraints(shot, draft);
  renderJobs();
}

function decisionPayloadFor(shot, draft, approved) {
  syncReferencePaths(draft);
  syncMediaPaths(draft);
  const isCurrent = currentShot() === shot;
  const wantsLocked = isCurrent ? $("#lockShot").checked : Boolean(draft.locked);
  const previous = shot.decision || {};
  const changesLockedRevision = Boolean(previous.locked) && (
    draft.dirty
    || Boolean(approved) !== Boolean(previous.approved)
    || Boolean(wantsLocked) !== Boolean(previous.locked)
  );
  return {
    generation_mode: draft.generation_mode,
    first_frame: draft.first_frame,
    last_frame: draft.last_frame,
    reference_images: draft.reference_images,
    reference_image_bindings: draft.reference_image_bindings,
    reference_videos: draft.reference_videos,
    reference_audios: draft.reference_audios,
    reference_video_bindings: draft.reference_video_bindings,
    reference_audio_bindings: draft.reference_audio_bindings,
    video_frame_bindings: draft.video_frame_bindings || [],
    reference_video_strategy: draft.reference_video_strategy || "sampled_frames",
    prompt: (isCurrent ? promptText.textContent : draft.prompt).trim(),
    prompt_llm_draft: draft.prompt_llm_draft || "",
    prompt_skill: draft.prompt_skill || "",
    prompt_pipeline: draft.prompt_pipeline || [],
    prompt_optimized_at: draft.prompt_optimized_at || null,
    prompt_inputs_stale: Boolean(draft.promptStale),
    approved,
    locked: wantsLocked,
    seed: draft.seed,
    force_unlock: changesLockedRevision,
    skip_prompt_optimization: true,
  };
}

function collectDecision(approved) {
  const shot = currentShot();
  return decisionPayloadFor(shot, draftFor(shot), approved);
}

function persistDraft(shot, draft, { quiet = false } = {}) {
  if (!shot || !draft || !state.online) return Promise.resolve(null);
  const runId = state.runId;
  const previous = draftSaveChains.get(shot.id) || Promise.resolve();
  const task = previous.catch(() => null).then(async () => {
    // Snapshot only when this queued save actually starts. This prevents an
    // earlier input event from writing its old Prompt after a later Skill
    // optimization has already completed.
    const editVersion = Number(draft.editVersion || 0);
    const payload = decisionPayloadFor(shot, draft, false);
    const body = await api(`/api/runs/${encodeURIComponent(runId)}/shots/${encodeURIComponent(shot.id)}/decision`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    if (state.runId === runId) {
      shot.decision = body.decision;
      if (Number(draft.editVersion || 0) === editVersion) draft.dirty = false;
      renderShots();
      renderShotConstraints(shot, draft);
      if (!quiet) showToast(`${shot.id} 镜头输入已自动保存到本地`);
    }
    return body.decision;
  }).catch((error) => {
    showToast(`自动保存失败：${error.message}`, true);
    return null;
  }).finally(() => {
    if (draftSaveChains.get(shot.id) === task) draftSaveChains.delete(shot.id);
  });
  draftSaveChains.set(shot.id, task);
  return task;
}

async function saveCurrent(approved, moveNext = false) {
  const shot = currentShot();
  if (!shot || !state.online) return showToast("后端未连接，无法保存", true);
  if (approved && (!$("#confirmInputs").checked || !$("#confirmContinuity").checked)) {
    return showToast("批准前请勾选两项人工确认", true);
  }
  const button = $("#approveShot");
  const draft = draftFor(shot);
  const alreadyApprovedLocked = Boolean(shot.decision && shot.decision.approved) && Boolean(shot.decision && shot.decision.locked) && !draft.dirty;
  if (approved && moveNext && alreadyApprovedLocked) {
    selectShot(Math.min(state.activeIndex + 1, state.shots.length - 1));
    return shot.decision;
  }
  const promptSnapshot = promptText.textContent.trim();
  button.disabled = true;
  try {
    const pendingAutoSave = draftSaveChains.get(shot.id);
    if (pendingAutoSave) await pendingAutoSave;
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/shots/${encodeURIComponent(shot.id)}/decision`, {
      method: "PUT",
      body: JSON.stringify(collectDecision(approved)),
    });
    shot.decision = body.decision;
    // Generating the current shot must not tear down and recreate the editor:
    // that used to flash an older persisted prompt into the UI. Keep the exact
    // reviewed text in memory; only moving to another shot needs rehydration.
    draft.prompt = promptSnapshot;
    draft.prompt_llm_draft = body.decision.prompt_llm_draft || draft.prompt_llm_draft || "";
    draft.prompt_skill = body.decision.prompt_skill || draft.prompt_skill || "";
    draft.prompt_pipeline = body.decision.prompt_pipeline || draft.prompt_pipeline || [];
    draft.prompt_optimized_at = body.decision.prompt_optimized_at || draft.prompt_optimized_at || null;
    draft.video_frame_bindings = Array.isArray(body.decision.video_frame_bindings)
      ? body.decision.video_frame_bindings.map((item) => ({ ...item }))
      : (draft.video_frame_bindings || []);
    draft.reference_video_strategy = body.decision.reference_video_strategy || draft.reference_video_strategy || "sampled_frames";
    draft.promptStale = Boolean(body.decision.prompt_inputs_stale);
    draft.approved = Boolean(body.decision.approved);
    draft.locked = Boolean(body.decision.locked);
    draft.dirty = false;
    promptText.textContent = promptSnapshot;
    showToast(`${shot.id} ${approved ? "已批准" : "草稿已保存"}；未重新生成 Prompt`);
    renderShots();
    if (moveNext) {
      shot.draft = null;
      selectShot(Math.min(state.activeIndex + 1, state.shots.length - 1));
    } else {
      renderShotConstraints(shot, draft);
      renderJobs();
      updateCount();
    }
    return body.decision;
  } catch (error) {
    showToast(error.message, true);
    return null;
  } finally {
    button.disabled = false;
  }
}

function openAssetLibrary(role) {
  state.assetRole = role;
  const labels = { first: "选择首帧", last: "选择尾帧", reference: "选择人物 / 场景 / 风格参考图（最多 9 张）", keyframe: "选择关键帧参考图（与其他参考图合计最多 9 张）", video: "选择参考视频（最多 3 个）", audio: "选择参考音频（最多 3 个）" };
  $("#assetRoleHint").textContent = labels[role];
  const requiredKind = role === "video" ? "video" : role === "audio" ? "audio" : "image";
  $("#assetGallery").classList.toggle("is-media-gallery", requiredKind !== "image");
  const uploadInput = $("#assetUpload");
  uploadInput.dataset.kind = requiredKind;
  $("#assetUploadLabel").textContent = `上传${requiredKind === "image" ? "图片" : requiredKind === "video" ? "视频" : "音频"}`;
  uploadInput.accept = requiredKind === "image" ? "image/png,image/jpeg,image/webp" : requiredKind === "video" ? "video/mp4,video/webm,video/quicktime,video/x-matroska" : "audio/wav,audio/mpeg,audio/flac,audio/mp4,audio/ogg,audio/aac";
  const choices = state.assets.map((asset, index) => ({ asset, index })).filter(({ asset }) => {
    if (asset.media_kind !== requiredKind) return false;
    if (requiredKind !== "video") return true;
    if (asset.asset_origin === "upload" || asset.role === "upload") return true;
    if (asset.asset_origin !== "generated_video" || !asset.source_shot_id) return false;
    const sourceIndex = state.shots.findIndex((item) => item.id === asset.source_shot_id);
    return sourceIndex >= 0 && sourceIndex < state.activeIndex;
  });
  $("#assetGallery").innerHTML = choices.length ? choices.map(({ asset, index }) => {
    const title = requiredKind === "video" ? (asset.asset_origin === "generated_video" ? `${asset.source_shot_id} · 前序镜头生成视频` : "用户上传视频") : (asset.label || asset.name);
    if (requiredKind === "image") return `<button class="asset-choice" data-asset-index="${index}"><img src="${escapeHtml(asset.url)}" alt="${escapeHtml(asset.label || asset.name)}" loading="lazy" /><strong>${escapeHtml(title)}</strong><small>${escapeHtml(asset.relative_path || asset.role)}</small></button>`;
    return `<article class="asset-choice asset-choice-media">
      ${requiredKind === "video" ? `<video src="${escapeHtml(asset.url)}" controls preload="metadata" playsinline aria-label="预览 ${escapeHtml(title)}"></video>` : `<audio src="${escapeHtml(asset.url)}" controls preload="metadata" aria-label="试听 ${escapeHtml(title)}"></audio>`}
      <strong>${escapeHtml(title)}</strong><small>${escapeHtml(asset.relative_path || asset.role)}</small>
      <button class="asset-use-button" type="button" data-asset-index="${index}">使用此${requiredKind === "video" ? "视频" : "音频"}</button>
    </article>`;
  }).join("") : `<p>${requiredKind === "video" ? "暂无可用视频；这里只显示用户上传视频和当前镜头之前生成的视频。" : `当前 run 暂无可用${requiredKind}素材，可先上传。`}</p>`;
  document.querySelectorAll("[data-asset-index]").forEach((button) => button.addEventListener("click", () => chooseAsset(state.assets[Number(button.dataset.assetIndex)])));
  $("#assetDialog").showModal();
}

async function chooseAsset(asset) {
  const shot = currentShot();
  const draft = draftFor(shot);
  const selectedRole = state.assetRole;
  if (!state.assets.some((item) => item.path === asset.path)) state.assets.unshift(asset);
  beginDraftEdit(shot, draft);
  if (["first", "last"].includes(selectedRole) && draft.generation_mode === "Ref2VA") {
    const otherFrame = selectedRole === "first" ? draft.last_frame : draft.first_frame;
    const count = new Set([asset.path, otherFrame, ...draft.reference_image_bindings.map((item) => item.path)].filter(Boolean)).size;
    if (count > 9) return showToast("Ref2VA 的首帧、尾帧和参考图合计最多 9 张", true);
  }
  if (selectedRole === "first") draft.first_frame = asset.path;
  else if (selectedRole === "last") draft.last_frame = asset.path;
  else if (["reference", "keyframe"].includes(selectedRole)) {
    const existing = draft.reference_image_bindings.find((item) => item.path === asset.path);
    if (existing) existing.usage = selectedRole === "keyframe" ? "keyframe" : (existing.usage || "scene");
    else if (new Set([draft.first_frame, draft.last_frame, ...draft.reference_image_bindings.map((item) => item.path), asset.path].filter(Boolean)).size <= 9) {
      draft.reference_image_bindings.push({ path: asset.path, usage: selectedRole === "keyframe" ? "keyframe" : "scene", character_ids: [], note: "" });
    } else return showToast("单个 H3 镜头的首帧、尾帧和参考图合计最多 9 张", true);
    syncReferencePaths(draft);
  }
  if (selectedRole === "video" && !draft.reference_video_bindings.some((item) => item.path === asset.path) && draft.reference_video_bindings.length < 3) {
    draft.reference_video_bindings.push({ path: asset.path, usage: "motion", note: "" });
  }
  if (selectedRole === "audio" && !draft.reference_audio_bindings.some((item) => item.path === asset.path) && draft.reference_audio_bindings.length < 3) {
    draft.reference_audio_bindings.push({ path: asset.path, usage: "soundscape", note: "" });
  }
  syncMediaPaths(draft);
  if ($("#assetDialog").open) $("#assetDialog").close();
  renderAsset("first", draft.first_frame);
  renderAsset("last", draft.last_frame);
  renderReferences(draft);
  renderShots();
  renderShotConstraints(shot, draft);
  renderCrossShotAssets();
  markPromptStale(draft);
  await persistDraft(shot, draft, { quiet: true });
  showToast(`${shot.id} 素材绑定已保存；请点击“重新生成并优化”更新 Prompt`);
}

function openAIImageDialog() {
  const shot = currentShot();
  if (!shot) return;
  const draft = draftFor(shot);
  const generator = state.imageGenerator || {};
  if (!generator.enabled || !generator.credential_ready) {
    return showToast(generator.enabled ? "AI 生图 API Key 未就绪" : "项目尚未启用 image_generator", true);
  }
  const roleOptions = [
    ["first", "指定为首帧（I2VA / FL2VA）"],
    ["last", "指定为尾帧（FL2VA）"],
    ["keyframe", "加入关键帧参考（Ref2VA）"],
    ["reference_identity", "加入人物身份参考（Ref2VA）"],
    ["reference_scene", "加入场景空间参考（Ref2VA）"],
    ["reference_style", "加入视觉风格参考（Ref2VA）"],
    ["library", "仅保存到当前素材库"],
  ];
  $("#aiImageRole").innerHTML = roleOptions.map(([value, label]) => `<option value="${value}">${label}</option>`).join("");
  $("#aiImageModelHint").textContent = `${generator.model || "已配置模型"} · ${generator.size || "按项目画幅"}`;
  $("#aiImagePrompt").value = [shot.composition, shot.action_timeline].filter(Boolean).join("。") || shot.title;
  $("#aiImagePromptCount").textContent = $("#aiImagePrompt").value.length;
  $("#aiImageDialog").showModal();
}

async function generateAIImage() {
  const shot = currentShot();
  if (!shot) return;
  const prompt = $("#aiImagePrompt").value.trim();
  const role = $("#aiImageRole").value;
  if (prompt.length < 4) return showToast("请写下至少 4 个字符的画面描述", true);
  const button = $("#generateAIImage");
  button.disabled = true;
  button.textContent = "AI 正在生成…";
  startAIProgress("image", `正在为 ${shot.id} 生成图片`, "图片会保存到当前项目本地");
  try {
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/shots/${encodeURIComponent(shot.id)}/images/generate`, {
      method: "POST",
      body: JSON.stringify({ prompt, role }),
    });
    const asset = body.asset;
    state.assets.unshift(asset);
    if (role !== "library") {
      const draft = draftFor(shot);
      beginDraftEdit(shot, draft);
      if (role === "first") draft.first_frame = asset.path;
      else if (role === "last") draft.last_frame = asset.path;
      else if (["reference", "reference_identity", "reference_scene", "reference_style", "keyframe"].includes(role)) {
        if (new Set([draft.first_frame, draft.last_frame, ...draft.reference_image_bindings.map((item) => item.path), asset.path].filter(Boolean)).size > 9) throw new Error("当前镜头的图片输入已达 9 张；图片已保存到素材库，但未自动绑定");
        const usage = role === "keyframe" ? "keyframe" : role.replace("reference_", "").replace("reference", "scene");
        draft.reference_image_bindings.push({ path: asset.path, usage, character_ids: [], note: "AI 生图并由用户指定用途" });
        syncReferencePaths(draft);
      }
      renderAsset("first", draft.first_frame);
      renderAsset("last", draft.last_frame);
      renderReferences(draft);
      renderShots();
      renderShotConstraints(shot, draft);
    }
    renderMaterialLibrary();
    renderCrossShotAssets();
    $("#aiImageDialog").close();
    finishAIProgress(`${shot.id} AI 图片已生成并保存`);
    const currentDraft = draftFor(shot);
    if (role !== "library") {
      markPromptStale(currentDraft);
      await persistDraft(shot, currentDraft, { quiet: true });
    }
    showToast(role === "library" ? "AI 图片已保存到当前素材库" : "AI 图片已绑定并保存；请重新生成并优化 Prompt");
  } catch (error) {
    failAIProgress(error.message);
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "✦ 生成并使用图片";
  }
}

async function uploadAsset(file) {
  if (!file) return;
  const kind = $("#assetUpload").dataset.kind || "image";
  if (uploadRemaining(kind) <= 0) return showToast(`${kind} 上传额度已满`, true);
  try {
    const query = new URLSearchParams({ filename: file.name, kind });
    const response = await fetch(`/api/runs/${encodeURIComponent(state.runId)}/assets?${query}`, {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.message || `上传失败 (${response.status})`);
    state.assets.unshift(body.asset);
    state.uploadCounts[kind] += 1;
    renderMaterialLibrary();
    chooseAsset(body.asset);
    showToast(`${kind === "image" ? "图片" : kind === "video" ? "视频" : "音频"}已上传并选中`);
  } catch (error) { showToast(error.message, true); }
  $("#assetUpload").value = "";
}

async function uploadMaterial(file, kind) {
  const query = new URLSearchParams({ filename: file.name, kind });
  const response = await fetch(`/api/runs/${encodeURIComponent(state.runId)}/assets?${query}`, {
    method: "POST",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
  });
  let body = {};
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) throw new Error(body.message || `${file.name} 上传失败`);
  state.assets.unshift(body.asset);
  state.uploadCounts[kind] += 1;
}

async function handleMaterialUpload(input) {
  const kind = input.dataset.uploadKind;
  const files = [...input.files];
  const remaining = uploadRemaining(kind);
  if (!files.length) return;
  if (files.length > remaining) {
    input.value = "";
    return showToast(`${kind} 还可上传 ${remaining} 个，请减少选择数量`, true);
  }
  setConnection(true, `正在上传 ${files.length} 个${kind}素材`);
  try {
    for (const file of files) await uploadMaterial(file, kind);
    renderMaterialLibrary();
    showToast(`${files.length} 个素材已上传`);
  } catch (error) {
    showToast(error.message, true);
    await reloadAssets();
  } finally {
    input.value = "";
    setConnection(true, "Studio 与 H3 已就绪");
  }
}

function renderUploadLimits() {
  for (const kind of ["image", "video", "audio"]) {
    const limit = state.uploadLimits[kind];
    const isFull = limit != null && state.uploadCounts[kind] >= limit;
    $(`#${kind}UploadCount`).textContent = `${state.uploadCounts[kind]} / ${limit == null ? "不限" : limit}`;
    const card = document.querySelector(`.upload-card[data-kind="${kind}"]`);
    card.classList.toggle("is-full", isFull);
    card.querySelector("em").textContent = isFull ? "额度已满" : "选择文件";
  }
  const fixedCount = state.assets.filter((asset) => asset.role !== "upload").length;
  const uploadedCount = state.assets.length - fixedCount;
  const summary = document.querySelector('[data-stage="素材准备"] .stage-copy small');
  if (summary) summary.textContent = `${fixedCount} 项固定 · ${uploadedCount} 项上传`;
}

function materialPreview(asset) {
  if (asset.media_kind === "image") return `<img src="${escapeHtml(asset.url)}" alt="${escapeHtml(asset.label || asset.name)}" loading="lazy" />`;
  return asset.media_kind === "video" ? "▷" : "♫";
}

function renderMaterialLibrary() {
  renderUploadLimits();
  if ($("#assetPreparationView").classList.contains("is-hidden")) return;
  const query = ($("#assetSearch").value || "").trim().toLowerCase();
  const assets = state.assets.filter((asset) => {
    const filterMatch = state.materialFilter === "all" || (state.materialFilter === "upload" ? asset.role === "upload" : asset.media_kind === state.materialFilter);
    return filterMatch && (!query || `${asset.label}${asset.name}${asset.relative_path}`.toLowerCase().includes(query));
  });
  const labels = { image: "图片", video: "视频", audio: "音频" };
  $("#materialGrid").innerHTML = assets.length ? assets.map((asset) => {
    const selectedAsInspiration = asset.media_kind === "image" && state.inspirationImagePaths.includes(asset.path);
    return `<article class="material-item ${selectedAsInspiration ? "is-selected" : ""}">
      <div class="material-visual ${escapeHtml(asset.media_kind)}">${materialPreview(asset)}<span class="material-type">${escapeHtml(labels[asset.media_kind])}</span></div>
      <div class="material-copy"><strong>${escapeHtml(asset.label || asset.name)}</strong><small>${asset.role === "upload" ? "人工上传" : "原流水线 · 固定"} · ${formatBytes(asset.size_bytes)}</small>${asset.media_kind === "image" ? `<button class="material-use ${selectedAsInspiration ? "is-selected" : ""}" data-use-material="${escapeHtml(asset.path)}">${selectedAsInspiration ? "✓ 已选作灵感" : "＋ 用作灵感"}</button>` : `<a class="material-open" href="${escapeHtml(asset.url)}" target="_blank">打开预览 ↗</a>`}</div>
    </article>`;
  }).join("") : '<div class="material-empty">当前筛选下没有素材</div>';
  document.querySelectorAll("[data-use-material]").forEach((button) => button.addEventListener("click", () => {
    toggleInspirationImage(button.dataset.useMaterial);
  }));
}

async function reloadAssets() {
  const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/assets`);
  state.assets = body.assets;
  state.uploadLimits = body.upload_limits || state.uploadLimits;
  state.uploadCounts = body.upload_counts || state.uploadCounts;
  renderMaterialLibrary();
}

function showStage(stage) {
  const views = {
    素材准备: ["#assetPreparationView", "assets"],
    画面理解: ["#descriptionsView", "descriptions"],
    故事规划: ["#storyView", "story"],
    分镜拆分: ["#shotsView", "shots"],
    人工编排: ["#orchestrationView", "orchestration"],
    字幕校对: ["#subtitleView", "subtitles"],
    合片验收: ["#assembleView", "assemble"],
  };
  if (!views[stage]) {
    return showToast(`未知页面：${stage}`, true);
  }
  document.querySelectorAll(".stage-view").forEach((view) => view.classList.add("is-hidden"));
  $(views[stage][0]).classList.remove("is-hidden");
  document.querySelectorAll(".stage-item").forEach((item) => item.classList.toggle("is-active", item.dataset.stage === stage));
  window.location.hash = views[stage][1];
  if (stage === "素材准备") renderMaterialLibrary();
  if (stage === "画面理解") renderDescriptions();
  if (stage === "故事规划") renderStoryPlan();
  if (stage === "分镜拆分") renderShotPlan();
  if (stage === "人工编排") { renderShots(); selectShot(state.activeIndex); }
  if (stage === "字幕校对") loadSubtitles();
  if (stage === "合片验收") loadAssemble();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function toggleInspirationImage(path) {
  const index = state.inspirationImagePaths.indexOf(path);
  if (index >= 0) state.inspirationImagePaths.splice(index, 1);
  else state.inspirationImagePaths.push(path);
  renderInspirationImages();
  renderMaterialLibrary();
}

function renderInspirationImages() {
  const selectedCount = state.inspirationImagePaths.length;
  $("#materialSelectionCount").textContent = selectedCount ? `已选择 ${selectedCount} 张灵感图片` : "尚未选择灵感图片";
  $("#saveImageInspiration").disabled = selectedCount === 0;
  $("#saveImageInspiration").textContent = selectedCount ? `确认已选 ${selectedCount} 张图片并继续` : "请至少选择 1 张图片";
}

function proposalText(value) {
  if (Array.isArray(value)) return value.join("、");
  if (value && typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value || "");
}

function renderProposal() {
  const item = state.inspiration && state.inspiration.current_proposal;
  if (!item || !item.proposal) {
    $("#inspirationResult").classList.add("is-hidden");
    return;
  }
  const proposal = item.proposal;
  $("#proposalTitle").textContent = proposal.title || "未命名剧情提案";
  $("#proposalMode").textContent = item.mode === "polish" ? "基于你的灵感润色" : "AI 从零生成";
  $("#proposalLogline").textContent = proposalText(proposal.logline);
  $("#proposalGenre").textContent = `${proposalText(proposal.genre)} · ${proposalText(proposal.tone)}`;
  $("#proposalHook").textContent = proposalText(proposal.hook);
  $("#proposalOutline").textContent = proposalText(proposal.story_outline);
  $("#proposalEnding").textContent = proposalText(proposal.ending);
  $("#proposalSavePath").textContent = (item.local_files && item.local_files.latest_markdown) || "02_story/studio_drafts/latest.md";
  $("#inspirationResult").classList.remove("is-hidden");
}

function renderInspiration() {
  const llmReady = state.llm && state.llm.configured && state.llm.credential_ready;
  $("#llmReady").classList.toggle("is-ready", Boolean(llmReady));
  $("#llmReady").lastChild.textContent = llmReady ? `${state.llm.model} 已就绪` : "LLM 凭据未就绪";
  state.inspirationImagePaths = [...((state.inspiration && state.inspiration.selected_images) || state.inspirationImagePaths)];
  renderInspirationImages();
  renderMaterialLibrary();
  renderProposal();
}

async function uploadIdeaImages(input) {
  const files = [...input.files];
  if (!files.length) return;
  const imageUploadRemaining = uploadRemaining("image");
  if (files.length > imageUploadRemaining) {
    input.value = "";
    return showToast(`图片上传额度只剩 ${imageUploadRemaining} 张`, true);
  }
  try {
    for (const file of files) {
      const query = new URLSearchParams({ filename: file.name, kind: "image" });
      const response = await fetch(`/api/runs/${encodeURIComponent(state.runId)}/assets?${query}`, {
        method: "POST",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.message || `${file.name} 上传失败`);
      state.assets.unshift(body.asset);
      state.uploadCounts.image += 1;
    }
    renderMaterialLibrary();
    renderInspirationImages();
    showToast("图片已上传到当前素材库，请在下方手动选择");
  } catch (error) {
    showToast(error.message, true);
    await reloadAssets();
  } finally { input.value = ""; }
}

async function saveImageInspiration({ advance = false } = {}) {
  try {
    if (!state.inspirationImagePaths.length) {
      showToast("请至少选择 1 张图片作为灵感", true);
      return false;
    }
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/inspiration`, {
      method: "PUT",
      body: JSON.stringify({ action: "select_images", image_paths: state.inspirationImagePaths }),
    });
    state.inspiration = body.inspiration;
    showToast(`${state.inspirationImagePaths.length} 张灵感图已保存`);
    if (advance) showStage("画面理解");
    return true;
  } catch (error) {
    showToast(error.message, true);
    return false;
  }
}

async function continueToDescriptions() {
  if (!state.inspirationImagePaths.length && !((state.inspiration && state.inspiration.selected_images) || []).length) {
    return showToast("请先在素材库勾选要用的灵感图", true);
  }
  if (state.inspirationImagePaths.length) {
    await saveImageInspiration({ advance: true });
    return;
  }
  showStage("画面理解");
}

function workflowEntry(stage) {
  return state.workflow[stage] || { document: null, status: "not_approved" };
}

function workflowStatusLabel(entry) {
  if (!entry.document) return "尚未生成";
  return entry.status === "approved" ? "已批准" : entry.status.startsWith("stale") ? "内容已修改 · 待重新批准" : "草稿 · 待批准";
}

function renderWorkflowNav() {
  const descriptions = workflowEntry("descriptions");
  const story = workflowEntry("story");
  const shots = workflowEntry("shots");
  const descCount = ((descriptions.document || {}).images || []).length;
  const beats = ((story.document || {}).beats || []);
  const storyDuration = beats.reduce((sum, item) => sum + Number(item.duration_s || 0), 0);
  const shotItems = ((shots.document || {}).shots || []);
  const rows = [
    ["画面理解", descCount ? `${descCount} 张图片 · ${workflowStatusLabel(descriptions)}` : "等待生成事实描述", descriptions],
    ["故事规划", beats.length ? `${beats.length} Beats · ${storyDuration} 秒` : "等待画面理解批准", story],
    ["分镜拆分", shotItems.length ? `${shotItems.length} 个镜头 · ${workflowStatusLabel(shots)}` : "等待故事规划批准", shots],
  ];
  rows.forEach(([stage, summary, entry]) => {
    const button = document.querySelector(`.stage-item[data-stage="${stage}"]`);
    if (!button) return;
    button.querySelector("small").textContent = summary;
    button.querySelector(".stage-status").textContent = entry.status === "approved" ? "✓" : entry.document ? "•" : "—";
    button.classList.toggle("is-done", entry.status === "approved");
  });
  for (const stage of ["descriptions", "story", "shots"]) {
    const entry = workflowEntry(stage);
    const node = $(`#${stage}Status`);
    node.textContent = workflowStatusLabel(entry);
    node.classList.toggle("is-approved", entry.status === "approved");
    node.classList.toggle("is-draft", Boolean(entry.document) && entry.status !== "approved");
  }
}

function listRowMarkup(name, value, placeholder, locked) {
  return `<div class="list-field-row">
    <input data-list-item="${name}" value="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}" ${locked ? "disabled" : ""} />
    <button class="list-field-remove" type="button" data-list-remove aria-label="删除这一条">×</button>
  </div>`;
}

function listFieldMarkup(name, items, options = {}) {
  const { tags = false, locked = false, placeholder = "", addLabel = "添加一条" } = options;
  const values = (Array.isArray(items) ? items : []).map((item) => String(item ?? ""));
  const rows = (values.length ? values : [""]).map((value) => listRowMarkup(name, value, placeholder, locked)).join("");
  return `<div class="list-field${tags ? " is-tags" : ""}${locked ? " is-locked" : ""}" data-list-field="${name}" data-list-placeholder="${escapeHtml(placeholder)}">
    <div class="list-field-rows">${rows}</div>
    <button class="list-field-add" type="button" data-list-add>＋ ${escapeHtml(addLabel)}</button>
  </div>`;
}

function listFieldValues(name) {
  return Array.from(document.querySelectorAll(`[data-list-item="${name}"]`)).map((input) => input.value.trim()).filter(Boolean);
}

function bindListFields(root) {
  (root || document).querySelectorAll("[data-list-field]").forEach((field) => {
    const name = field.dataset.listField;
    const placeholder = field.dataset.listPlaceholder || "";
    const rows = field.querySelector(".list-field-rows");
    const focusEnd = (input) => { input.focus(); input.setSelectionRange(input.value.length, input.value.length); };
    const insertRow = (after) => {
      const holder = document.createElement("div");
      holder.innerHTML = listRowMarkup(name, "", placeholder, false);
      const row = holder.firstElementChild;
      if (after) after.after(row); else rows.append(row);
      row.querySelector("input").focus();
    };
    const dropRow = (row) => {
      const neighbour = row.previousElementSibling || row.nextElementSibling;
      row.remove();
      if (!rows.children.length) insertRow(null);
      else if (neighbour) focusEnd(neighbour.querySelector("input"));
    };
    field.querySelector("[data-list-add]").addEventListener("click", () => insertRow(null));
    rows.addEventListener("click", (event) => {
      const button = event.target.closest("[data-list-remove]");
      if (button) dropRow(button.closest(".list-field-row"));
    });
    rows.addEventListener("keydown", (event) => {
      const input = event.target.closest("[data-list-item]");
      if (!input) return;
      if (event.key === "Enter") {
        event.preventDefault();
        insertRow(input.closest(".list-field-row"));
      } else if (event.key === "Backspace" && !input.value && rows.children.length > 1) {
        event.preventDefault();
        dropRow(input.closest(".list-field-row"));
      }
    });
  });
}

function captureDescriptionDraft() {
  const document = workflowEntry("descriptions").document;
  const item = document && document.images && document.images[state.activeDescriptionIndex];
  if (!item || !$("#descSetting")) return;
  item.setting = $("#descSetting").value.trim();
  item.mood_or_atmosphere = $("#descMood").value.trim();
  item.visible_facts = listFieldValues("descFacts");
  item.objects = listFieldValues("descObjects");
  item.uncertainties = listFieldValues("descUncertainties");
  const peopleText = $("#descPeople").value.trim();
  item.people = peopleText ? JSON.parse(peopleText) : [];
}

function selectedInspirationPaths() {
  const saved = (state.inspiration && state.inspiration.selected_images) || [];
  const local = state.inspirationImagePaths || [];
  return [...new Set((saved.length ? saved : local).filter(Boolean))];
}

function sameInspirationPath(left, right) {
  if (!left || !right) return false;
  if (left === right) return true;
  const name = (value) => String(value).split("/").pop();
  const leftName = name(left);
  const rightName = name(right);
  if (leftName === rightName) return true;
  const stem = (value) => value.replace(/_processed\.[^.]+$/i, "").replace(/^IMG\d+_/, "").replace(/\.[^.]+$/, "");
  const leftStem = stem(leftName);
  const rightStem = stem(rightName);
  return Boolean(leftStem) && (leftStem === rightStem || leftName.includes(rightStem) || rightName.includes(leftStem));
}

function renderDescriptions() {
  const entry = workflowEntry("descriptions");
  const describedImages = ((entry.document || {}).images || []);
  const selectedPaths = selectedInspirationPaths();
  const images = selectedPaths.length
    ? selectedPaths.map((path, index) => {
        const existing = describedImages.find((item) => sameInspirationPath(item.source_path, path));
        return existing
          ? { ...existing, source_path: path, image_id: existing.image_id || `IMG${String(index + 1).padStart(2, "0")}` }
          : {
              image_id: `IMG${String(index + 1).padStart(2, "0")}`,
              source_path: path,
              visible_facts: [], people: [], objects: [], uncertainties: [],
            };
      })
    : [];
  state.activeDescriptionIndex = Math.max(0, Math.min(state.activeDescriptionIndex, images.length - 1));
  const item = images[state.activeDescriptionIndex];
  if (!item) {
    $("#descriptionEditor").innerHTML = '<div class="planning-empty">请先在素材库勾选灵感图。画面理解只会处理你选中的图片，不会带上素材库其余图片。</div>';
    renderWorkflowNav();
    return;
  }
  const hasDescription = Boolean(describedImages.length);
  const sourceName = String(item.source_path || "").split("/").pop();
  $("#descriptionEditor").innerHTML = `<div class="description-switcher">
      <button class="description-arrow" id="previousDescription" ${state.activeDescriptionIndex === 0 ? "disabled" : ""} aria-label="上一张">←</button>
      <div><span class="panel-kicker">FRAME ${String(state.activeDescriptionIndex + 1).padStart(2, "0")}</span><strong>${escapeHtml(item.image_id)}</strong><small>${escapeHtml(sourceName)} · ${state.activeDescriptionIndex + 1} / ${images.length}</small></div>
      <div class="description-dots">${images.map((_, index) => `<button class="${index === state.activeDescriptionIndex ? "is-active" : ""}" data-description-index="${index}" aria-label="第${index + 1}张"></button>`).join("")}</div>
      <button class="description-arrow" id="nextDescription" ${state.activeDescriptionIndex === images.length - 1 ? "disabled" : ""} aria-label="下一张">→</button>
    </div>
    <div class="description-focus">
      <section class="description-canvas"><img src="${escapeHtml(mediaUrlForPath(item.source_path))}" alt="${escapeHtml(item.image_id)}" /><div class="image-caption"><span>${hasDescription ? "模型描述已生成" : "等待模型理解"}</span><strong>${escapeHtml(item.setting || "已选灵感素材")}</strong></div></section>
      <section class="description-fields-wrap">
        <div class="description-section-title"><span>FACT SHEET</span><h2>${hasDescription ? "当前画面事实" : "生成后在这里确认描述"}</h2><p>${hasDescription ? "所有字段均可人工修正，切换图片时保留当前修改。" : "图片已正确载入；点击页面上方“生成画面描述”调用多模态模型。"}</p></div>
        <div class="editor-fields">
          <label class="editor-field"><span>场景事实</span><input id="descSetting" value="${escapeHtml(item.setting || "")}" placeholder="地点、时间、天气" ${hasDescription ? "" : "disabled"} /></label>
          <label class="editor-field"><span>光线与氛围</span><input id="descMood" value="${escapeHtml(item.mood_or_atmosphere || "")}" placeholder="光线方向、色调、情绪" ${hasDescription ? "" : "disabled"} /></label>
          <div class="editor-field wide"><span>可见事实<em class="field-hint">一条一行，只写画面里看得见的内容</em></span>
            ${listFieldMarkup("descFacts", item.visible_facts, { locked: !hasDescription, placeholder: "例如：两个男孩同乘一辆粉色自行车经过桥面", addLabel: "添加一条事实" })}
          </div>
          <div class="editor-field"><span>关键物体</span>
            ${listFieldMarkup("descObjects", item.objects, { tags: true, locked: !hasDescription, placeholder: "物体名称", addLabel: "添加物体" })}
          </div>
          <div class="editor-field"><span>不确定信息</span>
            ${listFieldMarkup("descUncertainties", item.uncertainties, { locked: !hasDescription, placeholder: "画面无法确认的细节", addLabel: "添加一条" })}
          </div>
          <label class="editor-field wide"><span>人物结构<em class="field-badge">json</em></span><textarea class="field-code" id="descPeople" ${hasDescription ? "" : "disabled"}>${escapeHtml(JSON.stringify(item.people || [], null, 2))}</textarea></label>
        </div>
      </section>
    </div>`;
  const selectDescription = (index) => {
    try { captureDescriptionDraft(); } catch (error) { return showToast(`人物 JSON 格式错误：${error.message}`, true); }
    state.activeDescriptionIndex = index;
    renderDescriptions();
  };
  bindListFields($("#descriptionEditor"));
  $("#previousDescription").addEventListener("click", () => selectDescription(state.activeDescriptionIndex - 1));
  $("#nextDescription").addEventListener("click", () => selectDescription(state.activeDescriptionIndex + 1));
  document.querySelectorAll("[data-description-index]").forEach((button) => button.addEventListener("click", () => selectDescription(Number(button.dataset.descriptionIndex))));
  renderWorkflowNav();
}

async function saveDescriptions() {
  try {
    captureDescriptionDraft();
    const document = workflowEntry("descriptions").document;
    if (!document) return showToast("当前没有可保存的画面描述", true);
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/workflow/descriptions`, { method: "PUT", body: JSON.stringify({ document }) });
    state.workflow = body.workflow;
    renderDescriptions();
    showToast("画面描述已保存到本地");
  } catch (error) { showToast(error.message, true); }
}

function renderStoryPlan() {
  const entry = workflowEntry("story");
  const story = entry.document;
  if (!story) {
    $("#storyEditor").innerHTML = '<div class="planning-empty">请先批准画面理解，再点击“生成故事”。</div>';
    renderWorkflowNav();
    return;
  }
  const beats = story.beats || [];
  const duration = beats.reduce((sum, item) => sum + Number(item.duration_s || 0), 0);
  const characters = story.characters || [];
  const locations = story.locations || [];
  $("#storyEditor").innerHTML = `<div class="story-layout">
    <div class="stat-strip">
      ${statCard("剧情段落", beats.length, "个 Beat")}
      ${statCard("计划时长", duration, "秒")}
      ${statCard("角色", characters.length, "个")}
      ${statCard("场景", locations.length, "个")}
    </div>

    <section class="story-section">
      <div class="story-section-head"><div><span class="panel-kicker">LOGLINE</span><h3>故事骨架</h3></div><p>一句话说清主线，后面所有镜头都要服务于它。</p></div>
      <div class="story-pair">
        <label class="story-field"><span>剧名</span><input id="storyTitle" value="${escapeHtml(story.title || "")}" placeholder="给这条短剧起个名字" /></label>
        <label class="story-field"><span>一句话故事</span><input id="storyLogline" value="${escapeHtml(story.logline || "")}" placeholder="谁、想要什么、被什么阻拦" /></label>
      </div>
    </section>

    <section class="story-section">
      <div class="story-section-head">
        <div><span class="panel-kicker">BEAT MAP</span><h3>段落节奏</h3></div>
        <p>宽度按时长比例，鼠标悬停看该段落内容。</p>
      </div>
      ${beats.length ? `<div class="beat-strip">${beats.map((beat, index) => {
        const seconds = Number(beat.duration_s || 0);
        const summary = String(beat.summary || beat.description || beat.beat || "").slice(0, 160);
        return `<div class="beat-seg" style="flex:${Math.max(seconds, 1)}" title="${escapeHtml(summary)}">
          <b>${escapeHtml(beat.beat_id || `B${String(index + 1).padStart(2, "0")}`)}</b>
          <span>${seconds}s</span>
        </div>`;
      }).join("")}</div>` : '<div class="story-empty">还没有段落，先生成故事。</div>'}
    </section>

    <section class="story-section">
      <div class="story-section-head">
        <div><span class="panel-kicker">SCRIPT</span><h3>剧本正文</h3></div>
        <div class="story-tabs" role="group" aria-label="剧本视图">
          <button class="is-selected" data-story-tab="full" type="button">完整剧情</button>
          <button data-story-tab="screenplay" type="button">正式剧本</button>
          <button data-story-tab="style" type="button">风格与声音</button>
        </div>
      </div>
      <div class="story-panes">
        <label class="story-field is-selected" data-story-pane="full"><span>完整剧情<em class="field-hint" data-count-for="storyFull"></em></span><textarea class="story-main" id="storyFull">${escapeHtml(story.full_story || "")}</textarea></label>
        <label class="story-field" data-story-pane="screenplay"><span>正式剧本<em class="field-hint" data-count-for="storyScreenplay"></em></span><textarea class="story-main" id="storyScreenplay">${escapeHtml(story.screenplay || "")}</textarea></label>
        <label class="story-field" data-story-pane="style"><span>风格与声音规则<em class="field-hint" data-count-for="storyStyle"></em></span><textarea class="story-main" id="storyStyle">${escapeHtml(story.style_bible || "")}</textarea></label>
      </div>
    </section>

    <details class="story-json">
      <summary><span class="panel-kicker">STRUCTURED</span><strong>结构数据</strong><small>角色 ${characters.length} · 场景 ${locations.length} · Beats ${beats.length}</small></summary>
      <div class="story-json-body">
        <div class="story-pair">
          <label class="story-field"><span>角色<em class="field-badge">json</em></span><textarea class="field-code" id="storyCharacters">${escapeHtml(JSON.stringify(characters, null, 2))}</textarea></label>
          <label class="story-field"><span>场景<em class="field-badge">json</em></span><textarea class="field-code" id="storyLocations">${escapeHtml(JSON.stringify(locations, null, 2))}</textarea></label>
        </div>
        <label class="story-field"><span>剧情段落 Beats<em class="field-badge">json</em></span><textarea class="story-main field-code" id="storyBeats">${escapeHtml(JSON.stringify(beats, null, 2))}</textarea></label>
      </div>
    </details>
  </div>`;
  bindStoryTabs();
  bindCharacterCounts();
  renderWorkflowNav();
}

function statCard(label, value, unit) {
  return `<div class="stat-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}<em>${escapeHtml(unit || "")}</em></strong></div>`;
}

function bindStoryTabs() {
  const tabs = Array.from(document.querySelectorAll("[data-story-tab]"));
  tabs.forEach((tab) => tab.addEventListener("click", () => {
    tabs.forEach((item) => item.classList.toggle("is-selected", item === tab));
    document.querySelectorAll("[data-story-pane]").forEach((pane) => {
      pane.classList.toggle("is-selected", pane.dataset.storyPane === tab.dataset.storyTab);
    });
  }));
}

function bindCharacterCounts() {
  document.querySelectorAll("[data-count-for]").forEach((badge) => {
    const field = document.getElementById(badge.dataset.countFor);
    if (!field) return;
    const update = () => { badge.textContent = `${field.value.length.toLocaleString()} 字`; };
    field.addEventListener("input", update);
    update();
  });
}

function captureStoryDraft() {
  const story = workflowEntry("story").document;
  if (!story || !$("#storyTitle")) return;
  story.title = $("#storyTitle").value.trim();
  story.logline = $("#storyLogline").value.trim();
  story.full_story = $("#storyFull").value.trim();
  if ($("#storyScreenplay")) story.screenplay = $("#storyScreenplay").value;
  story.style_bible = $("#storyStyle").value.trim();
  story.characters = JSON.parse($("#storyCharacters").value || "[]");
  story.locations = JSON.parse($("#storyLocations").value || "[]");
  story.beats = JSON.parse($("#storyBeats").value || "[]");
}

async function saveStoryPlan() {
  try {
    captureStoryDraft();
    const document = workflowEntry("story").document;
    if (!document) return showToast("当前没有可保存的故事", true);
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/workflow/story`, { method: "PUT", body: JSON.stringify({ document }) });
    state.workflow = body.workflow;
    renderStoryPlan();
    showToast("故事规划已保存到本地");
  } catch (error) { showToast(`故事字段格式错误：${error.message}`, true); }
}

function renderShotPlan() {
  const entry = workflowEntry("shots");
  const shots = ((entry.document || {}).shots || []);
  const duration = shots.reduce((sum, item) => sum + Number(item.duration_s || 0), 0);
  const dialogueCount = shots.filter((item) => (item.dialogue || []).length).length;
  $("#shotPlanSummary").innerHTML = shots.length
    ? `<div class="stat-strip">${statCard("镜头总数", shots.length, "镜")}${statCard("计划时长", duration, "秒")}${statCard("对白镜头", dialogueCount, "镜")}</div>
       <span class="workflow-status ${entry.status === "approved" ? "is-approved" : "is-draft"}">${workflowStatusLabel(entry)}</span>`
    : '<div class="planning-empty">尚未生成镜头</div>';
  $("#shotPlanGrid").innerHTML = shots.length ? shots.map((shot, index) => {
    const dialogue = ((shot.dialogue || [])[0] || {}).text || "";
    return `<article class="shot-card panel">
    <header class="shot-card-head">
      <div class="shot-card-id"><strong>${escapeHtml(shot.shot_id)}</strong><span>第 ${index + 1} 镜</span></div>
      <div class="shot-card-chips">
        <span class="shot-chip">${escapeHtml(shot.beat_id || "—")}</span>
        <span class="shot-chip">${escapeHtml(shot.scene_id || "—")}</span>
        <span class="shot-chip ${dialogue ? "is-voice" : "is-mute"}">${dialogue ? "有对白" : "无对白"}</span>
      </div>
      <div class="shot-card-controls">
        <label class="shot-inline-field"><span>时长</span><input type="number" min="4" max="8" value="${escapeHtml(shot.duration_s)}" data-shot-field="duration_s" data-shot-index="${index}" /></label>
        <label class="shot-inline-field"><span>建议模式</span><select data-shot-field="generation_mode" data-shot-index="${index}">${["t2va","first_frame","first_last_frame","ref2va"].map((mode) => `<option ${shot.generation_mode === mode ? "selected" : ""}>${mode}</option>`).join("")}</select></label>
      </div>
    </header>
    <div class="shot-card-body shot-edit">
      <label class="wide"><span>镜头作用</span><input value="${escapeHtml(shot.story_purpose || "")}" placeholder="这一镜要推进什么" data-shot-field="story_purpose" data-shot-index="${index}" /></label>
      <label class="wide"><span>对白</span><input value="${escapeHtml(dialogue)}" placeholder="留空表示这一镜没有台词" data-shot-field="dialogue_text" data-shot-index="${index}" /></label>
      <label><span>构图与机位<em class="field-hint">第一行构图，其余为机位</em></span><textarea data-shot-field="composition" data-shot-index="${index}">${escapeHtml(`${shot.composition || ""}\n${shot.camera || ""}`)}</textarea></label>
      <label><span>按秒动作与衔接</span><textarea data-shot-field="action_timeline" data-shot-index="${index}">${escapeHtml(shot.action_timeline || "")}</textarea></label>
    </div></article>`;
  }).join("") : '<div class="planning-empty panel">请先批准故事规划，再点击“自动拆分镜头”。</div>';
  document.querySelectorAll("[data-shot-field]").forEach((input) => input.addEventListener("input", () => {
    const shot = shots[Number(input.dataset.shotIndex)];
    const field = input.dataset.shotField;
    if (field === "duration_s") shot.duration_s = Number(input.value);
    else if (field === "dialogue_text") {
      if (input.value.trim()) {
        const speaker = ((shot.dialogue || [])[0] || {}).speaker_id || (shot.characters || ["C01"])[0];
        shot.dialogue = [{ speaker_id: speaker, text: input.value.trim() }];
        shot.subtitle_text = input.value.trim();
      } else { shot.dialogue = []; shot.subtitle_text = ""; }
    } else if (field === "composition") {
      const parts = input.value.split("\n");
      shot.composition = parts.shift() || "";
      shot.camera = parts.join("\n");
    } else shot[field] = input.value;
  }));
  renderWorkflowNav();
}

async function saveShotPlan() {
  try {
    const document = workflowEntry("shots").document;
    if (!document) return showToast("当前没有可保存的分镜", true);
    let cursor = 0;
    (document.shots || []).forEach((shot) => { shot.planned_start_s = cursor; cursor += Number(shot.duration_s || 0); shot.planned_end_s = cursor; });
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/workflow/shots`, { method: "PUT", body: JSON.stringify({ document }) });
    state.workflow = body.workflow;
    await refreshOrchestrationWorkspace();
    renderShotPlan();
    showToast("分镜修改已保存到本地");
  } catch (error) { showToast(error.message, true); }
}

function renderWorkflow() {
  renderWorkflowNav();
  renderDescriptions();
  renderStoryPlan();
  renderShotPlan();
}

async function generateWorkflowStage(stage, button) {
  button.disabled = true;
  const names = { descriptions: "画面描述", story: "故事规划", shots: "分镜拆分" };
  startAIProgress(stage, `AI 正在生成${names[stage]}`);
  setConnection(true, `LLM 正在生成${names[stage]}`);
  try {
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/workflow/${stage}/generate`, { method: "POST", body: "{}" });
    state.workflow = body.workflow;
    if (stage === "shots") await refreshOrchestrationWorkspace();
    renderWorkflow();
    finishAIProgress(`${names[stage]}生成完成`);
    showToast(`${names[stage]}已生成并保存`);
  } catch (error) { failAIProgress(error.message); showToast(error.message, true); }
  finally { button.disabled = false; setConnection(true, "Studio 与 H3 已就绪"); }
}

async function reviseStory(button) {
  button.disabled = true;
  startAIProgress("story", "豆包正在重写正式剧本");
  setConnection(true, "LLM 正在重写正式剧本");
  try {
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/workflow/story/revise`, { method: "POST", body: "{}" });
    state.workflow = body.workflow;
    renderWorkflow();
    finishAIProgress("正式剧本已重写");
    showToast("正式剧本已用豆包重写并保存");
  } catch (error) { failAIProgress(error.message); showToast(error.message, true); }
  finally { button.disabled = false; setConnection(true, "Studio 与 H3 已就绪"); }
}

async function approveWorkflowStage(stage) {
  const names = { descriptions: "画面理解", story: "故事规划", shots: "分镜拆分" };
  try {
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/workflow/${stage}/approve`, { method: "POST", body: "{}" });
    state.workflow = body.workflow;
    if (stage === "shots") await refreshOrchestrationWorkspace();
    renderWorkflow();
    showToast(`${names[stage]}已批准`);
    showStage(stage === "descriptions" ? "故事规划" : stage === "story" ? "分镜拆分" : "人工编排");
  } catch (error) { showToast(error.message, true); }
}

async function runInspirationLLM(mode, requestOverride = null) {
  if (!state.llm || !state.llm.configured || !state.llm.credential_ready) return showToast("LLM 配置或 API Key 未就绪", true);
  const request = requestOverride || (mode === "polish" ? {
    mode,
    idea_text: $("#ideaDraft").value.trim(),
    genre: $("#polishGenre").value,
    tone: "电影感",
  } : {
    mode,
    idea_text: "",
    genre: $("#scratchGenre").value,
    tone: $("#scratchTone").value,
  });
  if (mode === "polish" && !request.idea_text) return showToast("请先写下你的剧情灵感", true);
  state.lastInspirationRequest = request;
  const button = mode === "polish" ? $("#polishIdea") : $("#generateFromScratch");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "LLM 正在构思…";
  startAIProgress("inspiration", mode === "polish" ? "AI 正在润色剧情灵感" : "AI 正在生成剧情提案");
  try {
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/inspiration/generate`, {
      method: "POST",
      body: JSON.stringify(request),
    });
    state.inspiration = body.inspiration;
    renderProposal();
    $("#inspirationResult").scrollIntoView({ behavior: "smooth", block: "center" });
    finishAIProgress(mode === "polish" ? "剧情润色完成" : "剧情提案生成完成");
    showToast(mode === "polish" ? "剧情灵感已润色" : "剧情提案已生成");
  } catch (error) { failAIProgress(error.message); showToast(error.message, true); }
  finally { button.disabled = false; button.textContent = original; }
}

async function generateCurrent() {
  const shot = currentShot();
  if (!shot) return;
  const draft = draftFor(shot);
  const promptSnapshot = promptText.textContent.trim();
  let decision = shot.decision;
  if (draft.dirty || !decision || !decision.approved || !decision.locked) {
    decision = await saveCurrent(true, false);
    if (!decision) return;
  }
  $("#generateShot").disabled = true;
  startAIProgress("video", `正在生成 ${shot.id}`, "根据 H3 队列状态实时更新");
  try {
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/shots/${encodeURIComponent(shot.id)}/generate`, {
      method: "POST",
      body: JSON.stringify({ expected_prompt: promptSnapshot }),
    });
    if (body.job.prompt_snapshot !== promptSnapshot) throw new Error("生成任务中的 Prompt 与页面最终优化稿不一致，任务已拒绝");
    state.jobs.unshift(body.job);
    aiProgressState.jobIds = [body.job.job_id];
    renderJobs();
    showToast(`${shot.id} 已进入 ComfyUI 生成队列`);
  } catch (error) { failAIProgress(error.message); showToast(error.message, true); }
  finally { $("#generateShot").disabled = false; }
}

async function generateApproved() {
  if (!state.online) return showToast("后端未连接", true);
  $("#runPipeline").disabled = true;
  startAIProgress("video", "正在批量生成已批准镜头", "根据 H3 队列状态实时更新");
  try {
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/generate-approved`, { method: "POST", body: "{}" });
    state.jobs = [...body.jobs, ...state.jobs];
    aiProgressState.jobIds = body.jobs.map((job) => job.job_id);
    renderJobs();
    showToast(`${body.jobs.length} 个已批准镜头已进入队列`);
  } catch (error) { failAIProgress(error.message); showToast(error.message, true); }
  finally { $("#runPipeline").disabled = false; }
}

function renderJobs() {
  const rank = { running: 0, uploading_inputs: 1, preflight: 2, queued: 3, failed: 4, completed: 5 };
  const shot = currentShot();
  if (!shot) return;
  const draft = draftFor(shot);
  const jobs = state.jobs.filter((job) => job.shot_id === shot.id).sort((a, b) => (rank[a.status] === undefined ? 9 : rank[a.status]) - (rank[b.status] === undefined ? 9 : rank[b.status]) || String(b.created_at).localeCompare(String(a.created_at))).slice(0, 6);
  const labels = { queued: "排队中", preflight: "检查环境", uploading_inputs: "上传输入", running: "生成中", completed: "已完成", failed: "失败" };
  const status = shotStatus(shot);
  const decision = `<div class="decision-entry"><span>${escapeHtml(shot.id)}</span><p><strong>${status === "approved" ? "已批准" : status === "review" ? "草稿已保存" : "尚未保存编排"}</strong> · ${escapeHtml(draft.generation_mode)}<br>${draft.locked ? "已锁定，批处理不可修改" : "未锁定，可继续编辑"}</p></div>`;
  const jobEntries = jobs.map((job) => {
    const videoUrl = job.video ? `/api/media?${new URLSearchParams({ run: state.runId, path: job.video })}` : null;
    return `<div><span>${escapeHtml(labels[job.status] || job.status)}</span><p><strong>${escapeHtml(job.job_id || "生成任务")}</strong> · ${escapeHtml(MODE_LABELS[job.generation_mode] || job.generation_mode)}${job.error ? `<br><span class="job-error">${escapeHtml(job.error)}</span>` : ""}${videoUrl ? `<br><a class="job-video" href="${escapeHtml(videoUrl)}" target="_blank">查看生成视频 ↗</a>` : ""}</p></div>`;
  }).join("");
  const empty = `<div class="history-empty"><span>生成</span><p>当前镜头暂无生成任务</p></div>`;
  $("#jobList").innerHTML = decision + (jobEntries || empty);
  syncVideoProgress();
}

async function refreshJobs() {
  if (!state.online || !state.runId || ($("#orchestrationView").classList.contains("is-hidden") && !aiProgressState.jobIds.length)) return;
  try {
    if (!(await ensureCurrentBuild())) return;
    state.jobs = (await api(`/api/runs/${encodeURIComponent(state.runId)}/jobs`)).jobs;
    renderJobs();
    renderCrossShotAssets();
  } catch { /* transient polling failure */ }
}

function applyWorkspaceState(workspace) {
  state.shots = workspace.shots.map((shot) => ({
    id: shot.shot_id,
    title: shot.title,
    scene: shot.scene,
    duration: shot.duration_s,
    beat: shot.beat_id,
    suggested_mode: shot.suggested_mode,
    suggested_prompt: shot.suggested_prompt,
    official_prompt: shot.official_prompt,
    prompt_skill: shot.prompt_skill,
    decision: shot.decision,
    characters: shot.characters,
    composition: shot.composition,
    camera: shot.camera,
    action_timeline: shot.action_timeline,
    continuity_in: shot.continuity_in,
    continuity_out: shot.continuity_out,
    dialogue: shot.dialogue,
    subtitle_text: shot.subtitle_text,
    audio_contract: shot.audio_contract,
    source_anchor_image: shot.source_anchor_image,
    depends_on: shot.depends_on,
    first_frame_desc: shot.first_frame_desc,
    last_frame_desc: shot.last_frame_desc,
  }));
  state.jobs = workspace.jobs || [];
  state.activeIndex = Math.max(0, state.shots.findIndex((shot) => shotStatus(shot) !== "approved"));
}

async function refreshOrchestrationWorkspace() {
  const workspace = await api(`/api/runs/${encodeURIComponent(state.runId)}/workspace`);
  applyWorkspaceState(workspace);
  renderJobs();
  renderShots();
  selectShot(state.activeIndex);
}

async function loadRun(runId) {
  state.runId = runId;
  state.activeIndex = 0;
  state.activeDescriptionIndex = 0;
  state.lastInspirationRequest = null;
  const [workspace, assetBody, inspirationBody, workflowBody] = await Promise.all([
    api(`/api/runs/${encodeURIComponent(runId)}/workspace`),
    api(`/api/runs/${encodeURIComponent(runId)}/assets`),
    api(`/api/runs/${encodeURIComponent(runId)}/inspiration`),
    api(`/api/runs/${encodeURIComponent(runId)}/workflow`),
  ]);
  applyWorkspaceState(workspace);
  state.assets = assetBody.assets;
  state.uploadLimits = assetBody.upload_limits || state.uploadLimits;
  state.uploadCounts = assetBody.upload_counts || state.uploadCounts;
  state.inspiration = inspirationBody.inspiration;
  state.llm = inspirationBody.llm;
  state.workflow = workflowBody.workflow;
  try { window.localStorage.setItem("sceneflow.activeProject", runId); } catch { /* storage may be unavailable */ }
  renderJobs();
  renderMaterialLibrary();
  renderInspiration();
  renderWorkflow();
  renderShots();
  selectShot(state.activeIndex);
}

function renderRunOptions(selectedRunId = state.runId) {
  const select = $("#runSelect");
  select.innerHTML = state.runs.map((run) => {
    const count = Number(run.shot_count || 0);
    const suffix = count ? `${count} 镜` : "素材准备";
    return `<option value="${escapeHtml(run.run_id)}">${escapeHtml(run.project_name || run.run_id)} · ${suffix}</option>`;
  }).join("");
  if (selectedRunId) select.value = selectedRunId;
}

function openNewProjectDialog() {
  const dialog = $("#newProjectDialog");
  $("#newProjectForm").reset();
  $("#createProject").disabled = false;
  $("#createProject").textContent = "创建并进入素材准备 →";
  dialog.showModal();
  requestAnimationFrame(() => $("#newProjectName").focus());
}

function closeNewProjectDialog() {
  const dialog = $("#newProjectDialog");
  if (dialog.open) dialog.close();
}

async function createProject(event) {
  event.preventDefault();
  const projectName = $("#newProjectName").value.trim();
  if (!projectName) return $("#newProjectName").focus();
  const submit = $("#createProject");
  submit.disabled = true;
  submit.textContent = "正在创建本地项目…";
  try {
    const body = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ project_name: projectName }),
    });
    const project = body.project;
    state.runs = [project, ...state.runs.filter((item) => item.run_id !== project.run_id)];
    renderRunOptions(project.run_id);
    await loadRun(project.run_id);
    closeNewProjectDialog();
    showStage("素材准备");
    showToast(`项目“${project.project_name}”已创建并保存到本地`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    submit.disabled = false;
    submit.textContent = "创建并进入素材准备 →";
  }
}

async function boot() {
  try {
    if (!(await ensureCurrentBuild())) return;
    const responses = await Promise.all([api("/api/bootstrap"), api("/api/health")]);
    const bootstrap = responses[0];
    const health = responses[1];
    if (health.ui_build && health.ui_build !== UI_BUILD) {
      window.location.reload();
      return;
    }
    state.imageGenerator = health.image_generator || null;
    state.runs = bootstrap.runs;
    let savedRunId = null;
    try { savedRunId = window.localStorage.getItem("sceneflow.activeProject"); } catch { /* storage may be unavailable */ }
    const initialRunId = state.runs.some((run) => run.run_id === savedRunId) ? savedRunId : bootstrap.default_run_id;
    renderRunOptions(initialRunId);
    if (initialRunId) await loadRun(initialRunId);
    const comfy = health.comfyui || {};
    const modelReady = comfy.online && comfy.fl2va_ready && comfy.ref2va_ready && comfy.ref_node_ready && comfy.text_encoder_ready && comfy.video_vae_ready && comfy.audio_vae_ready;
    setConnection(true, modelReady ? "Studio 与 H3 已就绪" : "Studio 已连接 · H3 未就绪");
    if (!modelReady) $("#connectionState").classList.remove("is-online");
    $("#connectionState").classList.toggle("is-offline", !modelReady);
    const hashStages = { "#assets": "素材准备", "#descriptions": "画面理解", "#story": "故事规划", "#shots": "分镜拆分", "#orchestration": "人工编排", "#subtitles": "字幕校对", "#assemble": "合片验收" };
    const initialProject = state.runs.find((run) => run.run_id === initialRunId);
    const initialStage = initialProject && Number(initialProject.shot_count || 0)
      ? (hashStages[window.location.hash] || "人工编排")
      : "素材准备";
    showStage(initialStage);
    if (!initialRunId) openNewProjectDialog();
    pollTimer = setInterval(refreshJobs, 3000);
  } catch (error) {
    setConnection(false, "后端未连接");
    showToast(`${error.message}；请用 Studio 服务打开页面`, true);
    shotList.innerHTML = "<p style='padding:16px;font-size:10px;color:#78837e'>启动后端后，这里会显示真实 run 的镜头。</p>";
  }
}

document.querySelectorAll(".mode-card").forEach((card) => card.addEventListener("click", () => setMode(card.dataset.mode)));
document.querySelectorAll(".segmented button").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".segmented button").forEach((item) => item.classList.remove("is-selected"));
  button.classList.add("is-selected");
  state.activeFilter = button.dataset.filter;
  renderShots();
}));
$("#shotSearch").addEventListener("input", renderShots);
$("#prevShot").addEventListener("click", () => selectShot(state.activeIndex - 1));
$("#nextShot").addEventListener("click", () => selectShot(state.activeIndex + 1));
$("#approveShot").addEventListener("click", () => saveCurrent(true, true));
$("#generateShot").addEventListener("click", generateCurrent);
$("#runPipeline").addEventListener("click", generateApproved);
$("#assetLibrary").addEventListener("click", () => openAssetLibrary(draftFor(currentShot()).generation_mode === "Ref2VA" ? "reference" : "first"));
$("#openAIImage").addEventListener("click", openAIImageDialog);
$("#closeAIImageDialog").addEventListener("click", () => $("#aiImageDialog").close());
$("#cancelAIImage").addEventListener("click", () => $("#aiImageDialog").close());
$("#closeImageLightbox").addEventListener("click", closeImageLightbox);
$("#imageLightbox").addEventListener("click", (event) => { if (event.target === event.currentTarget) closeImageLightbox(); });
$("#imageLightbox").addEventListener("close", () => $("#imageLightboxPreview").removeAttribute("src"));
$("#closeVideoPreview").addEventListener("click", closeVideoPreview);
$("#videoPreviewDialog").addEventListener("click", (event) => { if (event.target === event.currentTarget) closeVideoPreview(); });
$("#videoPreviewDialog").addEventListener("close", () => {
  const player = $("#videoPreviewPlayer");
  player.pause();
  player.removeAttribute("src");
  player.load();
});
$("#generateAIImage").addEventListener("click", generateAIImage);
$("#aiImagePrompt").addEventListener("input", (event) => { $("#aiImagePromptCount").textContent = event.target.value.length; });
$("#addReference").addEventListener("click", () => openAssetLibrary("reference"));
$("#addKeyframeReference").addEventListener("click", () => openAssetLibrary("keyframe"));
$("#addVideoReference").addEventListener("click", () => openAssetLibrary("video"));
$("#addAudioReference").addEventListener("click", () => openAssetLibrary("audio"));
document.querySelectorAll("[data-pick]").forEach((button) => button.addEventListener("click", () => openAssetLibrary(button.dataset.pick)));
document.querySelectorAll("[data-clear-frame]").forEach((button) => button.addEventListener("click", async () => {
  const shot = currentShot();
  const draft = draftFor(shot);
  const role = button.dataset.clearFrame;
  if (!draft[`${role}_frame`]) return;
  beginDraftEdit(shot, draft);
  draft[`${role}_frame`] = null;
  renderAsset(role, null);
  renderReferences(draft);
  renderShotConstraints(shot, draft);
  markPromptStale(draft);
  await persistDraft(shot, draft, { quiet: true });
  showToast(`${role === "first" ? "首帧" : "尾帧"}已删除，可重新选择`);
}));
document.querySelectorAll("[data-preview-frame]").forEach((button) => button.addEventListener("click", () => {
  const role = button.dataset.previewFrame;
  const draft = draftFor(currentShot());
  const path = role === "first" ? draft.first_frame : draft.last_frame;
  const asset = assetByPath(path);
  if (!path) return showToast(`当前镜头尚未选择${role === "first" ? "首帧" : "尾帧"}`, true);
  openImageLightbox(
    asset ? asset.url : mediaUrlForPath(path),
    (asset && (asset.label || asset.name)) || path.split("/").pop(),
    `${role === "first" ? "首帧 · 0 秒" : "尾帧 · 结束时刻"}${asset ? ` · ${formatBytes(asset.size_bytes)}` : ""}`,
  );
}));
$("#closeAssetDialog").addEventListener("click", () => $("#assetDialog").close());
$("#assetUpload").addEventListener("change", (event) => uploadAsset(event.target.files[0]));
$("#adoptRecommendation").addEventListener("click", (event) => setMode(event.currentTarget.dataset.mode));
$("#runSelect").addEventListener("change", async (event) => {
  try {
    const project = state.runs.find((item) => item.run_id === event.target.value);
    await loadRun(event.target.value);
    if (!Number((project || {}).shot_count || 0)) showStage("素材准备");
    showToast(`已切换到“${(project && project.project_name) || event.target.value}”`);
  }
  catch (error) { showToast(error.message, true); }
});
$("#openNewProject").addEventListener("click", openNewProjectDialog);
$("#closeNewProject").addEventListener("click", closeNewProjectDialog);
$("#cancelNewProject").addEventListener("click", closeNewProjectDialog);
$("#newProjectDialog").addEventListener("click", (event) => { if (event.target === event.currentTarget) closeNewProjectDialog(); });
$("#newProjectForm").addEventListener("submit", createProject);
$("#copyPrompt").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(promptText.textContent); showToast("Prompt 已复制"); }
  catch { showToast("浏览器未授权复制，请手动选择文本", true); }
});
$("#aiProgressDismiss").addEventListener("click", () => {
  $("#aiProgress").classList.remove("is-visible");
  $("#aiProgress").setAttribute("aria-hidden", "true");
});
$("#resetPrompt").addEventListener("click", () => {
  const shot = currentShot();
  if (!shot) return;
  const draft = draftFor(shot);
  beginDraftEdit(shot, draft);
  refreshOfficialPromptForMode(shot, draft);
  showToast("正在恢复当前模式的 MiniMax 官方 Skill 优化稿");
});
$("#lockShot").addEventListener("change", (event) => {
  const shot = currentShot();
  if (!shot) return;
  const draft = draftFor(shot);
  draft.locked = Boolean(event.target.checked);
  draft.approved = false;
  draft.dirty = true;
  promptText.contentEditable = draft.locked ? "false" : "true";
  renderShots();
  showToast(draft.locked ? "保存后将锁定当前镜头" : "已进入重新编辑状态，保存后正式解锁");
});
document.querySelectorAll(".stage-item").forEach((item) => item.addEventListener("click", () => showStage(item.dataset.stage)));
$("#continueToOrchestration").addEventListener("click", () => continueToDescriptions());
$("#saveImageInspiration").addEventListener("click", () => saveImageInspiration({ advance: true }));
$("#assetSearch").addEventListener("input", renderMaterialLibrary);
document.querySelectorAll("[data-material-filter]").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("[data-material-filter]").forEach((item) => item.classList.remove("is-selected"));
  button.classList.add("is-selected");
  state.materialFilter = button.dataset.materialFilter;
  renderMaterialLibrary();
}));
document.querySelectorAll(".material-upload").forEach((input) => input.addEventListener("change", () => handleMaterialUpload(input)));
$("#ideaImageUpload").addEventListener("change", (event) => uploadIdeaImages(event.target));
$("#saveDescriptions").addEventListener("click", saveDescriptions);
$("#saveStory").addEventListener("click", saveStoryPlan);
$("#saveShots").addEventListener("click", saveShotPlan);
document.querySelectorAll("[data-workflow-generate]").forEach((button) => button.addEventListener("click", () => generateWorkflowStage(button.dataset.workflowGenerate, button)));
document.querySelectorAll("[data-workflow-approve]").forEach((button) => button.addEventListener("click", () => approveWorkflowStage(button.dataset.workflowApprove)));
$("#reviseStory")?.addEventListener("click", (event) => reviseStory(event.currentTarget));
$("#generateFromScratch").addEventListener("click", () => runInspirationLLM("from_scratch"));
$("#polishIdea").addEventListener("click", () => runInspirationLLM("polish"));
$("#ideaDraft").addEventListener("input", (event) => { $("#ideaCharCount").textContent = `${event.target.value.length} / 8000`; });
$("#regenerateProposal").addEventListener("click", () => {
  const item = state.inspiration && state.inspiration.current_proposal;
  const request = state.lastInspirationRequest || (item ? { mode: item.mode, idea_text: item.idea_text, ...item.preferences } : null);
  if (request) runInspirationLLM(request.mode, request);
});
$("#acceptProposal").addEventListener("click", acceptCurrentProposal);

const TIMING_SOURCE_LABELS = {
  asr_sensevoice_vad: "已按成片人声对齐",
  asr_unmatched_interpolated: "人声对齐（该条为插值补齐）",
  default_safe_window: "计划时间轴（安全窗口）",
  manual: "人工编辑",
};

function timingSourceLabel(value) {
  const text = String(value || "");
  if (TIMING_SOURCE_LABELS[text]) return TIMING_SOURCE_LABELS[text];
  if (text.startsWith("action_timeline")) return "计划时间轴（按秒动作）";
  if (text.startsWith("asr")) return "人声对齐";
  return text || "—";
}

const SUBTITLE_STYLE_FIELDS = {
  font_name: "#styleFontName",
  font_size: "#styleFontSize",
  outline: "#styleOutline",
  shadow: "#styleShadow",
  margin_v: "#styleMarginV",
  margin_h: "#styleMarginH",
  max_chars_per_line: "#styleMaxChars",
  spacing: "#styleSpacing",
  bold: "#styleBold",
  fade_in_ms: "#styleFadeIn",
  fade_out_ms: "#styleFadeOut",
};

function renderSubtitleStyle(style, fonts) {
  const select = $("#styleFontName");
  const names = Array.from(new Set([...(fonts || []), style.font_name].filter(Boolean)));
  select.innerHTML = names.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("");
  select.value = style.font_name;
  $("#styleBold").value = style.bold ? "true" : "false";
  Object.entries(SUBTITLE_STYLE_FIELDS).forEach(([key, selector]) => {
    if (key === "font_name" || key === "bold") return;
    $(selector).value = Number(style[key] ?? 0);
  });
}

function collectSubtitleStyle() {
  const base = (state.subtitles && state.subtitles.style) || {};
  const style = { ...base, font_name: $("#styleFontName").value, bold: $("#styleBold").value === "true" };
  Object.entries(SUBTITLE_STYLE_FIELDS).forEach(([key, selector]) => {
    if (key === "font_name" || key === "bold") return;
    style[key] = Number($(selector).value);
  });
  return style;
}

function renderSubtitlePreview() {
  const body = state.subtitles || {};
  $("#subtitlePreview").value = (state.subtitleFormat === "srt" ? body.srt_text : body.ass_text) || "";
}

async function loadSubtitles() {
  if (!state.runId) return;
  try {
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/subtitles`);
    state.subtitles = body;
    const cues = body.cues || [];
    $("#subtitleStatus").textContent = body.exists ? `${cues.length} 条字幕` : "尚未生成";
    $("#subtitleStatus").classList.toggle("is-approved", Boolean(body.aligned));
    $("#subtitleTimingSource").textContent = cues.length
      ? `${body.aligned ? "已按成片人声对齐" : "计划时间轴"} · 成片 ${Number(body.video_duration_s || 0).toFixed(1)} 秒`
      : "—";
    $("#subtitleCueList").innerHTML = cues.length ? cues.map((cue, index) => {
      const start = Number(cue.film_start_s || 0);
      const end = Number(cue.film_end_s || 0);
      const text = cue.text || "";
      const byVoice = String(cue.timing_source || "").startsWith("asr");
      return `
      <div class="subtitle-cue">
        <div class="subtitle-cue-id">
          <b>${escapeHtml(cue.shot_id || "—")}</b>
          <small class="${byVoice ? "is-aligned" : ""}">${escapeHtml(timingSourceLabel(cue.timing_source))}</small>
        </div>
        <input data-cue-start="${index}" type="number" step="0.05" value="${start.toFixed(2)}" />
        <input data-cue-end="${index}" type="number" step="0.05" value="${end.toFixed(2)}" />
        <div class="subtitle-cue-text">
          <textarea data-cue-text="${index}" rows="2">${escapeHtml(text)}</textarea>
          <div class="subtitle-cue-foot"><span>#${index + 1}</span><span>时长 ${Math.max(end - start, 0).toFixed(2)} 秒</span><span>${text.length} 字</span></div>
        </div>
      </div>`;
    }).join("") : '<div class="planning-empty">还没有字幕条目。</div>';
    renderSubtitleStyle(body.style || {}, body.available_fonts);
    renderSubtitlePreview();
    updateLaterStageNav();
  } catch (error) { showToast(error.message, true); }
}

function collectSubtitleDraft() {
  const cues = state.subtitles && state.subtitles.cues ? state.subtitles.cues : [];
  return cues.map((cue, index) => ({
    ...cue,
    film_start_s: Number(document.querySelector(`[data-cue-start="${index}"]`)?.value ?? cue.film_start_s ?? 0),
    film_end_s: Number(document.querySelector(`[data-cue-end="${index}"]`)?.value ?? cue.film_end_s ?? 0),
    text: document.querySelector(`[data-cue-text="${index}"]`)?.value ?? cue.text ?? "",
  }));
}

async function saveSubtitles(options) {
  const quiet = Boolean(options && options.quiet);
  await api(`/api/runs/${encodeURIComponent(state.runId)}/subtitles`, {
    method: "PUT",
    body: JSON.stringify({ cues: collectSubtitleDraft(), style: collectSubtitleStyle() }),
  });
  await loadSubtitles();
  if (!quiet) showToast("字幕与样式已保存");
}

async function loadAssemble() {
  if (!state.runId) return;
  try {
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/final`);
    $("#assembleStatus").textContent = body.ready ? "已有成片" : "尚未合片";
    $("#assembleStatus").classList.toggle("is-approved", Boolean(body.ready));
    $("#finalPreview").innerHTML = body.url
      ? `<video src="${escapeHtml(body.url)}" controls></video>`
      : '<div class="planning-empty">还没有成片。</div>';
    const missing = (body.report && body.report.missing) || [];
    const videos = body.videos || [];
    const ready = videos.filter((item) => item.ready).length;
    const percent = videos.length ? Math.round((ready / videos.length) * 100) : 0;
    $("#assembleReport").innerHTML = `
      <div class="report-metric">
        <div class="report-metric-row"><span>可拼接镜头</span><strong>${ready} / ${videos.length}</strong></div>
        <div class="report-bar"><i style="width:${percent}%"></i></div>
      </div>
      <div class="report-metric">
        <div class="report-metric-row"><span>成片时长</span><strong>${Number((body.report && body.report.duration_s) || 0).toFixed(1)} 秒</strong></div>
      </div>
      <div class="report-note${missing.length ? " is-warn" : ""}">
        <b>${missing.length ? "!" : "✓"}</b>
        <span>${missing.length ? `缺少 ${missing.length} 个镜头：${escapeHtml(missing.join("、"))}` : "当前没有缺失镜头，或尚未合片。"}</span>
      </div>`;
    updateLaterStageNav();
  } catch (error) { showToast(error.message, true); }
}

function updateLaterStageNav() {
  const set = (stage, text, done) => {
    const button = document.querySelector(`.stage-item[data-stage="${stage}"]`);
    if (!button) return;
    button.querySelector("small").textContent = text;
    button.querySelector(".stage-status").textContent = done ? "✓" : "•";
    button.classList.toggle("is-done", Boolean(done));
  };
  const subtitles = state.subtitles;
  if (subtitles) {
    const count = (subtitles.cues || []).length;
    set(
      "字幕校对",
      count ? `${count} 条 · ${subtitles.aligned ? "已对齐人声" : "计划时间轴"}` : "对白与时间轴",
      count > 0 && subtitles.aligned,
    );
  }
}

async function acceptCurrentProposal() {
  const descApproved = workflowEntry("descriptions").status === "approved";
  if (state.inspirationImagePaths.length) await saveImageInspiration({ advance: false });
  showToast("已采用当前剧情提案");
  showStage(descApproved ? "故事规划" : "画面理解");
}

async function openJobHistory() {
  if (!state.runId) return showToast("请先选择项目", true);
  try {
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/jobs`);
    const jobs = body.jobs || [];
    $("#jobHistoryList").innerHTML = jobs.length ? jobs.slice().reverse().map((job) => `
      <article><strong>${escapeHtml(job.shot_id || job.job_id)}</strong>
      <small>${escapeHtml(job.status || "")} · ${escapeHtml(job.generation_mode || "")} · ${escapeHtml(job.updated_at || job.created_at || "")}</small>
      ${job.error ? `<p>${escapeHtml(job.error)}</p>` : ""}</article>`).join("") : '<div class="planning-empty">这个项目还没有生成任务。</div>';
    $("#jobHistoryDialog").showModal();
  } catch (error) { showToast(error.message, true); }
}

async function openProjectSettings() {
  if (!state.runId) return showToast("请先选择项目", true);
  try {
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/settings`);
    const run = body.run || {};
    const llm = body.llm || {};
    const comfy = body.comfyui || {};
    const health = body.health || {};
    $("#projectSettingsBody").innerHTML = [
      ["项目名", run.project_name],
      ["Run ID", run.run_id],
      ["状态", run.state],
      ["本地目录", body.run_dir],
      ["规划模型", `${llm.provider || ""} / ${llm.model || ""}`],
      ["ComfyUI", health.online ? `${comfy.base_url} · 在线` : `${comfy.base_url || ""} · 离线`],
      ["Motion Context", comfy.motion_context === false ? "关闭" : `${comfy.width || 864}×${comfy.height || 480} · ${comfy.steps || 8} steps`],
      ["H3 节点", health.motion_context_ready ? "Motion Context 已注册" : "Motion Context 未就绪"],
      ["生图", (body.image_generator && body.image_generator.model) || "未配置"],
    ].map(([label, value]) => `<div class="setting-row"><span>${label}</span><strong>${escapeHtml(String(value || "—"))}</strong></div>`).join("");
    $("#projectSettingsDialog").showModal();
  } catch (error) { showToast(error.message, true); }
}

function updateCount() {
  if ($("#charCount") && promptText) $("#charCount").textContent = `${promptText.textContent.length} 字符`;
}

$("#generateSubtitles")?.addEventListener("click", async () => {
  try {
    startAIProgress("subtitles", "正在按分镜对白生成计划字幕");
    await api(`/api/runs/${encodeURIComponent(state.runId)}/subtitles/generate`, { method: "POST", body: "{}" });
    await loadSubtitles();
    finishAIProgress("计划字幕已生成");
  } catch (error) { failAIProgress(error.message); showToast(error.message, true); }
});
$("#alignSubtitles")?.addEventListener("click", async () => {
  try {
    startAIProgress("subtitles", "正在用本机语音识别对齐成片人声，首次加载模型约需 1–3 分钟");
    const body = await api(`/api/runs/${encodeURIComponent(state.runId)}/subtitles/align`, { method: "POST", body: "{}" });
    await loadSubtitles();
    finishAIProgress(`已对齐 ${body.matched_cue_count || 0} 条字幕`);
  } catch (error) { failAIProgress(error.message); showToast(error.message, true); }
});
$("#saveSubtitles")?.addEventListener("click", async () => {
  try { await saveSubtitles(); }
  catch (error) { showToast(error.message, true); }
});
$("#burnSubtitles")?.addEventListener("click", async () => {
  try {
    await saveSubtitles({ quiet: true });
    startAIProgress("subtitles", "正在烧录硬字幕，视频重编码、音频原样保留");
    await api(`/api/runs/${encodeURIComponent(state.runId)}/subtitles/burn`, { method: "POST", body: "{}" });
    finishAIProgress("硬字幕已烧录，可在合片验收查看");
    await loadAssemble();
  } catch (error) { failAIProgress(error.message); showToast(error.message, true); }
});
$("#resetSubtitleStyle")?.addEventListener("click", async () => {
  try {
    await api(`/api/runs/${encodeURIComponent(state.runId)}/subtitles/style`, { method: "DELETE" });
    await loadSubtitles();
    showToast("已恢复默认院线字幕样式");
  } catch (error) { showToast(error.message, true); }
});
document.querySelectorAll("[data-subtitle-format]").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("[data-subtitle-format]").forEach((item) => item.classList.remove("is-selected"));
  button.classList.add("is-selected");
  state.subtitleFormat = button.dataset.subtitleFormat;
  renderSubtitlePreview();
}));
$("#runAssemble")?.addEventListener("click", async () => {
  try {
    startAIProgress("assemble", "正在拼接成片");
    await api(`/api/runs/${encodeURIComponent(state.runId)}/assemble`, { method: "POST", body: JSON.stringify({ burn_subtitles: true }) });
    await loadAssemble();
    finishAIProgress("合片完成");
  } catch (error) { failAIProgress(error.message); showToast(error.message, true); }
});
$("#openJobHistory")?.addEventListener("click", openJobHistory);
$("#closeJobHistory")?.addEventListener("click", () => $("#jobHistoryDialog").close());
$("#openProjectSettings")?.addEventListener("click", openProjectSettings);
$("#closeProjectSettings")?.addEventListener("click", () => $("#projectSettingsDialog").close());

promptText.addEventListener("input", () => {
  const shot = currentShot();
  if (shot) {
    const draft = draftFor(shot);
    beginDraftEdit(shot, draft);
    draft.prompt = promptText.textContent;
    renderShots();
  }
  updateCount();
});
updateCount();
boot();
