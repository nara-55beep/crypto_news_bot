# Security policy

## Secrets and private data

The repository must contain source and safe examples only. Keep these local or in a
deployment secret manager:

- `config.py` and `.env*` files other than `.env.example`;
- Telegram sessions, bot tokens, API ID/hash, chat IDs, and phone information;
- AI, exchange, market-data, and webhook credentials;
- Lighter wallet/account identifiers and signing keys;
- runtime databases, state JSON, logs, screenshots containing account information,
  and downloaded market data.

Copy `config.example.py` to `config.py` for local use and provide credentials through
environment variables listed in `.env.example`. Browser-delivered code cannot hold a
private secret: use a backend or serverless route for authenticated third-party calls.

## Before every push

Review `git status`, `git diff --cached`, and ignored-file behavior. Search the staged
snapshot for actual credential values as well as common labels such as `api_key`,
`secret`, `token`, `password`, `authorization`, and `private_key`.

If a credential appears in a commit, log, transcript, screenshot, or deployment bundle,
treat it as compromised: revoke or rotate it first, then remove it from the entire Git
history. Deleting it only from the newest revision is insufficient.
