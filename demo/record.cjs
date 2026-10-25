// Record the replay page in a real Chromium window at 1280x720 and write an mp4.
//   node demo/record.cjs scratch/demo docs/demo.mp4
// Playwright is found through the shared loader in the Professional workspace (PLAYWRIGHT_PATH overrides it).
// OBSERVE-OK: the page is this repo's own replay; it sets window.__done when its timeline ends, and each 300 ms poll reads that flag.
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

function loadPlaywright() {
  if (process.env.PLAYWRIGHT_PATH) return require(process.env.PLAYWRIGHT_PATH);
  try { return require('playwright'); } catch (e) { /* fall through to the workspace loader */ }
  const loader = path.resolve(__dirname, '..', '..', '..', '..', 'Operations', 'tools', 'playwright_path.cjs');
  return require(loader).loadPlaywright();
}

(async () => {
  const [dir, out] = process.argv.slice(2);
  const { chromium } = loadPlaywright();
  const videoDir = path.join(dir, 'video');
  fs.rmSync(videoDir, { recursive: true, force: true });
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 }, recordVideo: { dir: videoDir, size: { width: 1280, height: 720 } } });
  const page = await ctx.newPage();
  await page.goto('file://' + path.resolve(dir, 'index.html'));
  const started = Date.now();
  let done = false;
  while (!done && Date.now() - started < 120000) {
    done = await page.evaluate(() => window.__done === true);
    if (!done) await page.waitForTimeout(300);
  }
  await page.screenshot({ path: path.join(dir, 'last-frame.png') });
  console.log('page finished:', done, 'after', ((Date.now() - started) / 1000).toFixed(1), 's');
  await page.waitForTimeout(500);
  await ctx.close();
  await browser.close();
  const webm = fs.readdirSync(videoDir).find((f) => f.endsWith('.webm'));
  execFileSync('ffmpeg', ['-y', '-i', path.join(videoDir, webm), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-an', '-map_metadata', '-1', out], { stdio: 'inherit' });
  console.log('wrote', out);
  process.exit(done ? 0 : 1);
})();
