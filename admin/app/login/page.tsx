"use client";
import { useState, Suspense } from "react";
import { useSearchParams } from "next/navigation";

function LoginForm() {
  const params = useSearchParams();
  const errorParam = params.get("error");
  const [email, setEmail] = useState("");
  const [status, setStatus] = useState<"idle" | "sending" | "sent" | "error">("idle");
  const [errorMsg, setErrorMsg] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setStatus("sending");
    setErrorMsg("");
    try {
      const res = await fetch("/api/auth/request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        setErrorMsg(j.error || `error ${res.status}`);
        setStatus("error");
      } else {
        setStatus("sent");
      }
    } catch (e: any) {
      setErrorMsg(e.message || "network error");
      setStatus("error");
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-md bg-white rounded-lg shadow-sm border p-8">
        <h1 className="text-2xl font-semibold mb-1">Signal Agent — Admin</h1>
        <p className="text-sm text-gray-500 mb-6">
          Sign in with the email address on the access list.
        </p>

        {errorParam === "invalid" && (
          <div className="mb-4 p-3 bg-red-50 text-red-700 text-sm rounded">
            Link was invalid or expired. Request a new one below.
          </div>
        )}

        {status === "sent" ? (
          <div className="p-4 bg-green-50 text-green-800 text-sm rounded">
            If <strong>{email}</strong> is on the access list, a sign-in link is
            on its way. Check your inbox (and spam). The link is valid for 15
            minutes.
          </div>
        ) : (
          <form onSubmit={submit} className="space-y-4">
            <div>
              <label className="block text-sm font-medium mb-1">Email</label>
              <input
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@2070health.com"
                className="w-full border rounded px-3 py-2 text-sm"
                autoFocus
              />
            </div>
            {status === "error" && (
              <div className="text-sm text-red-600">{errorMsg}</div>
            )}
            <button
              type="submit"
              disabled={status === "sending"}
              className="w-full bg-black text-white rounded py-2 text-sm font-medium hover:bg-gray-800 disabled:opacity-50"
            >
              {status === "sending" ? "Sending..." : "Send sign-in link"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}
