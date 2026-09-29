import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { HealthStatus } from "@/components/HealthStatus";

afterEach(cleanup); // unmount between tests so each starts with an empty page

it("renders the healthy state", () => {
  render(
    <HealthStatus
      result={{ ok: true, httpStatus: 200, data: { status: "ok", db: "ok", redis: "ok" } }}
    />,
  );
  expect(screen.getByTestId("health-status").dataset.state).toBe("healthy");
  expect(screen.getByTestId("db-status").textContent).toBe("ok");
  expect(screen.getByTestId("redis-status").textContent).toBe("ok");
});

it("renders the degraded state and names the failed dependency", () => {
  render(
    <HealthStatus
      result={{ ok: true, httpStatus: 503, data: { status: "error", db: "error", redis: "ok" } }}
    />,
  );
  expect(screen.getByTestId("health-status").dataset.state).toBe("degraded");
  expect(screen.getByTestId("db-status").textContent).toBe("error");
  expect(screen.getByTestId("redis-status").textContent).toBe("ok");
});

it("renders the unreachable state with the error message", () => {
  render(<HealthStatus result={{ ok: false, error: "Could not reach the API" }} />);
  expect(screen.getByTestId("health-status").dataset.state).toBe("unreachable");
  expect(screen.getByText("Could not reach the API")).toBeTruthy();
});