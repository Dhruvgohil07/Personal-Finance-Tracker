import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RequireAuth } from "@/components/require-auth";
import { SLOW_AFTER_MS, WakingUpHint } from "@/components/waking-up-hint";
import { renderWithSession } from "@/test/session";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));

const HINT = /server sleeps when nobody is using it/;

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("WakingUpHint", () => {
  it("appears only after the wait has lasted SLOW_AFTER_MS", () => {
    render(<WakingUpHint waiting />);

    act(() => vi.advanceTimersByTime(SLOW_AFTER_MS - 1));
    expect(screen.queryByText(HINT)).not.toBeInTheDocument();

    act(() => vi.advanceTimersByTime(1));
    expect(screen.getByRole("status")).toHaveTextContent(HINT);
  });

  it("goes away when the wait ends, and starts counting again on the next one", () => {
    const { rerender } = render(<WakingUpHint waiting />);
    act(() => vi.advanceTimersByTime(SLOW_AFTER_MS));
    expect(screen.getByText(HINT)).toBeInTheDocument();

    rerender(<WakingUpHint waiting={false} />);
    expect(screen.queryByText(HINT)).not.toBeInTheDocument();

    rerender(<WakingUpHint waiting />);
    expect(screen.queryByText(HINT)).not.toBeInTheDocument();
    act(() => vi.advanceTimersByTime(SLOW_AFTER_MS));
    expect(screen.getByText(HINT)).toBeInTheDocument();
  });

  it("never appears for a wait that isn't happening", () => {
    render(<WakingUpHint waiting={false} />);

    act(() => vi.advanceTimersByTime(SLOW_AFTER_MS * 10));
    expect(screen.queryByText(HINT)).not.toBeInTheDocument();
  });
});

describe("RequireAuth while the backend wakes up", () => {
  it("adds the hint when the session check takes long", async () => {
    // The first request of a visit to a sleeping backend: no answer yet.
    renderWithSession(
      <RequireAuth>
        <p>Signed-in page</p>
      </RequireAuth>,
      { handler: (path) => (path === "/auth/refresh" ? new Promise(() => {}) : undefined) },
    );
    expect(screen.getByText("Checking your session…")).toBeInTheDocument();
    expect(screen.queryByText(HINT)).not.toBeInTheDocument();

    await act(() => vi.advanceTimersByTimeAsync(SLOW_AFTER_MS));

    expect(screen.getByText(HINT)).toBeInTheDocument();
  });
});
