# ocr-poller

Long-running poller that runs [open-code-review](https://github.com/alibaba/open-code-review)
on your own pull requests and posts the findings as GitHub review comments.
Built to run as a Dokploy application (any plain Docker host works too).

## How it works

Every `POLL_INTERVAL` seconds it lists open PRs authored by `PR_AUTHOR` in
`TARGET_REPOS` and reviews a PR only when:

- `PR_AUTHOR` posts a comment starting with **`/ocr`** on the PR.

(draft → ready transitions do not trigger a review.)

Each head SHA is reviewed at most once. The first time the poller sees a PR it
only records a baseline (no review), so restarts never cause surprise reviews.

If a review fails (network, git, LLM, …) the trigger is *not* consumed: it is
retried on the next poll, up to 5 attempts, before the poller gives up loudly
in the logs. Posting a new `/ocr` comment re-arms the trigger. One PR's
failure never blocks the other PRs in the same poll cycle.

The review itself runs `ocr review --from origin/<base> --to <head> --format json`
and posts results with the official
`post-review-comments.js` from the open-code-review repo (vendored under
`vendor/`, Apache-2.0), so inline comments, batching and the sticky summary
behave exactly like the GitHub Action — without needing any repo admin rights.

## Configuration (environment variables)

| Variable | Required | Description |
|---|---|---|
| `TARGET_REPOS` | yes | Comma-separated `owner/repo` list, e.g. `goplus/builder,goplus/builder-backend` |
| `PR_AUTHORS` | no | Comma-separated GitHub login list to watch (default `CORCTON`; legacy singular `PR_AUTHOR` also works) |
| `GITHUB_TOKEN` | yes | PAT with **Pull requests: read & write** on the target repos (fine-grained, repo-scoped) |
| `OCR_LLM_URL` | yes | LLM endpoint, e.g. `https://api.openai.com/v1/chat/completions` |
| `OCR_LLM_AUTH_TOKEN` | yes | LLM auth token (also accepted as `OCR_LLM_TOKEN`) |
| `OCR_LLM_MODEL` | yes | Model name |
| `OCR_LLM_USE_ANTHROPIC` | no | `true` for Anthropic Claude models. Translated internally to the CLI's `OCR_USE_ANTHROPIC` + `OCR_LLM_PROTOCOL` — the CLI defaults to Anthropic and ignores `OCR_LLM_USE_ANTHROPIC`, so without this translation an OpenAI-compatible endpoint would receive requests at `<url>/v1/messages` and fail every item |
| `POLL_INTERVAL` | no | Seconds between polls (default `300`) |

At startup the poller runs `ocr llm test` once and logs the result, so a wrong
LLM URL / protocol / key is visible in the logs immediately instead of only
when the first review is triggered.

## Local run

```bash
docker build -t ocr-poller .
docker run -d --name ocr-poller \
  -e TARGET_REPOS="goplus/builder,goplus/builder-backend" \
  -e GITHUB_TOKEN="..." \
  -e OCR_LLM_URL="..." -e OCR_LLM_AUTH_TOKEN="..." -e OCR_LLM_MODEL="..." \
  ocr-poller
docker logs -f ocr-poller
```
