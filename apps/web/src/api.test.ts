import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("api client", () => {
  it("sends the in-memory bearer token and includes the refresh cookie", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api<{ ok: boolean }>("/api/v1/projects", {}, "access-token")).resolves.toEqual({ ok: true });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer access-token");
    expect(init.credentials).toBe("include");
  });

  it("surfaces safe server error details and status", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Project not found" }), { status: 404 })));
    await expect(api("/api/v1/projects/missing")).rejects.toMatchObject({ message: "Project not found", status: 404 });
  });
});
