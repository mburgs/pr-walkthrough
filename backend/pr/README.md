# PR I/O — Stream 6

Fetches GitHub PR diffs and posts inline/general comments via the `gh` CLI.

## Prerequisites

### Install gh

```
brew install gh          # macOS
# or: https://cli.github.com/
```

### Authenticate

```
gh auth login
# follow prompts; choose GitHub.com + HTTPS + browser
```

Verify: `gh auth status`

---

## Install the Python package

From the `backend/` directory:

```
pip install -e ".[dev]"
```

The `contracts/` package must also be importable. Either install from the repo root or add it to `PYTHONPATH`:

```
export PYTHONPATH=/path/to/pr-walkthrough:$PYTHONPATH
```

---

## CLI Usage

### Fetch a PR

```
python -m pr_walkthrough.pr.cli fetch https://github.com/cli/cli/pull/9169
```

Prints `{"metadata": {...}, "diff": [...]}` to stdout matching the fixture shape.

### Post a general comment

```
python -m pr_walkthrough.pr.cli comment https://github.com/owner/repo/pull/42 \
  --body "Great change!"
```

Prints the URL of the new comment.

### Post an inline comment

```
python -m pr_walkthrough.pr.cli comment https://github.com/owner/repo/pull/42 \
  --body "Should this be validated?" \
  --file src/auth/session.py \
  --line 55
```

Multi-line anchor:

```
python -m pr_walkthrough.pr.cli comment https://github.com/owner/repo/pull/42 \
  --body "This block looks risky" \
  --file src/auth/session.py \
  --line 50 --end-line 62
```

### Dry-run (prints gh args, no network calls)

```
python -m pr_walkthrough.pr.cli comment https://github.com/owner/repo/pull/42 \
  --body "test" --file foo.py --line 10 --dry-run
```

---

## Running tests

```
cd backend
pytest tests/pr/
```

Skip live tests (default): live tests are marked `@pytest.mark.live` and only run when `GH_LIVE_TEST_PR` is set:

```
GH_LIVE_TEST_PR=https://github.com/cli/cli/pull/9169 pytest tests/pr/ -m live
```
