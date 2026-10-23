/** Backend access of the Run Explorer: REST calls, SSE streaming and lazy span lookup.
 *
 * The SSE reader keeps a ``seq`` set for the lifetime of one subscription. ``EventSource`` retries
 * automatically after a network error and replays the whole run from ``run_started``; filtering
 * already-seen sequences here means the store never sees the same frame twice.
 */

/** Error raised for a non-2xx API response. */
export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Fetch JSON and translate the backend error document into an ``ApiError``. */
async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  let payload = null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    payload = await response.json().catch(() => null);
  }
  if (!response.ok) {
    const message = payload && typeof payload.error === "string" ? payload.error : `HTTP ${response.status}`;
    throw new ApiError(message, response.status);
  }
  return payload;
}

/** Client of the sample Web UI API. */
export class ApiClient {
  constructor(baseUrl = "") {
    this.baseUrl = baseUrl;
    this.spanCache = new Map();
  }

  /** Read the read-only process metadata. */
  getMeta() {
    return requestJson(`${this.baseUrl}/api/meta`);
  }

  /** Start one run. */
  startRun(body) {
    return requestJson(`${this.baseUrl}/api/runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  /** Read one run snapshot. */
  getRun(runId) {
    return requestJson(`${this.baseUrl}/api/runs/${encodeURIComponent(runId)}`);
  }

  /** Lazily read one full span by id (cached for the lifetime of the page). */
  getSpan(runId, spanId) {
    const key = `${runId}:${spanId}`;
    if (!this.spanCache.has(key)) {
      const promise = requestJson(
        `${this.baseUrl}/api/runs/${encodeURIComponent(runId)}/spans/${encodeURIComponent(spanId)}`,
      ).catch((error) => {
        this.spanCache.delete(key);
        throw error;
      });
      this.spanCache.set(key, promise);
    }
    return this.spanCache.get(key);
  }

  /** Read the complete trace document of one run. */
  getTrace(runId) {
    return requestJson(`${this.baseUrl}/api/runs/${encodeURIComponent(runId)}/trace.json`);
  }

  /** Submit one manual ``NegotiationDecision``. */
  submitDecision(runId, decision) {
    return requestJson(`${this.baseUrl}/api/runs/${encodeURIComponent(runId)}/decisions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(decision),
    });
  }

  /** Return the download URLs of one run's trace and transcript documents. */
  exportUrls(runId) {
    const base = `${this.baseUrl}/api/runs/${encodeURIComponent(runId)}`;
    return {
      trace: `${base}/trace.json`,
      transcriptJson: `${base}/transcript.json`,
      transcriptMarkdown: `${base}/transcript.md`,
    };
  }

  /** Forget cached spans (a new run reuses no ids). */
  clearSpanCache() {
    this.spanCache.clear();
  }

  /**
   * Subscribe to one run's SSE stream.
   *
   * @param {string} runId run identifier.
   * @param {object} handlers callbacks: ``onFrame``, ``onOpen``, ``onError``, ``onFinished``,
   *   ``onClose``, ``onDuplicate``.
   * @returns {{close: () => void, isOpen: () => boolean, seen: Set<number>}}
   */
  subscribe(runId, handlers = {}) {
    const seen = new Set();
    let source = null;
    let closed = false;
    let finished = false;

    const close = () => {
      closed = true;
      if (source) {
        source.close();
        source = null;
      }
    };

    const open = () => {
      close();
      closed = false;
      source = new EventSource(`${this.baseUrl}/api/runs/${encodeURIComponent(runId)}/events`);
      source.onopen = () => {
        if (!closed) handlers.onOpen?.();
      };
      source.onmessage = (event) => {
        if (closed) return;
        let frame;
        try {
          frame = JSON.parse(event.data);
        } catch (error) {
          handlers.onError?.(new Error(`Invalid SSE payload: ${String(error?.message ?? error)}`), true);
          return;
        }
        if (!frame || typeof frame !== "object") return;
        if (Number.isInteger(frame.seq)) {
          if (seen.has(frame.seq)) {
            handlers.onDuplicate?.(frame);
            return;
          }
          seen.add(frame.seq);
        }
        handlers.onFrame?.(frame);
        if (frame.type === "finished") {
          // Keep listening during the server's post-finish grace window: the server process posts
          // its final LLM/tool spans a flush interval after the client finishes, and closing here
          // would drop the whole server side of the run from the timeline. The caller closes the
          // stream once its own grace timer fires (main.js) or the server ends the stream.
          finished = true;
          handlers.onFinished?.(frame);
        }
      };
      source.onerror = () => {
        if (closed) return;
        // EventSource reconnects by itself (readyState CONNECTING); the replay is deduplicated by
        // the seq set. CLOSED means the browser gave up (for example an unknown run id).
        if (source.readyState === EventSource.CLOSED) {
          handlers.onError?.(new Error("The event stream is closed."), true);
          return;
        }
        handlers.onError?.(new Error("The event stream was interrupted."), false);
      };
    };

    open();
    return {
      close,
      isOpen: () => Boolean(source) && !closed,
      isFinished: () => finished,
      seen,
      reopen: open,
    };
  }
}
