# Debrid Monitor

Polls your debrid provider's API and automatically switches addon URLs across all configured AIOManager accounts when the service goes down, then restores them when it recovers. 
Why would you want this? Maybe you have unlimited on a debrid provider but want a usenet backup, however paying monthly for usenet is an added cost. With this you could have an 
AIOStreams configuration that is setup to use a usenet block account that only switches over to that configuration when torbox/reldebrid is down. Allowing your block to not be used in normal streaming, but allow for redundancy.


# Credits
[Viren](https://github.com/Viren070) - For AIOStreams and all their stellar work in the stremio community

[Sonic](https://github.com/Sonicx161) - For AIOManager which makes managing addons a ton easier, this tool wouldnt be possible without the hydra api inside AIOManager.

The stremio team - Without them we wouldnt have a program to try and tinker/optimize more than we watch

Countless others who help all the time in viren's discord.

## How it works

On each poll cycle the monitor checks your debrid provider's API directly using your API key. Based on the result:

| Status | Action |
|---|---|
| **Up** | Restore debrid addon if accounts are on fallback |
| **Down** | Switch all accounts to the fallback addon URL |
| **Expired** | Log a warning, skip the cycle — no addon switching |

State is persisted to `.state.json` so restarts don't re-trigger unnecessary switches.

## Supported providers

| Provider | `DEBRID_PROVIDER` value |
|---|---|
| TorBox | `torbox` |
| Real-Debrid | `realdebrid` |

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

Either create a `.env` file (see sample below) or set environment variables directly (e.g. Replit Secrets).

### 3. Run

```bash
python monitor.py
```

---

## Configuration

All configuration can be provided via environment variables. `config.json` is optional — if present, any missing fields fall back to environment variables.

### Sample `.env`

```env
# ---------------------------------------------------------------------------
# Debrid Provider
# ---------------------------------------------------------------------------

# Which debrid provider to monitor: torbox | realdebrid
DEBRID_PROVIDER=torbox

# Your debrid provider API key
DEBRID_API_KEY=your_debrid_api_key_here

# ---------------------------------------------------------------------------
# AIOManager
# ---------------------------------------------------------------------------

# Your AIOManager instance URL
AIO_URL=https://aiomanager.example.com

# ---------------------------------------------------------------------------
# Addon URLs
# ---------------------------------------------------------------------------

# Default debrid addon manifest URL (used when no resolution is set, or resolution = 4k)
DEBRID_ADDON_URL=https://your-aiostreams-instance.com/stremio/uuid-here/token-here/manifest.json

# HD debrid addon manifest URL (used when account resolution = hd)
# In my use case, certain accounts I limit to 1080p as the devices its used on aren't 4k anyways
# This lets me restore to a different AIOStreams config when service returns.
DEBRID_HD_ADDON_URL=https://your-aiostreams-instance.com/stremio/uuid-here/hd-token-here/manifest.json

# Fallback addon manifest URL (used when debrid is down)
FALLBACK_ADDON_URL=https://your-fallback-addon.com/stremio/uuid-here/token-here/manifest.json

# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
# For each account, set ACCOUNT_<NAME>_API_KEY where <NAME> is the account
# name uppercased with spaces and hyphens replaced by underscores.
# ACCOUNT_<NAME>_RESOLUTION is optional — omit to use the default DEBRID_ADDON_URL.
# Resolution options: 4k | hd
# API key is found in the account on AIOmanager, under connections.

ACCOUNT_ALICE_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
ACCOUNT_ALICE_RESOLUTION=4k

ACCOUNT_BOB_API_KEY=yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy
ACCOUNT_BOB_RESOLUTION=hd

ACCOUNT_CHARLIE_API_KEY=zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz
# No resolution set — will use DEBRID_ADDON_URL

# Multi-word names use underscores:
# "Jack Main" -> ACCOUNT_JACK_MAIN_API_KEY
ACCOUNT_JACK_MAIN_API_KEY=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
ACCOUNT_JACK_MAIN_RESOLUTION=4k

# ---------------------------------------------------------------------------
# Timing (optional — defaults shown)
# ---------------------------------------------------------------------------

# How often to poll the debrid provider API (seconds)
POLL_INTERVAL_SECONDS=60

# HTTP request timeout (seconds)
REQUEST_TIMEOUT_SECONDS=10

# ---------------------------------------------------------------------------
# TorBox status page (optional — only used if provider is torbox)
# Kept for reference but the monitor now checks the API directly, not the status page
# ---------------------------------------------------------------------------
# TORBOX_STATUS_URL=https://status.torbox.app/index.json
```

---

## Replit Setup

Add each of the above as a **Secret** in your Replit project (Tools → Secrets). The monitor reads them automatically — no `.env` file needed. Accounts are discovered automatically from any secret matching `ACCOUNT_<NAME>_API_KEY`.

---

## State file

The monitor writes `.state.json` alongside `monitor.py` to track which direction each account is currently switched. Delete it to force a fresh check on next startup.

```json
{
  "Alice": { "direction": "debrid" },
  "Bob": { "direction": "fallback" }
}
```

---

## Adding a new debrid provider

Add a checker function and register it in `DEBRID_CHECKERS` in `monitor.py`:

```python
def check_myprovider(api_key: str, timeout: int) -> str:
    # Return "up", "down", or "expired"
    ...

DEBRID_CHECKERS = {
    "torbox": check_torbox,
    "realdebrid": check_realdebrid,
    "myprovider": check_myprovider,
}
```

Then set `DEBRID_PROVIDER=myprovider` in your environment.
