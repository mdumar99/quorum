import type { HealthResult } from "@/lib/health";

export function HealthStatus({ result }: { result: HealthResult }) {
  if (!result.ok) {
    return (
      <section data-testid="health-status" data-state="unreachable">
        <h2>API unreachable</h2>
        <p>{result.error}</p>
      </section>
    );
  }

  const { data } = result;
  const state = data.status === "ok" ? "healthy" : "degraded";

  return (
    <section data-testid="health-status" data-state={state}>
      <h2>System status: {data.status}</h2>
      <ul>
        <li>
          Database: <span data-testid="db-status">{data.db}</span>
        </li>
        <li>
          Redis: <span data-testid="redis-status">{data.redis}</span>
        </li>
      </ul>
    </section>
  );
}