export const MAX_RETRIES = 3;
export const RETRY_BACKOFF_MS = [1_000, 4_000, 16_000];

/**
 * Return true when the error is transient and worth retrying
 * (rate limits, throttles, network interruptions, 5xx server errors).
 */
export function retryableError(error: unknown): boolean {
  if (error instanceof Error) {
    const msg = error.message.toLowerCase();
    if (msg.includes("rate") || msg.includes("throttle") || msg.includes("capacity")) return true;
    if (msg.includes("network") || msg.includes("timeout") || msg.includes("econnrefused")) return true;
    if (/\b(500|502|503|504)\b/.test(msg)) return true;
    if (msg.includes("429")) return true;
    if (msg.includes("overloaded")) return true;
  }
  return false;
}

/**
 * Sleep for `ms`, resolving early if `signal` aborts so a cancelled run does
 * not wait out the full backoff before noticing.
 */
export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    let timer: ReturnType<typeof setTimeout>;
    const onAbort = (): void => {
      clearTimeout(timer);
      resolve();
    };
    timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export interface RetryOptions {
  maxRetries?: number;
  /** Caller-provided AbortSignal; checked between attempts. */
  signal?: AbortSignal;
  /** Optional label for log messages (e.g. task id). */
  label?: string;
}

/**
 * Execute `fn` with retry + exponential backoff on transient errors.
 * On each attempt the retryableError predicate is applied to the caught
 * error; non-retryable errors (and the last retryable failure after
 * maxRetries) are re-thrown.
 */
export async function withRetry<T>(
  fn: () => Promise<T>,
  options: RetryOptions = {},
): Promise<T> {
  const maxRetries = options.maxRetries ?? MAX_RETRIES;
  let lastError: unknown;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await fn();
    } catch (error) {
      lastError = error;
      if (options.signal?.aborted) break;
      if (attempt < maxRetries && retryableError(error)) {
        const delay = RETRY_BACKOFF_MS[Math.min(attempt, RETRY_BACKOFF_MS.length - 1)];
        const tag = options.label ? ` ${options.label}` : "";
        console.error(
          `Attempt ${attempt + 1} failed${tag}, retrying in ${delay}ms: ` +
            `${error instanceof Error ? error.message : String(error)}`,
        );
        await sleep(delay, options.signal);
        if (options.signal?.aborted) break;
        continue;
      }
      break;
    }
  }
  throw lastError;
}
