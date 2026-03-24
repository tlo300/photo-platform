import { checkHealth, login, register, logout, refresh } from "@/lib/api";

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

describe("login", () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("returns the access token on success", async () => {
    jest.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ access_token: "tok_abc" }),
    } as Response);

    const token = await login("user@example.com", "secret");

    expect(token).toBe("tok_abc");
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/auth/login"),
      expect.objectContaining({ method: "POST", credentials: "include" })
    );
  });

  it("throws with the server detail on 401", async () => {
    jest.spyOn(global, "fetch").mockResolvedValue({
      ok: false,
      json: async () => ({ detail: "Invalid credentials." }),
    } as Response);

    await expect(login("user@example.com", "wrong")).rejects.toThrow("Invalid credentials.");
  });

  it("throws a fallback message when the error body is not JSON", async () => {
    jest.spyOn(global, "fetch").mockResolvedValue({
      ok: false,
      json: async () => { throw new Error("not json"); },
    } as unknown as Response);

    await expect(login("user@example.com", "wrong")).rejects.toThrow("Login failed");
  });
});

describe("register", () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("returns the access token on success", async () => {
    jest.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ access_token: "tok_xyz" }),
    } as Response);

    const token = await register("new@example.com", "Alice", "password123");

    expect(token).toBe("tok_xyz");
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/auth/register"),
      expect.objectContaining({ method: "POST" })
    );
  });

  it("throws with the server detail on 409 conflict", async () => {
    jest.spyOn(global, "fetch").mockResolvedValue({
      ok: false,
      json: async () => ({ detail: "Email already registered." }),
    } as Response);

    await expect(register("existing@example.com", "Alice", "password123")).rejects.toThrow(
      "Email already registered."
    );
  });

  it("throws with the server detail on 403 when open registration is disabled", async () => {
    jest.spyOn(global, "fetch").mockResolvedValue({
      ok: false,
      json: async () => ({ detail: "Open registration is disabled. Contact an administrator." }),
    } as Response);

    await expect(register("user@example.com", "Bob", "password123")).rejects.toThrow(
      "Open registration is disabled. Contact an administrator."
    );
  });
});

describe("logout", () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("calls POST /auth/logout with credentials", async () => {
    jest.spyOn(global, "fetch").mockResolvedValue({ ok: true } as Response);

    await logout();

    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/auth/logout"),
      expect.objectContaining({ method: "POST", credentials: "include" })
    );
  });

  it("resolves without throwing even if the request fails", async () => {
    jest.spyOn(global, "fetch").mockRejectedValue(new Error("Network error"));

    await expect(logout()).resolves.not.toThrow();
  });
});

describe("refresh", () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("returns the access token when the refresh cookie is valid", async () => {
    jest.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ access_token: "tok_refreshed" }),
    } as Response);

    const token = await refresh();

    expect(token).toBe("tok_refreshed");
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/auth/refresh"),
      expect.objectContaining({ method: "POST", credentials: "include" })
    );
  });

  it("returns null when the refresh cookie is missing or expired", async () => {
    jest.spyOn(global, "fetch").mockResolvedValue({ ok: false } as Response);

    expect(await refresh()).toBeNull();
  });

  it("returns null when the network request throws", async () => {
    jest.spyOn(global, "fetch").mockRejectedValue(new Error("Network error"));

    expect(await refresh()).toBeNull();
  });
});
