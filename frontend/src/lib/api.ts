// API_URL is used by server components (internal Docker network).
// NEXT_PUBLIC_API_URL is used by the browser (goes through Caddy).
const API_BASE_URL =
  process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// Browser-only base URL — never use API_URL here as it is not exposed to the client.
const CLIENT_API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function checkHealth(): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE_URL}/health`, { cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
}

export async function login(email: string, password: string): Promise<string> {
  const res = await fetch(`${CLIENT_API_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Login failed");
  }
  const data = await res.json();
  return (data as { access_token: string }).access_token;
}

export async function register(
  email: string,
  displayName: string,
  password: string
): Promise<string> {
  const res = await fetch(`${CLIENT_API_URL}/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ email, display_name: displayName, password }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Registration failed");
  }
  const data = await res.json();
  return (data as { access_token: string }).access_token;
}

export async function logout(): Promise<void> {
  try {
    await fetch(`${CLIENT_API_URL}/auth/logout`, {
      method: "POST",
      credentials: "include",
    });
  } catch {
    // Best-effort — client state is cleared regardless.
  }
}

export async function refresh(): Promise<string | null> {
  try {
    const res = await fetch(`${CLIENT_API_URL}/auth/refresh`, {
      method: "POST",
      credentials: "include",
    });
    if (!res.ok) return null;
    const data = await res.json();
    return (data as { access_token: string }).access_token;
  } catch {
    return null;
  }
}

export async function registerWithInvitation(
  invitationToken: string,
  email: string,
  displayName: string,
  password: string
): Promise<string> {
  const res = await fetch(`${CLIENT_API_URL}/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({
      email,
      display_name: displayName,
      password,
      invitation_token: invitationToken,
    }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Registration failed");
  }
  const data = await res.json();
  return (data as { access_token: string }).access_token;
}

export async function getInvite(token: string): Promise<{ email: string }> {
  const res = await fetch(`${CLIENT_API_URL}/auth/invite/${token}`);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Invalid invitation");
  }
  return res.json();
}

export interface InvitationOut {
  id: string;
  email: string;
  expires_at: string;
  created_at: string;
}

export interface InvitationsPage {
  total: number;
  page: number;
  page_size: number;
  items: InvitationOut[];
}

export async function listInvitations(
  token: string,
  page = 1
): Promise<InvitationsPage> {
  const res = await fetch(
    `${CLIENT_API_URL}/admin/invitations?page=${page}`,
    { headers: { Authorization: `Bearer ${token}` } }
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to load invitations");
  }
  return res.json();
}

export async function createInvitation(
  token: string,
  email: string
): Promise<{ invitation_token: string; email: string; expires_at: string }> {
  const res = await fetch(`${CLIENT_API_URL}/admin/invitations`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ email }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to create invitation");
  }
  return res.json();
}
