# Repository agent workflow

For every completed code task requested by the repository owner:

- Work on a focused `agent/<task>` branch rather than leaving finished work only in
  the working tree.
- Run the relevant tests before publishing. Do not describe failing or untested work
  as complete.
- Commit only the task's files using the repository-configured author identity. Never
  create empty, backdated, padded, or artificial commits to manipulate contribution
  activity.
- Push the branch, open a pull request to `main`, and merge it only when the task is
  complete and its checks pass. Preserve commit history with a normal merge; do not
  force-push or rewrite published history just to change the contribution graph.
- Never commit credentials, session files, runtime databases, generated state, or
  other private data. Stop and report the problem if a secret appears in the diff.
- Finish by reporting the commit, pull request, merge status, and verification run.

GitHub contribution squares are a side effect of genuine merged work, not the goal of
the commit structure. Prefer one meaningful, reviewable commit over many noisy commits.
