import { checkHealth } from "@/lib/api";

describe("checkHealth", () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("returns true when /health responds 200", async () => {
    jest.spyOn(global, "fetch").mockResolvedValue({ ok: true } as Response);

    const result = await checkHealth();

    expect(result).toBe(true);
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/health"),
      expect.objectContaining({ cache: "no-store" })
    );
  });

  it("returns false when /health responds with a non-2xx status", async () => {
    jest.spyOn(global, "fetch").mockResolvedValue({ ok: false } as Response);

    expect(await checkHealth()).toBe(false);
  });

  it("returns false when the network request throws", async () => {
    jest.spyOn(global, "fetch").mockRejectedValue(new Error("Network error"));

    expect(await checkHealth()).toBe(false);
  });
});
