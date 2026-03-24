import { checkHealth } from "@/lib/api";

export default async function Home() {
  const connected = await checkHealth();

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-4 p-8">
      <h1 className="text-3xl font-bold">Photo Platform</h1>
      {connected ? (
        <p className="rounded-md bg-green-100 px-4 py-2 text-green-800">
          API connected
        </p>
      ) : (
        <p className="rounded-md bg-red-100 px-4 py-2 text-red-800">
          API unavailable
        </p>
      )}
    </main>
  );
}
