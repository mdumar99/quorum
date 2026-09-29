export type DependencyStatus = "ok" | "error";

/** Mirrors the API's /health contract (P0-3). */
export interface HealthData {
  status: DependencyStatus;
  db: DependencyStatus;
  redis: DependencyStatus;
}

/**
 * ok: true  -> the API answered with a valid health body (200 OR 503).
 *              A 503 is data: it tells us WHICH dependency failed.
 * ok: false -> no valid answer at all: network error, timeout, or unparseable body.
 */
export type HealthResult =
  | { ok: true; httpStatus: number; data: HealthData }
  | { ok: false; error: string };

// Must be LONGER than the API's own 2s per-check timeout. Otherwise a hung
// database (API answers 503 after ~2s) would look like "API unreachable"
// instead of "degraded", and we'd lose which dependency failed.
const HEALTH_TIMEOUT_MS = 3000;

const STATUSES: readonly string[] = ["ok", "error"];

function isHealthData(value: unknown): value is HealthData {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return [v.status, v.db, v.redis].every(
    (field) => typeof field === "string" && STATUSES.includes(field),
  );
}

// API_URL is server-side only (see ADR-0001). Never NEXT_PUBLIC_API_URL here:
// that would bake the address into the browser bundle.
export async function getHealth(
  baseUrl: string | undefined = process.env.API_URL,
): Promise<HealthResult> {
  if (!baseUrl) return { ok: false, error: "API_URL is not set" };

  try {
    const response = await fetch(`${baseUrl}/health`, {
      cache: "no-store",
      signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS),
    });

    if (response.status !== 200 && response.status !== 503) {
      return { ok: false, error: `Unexpected HTTP ${response.status}` };
    }

    const body: unknown = await response.json();
    if (!isHealthData(body)) {
      return { ok: false, error: "Unrecognised response from API" };
    }
    return { ok: true, httpStatus: response.status, data: body };
  } catch (err) {
    const name = err instanceof Error ? err.name : "";
    if (name === "TimeoutError")
      return { ok: false, error: "API did not respond in time" };
    if (name === "SyntaxError")
      return { ok: false, error: "Unrecognised response from API" };
    return { ok: false, error: "Could not reach the API" };
  }
}
