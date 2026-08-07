# Running the bot on your own PC

You do **not** need AWS or any server. This bot runs fine on a normal laptop or
desktop. AWS only mattered for shaving network latency to the exchange — which,
as covered in the README, is not a realistic edge for a retail news bot. Run it
locally.

The only real downsides vs a server: your PC must **stay on and online** (if it
sleeps or loses Wi-Fi, the bot stops and misses news), and home internet is a bit
slower/less reliable than a datacenter. For paper trading, none of that matters.

**Hardware needed:** almost nothing. Any PC from the last 10 years. **No GPU**
needed when using a hosted AI API. A GPU only matters if you later run a *local*
LLM (see README "Latency").

---

## Step 1 — Install Python 3.11+

### Windows
1. Go to https://www.python.org/downloads/ and download the latest Python 3.
2. Run the installer. **CHECK the box "Add python.exe to PATH"** on the first
   screen (this is the #1 mistake). Then "Install Now".
3. Open **PowerShell** (Start menu → type "PowerShell") and verify:
   ```powershell
   python --version
   ```
   You should see `Python 3.12.x` (or similar). If it says "not recognized",
   reinstall and tick the PATH box.

### macOS
1. Easiest: install Homebrew (https://brew.sh), then:
   ```bash
   brew install python
   ```
   Or download the installer from python.org.
2. Verify in **Terminal**:
   ```bash
   python3 --version
   ```

### Linux (Ubuntu/Debian)
```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip
python3 --version
```

---

## Step 2 — Put the bot folder on your PC

Move the `crypto_news_bot` folder (all the `.py` files + `requirements.txt`) to
somewhere easy, e.g.:
- Windows: `C:\Users\<you>\crypto_news_bot`
- macOS/Linux: `~/crypto_news_bot`

Open a terminal **in that folder**:
- Windows: in File Explorer, open the folder, click the address bar, type
  `powershell`, press Enter.
- macOS: right-click the folder → "New Terminal at Folder" (or `cd ~/crypto_news_bot`).
- Linux: `cd ~/crypto_news_bot`

---

## Step 3 — Create a virtual environment (recommended)

Keeps the bot's packages separate from your system Python.

### Windows (PowerShell)
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```
If you get a script-execution error, run this once, then retry Activate:
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### macOS / Linux
```bash
python3 -m venv venv
source venv/bin/activate
```

You'll know it worked when your prompt shows `(venv)` at the start.

---

## Step 4 — Install dependencies

```bash
pip install -r requirements.txt
```
(Windows: same command. If `pip` isn't found, use `python -m pip install -r requirements.txt`.)

---

## Step 5 — Get your free Telegram credentials

1. Go to https://my.telegram.org and log in with your phone number.
2. Click **"API development tools"**.
3. Fill the form (App title / short name — anything, e.g. "newsbot"). Platform:
   Desktop. Submit.
4. You'll get an **api_id** (a number) and an **api_hash** (a long string).
5. In the Telegram app on your phone, **join** the three channels so your account
   can read them: BWEnews, trad_fin, SynopticNewswire.

---

## Step 6 — Get an AI key (the only paid part) — or run it free locally

**Option A — hosted (simplest):** sign up with an AI provider, create an API key.
Store the key as a private environment/deployment secret. Configure the non-secret
base URL and a *fast/cheap* model name in your local `config.py`.
- OpenAI:     base_url `https://api.openai.com/v1`
- OpenRouter: base_url `https://openrouter.ai/api/v1` (one key → Claude/GPT/Llama/…)

**Option B — fully free, local model:** install Ollama (https://ollama.com), then
`ollama pull llama3.1` (or similar), and in `config.py` set
`AI_BASE_URL = "http://localhost:11434/v1"`, `AI_API_KEY = "ollama"` (any string),
`AI_MODEL = "llama3.1"`. This removes all AI cost and network round-trip. A local
model benefits from a GPU but small ones run on CPU.

---

## Step 7 — Create private local configuration

Copy the safe template. The resulting `config.py` is ignored by Git:

```powershell
Copy-Item config.example.py config.py
```

Set credentials as system environment variables or deployment secrets. The names
are documented in `.env.example`, which intentionally contains no values. At minimum:

- `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` (from Step 5)
- `GROQ_API_KEY` or the key expected by your chosen AI provider

Edit only non-secret settings such as `AI_BASE_URL` and `AI_MODEL` in local
`config.py`. Never put credentials in `config.example.py`; never expose them through
frontend JavaScript or `VITE_*`/`NEXT_PUBLIC_*` variables.

Leave `MODE = "PAPER"`. Save.

---

## Step 8 — Run it

```bash
python main.py
```
(macOS/Linux may need `python3 main.py`.)

**First run only:** Telethon will ask in the terminal for your **phone number**,
then a **login code** Telegram sends to your app (and possibly your 2FA password).
This happens once; a session file in `data/` keeps you logged in afterward.

Then you'll see:
- `market data live: {...}` — live BTC/ETH/SOL prices flowing in
- `[telegram] listening on: ...`
- a stream of `[SKIP]` (news the AI judged not tradable), and when something
  passes every gate: `[OPEN]` / `[CLOSE]` lines, plus a `[status]` heartbeat each
  minute showing your virtual equity.

All news + trades are saved to `data/trades.db` (SQLite). Open it later with any
SQLite viewer (e.g. "DB Browser for SQLite") to review what happened.

---

## Step 9 — Keep it running

The bot only catches news while it's running, so for real testing it needs to run
continuously.

- **Simplest:** just leave the terminal window open. Disable your PC's sleep mode
  (so it doesn't suspend): Windows → Settings → Power → "Screen and sleep" → set
  "When plugged in, put my device to sleep after" to **Never**. macOS → System
  Settings → Lock Screen / Battery → prevent sleep when plugged in.
- **macOS/Linux, run in background:**
  ```bash
  nohup python3 main.py > bot.log 2>&1 &
  ```
  …then `tail -f bot.log` to watch it. Or use `tmux` / `screen` so it survives
  closing the terminal.
- **Windows, run in background:** keep the PowerShell window open, or use Task
  Scheduler to start `venv\Scripts\python.exe main.py` at logon. (Note: the
  one-time Telegram phone/code login must be done interactively first.)

**To stop:** press `Ctrl-C` in the terminal.

---

## Troubleshooting

- **`ModuleNotFoundError`** → your venv isn't active or deps aren't installed.
  Re-activate (Step 3) and re-run Step 4.
- **`python` not recognized (Windows)** → PATH box wasn't ticked during install;
  reinstall Python, or use the full path / the `py` launcher (`py main.py`).
- **Telegram: "Cannot find any entity corresponding to ..."** → you haven't
  joined that channel in the Telegram app. Join it, restart the bot.
- **Telegram: FloodWait / asks for code repeatedly** → you're logging in too
  often; wait, and don't delete the `data/` session file.
- **AI errors / 401 Unauthorized** → wrong `AI_API_KEY` or `AI_BASE_URL`/`AI_MODEL`
  mismatch for your provider. The bot will just print the error and not trade.
- **No prices / market data warning** → check your internet; Binance public market
  data must be reachable (`fstream.binance.com`).
- **`[nitter] all instances failed`** → expected until you paste *currently-working*
  instances into `NITTER_INSTANCES` from https://status.d420.de. Leave the list
  empty to silence it.
- **Nothing trades for a long time** → that's normal and correct. The gates are
  strict by design; most news isn't tradable. Loosen thresholds in `config.py`
  only if your `data/trades.db` review justifies it.
