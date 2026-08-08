import Link from "next/link";
import { getSession } from "@/lib/auth";

export default async function Home() {
  const session = await getSession();
  return (
    <div className="min-h-screen">
      <header className="border-b bg-white">
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
          <div>
            <h1 className="text-lg font-semibold">Signal Agent — Admin</h1>
            <p className="text-xs text-gray-500">{session?.email}</p>
          </div>
          <form action="/api/auth/logout" method="POST">
            <button
              formMethod="post"
              type="submit"
              className="text-sm text-gray-600 hover:text-gray-900"
            >
              Sign out
            </button>
          </form>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-10">
        <div className="grid md:grid-cols-2 gap-4">
          <Link
            href="/prompts"
            className="block p-6 bg-white border rounded-lg hover:border-gray-400"
          >
            <h2 className="font-semibold mb-1">Prompts</h2>
            <p className="text-sm text-gray-600">
              Edit the two LLM prompts: ranker tone &amp; magnitude rubric (what
              counts as Tier S / A / B / C).
            </p>
          </Link>

          <div className="block p-6 bg-gray-50 border border-dashed rounded-lg">
            <h2 className="font-semibold mb-1 text-gray-700">
              Keywords, Sources, Tuning, Portfolio, Content
            </h2>
            <p className="text-sm text-gray-600">
              Now edited in{" "}
              <a
                className="underline"
                href="https://2070health.sharepoint.com/sites/SignalAgent"
                target="_blank"
                rel="noreferrer"
              >
                SharePoint
              </a>
              , not here. The agent reads that folder directly at the start of
              every run, so an edit there is live the next morning — no deploy.
            </p>
          </div>
        </div>

        <p className="mt-10 text-xs text-gray-500">
          Prompt changes commit to{" "}
          <code>
            {process.env.GITHUB_OWNER || "signal-agent"}/
            {process.env.GITHUB_REPO || "signal-agent"}@
            {process.env.GITHUB_BRANCH || "main"}
          </code>{" "}
          and apply on the next deploy. Everything under <code>inputs/</code>{" "}
          comes from SharePoint instead and needs no deploy.
        </p>
      </main>
    </div>
  );
}
