"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { getInvite, registerWithInvitation } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";

interface Props {
  params: { token: string };
}

export default function InvitePage({ params }: Props) {
  const { token } = params;
  const router = useRouter();
  const { loginWithToken } = useAuth();

  const [email, setEmail] = useState<string | null>(null);
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    getInvite(token)
      .then((data) => setEmail(data.email))
      .catch((err) =>
        setInviteError(err instanceof Error ? err.message : "Invalid invitation")
      );
  }, [token]);

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!email) return;
    setFormError(null);
    setLoading(true);

    const form = e.currentTarget;
    const displayName = (form.elements.namedItem("displayName") as HTMLInputElement).value;
    const password = (form.elements.namedItem("password") as HTMLInputElement).value;

    try {
      const accessToken = await registerWithInvitation(token, email, displayName, password);
      loginWithToken(accessToken);
      router.replace("/");
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Registration failed");
    } finally {
      setLoading(false);
    }
  }

  if (inviteError) {
    return (
      <main className="flex min-h-screen flex-col items-center justify-center p-8">
        <div className="w-full max-w-sm">
          <h1 className="mb-4 text-2xl font-bold">Invalid invitation</h1>
          <p className="rounded-md bg-red-100 px-3 py-2 text-sm text-red-800">{inviteError}</p>
        </div>
      </main>
    );
  }

  if (email === null) {
    return (
      <main className="flex min-h-screen flex-col items-center justify-center p-8">
        <p className="text-sm text-gray-500">Validating invitation…</p>
      </main>
    );
  }

  return (
    <main className="flex min-h-screen flex-col items-center justify-center p-8">
      <div className="w-full max-w-sm">
        <h1 className="mb-6 text-2xl font-bold">Create account</h1>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1">
            <label htmlFor="email" className="text-sm font-medium">
              Email
            </label>
            <input
              id="email"
              name="email"
              type="email"
              value={email}
              readOnly
              className="rounded-md border bg-gray-50 px-3 py-2 text-sm text-gray-500"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="displayName" className="text-sm font-medium">
              Display name
            </label>
            <input
              id="displayName"
              name="displayName"
              type="text"
              required
              autoComplete="name"
              className="rounded-md border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-black"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="password" className="text-sm font-medium">
              Password
            </label>
            <input
              id="password"
              name="password"
              type="password"
              required
              autoComplete="new-password"
              className="rounded-md border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-black"
            />
          </div>
          {formError && (
            <p className="rounded-md bg-red-100 px-3 py-2 text-sm text-red-800">{formError}</p>
          )}
          <button
            type="submit"
            disabled={loading}
            className="rounded-md bg-black px-4 py-2 text-sm font-medium text-white hover:bg-gray-800 disabled:opacity-50"
          >
            {loading ? "Creating account…" : "Create account"}
          </button>
        </form>
      </div>
    </main>
  );
}
