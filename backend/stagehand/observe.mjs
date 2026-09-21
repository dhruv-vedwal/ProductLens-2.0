import {Stagehand, browserbase, localBrowser} from "@browserbasehq/stagehand";
import {z} from "zod/v4";

// JSON-lines bridge. Observation and bounded rehearsal are exposed here;
// ProductLens' Python interaction kernel remains the authority for policy,
// production dispatch, verification, and the final trace.
const read = await new Promise((resolve, reject) => {
  let input = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => input += chunk);
  process.stdin.on("end", () => { try { resolve(JSON.parse(input)); } catch (error) { reject(error); } });
});
let stagehand;
let browser;
let extensionDiscoverySocket;
// ProductLens owns the Browserbase session in this mode. Stagehand attaches
// only to enrich an already-authenticated state; closing the Stagehand client
// must not close the shared remote context that Playwright is recording.
const attachedExistingSession = Boolean(
  read.environment === "BROWSERBASE" &&
  // A session id is sufficient ownership proof.  The signed CDP URL may be
  // unavailable on a transient provider response, but Stagehand must still
  // never close ProductLens' shared production session on process teardown.
  read.browserbaseSessionID
);
// URL equality must be about the product origin, not the exact serialized
// URL. Browserbase may expose an HTTP entry URL as HTTPS after redirect, and
// sessions can retain query/hash state from the previous page. Comparing the
// normalized origin prevents a false page mismatch and a duplicate opening
// navigation while still rejecting cross-origin observations.
const originKey = (value) => {
  try {
    const parsed = new URL(value);
    if (!['http:', 'https:'].includes(parsed.protocol)) return null;
    const port = parsed.port || (parsed.protocol === 'https:' ? '443' : '80');
    return `${parsed.hostname.toLowerCase().replace(/\.$/, '')}:${port}`;
  } catch (_) {
    return null;
  }
};
const sameProductOrigin = (requested, observed) => {
  const requestedKey = originKey(requested);
  const observedKey = originKey(observed);
  if (!requestedKey || !observedKey) return false;
  if (requestedKey === observedKey) return true;
  // A public HTTP entry point commonly upgrades to HTTPS. Never permit the
  // reverse downgrade, and keep the hostname/standard ports exact.
  try {
    const request = new URL(requested);
    const target = new URL(observed);
    return request.protocol === 'http:' && target.protocol === 'https:' &&
      request.hostname.toLowerCase() === target.hostname.toLowerCase() &&
      (request.port || '80') === '80' && (target.port || '443') === '443';
  } catch (_) {
    return false;
  }
};

// Browserbase exposes the signed CDP endpoint to ProductLens, while the
// Stagehand extension that is preloaded in the Browserbase Chrome has a
// Chrome extension id (the id returned by the Extensions API is not that
// runtime id). Discover the preloaded worker through CDP instead of baking a
// provider- or project-specific id into the bridge. This also works when
// Browserbase rotates its extension build.
const resolveCdpWebSocketUrl = async (cdpUrl) => {
  if (cdpUrl.startsWith("ws://") || cdpUrl.startsWith("wss://")) return cdpUrl;
  const response = await fetch(`${cdpUrl.replace(/\/$/, "")}/json/version`);
  if (!response.ok) throw new Error(`CDP version endpoint returned ${response.status}`);
  const version = await response.json();
  if (!version?.webSocketDebuggerUrl) throw new Error("CDP version endpoint did not return a websocket URL");
  return version.webSocketDebuggerUrl;
};

const cdpCommand = async (socket, id, method, params = {}) => {
  return await new Promise((resolve, reject) => {
    const onMessage = (event) => {
      let value;
      try { value = JSON.parse(typeof event.data === "string" ? event.data : String(event.data)); }
      catch (_) { return; }
      if (value?.id !== id) return;
      socket.removeEventListener("message", onMessage);
      socket.removeEventListener("error", onError);
      if (value.error) reject(new Error(value.error.message || `CDP ${method} failed`));
      else resolve(value.result || {});
    };
    const onError = () => {
      socket.removeEventListener("message", onMessage);
      socket.removeEventListener("error", onError);
      reject(new Error("CDP websocket failed while discovering Stagehand extension"));
    };
    socket.addEventListener("message", onMessage);
    socket.addEventListener("error", onError);
    // Install listeners before dispatching: Browserbase can answer a small
    // CDP command in the same turn as send(), and registering afterwards can
    // lose the response and make an otherwise healthy session look offline.
    try {
      socket.send(JSON.stringify({id, method, params}));
    } catch (error) {
      socket.removeEventListener("message", onMessage);
      socket.removeEventListener("error", onError);
      reject(error);
    }
  });
};

const discoverPreloadedStagehandExtension = async (cdpUrl) => {
  const socket = new WebSocket(await resolveCdpWebSocketUrl(cdpUrl));
  await new Promise((resolve, reject) => {
    const onOpen = () => { socket.removeEventListener("open", onOpen); socket.removeEventListener("error", onError); resolve(); };
    const onError = () => { socket.removeEventListener("open", onOpen); socket.removeEventListener("error", onError); reject(new Error("CDP websocket could not be opened")); };
    socket.addEventListener("open", onOpen);
    socket.addEventListener("error", onError);
  });
  try {
    const targets = await cdpCommand(socket, 1, "Target.getTargets");
    const worker = (targets.targetInfos || []).find((target) =>
      target.type === "service_worker" &&
      typeof target.url === "string" &&
      target.url.startsWith("chrome-extension://") &&
      target.url.includes("/service-worker.js")
    );
    if (!worker) throw new Error("Browserbase session has no preloaded Stagehand service worker");
    const match = worker.url.match(/^chrome-extension:\/\/([^/]+)\//);
    if (!match?.[1]) throw new Error("Stagehand service-worker URL has no extension id");
    // Keep this discovery socket alive until Stagehand has attached its own
    // CDP client. Browserbase accepts concurrent connections, but can reject
    // a second connection if the first one is closed during the hand-off.
    return {extensionId: match[1], socket};
  } catch (error) {
    try { socket.close(); } catch (_) {}
    throw error;
  }
};

const openRouterContent = (content) => {
  const blocks = Array.isArray(content) ? content : [content];
  return blocks.map((block) => {
    if (block?.type === "text") return {type: "text", text: block.text};
    if (block?.type === "image") {
      return {type: "image_url", image_url: {url: `data:${block.mimeType};base64,${block.data}`}};
    }
    if (block?.type === "tool_use") {
      return {type: "text", text: JSON.stringify({tool_use: block})};
    }
    if (block?.type === "tool_result") {
      return {type: "text", text: JSON.stringify({tool_result: block})};
    }
    return {type: "text", text: String(block ?? "")};
  });
};

const openRouterGenerate = async (input) => {
  const apiKey = process.env.OPENROUTER_API_KEY;
  if (!apiKey) throw new Error("OpenRouter API key is required for the Stagehand client model");
  const messages = [];
  if (input.systemPrompt) messages.push({role: "system", content: input.systemPrompt});
  for (const message of input.messages || []) {
    messages.push({role: message.role, content: openRouterContent(message.content)});
  }
  const body = {
    model: process.env.OPENROUTER_MODEL || read.model,
    messages,
    ...(typeof input.temperature === "number" ? {temperature: input.temperature} : {}),
    ...(Array.isArray(input.stopSequences) && input.stopSequences.length ? {stop: input.stopSequences} : {}),
  };
  if (Array.isArray(input.tools) && input.tools.length) {
    body.tools = input.tools.map((tool) => ({type: "function", function: {
      name: tool.name, description: tool.description || "", parameters: tool.inputSchema || {type: "object"},
    }}));
    if (input.toolChoice?.mode) body.tool_choice = input.toolChoice.mode;
  }
  if (input.responseFormat?.type === "json_schema") {
    body.response_format = {
      type: "json_schema",
      json_schema: {
        name: input.responseFormat.name,
        description: input.responseFormat.description,
        strict: true,
        schema: input.responseFormat.schema,
      },
    };
  }
  const response = await fetch("https://openrouter.ai/api/v1/chat/completions", {
    method: "POST",
    headers: {"Authorization": `Bearer ${apiKey}`, "Content-Type": "application/json", "HTTP-Referer": "https://productlens.local"},
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`OpenRouter Stagehand inference returned ${response.status}`);
  const payload = await response.json();
  const message = payload?.choices?.[0]?.message;
  if (!message) throw new Error("OpenRouter Stagehand inference returned no message");
  const contentText = typeof message.content === "string" ? message.content : JSON.stringify(message.content ?? "");
  const content = [{type: "text", text: contentText}];
  if (Array.isArray(message.tool_calls)) {
    for (const call of message.tool_calls) {
      let parsedInput = {};
      try { parsedInput = JSON.parse(call.function?.arguments || "{}"); } catch (_) {}
      content.push({type: "tool_use", id: call.id || "tool", name: call.function?.name || "tool", input: parsedInput});
    }
  }
  const usage = payload.usage ? {
    inputTokens: Number(payload.usage.prompt_tokens || 0),
    outputTokens: Number(payload.usage.completion_tokens || 0),
    totalTokens: Number(payload.usage.total_tokens || 0),
  } : undefined;
  if (input.responseFormat?.type === "json_schema") {
    let structuredContent;
    try { structuredContent = JSON.parse(contentText); }
    catch (_) { throw new Error("OpenRouter Stagehand structured response was not valid JSON"); }
    return {role: "assistant", content, outputFormat: "json_schema", structuredContent, usage};
  }
  return {role: "assistant", content, outputFormat: "text", stopReason: payload.choices?.[0]?.finish_reason, usage};
};
const readPageUrl = async (candidate, fallback = null) => {
  const raw = await candidate.url();
  if (typeof raw === "string") return raw;
  if (raw && typeof raw === "object" && typeof raw.url === "string") return raw.url;
  return fallback;
};
try {
  if (read.environment === "BROWSERBASE") {
    if (!process.env.BROWSERBASE_API_KEY) {
      throw new Error("Browserbase API key is required for cloud observation");
    }
    // An authenticated ProductLens discovery session is the source of truth.
    // Stagehand v4 can attach its extension/runtime to that existing
    // Browserbase session, retaining the authenticated state and avoiding a
    // second CAPTCHA/login or duplicated opening load. A launch is only for
    // an explicitly standalone observation.
    // The provider provisions the official extension on the existing session,
    // so Stagehand can use its supported Browserbase session-ID connection
    // with that extension. A direct CDP attach remains a compatibility path
    // for callers that do not have a session ID.
    browser = read.browserbaseSessionID && read.browserbaseConnectUrl
      ? await (async () => {
          // Browserbase's retrieve-session response does not reliably include
          // the original connectUrl. ProductLens already owns that signed CDP
          // endpoint, so attach directly to the exact authenticated browser
          // first; this also avoids a second session lookup/handshake.
          try {
            // The signed CDP endpoint is the source of truth for this
            // session. The uploaded extension ID lets Stagehand bind to the
            // service worker already provisioned in that Browserbase page;
            // without it, localBrowser.connect attempts Extensions.loadUnpacked
            // (unsupported by Browserbase's Chrome build).
            const discovered = await discoverPreloadedStagehandExtension(read.browserbaseConnectUrl);
            extensionDiscoverySocket = discovered.socket;
            return await localBrowser.connect({
              cdpUrl: read.browserbaseConnectUrl,
              extensionId: discovered.extensionId,
            });
          } catch (error) {
            // The session-id connector cannot recover a missing connectUrl on
            // Browserbase's retrieve endpoint. Surface the direct attachment
            // failure instead of masking it with a second guaranteed lookup;
            // the Python boundary records this as a repairable provider error.
            throw new Error(`direct CDP attachment failed: ${String(error?.message ?? error).slice(0, 300)}`);
          }
        })()
      : read.browserbaseSessionID
      ? await (async () => {
          const connectOptions = {
            apiKey: process.env.BROWSERBASE_API_KEY,
            sessionId: read.browserbaseSessionID,
            ...(read.stagehandExtensionId
              ? {extensionId: read.stagehandExtensionId}
              : {}),
          };
          try {
            // ProductLens provisions the official extension on the session
            // before Playwright connects. Prefer that exact extension so the
            // advisory runtime observes the same page state.
            return await browserbase.connect(connectOptions);
          } catch (error) {
            // Browserbase can reject a just-uploaded extension while the
            // session is already usable (for example during extension
            // propagation). Retry attachment once with the SDK's preloaded
            // extension; this changes no product state and avoids turning a
            // transient extension race into a lost discovery run.
            if (!read.stagehandExtensionId) throw error;
            return await browserbase.connect({
              apiKey: process.env.BROWSERBASE_API_KEY,
              sessionId: read.browserbaseSessionID,
            });
          }
        })()
      : read.browserbaseConnectUrl
        ? await localBrowser.connect({cdpUrl: read.browserbaseConnectUrl})
        : await browserbase.launch({
            apiKey: process.env.BROWSERBASE_API_KEY,
          });
  } else {
    browser = await localBrowser.launch({viewport: {width: 1440, height: 900}});
  }
  const stagehandOptions = {
    browser,
    selfHeal: false,
    domSettleTimeoutMs: read.domSettleTimeout ?? 10000,
    // The bridge is a JSON-lines protocol. Disable Stagehand's supported v4
    // logging channel so diagnostics can never corrupt the single JSON
    // response written to stdout.
    logging: {level: "off", format: "json"},
  };
  if (read.environment === "BROWSERBASE" && process.env.BROWSERBASE_API_KEY) {
    // A local CDP attachment does not carry Browserbase's worker metadata.
    // Supplying the secret only to Stagehand's in-memory init enables the
    // Model Gateway and server-side caching without writing it to the JSON
    // bridge, trace, screenshots, or logs.
    stagehandOptions.apiKey = process.env.BROWSERBASE_API_KEY;
    stagehandOptions.cache = true;
    if (read.browserbaseSessionID) {
      // localBrowser.connect() intentionally has no provider metadata. The
      // public workerInitMetadata field is merged into Stagehand's protocol
      // init by v4; attach only the opaque session id and API key in memory
      // so Model Gateway can associate inference with this exact session.
      browser.workerInitMetadata = {
        apiKey: process.env.BROWSERBASE_API_KEY,
        browser: {sessionId: read.browserbaseSessionID},
      };
    }
  }
  // Browserbase Model Gateway authenticates via browserbase.launch(). With no
  // configured model, Stagehand selects a supported gateway model itself.
  // Never inject OpenRouter/provider placeholders here.
  if (read.environment === "BROWSERBASE" && process.env.OPENROUTER_API_KEY) {
    // Browserbase sessions can expose the Stagehand extension over CDP while
    // Model Gateway availability varies by account/session. Use the same
    // configured OpenRouter provider through Stagehand's official client-LLM
    // callback so observation remains automatic and provider-independent.
    stagehandOptions.model = {generate: openRouterGenerate};
  } else if (typeof read.model === "string" && read.model.trim()) {
    stagehandOptions.model = {modelName: read.model.trim()};
  }
  stagehand = await Stagehand.create(stagehandOptions);
  const pages = await browser.context.pages();
  const requestedOrigin = originKey(read.url);
  const pageRecords = await Promise.all(pages.map(async (candidate) => ({
    candidate, url: await readPageUrl(candidate),
  })));
  const page = pageRecords.find(({url}) => {
    return requestedOrigin && url && sameProductOrigin(read.url, url);
  })?.candidate ?? pageRecords.find(({url}) => {
    return url && url !== "about:blank";
  })?.candidate ?? pages[0];
  if (!page) throw new Error("Stagehand initialized without an active page");
  if (!read.browserbaseSessionID) {
    await page.goto(read.url);
  } else {
    // A newly attached Browserbase page can briefly report an empty URL while
    // its first document is being adopted. Treat that as an uninitialized
    // page and navigate once; only reject a valid URL that is truly outside
    // the requested origin. This avoids both Invalid URL failures and an
    // unnecessary duplicate opening navigation.
    const sameOrigin = sameProductOrigin(read.url, await readPageUrl(page, read.url));
    if (!sameOrigin) {
      await page.goto(read.url);
    }
  }
  if (read.mode === "rehearse_agent") {
    // A free-form agent is intentionally restricted to isolated rehearsal.
    // Production must dispatch one ProductLens-approved candidate at a time
    // so an LLM cannot create an unverified side effect or bypass the trace.
    if (read.rehearsal !== true) throw new Error("rehearse_agent requires rehearsal=true");
    if (typeof read.instruction !== "string" || !read.instruction.trim()) {
      throw new Error("rehearse_agent requires an instruction");
    }
    const maxSteps = Number.isInteger(read.maxSteps) ? Math.max(1, Math.min(40, read.maxSteps)) : 12;
    const agentOptions = {
      mode: read.agentMode === "dom" ? "dom" : "hybrid",
      ...(typeof read.model === "string" && read.model.trim() ? {model: read.model.trim()} : {}),
    };
    const agent = stagehand.agent(agentOptions);
    const result = await agent.execute({
      instruction: read.instruction.trim(),
      maxSteps,
      highlightCursor: false,
    });
    process.stdout.write(JSON.stringify({
      version: 4,
      mode: "rehearse_agent",
      environment: read.environment,
      observedUrl: ((value) => value && value !== "about:blank" ? value : read.url)(await readPageUrl(page, read.url)),
      result: {
        success: Boolean(result?.success),
        message: String(result?.message ?? "").slice(0, 1000),
        actions: Array.isArray(result?.actions) ? result.actions.map((item) => ({
          type: String(item?.type ?? ""),
          pageUrl: typeof item?.pageUrl === "string" ? item.pageUrl : null,
          taskCompleted: Boolean(item?.taskCompleted),
          reasoning: typeof item?.reasoning === "string" ? item.reasoning.slice(0, 500) : null,
          timestamp: Number.isFinite(item?.timestamp) ? item.timestamp : null,
        })) : [],
      },
      metrics: {...await stagehand.metrics(), stagehandSessionId: browser.sessionId ?? null},
    }));
  } else if (read.mode === "act_observed") {
    const action = read.action;
    if (!action || typeof action.selector !== "string" || typeof action.description !== "string" ||
        typeof action.method !== "string" || !Array.isArray(action.arguments)) {
      throw new Error("act_observed requires a previously observed action");
    }
    if (action.method.toLowerCase() !== "click" || action.arguments.length !== 0) {
      throw new Error("act_observed only permits a zero-argument observed click");
    }
    // Passing the exact observation to act avoids another model decision. The
    // Python caller has already accepted only a DOM-grounded, read-only probe;
    // this bridge never receives credentials, form values, or free-form task
    // instructions in action mode.
    const result = await stagehand.act(action, {page});
    process.stdout.write(JSON.stringify({
      version: 3,
      mode: "act_observed",
      environment: read.environment,
      observedUrl: ((value) => value && value !== "about:blank" ? value : read.url)(await readPageUrl(page, read.url)),
      result: {
        success: Boolean(result?.success),
        message: String(result?.message ?? "").slice(0, 500),
        action: String(result?.action ?? action.description).slice(0, 500),
      },
      metrics: {...await stagehand.metrics(), stagehandSessionId: browser.sessionId ?? null},
    }));
  } else {
  const observed = await stagehand.observe(read.instruction, {page});
  const observedData = Array.isArray(observed?.data)
    ? observed.data
    : Array.isArray(observed)
      ? observed
      : [];
  const candidates = observedData.map((item) => ({
    selector: item.selector,
    description: item.description,
    method: item.method,
    arguments: item.arguments ?? [],
  }));
  // Observation finds semantic controls; extraction separately asks for a
  // compact description of what is visibly present now.  ProductLens still
  // re-ground every returned phrase against its Playwright DOM before using
  // it as evidence, so this can improve understanding without granting the
  // agent authority to invent facts or act on the product.
  let analysis = null;
  let analysisError = null;
  if (typeof read.analysisInstruction === "string" && read.analysisInstruction.trim()) {
    const schema = z.object({
      // Model Gateway responses are advisory and may omit an empty category;
      // optional arrays keep a valid partial extraction from becoming a hard
      // provider failure while the Python layer still validates every phrase
      // against the live DOM.
      visibleSections: z.array(z.string().min(1).max(300)).max(16).optional().default([]),
      meaningfulControls: z.array(z.string().min(1).max(300)).max(16).optional().default([]),
      safeNextActions: z.array(z.string().min(1).max(300)).max(16).optional().default([]),
    });
    try {
      const extracted = await stagehand.extract(read.analysisInstruction, schema, {page});
      analysis = extracted.data ?? extracted;
    } catch (error) {
      // Extraction is an advisory enrichment.  A gateway/model schema miss
      // must not discard valid observe() actions; Playwright still
      // re-grounds those actions and remains the evidence authority.
      analysisError = String(error?.message ?? error).slice(0, 500);
    }
  }
  process.stdout.write(JSON.stringify({
    version: 2,
    environment: read.environment,
    observedUrl: ((value) => value && value !== "about:blank" ? value : read.url)(await readPageUrl(page, read.url)),
    candidates,
    analysis,
    analysisError,
    metrics: {...await stagehand.metrics(), stagehandSessionId: browser.sessionId ?? null},
  }));
  }
} catch (error) {
  process.stderr.write(error?.message ?? String(error));
  process.exitCode = 1;
} finally {
  try { extensionDiscoverySocket?.close(); } catch (_) {}
  if (!attachedExistingSession) {
    if (stagehand) await stagehand.close();
    else if (browser) await browser.close();
  }
}
