// Captures the live Studio UI into stills used by the Remotion demo.
// Run with: node scripts/capture.mjs  (studio_server must be listening)
import { mkdir } from "node:fs/promises";
import path from "node:path";
import puppeteer from "puppeteer-core";

const BASE = process.env.STUDIO_BASE || "http://127.0.0.1:4173";
const OUT = path.resolve("public/shots");
const CHROME = path.resolve("node_modules/.remotion/chrome-headless-shell/linux64/chrome-headless-shell-linux64/chrome-headless-shell");

const VIEWPORT = { width: 1920, height: 1080, deviceScaleFactor: 1.5 };

const TARGETS = [
  { name: "gate", url: `${BASE}/`, settle: 4200 },
  { name: "assets", url: `${BASE}/studio#assets`, scroll: 520 },
  { name: "descriptions", url: `${BASE}/studio#descriptions`, scroll: 460 },
  { name: "story", url: `${BASE}/studio#story`, scroll: 620 },
  { name: "shots", url: `${BASE}/studio#shots`, scroll: 700 },
  { name: "orchestration", url: `${BASE}/studio#orchestration`, scroll: 560 },
  { name: "subtitles", url: `${BASE}/studio#subtitles`, scroll: 640 },
  { name: "assemble", url: `${BASE}/studio#assemble`, scroll: 420 },
];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Some media elements never settle (streamed range requests), so every wait
// here is capped instead of awaited indefinitely.
async function waitForQuiet(page) {
  await page.evaluate(async () => {
    const cap = (promise, ms) => Promise.race([promise, new Promise((resolve) => setTimeout(resolve, ms))]);
    await cap(
      Promise.all(
        Array.from(document.images)
          .filter((img) => !img.complete)
          .map((img) => new Promise((resolve) => { img.onload = img.onerror = resolve; })),
      ),
      4000,
    );
    if (document.fonts && document.fonts.ready) await cap(document.fonts.ready, 2000);
  }).catch(() => {});
}

// The stage content lives in an inner scroller, so find the tallest one.
async function scrollBy(page, amount) {
  await page.evaluate((delta) => {
    const nodes = [document.scrollingElement, ...document.querySelectorAll("main, .workspace, .stage-view, .stage-body, div")];
    let best = document.scrollingElement;
    let bestOverflow = 0;
    for (const node of nodes) {
      if (!node) continue;
      const overflow = node.scrollHeight - node.clientHeight;
      const style = node === document.scrollingElement ? null : getComputedStyle(node);
      const scrollable = node === document.scrollingElement || ["auto", "scroll"].includes(style.overflowY);
      if (scrollable && overflow > bestOverflow) { best = node; bestOverflow = overflow; }
    }
    best.scrollTop = Math.min(delta, best.scrollHeight - best.clientHeight);
  }, amount);
}

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: true,
  protocolTimeout: 90000,
  args: ["--no-sandbox", "--hide-scrollbars", "--force-color-profile=srgb", "--font-render-hinting=none", "--disable-lcd-text"],
  defaultViewport: VIEWPORT,
});

await mkdir(OUT, { recursive: true });
const page = await browser.newPage();
page.on("pageerror", (error) => console.warn("  page error:", error.message));

for (const target of TARGETS) {
  // A hash-only change would not re-run the app, so force a fresh document.
  await page.goto("about:blank");
  await page.goto(target.url, { waitUntil: "networkidle2", timeout: 60000 });
  await sleep(target.settle ?? 2600);
  await waitForQuiet(page);
  await page.screenshot({ path: path.join(OUT, `${target.name}-a.png`) });
  console.log(`captured ${target.name}-a.png`);

  if (target.scroll) {
    await scrollBy(page, target.scroll);
    await sleep(900);
    await waitForQuiet(page);
    await page.screenshot({ path: path.join(OUT, `${target.name}-b.png`) });
    console.log(`captured ${target.name}-b.png`);
  }
}

await browser.close();
console.log("done ->", OUT);
