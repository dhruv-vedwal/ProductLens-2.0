const API_BASE = (
  process.env.NEXT_PUBLIC_PRODUCTLENS_API ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://localhost:8000"
).replace(/\/$/, "");

function authHeader(token: string | null): HeadersInit {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function parseError(res: Response, method: string, path: string): Promise<never> {
  let detail: unknown = res.statusText;
  try {
    const data = await res.json();
    detail = data.detail || data;
  } catch {
    /* ignore */
  }
  const message = typeof detail === "string" ? detail : JSON.stringify(detail);
  console.error("API request failed", { method, path, status: res.status, detail });
  throw new Error(message || "Request failed");
}

export async function http<T>(
  path: string,
  options: RequestInit & { token?: string | null } = {},
): Promise<T> {
  const { token, headers, ...rest } = options;
  const method = String(rest.method || "GET").toUpperCase();
  const res = await fetch(`${API_BASE}${path}`, {
    ...rest,
    headers: {
      ...(rest.body ? { "Content-Type": "application/json" } : {}),
      ...authHeader(token ?? null),
      ...headers,
    },
  });
  if (!res.ok) await parseError(res, method, path);
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export async function httpDownload(
  path: string,
  token: string,
  filename: string,
): Promise<void> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: authHeader(token),
  });
  if (!res.ok) await parseError(res, "GET", path);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function runStreamUrl(runId: string, token: string) {
  return `${API_BASE}/runs/${runId}/stream?token=${encodeURIComponent(token)}`;
}

export function runPosterUrl(runId: string, token: string) {
  return `${API_BASE}/runs/${runId}/poster?token=${encodeURIComponent(token)}`;
}

export function runVideoUrl(runId: string, token: string) {
  return `${API_BASE}/runs/${runId}/video?token=${encodeURIComponent(token)}`;
}

export function runEventsUrl(runId: string, token: string) {
  return `${API_BASE}/runs/${runId}/events?token=${encodeURIComponent(token)}`;
}

export type RunEventPayload = {
  run_id: string;
  status: string;
  stage: string | null;
  error_code?: string | null;
  updated_at?: string | null;
  stages?: Array<Record<string, unknown>>;
};

/**
 * Subscribe to run progress via SSE (`EventSource` + `?token=`).
 * Falls back to polling `GET /runs/{id}` when EventSource fails or is unavailable.
 */
export function subscribeRunEvents(
  runId: string,
  token: string,
  onEvent: (payload: RunEventPayload) => void,
  options?: { pollMs?: number },
): () => void {
  const pollMs = options?.pollMs ?? 2000;
  let cancelled = false;
  let pollTimer: number | undefined;
  let source: EventSource | null = null;
  const terminal = new Set(["COMPLETE", "FAILED", "CANCELLED", "succeeded", "failed", "cancelled"]);

  const stopPoll = () => {
    if (pollTimer !== undefined) {
      window.clearTimeout(pollTimer);
      pollTimer = undefined;
    }
  };

  const pollOnce = async () => {
    if (cancelled) return;
    try {
      const run = await http<{
        id: string;
        status: string;
        stage?: string | null;
        error_code?: string | null;
        updated_at?: string | null;
      }>(`/runs/${runId}`, { token });
      if (cancelled) return;
      onEvent({
        run_id: runId,
        status: run.status,
        stage: run.stage ?? null,
        error_code: run.error_code,
        updated_at: run.updated_at,
      });
      if (terminal.has(String(run.status))) return;
    } catch {
      /* keep polling */
    }
    pollTimer = window.setTimeout(() => void pollOnce(), pollMs);
  };

  const startPoll = () => {
    stopPoll();
    void pollOnce();
  };

  if (typeof EventSource !== "undefined") {
    try {
      source = new EventSource(runEventsUrl(runId, token));
      source.addEventListener("status", (ev) => {
        try {
          const payload = JSON.parse((ev as MessageEvent).data as string) as RunEventPayload;
          onEvent(payload);
          if (terminal.has(String(payload.status))) {
            source?.close();
          }
        } catch {
          /* ignore malformed frames */
        }
      });
      source.onerror = () => {
        source?.close();
        source = null;
        if (!cancelled) startPoll();
      };
    } catch {
      startPoll();
    }
  } else {
    startPoll();
  }

  return () => {
    cancelled = true;
    source?.close();
    stopPoll();
  };
}

export { API_BASE };
