# Dev workflow — branch → PR → CI → merge

This is how a multi-developer team (each with their own Claude Code) ships changes without the merge
pain. The golden rule: **`main` is always green and deployable; everything else happens on a branch
behind a PR that CI has to pass.**

## Why

Branches that never run CI hide integration bugs until they hit `main`. (We learned this the hard
way: two devs rebuilt the same Atrium screen, and a third's route met a fourth's test only at merge
time — CI on `main` caught it, but only *after* it landed.) PRs run CI *before* merge, so conflicts
and breakages surface early, on the branch, where they're cheap to fix.

## 1. Start from a fresh main

```powershell
git switch main
git pull origin main
```

Pulling first means your branch starts from everyone else's latest — the single biggest reducer of
merge conflicts.

## 2. Work, then push to YOUR machine's branch

Each machine gets its own branch so two people never push to the same one. Set your name once:

```powershell
.\tools\push-branch.ps1 -Dev alex        # remembers it in tools/.devname (gitignored)
```

After that, just push whenever you want to share work or open a PR:

```powershell
.\tools\push-branch.ps1                      # -> branch alex/work
.\tools\push-branch.ps1 -Desc checkout-fix   # -> branch alex/checkout-fix
.\tools\push-branch.ps1 -Message "WIP nav"   # custom commit message
```

It stages everything, refuses to commit secret-looking files, and force-with-lease pushes your
branch (safe — it only updates *your* branch).

## 3. Open a Pull Request to `main`

On GitHub, open a PR from your branch into `main`. **CI runs automatically** (`.github/workflows/ci.yml`):

- esprima JS gate on every dashboard/template (`tools/_validate_dash_js.py`)
- `py_compile` on all portal modules
- the off-cloud Atrium tests (`_workspace_localtest.py`, `_atrium_smoketest.py`)

A red PR cannot merge. Fix it on the branch and push again.

## 4. Integrate everyone's branches

When several branches are ready, integrate them safely:

```powershell
.\tools\merge-branches.ps1
```

It fetches all per-machine branches, merges the clean ones onto a throwaway `integration/merge`
branch, runs the CI tests locally, and **stops for a human on the first conflict or red test** — it
never auto-pushes `main` and never deletes anything. If it stops on a conflict, **ask Claude to merge
the conflicting branch** (it handles the semantic ones — e.g. two people who rebuilt the same screen).

When it's clean and green, it prints the exact commands to land it:

```powershell
git switch main; git merge --ff-only integration/merge; git push origin main
.\tools\merge-branches.ps1 -DeleteMerged    # prune the branches now contained in main
```

`-DeleteMerged` only deletes remote branches whose commits are already in `main`, so it can never
drop unmerged work.

## 5. Make CI required (one-time, GitHub UI)

Settings → Branches → add a protection rule for `main`:

- ✅ Require a pull request before merging (≥1 approval)
- ✅ Require status checks to pass → select the **test** check
- ✅ Require branches to be up to date before merging

After this, nobody — human or AI — can merge red or stale code into `main`.

## Conventions that keep merges clean

- **Pull `main` before you branch.** Stale bases cause most conflicts.
- **Small, focused PRs** beat one giant branch — they conflict less and review faster.
- **Don't all edit the same file.** If two people must touch `services/portal/dash/main.py` or a
  shared template, say so up front — that's where real conflicts live.
- **Let CI gate it.** If it's red, it doesn't merge. No exceptions.
