"use strict";
// Run the vendored official post-review-comments.js outside GitHub Actions.
//
// Injects minimal `github` / `context` / `core` / `fs` implementations that
// speak the GitHub REST + GraphQL APIs with the GITHUB_TOKEN env var.
//
// Usage:
//   node run_poster.js --owner goplus --repo builder --pr 123 \
//     --head-sha <sha> --result /tmp/ocr-result.json --stderr /tmp/ocr-stderr.log
const fs = require("fs");
const poster = require("./vendor/post-review-comments.js");

function arg(name) {
  const i = process.argv.indexOf("--" + name);
  if (i < 0 || i + 1 >= process.argv.length) return null;
  return process.argv[i + 1];
}

const owner = arg("owner");
const repo = arg("repo");
const prNumber = parseInt(arg("pr"), 10);
const headSha = arg("head-sha");
const resultPath = arg("result") || "/tmp/ocr-result.json";
const stderrPath = arg("stderr") || "/tmp/ocr-stderr.log";

if (!owner || !repo || !prNumber || !headSha) {
  console.error("missing required args: --owner --repo --pr --head-sha");
  process.exit(2);
}

class GhError extends Error {
  constructor(status, message, headers) {
    super(message);
    this.status = status;
    this.headers = headers || {};
  }
}

async function ghFetch(method, url, body) {
  const headers = {
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    Authorization: `Bearer ${process.env.GITHUB_TOKEN || ""}`,
    "User-Agent": "ocr-poller/1.0",
  };
  let payload;
  if (body !== undefined) {
    payload = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(url, { method, headers, body: payload });
  const resHeaders = {};
  res.headers.forEach((v, k) => {
    resHeaders[k.toLowerCase()] = v;
  });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const msg =
      data && typeof data === "object"
        ? data.message || JSON.stringify(data).slice(0, 300)
        : String(data).slice(0, 300);
    throw new GhError(res.status, `GitHub API ${method} ${url} -> ${res.status}: ${msg}`, resHeaders);
  }
  return { data, headers: resHeaders, status: res.status };
}

const API = "https://api.github.com";
const R = (p) => `${API}/repos/${p.owner}/${p.repo}`;
const paged = (p) => `per_page=${p.per_page || 100}&page=${p.page || 1}`;

const github = {
  rest: {
    pulls: {
      createReview: (p) =>
        ghFetch("POST", `${R(p)}/pulls/${p.pull_number}/reviews`, {
          commit_id: p.commit_id,
          event: p.event,
          body: p.body,
          comments: p.comments,
        }),
      listReviewComments: (p) =>
        ghFetch("GET", `${R(p)}/pulls/${p.pull_number}/comments?${paged(p)}`),
      listReviews: (p) =>
        ghFetch("GET", `${R(p)}/pulls/${p.pull_number}/reviews?${paged(p)}`),
      listFiles: (p) =>
        ghFetch("GET", `${R(p)}/pulls/${p.pull_number}/files?${paged(p)}`),
      get: (p) => ghFetch("GET", `${R(p)}/pulls/${p.pull_number}`),
    },
    issues: {
      createComment: (p) =>
        ghFetch("POST", `${R(p)}/issues/${p.issue_number}/comments`, { body: p.body }),
      updateComment: (p) =>
        ghFetch("PATCH", `${R(p)}/issues/comments/${p.comment_id}`, { body: p.body }),
      listComments: (p) =>
        ghFetch("GET", `${R(p)}/issues/${p.issue_number}/comments?${paged(p)}`),
    },
    users: {
      getAuthenticated: () => ghFetch("GET", `${API}/user`),
    },
  },
  // octokit graphql() resolves with the inner `data` object.
  graphql: async (query, variables) => {
    const res = await ghFetch("POST", `${API}/graphql`, { query, variables });
    if (res.data && res.data.errors) {
      throw new GhError(
        200,
        "GraphQL errors: " + JSON.stringify(res.data.errors).slice(0, 500),
        res.headers
      );
    }
    return res.data ? res.data.data : null;
  },
};

const context = {
  repo: { owner, repo },
  issue: { number: prNumber },
  sha: headSha,
  runId: Date.now(),
  runAttempt: 1,
  eventName: "schedule",
  payload: {},
};

const core = {
  info: (m) => console.log(String(m)),
  warning: (m) => console.warn("warning: " + String(m)),
  setOutput: (k, v) => console.log(`output ${k}=${v}`),
};

(async () => {
  await poster.runPostReviewComments({
    github,
    context,
    core,
    fs,
    prNumber,
    resultPath,
    stderrPath,
  });
  console.log("poster done");
})().catch((e) => {
  console.error("poster failed:", (e && e.message) || e);
  process.exit(1);
});
