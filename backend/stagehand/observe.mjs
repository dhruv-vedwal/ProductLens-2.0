import {Stagehand, browserbase, localBrowser} from "@browserbasehq/stagehand";

// JSON-lines bridge. It deliberately only observes; ProductLens' Python
// Playwright engine validates and executes every final semantic operation.
const read = await new Promise((resolve, reject) => {
  let input = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => input += chunk);
  process.stdin.on("end", () => { try { resolve(JSON.parse(input)); } catch (error) { reject(error); } });
});
let stagehand;
let browser;
try {
  if (read.environment === "BROWSERBASE") {
    if (!process.env.BROWSERBASE_API_KEY) {
      throw new Error("Browserbase API key is required for cloud observation");
    }
    // Stagehand v4 launches a browser with its extension/runtime installed.
    // A plain Playwright CDP session cannot be retrofitted with that runtime,
    // so ProductLens uses this short-lived read-only observation browser and
    // re-grounds every returned selector in the separate production session.
    browser = await browserbase.launch({
      apiKey: process.env.BROWSERBASE_API_KEY,
    });
  } else {
    browser = await localBrowser.launch({viewport: {width: 1440, height: 900}});
  }
  const stagehandOptions = {
    browser,
    selfHeal: false,
    domSettleTimeoutMs: read.domSettleTimeout ?? 10000,
  };
  // Browserbase Model Gateway authenticates via browserbase.launch(). With no
  // configured model, Stagehand selects a supported gateway model itself.
  // Never inject OpenRouter/provider placeholders here.
  if (typeof read.model === "string" && read.model.trim()) {
    stagehandOptions.model = {modelName: read.model.trim()};
  }
  stagehand = await Stagehand.create(stagehandOptions);
  const pages = await browser.context.pages();
  const page = pages[0];
  if (!page) throw new Error("Stagehand initialized without an active page");
  await page.goto(read.url);
  const observed = await stagehand.observe(read.instruction);
  const candidates = (observed.data ?? []).map((item) => ({
    selector: item.selector,
    description: item.description,
    method: item.method,
    arguments: item.arguments ?? [],
  }));
  process.stdout.write(JSON.stringify({
    version: 1,
    environment: read.environment,
    observedUrl: page.url(),
    candidates,
    metrics: {...await stagehand.metrics(), stagehandSessionId: browser.sessionId ?? null},
  }));
} catch (error) {
  process.stderr.write(error?.message ?? String(error));
  process.exitCode = 1;
} finally {
  if (stagehand) await stagehand.close();
  else if (browser) await browser.close();
}
