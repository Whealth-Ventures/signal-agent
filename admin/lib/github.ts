import { Octokit } from "@octokit/rest";

function client(): Octokit {
  const token = process.env.GITHUB_TOKEN;
  if (!token) throw new Error("GITHUB_TOKEN env var not set");
  return new Octokit({ auth: token });
}

function repo() {
  const owner = process.env.GITHUB_OWNER;
  const name = process.env.GITHUB_REPO;
  const branch = process.env.GITHUB_BRANCH || "main";
  if (!owner || !name) throw new Error("GITHUB_OWNER / GITHUB_REPO not set");
  return { owner, repo: name, branch };
}

export type FileRead = { content: Buffer; sha: string };

export async function readFile(path: string): Promise<FileRead> {
  const gh = client();
  const { owner, repo: name, branch } = repo();
  const { data } = await gh.repos.getContent({
    owner, repo: name, path, ref: branch,
  });
  if (Array.isArray(data) || data.type !== "file") {
    throw new Error(`Expected file at ${path}, got something else`);
  }
  // data.content is base64 with newlines
  const content = Buffer.from(data.content, "base64");
  return { content, sha: data.sha };
}

export async function writeFile(
  path: string,
  content: Buffer | string,
  message: string,
  authorEmail: string,
): Promise<void> {
  const gh = client();
  const { owner, repo: name, branch } = repo();

  // Get the current SHA (required for updates). If the file doesn't exist
  // yet, we'd need to omit sha — but for our use case all files pre-exist.
  let sha: string | undefined;
  try {
    const existing = await gh.repos.getContent({
      owner, repo: name, path, ref: branch,
    });
    if (!Array.isArray(existing.data) && existing.data.type === "file") {
      sha = existing.data.sha;
    }
  } catch (e: any) {
    if (e.status !== 404) throw e;
  }

  const buf = typeof content === "string" ? Buffer.from(content, "utf-8") : content;
  await gh.repos.createOrUpdateFileContents({
    owner,
    repo: name,
    path,
    message,
    content: buf.toString("base64"),
    branch,
    sha,
    committer: { name: "Signal Agent Admin", email: authorEmail },
    author: { name: authorEmail.split("@")[0], email: authorEmail },
  });
}
