# GitHub Private Repository Setup

This repository already contains the application code. The steps below show how to publish the current local repo to a **private** GitHub repository using either the GitHub CLI (`gh`) or the GitHub website.

## What this project runs

- Development: `run.sh` starts `uvicorn app.main:app` after creating `.venv`, installing `requirements.txt`, and ensuring `audio_cache/` exists.
- Docker: `Dockerfile` runs `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2`.
- `docker-compose.yml` starts the app on port `8000` and Redis on `127.0.0.1:6379`.

## 1) Verify GitHub CLI authentication

Check whether `gh` is installed and authenticated:

```bash
gh --version
gh auth status
```

If you are not authenticated, sign in:

```bash
gh auth login
```

Recommended choices for this repo:

- GitHub.com
- HTTPS
- Login with a web browser
- Authenticate for Git operations as prompted

## 2) Initialize Git only if needed

If this directory does not already have a `.git` folder, initialize it first:

```bash
git init
```

If `.git` already exists, do not run `git init` again.

## 3) Create the private GitHub repository with `gh`

From the repository root, create a private repo and connect it to the current directory:

```bash
gh repo create sms-voice-gateway --private --source=. --remote=origin --push
```

Notes:

- Replace `sms-voice-gateway` with your preferred GitHub repository name if needed.
- `--source=.` uses the current folder as the local source.
- `--remote=origin` adds the `origin` remote automatically.
- `--push` pushes the current branch after the first commit is created.

If you want to create the repository without pushing immediately, omit `--push`:

```bash
gh repo create sms-voice-gateway --private --source=. --remote=origin
```

## 4) Commit and push the current branch manually

If you prefer to do the Git steps yourself, use this flow:

```bash
git status
git add .
git commit -m "Initial commit"
git branch --show-current
git push -u origin HEAD
```

If the branch already has commits, you can still use:

```bash
git push -u origin HEAD
```

## 5) Add the remote manually if `origin` already exists

If `origin` is already configured, inspect it first:

```bash
git remote -v
```

If you need to change it to the GitHub repository URL, update the existing remote:

```bash
git remote set-url origin https://github.com/<your-username>/sms-voice-gateway.git
```

If no remote exists, add one:

```bash
git remote add origin https://github.com/<your-username>/sms-voice-gateway.git
```

Then push:

```bash
git push -u origin HEAD
```

## 6) Manual GitHub website alternative

If you do not want to use `gh` to create the repository:

1. Sign in to GitHub in your browser.
2. Click **New repository**.
3. Enter the repository name, for example `sms-voice-gateway`.
4. Set visibility to **Private**.
5. Do **not** initialize with a README, `.gitignore`, or license if you already have a local repository.
6. Create the repository.
7. Copy the remote URL shown by GitHub.
8. Add or update your local `origin` remote:

```bash
git remote add origin https://github.com/<your-username>/sms-voice-gateway.git
```

or, if `origin` already exists:

```bash
git remote set-url origin https://github.com/<your-username>/sms-voice-gateway.git
```

9. Push your current branch:

```bash
git push -u origin HEAD
```

## 7) Quick checklist

- `gh auth status` succeeds
- `.git` exists, or you ran `git init`
- A private GitHub repository exists
- `origin` points to the correct GitHub URL
- Your current branch is committed and pushed

## Troubleshooting

- **`gh: command not found`**: Install GitHub CLI first, then retry.
- **Authentication errors**: Run `gh auth login` again and confirm HTTPS/browser login.
- **Push rejected because remote has work**: Fetch first, then merge or rebase as appropriate.
- **Remote already exists**: Use `git remote -v` and `git remote set-url origin ...` instead of adding a duplicate remote.
- **No commits to push**: Run `git add .` and `git commit -m "Initial commit"` before pushing.