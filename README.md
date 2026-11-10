# calsync

Read a schedule, a photo of a calendar, or a sentence, and put the events in **Notion** (so they show in Notion Calendar). See and edit that calendar, with your Google Calendar next to it, in one small local app.

> ## Built entirely by AI
> **Every line of this project was written by Claude** (an AI model made by Anthropic), working through Claude Code, at the direction of one person. That includes the Python, the web page, the build scripts, the tests, the workflows and this README.
>
> A human described what they wanted, ran the app on their own computer, and reported what broke. They did **not** write or audit the code. It has **not** had an independent security review. Treat it like any young, unreviewed software: read it before you give it keys, and see [What is not tested](#what-is-not-tested).
>
> The AI also made mistakes while building this, for example garbling some text through a file-encoding slip, and wrecking a file with a bad find-and-replace (caught and repaired from backups before this repository existed). The tests in `tests/` exist partly because of that.

## What it does

| Tab | What you give it | What you get |
|---|---|---|
| **Work** (rename it freely) | A screenshot, photo or PDF of a work schedule or roster, a text file, or your email inbox | Your shifts as cards you tick, edit, and add to Notion. Only shifts for the names you list are kept. |
| **Personal** | A photo of a handwritten family calendar (fridge, whiteboard) | Every event with who it is for, flagged where the handwriting was unclear. |
| Personal › **Past hangouts** | A date range | Outings with friends found in your [Immich](https://immich.app) photos (when and where the photos were taken). Needs Immich. |
| **Describe** | A sentence: "Dentist next Tuesday at 3pm", "soccer with Mia every Thursday in November, 5 to 6" | Real events with dates worked out. "Evening" becomes 6 to 9pm, not all-day. |
| **Calendar** | Nothing | A month grid or list of your Notion calendar. Hover an event for details, click to edit in a card, drag to another day, delete (to Notion's trash). Refreshes every 30 s, so edits made in Notion Calendar show up. |

Also:

- **Nothing is added until you tick it and press Add.** Events already in Notion are detected and skipped, so reading the same schedule twice never duplicates.
- **Pick your AI reader:** Gemini (has a free tier), Claude, or a local model through Ollama (nothing leaves your computer). Gemini falls back through other models automatically when one is busy or retired.
- **Google Calendar:** show it read-only with a private iCal link (simple), or connect your Google account to edit and drag Google events too.
- **Your own tab names and subtitles**, and a colour per tab and per kind of event. Type `-` as a tab name to hide it.
- **Runs your way:** a Windows `.exe` with its own window, in a browser tab, on a phone over your Wi-Fi, or on a home server shared privately through [Tailscale](https://tailscale.com).
- **Updates itself, only when you say so:** it tells you when a new release exists and installs it when you press **Update now**.

## Install

### Windows (easiest)
1. Download `calsync.exe` and `SHA256SUMS` from the [latest release](../../releases/latest).
2. Optional check: `certutil -hashfile calsync.exe SHA256` should match the line in `SHA256SUMS`.
3. Put it in its own folder and double-click it. Windows SmartScreen may warn once because the file is not code-signed: **More info → Run anyway**.
4. With no settings yet it starts in demo mode. Open the gear (top right) and fill in what you need.

Settings are saved in `config/calsync.env` next to the `.exe`. Closing the window stops calsync. To keep it running in the background, set `CALSYNC_OPEN=tab` in that file (it then opens as a tab in your browser and keeps running).

### From source (Windows, macOS, Linux)
```
pip install -r requirements.txt
python launcher.py            # opens calsync
python launcher.py --demo     # sample data, nothing sent or saved
python launcher.py --phone    # also reachable by phones on your Wi-Fi
```
Python 3.10 or newer. The `Start calsync (…)` scripts do the same and install the packages into a `lib/` folder next to the program.

### On a home server with Tailscale (Linux)
`install-server.sh` sets calsync up as a background service that starts at boot, finds Immich if it runs on that machine, and shares calsync **only on your Tailscale network** over https with `tailscale serve`.

- From Windows: double-click `Deploy to home server (Windows).bat`, give it `user@server-name` once.
- On the server itself: `bash install-server.sh`.

Open the link it prints on any device signed in to your Tailscale network. On iPhone: Safari → Share → Add to Home Screen.

## Getting your keys (Settings page)

| Setting | Where to get it |
|---|---|
| **Gemini key** (free tier) | <https://aistudio.google.com/apikey> |
| **Claude key** | <https://console.anthropic.com> → API keys |
| **Local model** | Install [Ollama](https://ollama.com), `ollama pull qwen2.5vl:7b`, address `http://localhost:11434`. Cannot read PDFs. |
| **Notion** | <https://www.notion.so/profile/integrations> → New integration → copy the token. Make a database with these columns: `Name` (title), `Date` (date), `Type` (select), `Status` (select), `People`, `Location`, `Notes`, `Source ID` (all text). In the database: `···` → Connections → add your integration. Paste the database link into calsync. |
| **Immich** | Your Immich address, and Account Settings → API Keys → New. |
| **Email** (optional) | Your IMAP server and an *app password* (Gmail: Google Account → Security → App passwords). |
| **Google Calendar, read-only** | Google Calendar (web) → Settings → your calendar → *Integrate calendar* → **Secret address in iCal format**. |
| **Google Calendar, edit and drag** | The Settings page has a "How to get these" list: a Google Cloud project, the Calendar API, an OAuth consent screen set to *In production*, and a **Desktop app** client. You can load the downloaded client JSON straight into the two boxes. |

## Privacy and security, plainly

- **Where your data goes.** Photos, PDFs, emails and typed text you submit are sent to the AI reader you chose (Gemini, Claude, or your own machine for Local). Events go to Notion. Calendar requests go to Google if you use it, Immich requests to your server, and email to your mail server. calsync also asks GitHub whether a newer release exists (no data about you is sent). There is no calsync server and no analytics.
- **Free AI tiers can differ.** Google's free Gemini tier may use submitted content to improve its products. Use a paid key or the Local reader if that matters.
- **Keys are stored as plain text** in `config/calsync.env`. Anyone with that file has your keys. Keep it off shared drives; this repository's `.gitignore` excludes it.
- **The Google iCal link is a password.** Anyone with it can read your calendar. Reset it in Google if it leaks. calsync only accepts `https://` addresses for it.
- **Access code.** calsync only listens on your own computer by default. Every request needs a private access code (`CALSYNC_APP_TOKEN`) carried in the link. `--phone` opens it to your local network; on public Wi-Fi do not use it.
- **`calsync.log`** (the `.exe`'s log) contains the link with your access code. Don't share it.
- **Google sign-in** asks for the `calendar` permission (read and edit your events). calsync stores only a refresh token in the settings file. Revoke it any time at <https://myaccount.google.com/permissions>.
- **Updates** download only from this project's own GitHub release, and the `.exe` must match the `SHA256SUMS` published with it. That catches a corrupted download. It does not protect against someone taking over the GitHub account, so keep two-factor on that account if you publish a fork.
- Full policy: [PRIVACY.md](PRIVACY.md). Report a problem privately: see [SECURITY.md](SECURITY.md).

## What is not tested

Honesty list. These were written but **never run against the real thing**:

- **Google sign-in and editing Google events** (needs a Google account and Cloud project; the code and its error messages are untested live).
- **`install-server.sh` and the Windows deploy script** on a real server, and `tailscale serve` (no Linux machine was available while writing them; only a syntax check in CI).
- **The Claude reader** (model name and API options are unconfirmed), **the Local/Ollama reader**, and **PDF reading**.
- **A real Immich "Past hangouts" run.**
- **Reading real schedule emails** (the login works; extraction from real emails was not run).
- **Touch dragging** on phones and tablets, and the layout at phone width.
- **macOS and Linux launchers.** Everything was exercised on **Windows 11 with Python 3.14**. CI runs the unit tests on Linux and Windows with Python 3.10 and 3.13.
- **The self-updater against a real GitHub release.** Its logic is unit-tested against a local fake server.

What *is* exercised by the 27 tests in `tests/`: card/time conversion, settings validation and secret handling, Google calendar parsing (recurring and all-day events), the describe step, URL and id safety checks, and the updater's version, checksum, trust and file-replacement rules.

## Updating

When a newer release exists a banner appears: **Update now** installs it, **Later** hides it. The `.exe` swaps itself in and restarts; a source install replaces only program files (never `config/`, `lib/` or `window/`). Settings → *About* → **Check for updates** checks on demand. To turn update checks off completely, add `CALSYNC_UPDATE_CHECK=off` to `config/calsync.env`. A fork can point the updater at its own releases with `CALSYNC_REPO=owner/name`.

## Development

```
python -m unittest discover -s tests -v   # 27 tests, no accounts or network needed
build.bat                                  # builds calsync.exe and SHA256SUMS (Windows)
python launcher.py --demo --tab            # try the UI with sample data
```

Layout:

```
launcher.py            starts the app, opens the window, finds packages
app/app.py             the local web server and its JSON API (standard library only)
app/calsync.py         reading schedules/photos/text, the AI readers, Notion, Google, Immich, email
app/update.py          the update check and installer
app/web/index.html     the whole interface, one file, no build step
tests/                 unit tests
install-server.sh      home-server setup (Linux, systemd, Tailscale)
```

### Releasing
1. Bump `app/version.py` and add a note to `CHANGELOG.md`.
2. `git tag v1.1.0 && git push --tags`.
3. The `release` workflow builds `calsync.exe` from the public source on GitHub's servers, writes `SHA256SUMS`, and publishes the release. Running copies will offer it as an update.

## License

MIT, see [LICENSE](LICENSE). No warranty. See the AI notice at the top before relying on it.
