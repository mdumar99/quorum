import { HealthStatus } from "@/components/HealthStatus";
import { getHealth } from "@/lib/health";

// Render on every request. Without this, `next build` may prerender the page
// once, freezing whatever status the API had at build time (or failing the
// build if the API isn't running).
export const dynamic = "force-dynamic";

export default async function Home() {
  const result = await getHealth();
  return (
    <main style={{ fontFamily: "system-ui, sans-serif", padding: "2rem" }}>
      <h1>Quorum</h1>
      <HealthStatus result={result} />
    </main>
  );
}