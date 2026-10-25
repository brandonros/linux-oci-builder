// Run with NODE_PATH pointing at the local Playwright installation.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

(async () => {
  const url = process.argv[2];
  const output = process.argv[3];
  if (!url || !output) throw new Error('usage: node editor-smoke.cjs URL OUTPUT');
  fs.mkdirSync(output, {recursive: false});
  const evidence = {url, started: new Date().toISOString(), errors: [], console: []};
  let browser, page;
  try {
    const deadline = Date.now() + 240000;
    while (true) {
      try {
        const response = await fetch(new URL('/healthz', url), {signal: AbortSignal.timeout(2000)});
        if (response.ok) break;
      } catch {}
      if (Date.now() > deadline) throw new Error('HTTP readiness timeout');
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    browser = await chromium.launch({headless: true, channel: 'chromium'});
    page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    page.on('pageerror', error => evidence.errors.push(String(error)));
    page.on('console', message => evidence.console.push({type: message.type(), text: message.text()}));
    page.setDefaultTimeout(240000);
    await page.goto(url, {waitUntil: 'domcontentloaded', timeout: 240000});
    await page.locator('.monaco-workbench').waitFor({state: 'visible'});
    evidence.workbench = true;
    await page.screenshot({path: path.join(output, 'workbench.png')});
    fs.writeFileSync(path.join(output, 'body.txt'), await page.locator('body').innerText());
    const trust = page.getByRole('button', {name: /Yes, I trust the authors/});
    if (await trust.count()) await trust.click();
    // Command Center's contributed activity entry establishes extension scan;
    // its demo ticket view establishes that the extension host activated it.
    const commandCenter = page.getByRole('tab', {name: /Command Center/});
    await commandCenter.click();
    await page.getByText('Tickets', {exact: true}).first().waitFor({state: 'visible'});
    evidence.commandCenterView = true;
    await page.getByText(/DEMO-101/).first().waitFor({state: 'visible'});
    evidence.commandCenterActivated = true;
    evidence.body = await page.locator('body').innerText();
    await page.screenshot({path: path.join(output, 'command-center.png')});
    evidence.complete = true;
  } catch (error) {
    evidence.failure = String(error);
    if (page && !page.isClosed()) {
      await page.screenshot({path: path.join(output, 'failure.png')}).catch(() => {});
      evidence.body = await page.locator('body').innerText().catch(() => '');
    }
    process.exitCode = 1;
  } finally {
    evidence.finished = new Date().toISOString();
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(evidence, null, 2));
    if (browser) await browser.close();
  }
})();
