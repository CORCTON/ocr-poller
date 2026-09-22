#!/usr/bin/env python3
"""Poll GitHub for review triggers and run open-code-review.

Triggers (only for PRs authored by PR_AUTHOR, in TARGET_REPOS):
  - draft -> ready_for_review transition
  - a new comment by PR_AUTHOR whose body starts with /ocr

State is kept in STATE_PATH as JSON:
  {"<repo>#<pr>": {"head_sha":..., "draft":..., "last_comment_id":..., "reviewed":[...]}, ...}
The first sighting of a PR only records a baseline (no review), so container
restarts or pre-existing ready PRs never cause a surprise review.

Env:
  TARGET_REPOS   comma-separated, e.g. "goplus/builder,goplus/builder-backend" (required)
  PR_AUTHOR      GitHub login to watch, default "CORCTON"
  GITHUB_TOKEN   PAT with pull-requests write on the target repos (required)
  POLL_INTERVAL  seconds between polls, default "300"
  STATE_PATH     default "/state/state.json"
  WORK_DIR       clone dir, default "/work"
  OCR_LLM_URL / OCR_LLM_AUTH_TOKEN / OCR_LLM_MODEL [/ OCR_LLM_USE_ANTHROPIC]
                 passed through to `ocr` (user fills these in Dokploy UI)
"""
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request

REPOS = [r.strip() for r in os.environ.get("TARGET_REPOS", "").split(",") if r.strip()]
AUTHOR = os.environ.get("PR_AUTHOR", "CORCTON")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))
STATE_PATH = os.environ.get("STATE_PATH", "/state/state.json")
WORK_DIR = os.environ.get("WORK_DIR", "/work")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
SUMMARY_MARKER = "<!-- ocr-summary -->"  # our own poster comments must not retrigger


def log(*a):
    print(time.strftime("[%Y-%m-%dT%H:%M:%S]"), *a, flush=True)


def gh(path, method="GET", data=None):
    url = "https://api.github.com" + path
    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": "Bearer %s" % GH_TOKEN,
        "User-Agent": "ocr-poller/1.0",
    }
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def sh(cmd, cwd=None, timeout=120):
    return subprocess.run(cmd, cwd=cwd, timeout=timeout,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_PATH)


def git_url(repo):
    base = "https://github.com/%s.git" % repo
    if GH_TOKEN:
        return "https://x-access-token:%s@github.com/%s.git" % (GH_TOKEN, repo)
    return base


def ensure_clone(repo):
    owner, name = repo.split("/")
    d = os.path.join(WORK_DIR, "%s__%s" % (owner, name))
    if not os.path.isdir(os.path.join(d, ".git")):
        log("cloning", repo)
        r = sh(["git", "clone", "--quiet", git_url(repo), d], timeout=900)
        if r.returncode != 0:
            raise RuntimeError("clone failed: %s" % r.stderr[-500:])
    else:
        r = sh(["git", "fetch", "--quiet", "origin", "--prune"], cwd=d, timeout=600)
        if r.returncode != 0:
            raise RuntimeError("fetch origin failed: %s" % r.stderr[-500:])
    return d


def run_review(repo, number, base_ref, head_sha, fork_repo):
    d = ensure_clone(repo)
    r = sh(["git", "fetch", "--quiet", git_url(fork_repo), head_sha], cwd=d, timeout=900)
    if r.returncode != 0:
        raise RuntimeError("fetch fork head failed: %s" % r.stderr[-500:])
    result_path, stderr_path = "/tmp/ocr-result.json", "/tmp/ocr-stderr.log"
    env = dict(os.environ)
    # action.yml maps the token to OCR_LLM_TOKEN; accept the documented name too
    if env.get("OCR_LLM_AUTH_TOKEN") and not env.get("OCR_LLM_TOKEN"):
        env["OCR_LLM_TOKEN"] = env["OCR_LLM_AUTH_TOKEN"]
    cmd = ["ocr", "review", "--from", "origin/%s" % base_ref, "--to", head_sha,
           "--audience", "agent", "--format", "json", "--timeout", "1500"]
    log("running ocr for %s#%s @ %s" % (repo, number, head_sha[:8]))
    with open(result_path, "wb") as out, open(stderr_path, "wb") as err:
        subprocess.run(cmd, cwd=d, stdout=out, stderr=err, env=env, timeout=1560)
    owner, name = repo.split("/")
    cmd = ["node", os.path.join(APP_DIR, "run_poster.js"),
           "--owner", owner, "--repo", name, "--pr", str(number),
           "--head-sha", head_sha, "--result", result_path, "--stderr", stderr_path]
    r = subprocess.run(cmd, env=env, timeout=900,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail = r.stdout[-3000:] if r.stdout else ""
    log("poster exit %d\n%s" % (r.returncode, tail))
    if r.returncode != 0:
        raise RuntimeError("poster failed")


def poll_repo(repo, state):
    prs = gh("/repos/%s/pulls?state=open&per_page=100" % repo)
    mine = [p for p in prs if p.get("user", {}).get("login") == AUTHOR]
    seen = set()
    for pr in mine:
        number = pr["number"]
        key = "%s#%s" % (repo, number)
        seen.add(key)
        head = pr["head"]["sha"]
        base = pr["base"]["ref"]
        draft = bool(pr.get("draft"))
        fork = (pr.get("head") or {}).get("repo", {}).get("full_name") or repo
        comments = gh("/repos/%s/issues/%s/comments?per_page=100" % (repo, number))
        max_id = 0
        for c in comments:
            max_id = max(max_id, c.get("id", 0))
        prev = state.get(key)
        if prev is None:
            state[key] = {"head_sha": head, "draft": draft,
                          "last_comment_id": max_id, "reviewed": []}
            log("baseline %s head=%s draft=%s" % (key, head[:8], draft))
            continue
        triggers = []
        if prev.get("draft") and not draft:
            triggers.append("ready_for_review")
        new_cmds = [c.get("id", 0) for c in comments
                    if c.get("id", 0) > prev.get("last_comment_id", 0)
                    and c.get("user", {}).get("login") == AUTHOR
                    and (c.get("body") or "").lstrip().startswith("/ocr")
                    and SUMMARY_MARKER not in (c.get("body") or "")]
        if new_cmds:
            triggers.append("/ocr")
        prev["last_comment_id"] = max_id
        prev["head_sha"] = head
        prev["draft"] = draft
        if not triggers:
            continue
        if head in prev.get("reviewed", []):
            log("%s triggered by %s but head %s already reviewed; skip"
                % (key, triggers, head[:8]))
            continue
        log("%s triggered by %s; reviewing head %s" % (key, triggers, head[:8]))
        run_review(repo, number, base, head, fork)
        prev.setdefault("reviewed", []).append(head)
        prev["reviewed"] = prev["reviewed"][-30:]
        log("%s review posted" % key)
    for k in [k for k in state if k.startswith(repo + "#") and k not in seen]:
        del state[k]
        log("prune", k)


def poll_once(state):
    for repo in REPOS:
        try:
            poll_repo(repo, state)
        except Exception as e:
            log("repo %s error: %s" % (repo, e))


def main():
    if not REPOS:
        log("TARGET_REPOS is empty; nothing to do")
        sys.exit(1)
    if not GH_TOKEN:
        log("WARNING: GITHUB_TOKEN is empty; GitHub API calls will fail")
    log("poller start repos=%s author=%s interval=%ss" % (REPOS, AUTHOR, INTERVAL))
    state = load_state()
    while True:
        try:
            poll_once(state)
        except Exception:
            log("fatal poll error:\n%s" % traceback.format_exc()[-2000:])
        try:
            save_state(state)
        except Exception as e:
            log("state save failed: %s" % e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
