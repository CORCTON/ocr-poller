#!/usr/bin/env python3
"""Poll GitHub for review triggers and run open-code-review.

Triggers (only for PRs authored by someone in PR_AUTHORS, in TARGET_REPOS):
  - draft -> ready_for_review transition
  - a new comment by someone in PR_AUTHORS whose body starts with /ocr

State is kept in STATE_PATH as JSON:
  {"<repo>#<pr>": {"head_sha":..., "draft":..., "last_comment_id":..., "reviewed":[...]}, ...}
The first sighting of a PR only records a baseline (no review), so container
restarts or pre-existing ready PRs never cause a surprise review.

Env:
  TARGET_REPOS   comma-separated, e.g. "goplus/builder,goplus/builder-backend" (required)
  PR_AUTHORS     comma-separated GitHub logins to watch, e.g. "CORCTON,teammate"
                 (also accepts the legacy singular PR_AUTHOR; default "CORCTON")
  GITHUB_TOKEN   PAT with pull-requests write on the target repos (required).
                 Used for the GitHub API (Authorization header) and for git
                 via a short-lived GIT_ASKPASS helper -- it is never embedded
                 in clone URLs (which git would persist into .git/config),
                 never appears in argv, and is redacted from all logs.
  POLL_INTERVAL  seconds between polls, default "300"
  STATE_PATH     default "/state/state.json"
  WORK_DIR       clone dir, default "/work"
  OCR_LLM_URL / OCR_LLM_AUTH_TOKEN / OCR_LLM_MODEL [/ OCR_LLM_USE_ANTHROPIC]
                 passed through to `ocr` (user fills these in Dokploy UI)
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request

REPOS = [r.strip() for r in os.environ.get("TARGET_REPOS", "").split(",") if r.strip()]
AUTHORS = [a.strip() for a in
           os.environ.get("PR_AUTHORS", os.environ.get("PR_AUTHOR", "CORCTON")).split(",")
           if a.strip()]
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))
STATE_PATH = os.environ.get("STATE_PATH", "/state/state.json")
WORK_DIR = os.environ.get("WORK_DIR", "/work")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
SUMMARY_MARKER = "<!-- ocr-summary -->"  # our own poster comments must not retrigger


def redact(s):
    # Belt and suspenders: tokens must never reach logs, even if a
    # subprocess echoes a URL or header back in an error message.
    for tok in (GH_TOKEN, os.environ.get("OCR_LLM_AUTH_TOKEN", ""),
                os.environ.get("OCR_LLM_TOKEN", "")):
        if tok and tok in s:
            s = s.replace(tok, "***")
    return s


def log(*a):
    print(time.strftime("[%Y-%m-%dT%H:%M:%S]"),
          *[redact(str(x)) for x in a], flush=True)


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


def make_askpass():
    """Create a short-lived askpass helper that feeds GIT_ASKPASS_TOKEN to git.

    Returns the script path; the caller must delete it when done.
    """
    fd, path = tempfile.mkstemp(prefix="git-askpass-")
    with os.fdopen(fd, "w") as f:
        f.write('#!/bin/sh\nexec echo "$GIT_ASKPASS_TOKEN"\n')
    os.chmod(path, 0o700)
    return path


def git(args, cwd=None, timeout=600):
    """Run git with credentials via GIT_ASKPASS.

    The token is never embedded in the clone URL (which git would persist
    into .git/config) and never appears in argv; it is only handed to git
    through a short-lived askpass helper. Public repos work fine without a
    token too.
    """
    env = dict(os.environ)
    askpass = None
    if GH_TOKEN:
        askpass = make_askpass()
        env["GIT_ASKPASS"] = askpass
        env["GIT_ASKPASS_TOKEN"] = GH_TOKEN
        env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(["git"] + args, cwd=cwd, timeout=timeout, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)
    finally:
        if askpass:
            try:
                os.unlink(askpass)
            except OSError:
                pass


def git_url(repo):
    # Deliberately token-free: auth goes through GIT_ASKPASS in git().
    return "https://github.com/%s.git" % repo


def ensure_clone(repo):
    owner, name = repo.split("/")
    d = os.path.join(WORK_DIR, "%s__%s" % (owner, name))
    if not os.path.isdir(os.path.join(d, ".git")):
        log("cloning", repo)
        r = git(["clone", "--quiet", git_url(repo), d], timeout=900)
        if r.returncode != 0:
            raise RuntimeError("clone failed: %s" % r.stderr[-500:])
    else:
        r = git(["fetch", "--quiet", "origin", "--prune"], cwd=d, timeout=600)
        if r.returncode != 0:
            raise RuntimeError("fetch origin failed: %s" % r.stderr[-500:])
    return d


def raise_if_llm_total_failure(result_path, stderr_path):
    """The `ocr` CLI exits 0 even when every selected item failed at the LLM
    ("N of N selected item(s) failed"). That is not a completed review, so
    surface the real error and raise: the caller retries instead of posting
    a misleading "Review failed" summary as if the review had happened."""
    try:
        with open(result_path) as f:
            result = json.load(f)
    except Exception:
        return  # unparsable output: let the poster deal with it
    msg = str((result or {}).get("message", ""))
    m = re.search(r"(\d+)\s+of\s+(\d+)\s+selected item\(s\) failed", msg)
    if not m or m.group(1) != m.group(2) or m.group(1) == "0":
        return
    try:
        with open(stderr_path, encoding="utf-8", errors="replace") as f:
            serr = f.read()
    except OSError:
        serr = ""
    raise RuntimeError("ocr: all %s selected items failed at the LLM; "
                       "stderr tail: %s" % (m.group(2), serr[-1500:]))


def apply_llm_env(env):
    """Translate user-facing LLM env vars into what the ocr CLI actually reads.

    The CLI defaults to the Anthropic protocol and does NOT read
    OCR_LLM_USE_ANTHROPIC. The official action maps llm_use_anthropic ->
    OCR_USE_ANTHROPIC, and OCR_LLM_PROTOCOL is honored explicitly. Without
    this, an OpenAI-compatible endpoint silently receives requests at
    <url>/v1/messages and every review item fails instantly.
    """
    # action.yml maps the token to OCR_LLM_TOKEN; accept the documented name too
    if env.get("OCR_LLM_AUTH_TOKEN") and not env.get("OCR_LLM_TOKEN"):
        env["OCR_LLM_TOKEN"] = env["OCR_LLM_AUTH_TOKEN"]
    raw = str(env.get("OCR_LLM_USE_ANTHROPIC", "false")).strip().lower()
    use_anthropic = raw in ("1", "true", "yes")
    env["OCR_USE_ANTHROPIC"] = "true" if use_anthropic else "false"
    env["OCR_LLM_PROTOCOL"] = "anthropic" if use_anthropic else "openai"
    env.pop("OCR_LLM_USE_ANTHROPIC", None)  # not a real CLI variable
    return env


def run_review(repo, number, base_ref, head_sha, fork_repo):
    d = ensure_clone(repo)
    r = git(["fetch", "--quiet", git_url(fork_repo), head_sha], cwd=d, timeout=900)
    if r.returncode != 0:
        raise RuntimeError("fetch fork head failed: %s" % r.stderr[-500:])
    result_path, stderr_path = "/tmp/ocr-result.json", "/tmp/ocr-stderr.log"
    env = apply_llm_env(dict(os.environ))
    cmd = ["ocr", "review", "--from", "origin/%s" % base_ref, "--to", head_sha,
           "--audience", "agent", "--format", "json", "--timeout", "1500"]
    log("running ocr for %s#%s @ %s" % (repo, number, head_sha[:8]))
    with open(result_path, "wb") as out, open(stderr_path, "wb") as err:
        subprocess.run(cmd, cwd=d, stdout=out, stderr=err, env=env, timeout=1560)
    raise_if_llm_total_failure(result_path, stderr_path)
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
    mine = [p for p in prs if p.get("user", {}).get("login") in AUTHORS]
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
                    and c.get("user", {}).get("login") in AUTHORS
                    and (c.get("body") or "").lstrip().startswith("/ocr")
                    and SUMMARY_MARKER not in (c.get("body") or "")]
        if new_cmds:
            triggers.append("/ocr")

        def advance():
            prev["last_comment_id"] = max_id
            prev["head_sha"] = head
            prev["draft"] = draft
            prev.pop("fail_count", None)

        if not triggers:
            advance()
            continue
        if head in prev.get("reviewed", []):
            log("%s triggered by %s but head %s already reviewed; skip"
                % (key, triggers, head[:8]))
            advance()
            continue
        fails = prev.get("fail_count", 0)
        if fails >= 5:
            log("%s giving up after %d failed attempts; post a new /ocr to retry"
                % (key, fails))
            advance()
            continue
        log("%s triggered by %s; reviewing head %s (attempt %d)"
            % (key, triggers, head[:8], fails + 1))
        try:
            run_review(repo, number, base, head, fork)
        except Exception as e:
            # Do NOT advance last_comment_id: the trigger stays unconsumed and
            # will be retried on the next poll. One PR's failure must not abort
            # the rest of the repo loop either.
            prev["fail_count"] = fails + 1
            log("%s review failed (attempt %d): %s" % (key, fails + 1, e))
            continue
        advance()
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


def llm_self_test():
    """Run `ocr llm test` once at startup so an LLM misconfiguration (wrong
    protocol, bad URL, bad key) is visible in the logs immediately instead of
    surfacing only when a review is triggered. Never blocks startup."""
    env = apply_llm_env(dict(os.environ))
    try:
        r = subprocess.run(["ocr", "llm", "test"], env=env, timeout=60,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
        out = (r.stdout or "")[-800:]
        log("llm self-test exit %d\n%s" % (r.returncode, out))
    except Exception as e:
        log("llm self-test error: %s" % e)


def main():
    if not REPOS:
        log("TARGET_REPOS is empty; nothing to do")
        sys.exit(1)
    if not GH_TOKEN:
        log("WARNING: GITHUB_TOKEN is empty; GitHub API calls will fail")
    log("poller start repos=%s authors=%s interval=%ss" % (REPOS, AUTHORS, INTERVAL))
    llm_self_test()
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
