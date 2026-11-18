// Human-readable job states. ERROR_TEXT mirrors shared/vidnotes_common/errors.py
// USER_MESSAGES -- keep the two in sync.

export const STEPS = [
  { status: "QUEUED", label: "Queued" },
  { status: "TRANSCRIBING", label: "Transcribing" },
  { status: "SUMMARIZING", label: "Summarizing" },
  { status: "EXTRACTING_FRAMES", label: "Extracting frames" },
  { status: "PUBLISHING", label: "Publishing" },
];

export const TERMINAL = new Set(["COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED", "REJECTED"]);

const ERROR_TEXT = {
  INVALID_URL: "That doesn't look like a YouTube video link.",
  INVALID_REQUEST: "The request was malformed.",
  MISSING_NOTION_TOKEN: "Connect Notion first, then try again.",
  VIDEO_TOO_LONG: "This video is longer than the 20-minute limit.",
  LIVE_STREAM: "Live streams and premieres can't be summarized.",
  BROKER_UNAVAILABLE: "The service is temporarily unavailable. Try again in a moment.",
  JOB_NOT_FOUND: "This job no longer exists on the server.",
  VIDEO_UNAVAILABLE: "YouTube says this video is unavailable (private, removed or region-locked).",
  NO_TRANSCRIPT: "This video has no captions, so it can't be summarized.",
  YOUTUBE_BLOCKED: "YouTube is blocking requests from the server right now.",
  RATE_LIMITED: "Rate-limited by an external service.",
  LLM_ERROR: "The summarizer had a problem.",
  LLM_AUTH_FAILED: "The server's AI key was rejected.",
  LLM_BAD_OUTPUT: "The summarizer returned something unreadable.",
  STREAM_UNAVAILABLE: "Couldn't open the video stream to capture frames.",
  NOTION_UNAUTHORIZED: "Notion access was revoked. Reconnect Notion.",
  NOTION_FORBIDDEN: "The integration can't write to that Notion page. Share it with the integration.",
  NOTION_PARENT_NOT_FOUND: "The Notion destination page wasn't found or isn't shared with the integration.",
  NOTION_BAD_REQUEST: "Notion rejected the page.",
  NOTION_ERROR: "Notion had a problem.",
  NO_NOTION_DESTINATION: "No Notion page is shared with the integration to publish into.",
  RETRIES_EXHAUSTED: "Gave up after several attempts.",
  UNEXPECTED_ERROR: "Something went wrong on the server.",
  SERVER_UNREACHABLE: "Can't reach the notes server. Is it running?",
};

const WARNING_TEXT = {
  FRAME_EXTRACTION_FAILED: "couldn't be captured",
  FRAME_UPLOAD_FAILED: "couldn't be uploaded to Notion",
  FRAME_MISSING_CAPTION: "was skipped (no caption)",
};

export function fmtTs(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${r}` : `${m}:${r}`;
}

export function errorText(error) {
  if (!error) return "Something went wrong.";
  const code = error.code || "UNKNOWN";
  if (code === "VIDEO_TOO_LONG" && error.details && error.details.duration_sec) {
    const mins = Math.round(error.details.duration_sec / 60);
    const limit = Math.round((error.details.limit_sec || 1200) / 60);
    return `This video is ${mins} min; the limit is ${limit} min.`;
  }
  if (code === "RETRIES_EXHAUSTED") {
    const last = error.last_error ? errorText(error.last_error) : "";
    const n = error.attempts ? `${error.attempts} attempts` : "several attempts";
    return `Gave up after ${n}.${last ? " " + last : ""}`;
  }
  return ERROR_TEXT[code] || `Something went wrong (code ${code}).`;
}

export function stepIndex(status) {
  const i = STEPS.findIndex((s) => s.status === status);
  if (i >= 0) return i;
  return TERMINAL.has(status) ? STEPS.length : 0;
}

function waitingText(job, now) {
  if (!job.waiting_until) return null;
  const left = Math.ceil((Date.parse(job.waiting_until) - now) / 1000);
  if (Number.isNaN(left) || left < -5) return null;
  const reason = job.retry_reason || "Temporary problem";
  const attempt = job.attempt && job.max_attempts ? ` (attempt ${job.attempt} of ${job.max_attempts})` : "";
  return left > 0 ? `${reason} — retrying in ${left} s${attempt}` : `${reason} — retrying now${attempt}`;
}

function warningSummary(job) {
  const r = job.result || {};
  const parts = [];
  if (r.frames_requested) {
    parts.push(`${r.frames_published} of ${r.frames_requested} frames added`);
  }
  const frameWarnings = (job.warnings || []).filter((w) => w.timestamp_sec !== undefined && w.timestamp_sec !== null);
  if (frameWarnings.length) {
    const first = frameWarnings[0];
    const what = WARNING_TEXT[first.code] || "had a problem";
    const more = frameWarnings.length > 1 ? ` (+${frameWarnings.length - 1} more)` : "";
    parts.push(`${frameWarnings.length} frame${frameWarnings.length > 1 ? "s" : ""}; the one at ${fmtTs(first.timestamp_sec)} ${what}${more}`);
  } else if ((job.warnings || []).some((w) => w.code === "STREAM_UNAVAILABLE")) {
    parts.push("the video stream couldn't be opened, so the page is text-only");
  }
  return parts.join("; ") || "Published with minor issues.";
}

// -> { tone: "info"|"progress"|"waiting"|"success"|"warning"|"error", headline, detail }
export function describeJob(job, now = Date.now()) {
  switch (job.status) {
    case "COMPLETED":
      return {
        tone: "success",
        headline: "Done",
        detail: job.result && job.result.frames_requested
          ? `${job.result.frames_published} frame${job.result.frames_published === 1 ? "" : "s"} added`
          : "Text notes published",
      };
    case "COMPLETED_WITH_WARNINGS":
      return { tone: "warning", headline: "Done, with warnings", detail: warningSummary(job) };
    case "FAILED":
      return { tone: "error", headline: "Failed", detail: errorText(job.error) };
    case "DEAD_LETTERED":
      return { tone: "error", headline: "Couldn't finish", detail: errorText(job.error) };
    case "REJECTED":
      return { tone: "error", headline: "Not accepted", detail: errorText(job.error) };
    default: {
      const waiting = waitingText(job, now);
      if (waiting) return { tone: "waiting", headline: "Waiting", detail: waiting };
      const step = STEPS[stepIndex(job.status)];
      const label = step ? step.label : "Working";
      return {
        tone: job.status === "QUEUED" ? "info" : "progress",
        headline: job.status === "QUEUED" ? "Queued" : `${label}…`,
        detail: job.status === "QUEUED" ? "Waiting for a free worker" : `${job.progress || 0}% complete`,
      };
    }
  }
}

export function needsReconnect(job) {
  const code = job && job.error && (job.error.code || (job.error.last_error && job.error.last_error.code));
  return code === "NOTION_UNAUTHORIZED" || code === "MISSING_NOTION_TOKEN";
}
