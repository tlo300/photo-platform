"use client";

import { FormEvent, useEffect, useState } from "react";
import { useAuth } from "@/context/AuthContext";
import {
  listInvitations,
  createInvitation,
  type InvitationOut,
} from "@/lib/api";

export default function AdminInvitationsPage() {
  const { token } = useAuth();

  const [invitations, setInvitations] = useState<InvitationOut[]>([]);
  const [total, setTotal] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [email, setEmail] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [createdToken, setCreatedToken] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  async function load() {
    if (!token) return;
    setLoadError(null);
    try {
      const page = await listInvitations(token);
      setInvitations(page.items);
      setTotal(page.total);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "Failed to load invitations");
    }
  }

  useEffect(() => {
    load();
  }, [token]); // eslint-disable-line react-hooks/exhaustive-deps

  async function handleCreate(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!token) return;
    setCreateError(null);
    setCreatedToken(null);
    setCreating(true);
    try {
      const result = await createInvitation(token, email);
      setCreatedToken(result.invitation_token);
      setEmail("");
      await load();
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : "Failed to create invitation");
    } finally {
      setCreating(false);
    }
  }

  return (
    <main className="mx-auto max-w-2xl p-8">
      <h1 className="mb-6 text-2xl font-bold">Invitations</h1>

      {/* Create invitation form */}
      <section className="mb-8">
        <h2 className="mb-3 text-lg font-semibold">Invite someone</h2>
        <form onSubmit={handleCreate} className="flex gap-2">
          <input
            type="email"
            required
            placeholder="Email address"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="flex-1 rounded-md border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-black"
          />
          <button
            type="submit"
            disabled={creating}
            className="rounded-md bg-black px-4 py-2 text-sm font-medium text-white hover:bg-gray-800 disabled:opacity-50"
          >
            {creating ? "Creating…" : "Create invitation"}
          </button>
        </form>
        {createError && (
          <p className="mt-2 rounded-md bg-red-100 px-3 py-2 text-sm text-red-800">{createError}</p>
        )}
        {createdToken && (
          <div className="mt-3 rounded-md bg-green-50 px-3 py-2 text-sm">
            <p className="mb-1 font-medium text-green-800">Invitation created. Send this link to the invitee:</p>
            <code className="block break-all text-green-900">
              {window.location.origin}/invite/{createdToken}
            </code>
            <p className="mt-1 text-xs text-green-700">This link will not be shown again.</p>
          </div>
        )}
      </section>

      {/* Pending invitations list */}
      <section>
        <h2 className="mb-3 text-lg font-semibold">
          Pending invitations{total > 0 && <span className="ml-2 text-sm font-normal text-gray-500">({total})</span>}
        </h2>
        {loadError && (
          <p className="rounded-md bg-red-100 px-3 py-2 text-sm text-red-800">{loadError}</p>
        )}
        {!loadError && invitations.length === 0 && (
          <p className="text-sm text-gray-500">No pending invitations.</p>
        )}
        {invitations.length > 0 && (
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b text-left text-xs font-medium uppercase text-gray-500">
                <th className="pb-2 pr-4">Email</th>
                <th className="pb-2">Expires</th>
              </tr>
            </thead>
            <tbody>
              {invitations.map((inv) => (
                <tr key={inv.id} className="border-b last:border-0">
                  <td className="py-2 pr-4">{inv.email}</td>
                  <td className="py-2 text-gray-600">
                    {new Date(inv.expires_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}
