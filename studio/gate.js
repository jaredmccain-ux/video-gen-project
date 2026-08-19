const BRAND = "SCENEFLOW";
const STUDIO_URL = "/studio";

// 5x5 block alphabet used to draw the banner, so renaming the gate only means
// editing BRAND above.
const GLYPHS = {
  A: ".###.|#...#|#####|#...#|#...#",
  B: "####.|#...#|####.|#...#|####.",
  C: ".####|#....|#....|#....|.####",
  D: "####.|#...#|#...#|#...#|####.",
  E: "#####|#....|####.|#....|#####",
  F: "#####|#....|####.|#....|#....",
  G: ".####|#....|#..##|#...#|.####",
  H: "#...#|#...#|#####|#...#|#...#",
  I: "#####|..#..|..#..|..#..|#####",
  J: "#####|...#.|...#.|#..#.|.##..",
  K: "#...#|#..#.|###..|#..#.|#...#",
  L: "#....|#....|#....|#....|#####",
  M: "#...#|##.##|#.#.#|#...#|#...#",
  N: "#...#|##..#|#.#.#|#..##|#...#",
  O: ".###.|#...#|#...#|#...#|.###.",
  P: "####.|#...#|####.|#....|#....",
  Q: ".###.|#...#|#.#.#|#..#.|.##.#",
  R: "####.|#...#|####.|#..#.|#...#",
  S: ".####|#....|.###.|....#|####.",
  T: "#####|..#..|..#..|..#..|..#..",
  U: "#...#|#...#|#...#|#...#|.###.",
  V: "#...#|#...#|#...#|.#.#.|..#..",
  W: "#...#|#...#|#.#.#|##.##|#...#",
  X: "#...#|.#.#.|..#..|.#.#.|#...#",
  Y: "#...#|.#.#.|..#..|..#..|..#..",
  Z: "#####|...#.|..#..|.#...|#####",
  "0": ".###.|#..##|#.#.#|##..#|.###.",
  "1": "..#..|.##..|..#..|..#..|.###.",
  "2": ".###.|#...#|..##.|.#...|#####",
  "3": "####.|....#|.###.|....#|####.",
  "4": "#..#.|#..#.|#####|...#.|...#.",
  "5": "#####|#....|####.|....#|####.",
  "6": ".###.|#....|####.|#...#|.###.",
  "7": "#####|....#|...#.|..#..|..#..",
  "8": ".###.|#...#|.###.|#...#|.###.",
  "9": ".###.|#...#|.####|....#|.###.",
  " ": ".....|.....|.....|.....|.....",
};

const STAGES = [
  ["01", "素材准备", "灵感图片、视频与音频入库"],
  ["02", "画面理解", "多模态模型只写看得见的事实"],
  ["03", "故事规划", "骨架、正式剧本与段落节奏"],
  ["04", "分镜拆分", "构图、机位、动作与对白"],
  ["05", "人工编排", "逐镜确认生成模式与输入图"],
  ["06", "字幕校对", "人声对齐、样式与硬字幕烧录"],
  ["07", "合片验收", "拼接、烧录与技术检查"],
];

const consoleEl = document.getElementById("console");
const promptLine = document.getElementById("promptLine");
const input = document.getElementById("command");
const caret = document.getElementById("caret");
const typedBefore = document.getElementById("typedBefore");
const typedAfter = document.getElementById("typedAfter");

const history = [];
let historyIndex = 0;
let booted = false;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

function renderBanner(text) {
  const rows = ["", "", "", "", ""];
  for (const char of text.toUpperCase()) {
    const glyph = GLYPHS[char] || GLYPHS[" "];
    glyph.split("|").forEach((row, index) => {
      rows[index] += `${row.replace(/#/g, "█").replace(/\./g, " ")} `;
    });
  }
  return rows.map((row) => row.replace(/\s+$/, "")).join("\n");
}

function print(html, className = "") {
  const line = document.createElement("div");
  line.className = `line ${className}`.trim();
  line.innerHTML = html;
  consoleEl.append(line);
  return line;
}

function printStatus(state, text) {
  const marks = { ok: "[  ok  ]", boot: "[ boot ]", warn: "[ warn ]" };
  print(`<b>${marks[state] || marks.ok}</b><span>${text}</span>`, state === "warn" ? "is-warn" : state === "boot" ? "is-muted" : "is-ok");
}

function typeLine(text, className = "") {
  return new Promise((resolve) => {
    const line = print("", className);
    let index = 0;
    const tick = () => {
      line.textContent = text.slice(0, (index += 1));
      if (index < text.length) setTimeout(tick, 18);
      else resolve();
    };
    tick();
  });
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function fetchJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(String(response.status));
  return response.json();
}

async function boot() {
  document.getElementById("banner").textContent = renderBanner(BRAND);
  await wait(420);

  let version = "—";
  try {
    const health = await fetchJson("/api/health");
    version = health.ui_build || "—";
    printStatus("boot", `SceneFlow gate · ui build ${escapeHtml(version)}`);
    await wait(110);
    const comfy = health.comfyui || {};
    if (comfy.online) printStatus("ok", `ComfyUI 在线 · ${escapeHtml(comfy.devices ?? 0)} GPU · ${escapeHtml(comfy.base_url || "")}`);
    else printStatus("warn", "ComfyUI 离线 · 仍可编排，生成会排队失败");
  } catch (error) {
    printStatus("warn", "后端未响应 · 请确认 studio_server 正在运行");
  }

  await wait(110);
  try {
    const bootstrap = await fetchJson("/api/bootstrap");
    const runs = bootstrap.runs || [];
    const shots = runs.reduce((sum, run) => sum + Number(run.shot_count || 0), 0);
    printStatus("ok", `本地项目 ${runs.length} 个 · 累计 ${shots} 个镜头`);
  } catch (error) {
    printStatus("warn", "读取本地项目失败");
  }

  await wait(180);
  print("&nbsp;");
  print(`Visit <a class="gate-link" href="${STUDIO_URL}">制作台</a>`, "is-muted");
  await typeLine("Type help to start", "is-muted");
  booted = true;
  promptLine.hidden = false;
  syncEntry();
  input.focus();
}

/* ---------- commands ---------- */

const COMMANDS = {
  help: {
    describe: "列出所有可用命令",
    run() {
      const rows = Object.entries(COMMANDS)
        .map(([name, item]) => `<dt>${name}</dt><dd>${item.describe}</dd>`)
        .join("");
      print(`<dl class="help-grid">${rows}</dl>`);
    },
  },
  start: {
    describe: "进入制作台（等同 enter / studio）",
    async run() {
      await typeLine("正在进入制作台 …", "is-accent");
      window.location.href = STUDIO_URL;
    },
  },
  flow: {
    describe: "查看七个制作阶段",
    run() {
      STAGES.forEach(([index, name, note]) => {
        print(`<b>${index}</b><em>${name}</em><span>· ${note}</span>`, "stage-row");
      });
    },
  },
  runs: {
    describe: "列出本地项目",
    async run() {
      try {
        const bootstrap = await fetchJson("/api/bootstrap");
        const runs = bootstrap.runs || [];
        if (!runs.length) return print("还没有项目，进入制作台后新建一个。", "is-muted");
        runs.forEach((run) => {
          const active = run.run_id === bootstrap.default_run_id ? " ←" : "";
          print(`<span class="key">${escapeHtml(run.project_name || run.run_id)}</span><span class="val">· ${escapeHtml(run.shot_count ?? 0)} 镜 · ${escapeHtml(run.state || "")}${active}</span>`);
        });
      } catch (error) {
        print("读取项目失败：后端未响应。", "is-warn");
      }
    },
  },
  status: {
    describe: "后端与 ComfyUI 状态",
    async run() {
      try {
        const health = await fetchJson("/api/health");
        const comfy = health.comfyui || {};
        print(`<span class="key">ui build</span><span class="val">${escapeHtml(health.ui_build || "—")}</span>`);
        print(`<span class="key">server</span><span class="val">${health.ok ? "online" : "degraded"} · ${escapeHtml(health.time || "")}</span>`);
        print(`<span class="key">comfyui</span><span class="val">${comfy.online ? "online" : "offline"} · ${escapeHtml(comfy.base_url || "—")}</span>`);
      } catch (error) {
        print("后端未响应。", "is-warn");
      }
    },
  },
  about: {
    describe: "这是什么",
    run() {
      print("SceneFlow 是 MiniMax H3 的短剧生成流水线工作台。", "is-muted");
      print("从灵感素材到成片，七个阶段串成一条线；每一步的产出都可以人工修改后再批准。", "is-muted");
      print("原则：机器负责执行，镜头怎么拍由人决定。", "is-muted");
    },
  },
  whoami: {
    describe: "你是谁",
    run() {
      print("director · 拥有每一镜的最终决定权", "is-accent");
    },
  },
  date: {
    describe: "当前时间",
    run() {
      print(new Date().toLocaleString("zh-CN", { hour12: false }), "is-muted");
    },
  },
  clear: {
    describe: "清屏",
    run() {
      consoleEl.innerHTML = "";
    },
  },
  exit: {
    describe: "离开",
    run() {
      print("这里没有出口，只有下一镜。试试 start。", "is-muted");
    },
  },
};

const ALIASES = { enter: "start", studio: "start", open: "start", ls: "flow", stages: "flow", projects: "runs", "?": "help", man: "help", cls: "clear", now: "date", quit: "exit" };

async function execute(raw) {
  const text = raw.trim();
  print(`<span class="prompt"><b>λ</b><i>::</i><u>~</u><s>&gt;&gt;</s></span><span class="cmd">${escapeHtml(text)}</span>`, "echo");
  if (!text) return;
  history.push(text);
  historyIndex = history.length;

  const [head, ...rest] = text.split(/\s+/);
  const key = (ALIASES[head.toLowerCase()] || head.toLowerCase());
  if (key === "sudo") {
    print(rest.length ? `权限被拒绝：${escapeHtml(rest.join(" "))} 需要导演本人确认。` : "sudo: 需要一个动词。", "is-warn");
    return;
  }
  const command = COMMANDS[key];
  if (!command) {
    print(`未知命令：${escapeHtml(head)} · 输入 <span class="key">help</span> 查看可用命令。`, "is-warn");
    return;
  }
  await command.run(rest);
}

/* ---------- input plumbing ---------- */

function syncEntry() {
  const value = input.value;
  const position = input.selectionStart ?? value.length;
  typedBefore.textContent = value.slice(0, position);
  const under = value.slice(position, position + 1);
  caret.textContent = under || "\u00a0";
  caret.classList.toggle("has-char", Boolean(under));
  typedAfter.textContent = value.slice(position + 1);
}

input.addEventListener("input", syncEntry);
input.addEventListener("click", syncEntry);
input.addEventListener("keyup", syncEntry);
input.addEventListener("focus", () => caret.classList.remove("is-idle"));
input.addEventListener("blur", () => caret.classList.add("is-idle"));

input.addEventListener("keydown", async (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    const value = input.value;
    input.value = "";
    syncEntry();
    await execute(value);
    window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
    return;
  }
  if (event.key === "ArrowUp" || event.key === "ArrowDown") {
    if (!history.length) return;
    event.preventDefault();
    historyIndex = Math.max(0, Math.min(history.length, historyIndex + (event.key === "ArrowUp" ? -1 : 1)));
    input.value = history[historyIndex] || "";
    input.setSelectionRange(input.value.length, input.value.length);
    syncEntry();
    return;
  }
  if (event.key === "Tab") {
    event.preventDefault();
    const typed = input.value.trim().toLowerCase();
    if (!typed) return;
    const names = [...Object.keys(COMMANDS), ...Object.keys(ALIASES)];
    const matches = names.filter((name) => name.startsWith(typed));
    if (matches.length === 1) {
      input.value = matches[0];
      syncEntry();
    } else if (matches.length > 1) {
      print(matches.join("   "), "is-muted");
    }
    return;
  }
  if (event.key === "l" && event.ctrlKey) {
    event.preventDefault();
    consoleEl.innerHTML = "";
  }
});

document.addEventListener("click", (event) => {
  if (booted && !event.target.closest("a")) input.focus();
});

document.addEventListener("keydown", (event) => {
  if (booted && document.activeElement !== input && !event.metaKey && !event.ctrlKey && !event.altKey) input.focus();
});

// Old bookmarks pointed at "/#stage"; keep them working by forwarding.
if (window.location.hash) window.location.replace(`${STUDIO_URL}${window.location.hash}`);
else boot();
