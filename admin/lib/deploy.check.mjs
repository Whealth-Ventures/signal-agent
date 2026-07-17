// Check for deploy trigger URL/auth assembly. Run from admin/:
//   node lib/deploy.check.mjs
import { buildTriggerRequest } from "./deploy.ts";

const A = (c, m) => { if (!c) { console.error("FAIL:", m); process.exit(1); } };

// No token, no auth: URL passes through, no headers.
let r = buildTriggerRequest("https://ci/job/x/build");
A(r.target === "https://ci/job/x/build", "plain url");
A(Object.keys(r.headers).length === 0, "no headers");

// Token on a URL with no query string -> ?token=
r = buildTriggerRequest("https://ci/job/x/build", "s3cr3t");
A(r.target === "https://ci/job/x/build?token=s3cr3t", "token appended with ?");

// Token on a URL that already has a query -> &token=, and it's url-encoded.
r = buildTriggerRequest("https://ci/build?job=x", "a b");
A(r.target === "https://ci/build?job=x&token=a%20b", "token appended with & + encoded");

// Basic auth header.
r = buildTriggerRequest("https://ci/build", undefined, "user:tok");
A(r.headers.Authorization === "Basic " + Buffer.from("user:tok").toString("base64"), "basic auth");

console.log("deploy trigger assembly OK");
