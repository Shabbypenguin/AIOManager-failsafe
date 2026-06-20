#!/usr/bin/env python3
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.json"
STATE_PATH = Path(__file__).parent / ".state.json"

# TorBox statuses that count as "down"
DOWN_STATUSES = {"major_outage", "under_maintenance"}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("debrid-monitor")


# ---------------------------------------------------------------------------
# Config + state helpers
# ---------------------------------------------------------------------------


def env_str(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val is not None:
        try:
            return int(val)
        except ValueError:
            log.warning(
                f"Env var {key}='{val}' is not a valid integer, using default {default}"
            )
    return default


def load_config() -> dict:
    config = {}

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    else:
        log.info("No config.json found — loading entirely from environment variables.")

    # --- Global string fields ---
    for key, env_var in {
        "aio_url": "AIO_URL",
        "debrid_provider": "DEBRID_PROVIDER",
        "debrid_api_key": "DEBRID_API_KEY",
        "debrid_addon_url": "DEBRID_ADDON_URL",
        "debrid_hd_addon_url": "DEBRID_HD_ADDON_URL",
        "fallback_addon_url": "FALLBACK_ADDON_URL",
    }.items():
        if not config.get(key):
            config[key] = env_str(env_var)
            if not config[key]:
                log.warning(f"No value for '{key}' (env var: {env_var})")

    # --- Global int fields ---
    if not config.get("poll_interval_seconds"):
        config["poll_interval_seconds"] = env_int("POLL_INTERVAL_SECONDS", 60)

    if not config.get("request_timeout_seconds"):
        config["request_timeout_seconds"] = env_int("REQUEST_TIMEOUT_SECONDS", 10)

    # --- Accounts ---
    # Pattern: ACCOUNT_<NAME>_API_KEY must be set to register an account.
    # Optionally: ACCOUNT_<NAME>_RESOLUTION
    # <NAME> is the account name uppercased with spaces/hyphens replaced by underscores.
    # e.g. "Alice" -> ACCOUNT_ALICE_API_KEY, ACCOUNT_ALICE_RESOLUTION
    #      "Jack-Main" -> ACCOUNT_JACK_MAIN_API_KEY, ACCOUNT_JACK_MAIN_RESOLUTION
    if not config.get("accounts"):
        config["accounts"] = []
        prefix = "ACCOUNT_"
        suffix = "_API_KEY"
        for key, value in os.environ.items():
            if key.startswith(prefix) and key.endswith(suffix):
                # Extract the name portion between ACCOUNT_ and _API_KEY
                name_upper = key[len(prefix) : -len(suffix)]
                if not name_upper:
                    continue
                # Convert back to a readable name (underscores -> spaces, title case)
                name = name_upper.replace("_", " ").title()
                resolution = env_str(f"{prefix}{name_upper}_RESOLUTION")
                config["accounts"].append(
                    {
                        "name": name,
                        "api_key": value,
                        "resolution": resolution,
                    }
                )
                log.debug(f"Discovered account '{name}' from env var {key}")

    # Fill in any missing per-account fields from env vars (for accounts defined in config.json)
    for account in config.get("accounts", []):
        name_upper = account["name"].upper().replace("-", "_").replace(" ", "_")

        if not account.get("api_key"):
            env_var = f"ACCOUNT_{name_upper}_API_KEY"
            account["api_key"] = env_str(env_var)
            if not account["api_key"]:
                log.warning(
                    f"No api_key for account '{account['name']}' (tried env var: {env_var})"
                )

        if not account.get("resolution"):
            env_var = f"ACCOUNT_{name_upper}_RESOLUTION"
            account["resolution"] = env_str(env_var)

    if not config["accounts"]:
        log.error(
            "No accounts configured. Add accounts to config.json or set "
            "ACCOUNT_<NAME>_API_KEY env vars (e.g. ACCOUNT_ALICE_API_KEY)."
        )
        sys.exit(1)

    return config


def load_state() -> dict:
    """Persisted state so restarts don't re-trigger switches unnecessarily."""
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Debrid provider status checks
# ---------------------------------------------------------------------------


def check_torbox(api_key: str, timeout: int) -> str:
    """
    Returns:
      'up'      — API reachable, account active
      'down'    — API unreachable (connection error, timeout, bad status)
      'expired' — API reachable but account inactive/expired (billing issue)
    """
    try:
        resp = requests.get(
            "https://api.torbox.app/v1/api/user/me?settings=true",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        if not resp.ok:
            log.warning(f"TorBox API returned {resp.status_code}")
            return "down"
        data = resp.json().get("data") or resp.json()
        is_subscribed = data.get("is_subscribed") or (data.get("plan", 0) > 0)
        return "up" if is_subscribed else "expired"
    except Exception as e:
        log.warning(f"TorBox API check failed: {e}")
        return "down"


def check_realdebrid(api_key: str, timeout: int) -> str:
    """
    Returns:
      'up'      — API reachable, account is premium
      'down'    — API unreachable (connection error, timeout, bad status)
      'expired' — API reachable but account is not premium/expired (billing issue)
    """
    try:
        resp = requests.get(
            "https://api.real-debrid.com/rest/1.0/user",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        if not resp.ok:
            log.warning(f"Real-Debrid API returned {resp.status_code}")
            return "down"
        data = resp.json()
        is_premium = data.get("type") == "premium" and bool(data.get("expiration"))
        return "up" if is_premium else "expired"
    except Exception as e:
        log.warning(f"Real-Debrid API check failed: {e}")
        return "down"


DEBRID_CHECKERS = {
    "torbox": check_torbox,
    "realdebrid": check_realdebrid,
}


def check_debrid_status(provider: str, api_key: str, timeout: int) -> str:
    """
    Dispatches to the correct debrid provider check.
    Returns 'up', 'down', or 'expired'.
    """
    checker = DEBRID_CHECKERS.get(provider.lower())
    if checker is None:
        log.error(
            f"Unknown debrid provider '{provider}'. Supported: {list(DEBRID_CHECKERS.keys())}"
        )
        return "down"
    return checker(api_key, timeout)


def is_down(status: str) -> bool:
    return status == "down"


# ---------------------------------------------------------------------------
# Hydra API calls
# ---------------------------------------------------------------------------


def hydra_headers(api_key: str) -> dict:
    return {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }


def get_current_addon_url(
    base_url: str, api_key: str, target_urls: list[str], timeout: int
) -> str | None:
    """
    Returns whichever of target_urls is currently installed, or None if neither is.
    """
    try:
        resp = requests.get(
            f"{base_url}/hydra/addons",
            headers=hydra_headers(api_key),
            timeout=timeout,
        )
        if not resp.ok:
            log.error(
                f"  [{base_url}] GET /hydra/addons returned {resp.status_code}: {resp.text[:200]}"
            )
            return None
        if not resp.text.strip():
            log.error(f"  [{base_url}] GET /hydra/addons returned empty body")
            return None
        addons = resp.json().get("addons", [])
        installed = {a["transportUrl"] for a in addons}
        for url in target_urls:
            if url in installed:
                return url
        return None
    except Exception as e:
        log.error(f"  [{base_url}] Failed to read addon list: {e}")
        return None


def switch_addon(
    base_url: str, api_key: str, remove_url: str, install_url: str, timeout: int
) -> bool:
    """
    Removes remove_url and installs install_url via Hydra.
    Returns True on success.
    """
    try:
        resp = requests.delete(
            f"{base_url}/hydra/addons",
            headers=hydra_headers(api_key),
            params={"url": remove_url},
            timeout=timeout,
        )
        if resp.status_code == 404:
            log.info(
                f"  [{base_url}] Addon to remove wasn't installed, skipping DELETE."
            )
        elif not resp.ok:
            log.error(f"  [{base_url}] DELETE failed ({resp.status_code}): {resp.text}")
            return False
        else:
            log.info(f"  [{base_url}] Removed: {remove_url}")
    except Exception as e:
        log.error(f"  [{base_url}] DELETE error: {e}")
        return False

    try:
        resp = requests.post(
            f"{base_url}/hydra/addons",
            headers=hydra_headers(api_key),
            json={"url": install_url},
            timeout=timeout,
        )
        if not resp.ok:
            log.error(f"  [{base_url}] POST failed ({resp.status_code}): {resp.text}")
            return False
        log.info(f"  [{base_url}] Installed: {install_url}")
        return True
    except Exception as e:
        log.error(f"  [{base_url}] POST error: {e}")
        return False


# ---------------------------------------------------------------------------
# Per-account processing
# ---------------------------------------------------------------------------


def process_account(
    account: dict,
    debrid_url: str,
    fallback_url: str,
    debrid_is_down: bool,
    state: dict,
    timeout: int,
):
    name = account["name"]
    base_url = account["aio_url"].rstrip("/")
    api_key = account["api_key"]

    target_url = fallback_url if debrid_is_down else debrid_url
    source_url = debrid_url if debrid_is_down else fallback_url
    direction = "fallback" if debrid_is_down else "debrid"

    account_state = state.get(name, {})
    last_direction = account_state.get("direction")

    if last_direction == direction:
        log.debug(f"  [{name}] Already on {direction}, no action needed.")
        return

    log.info(f"  [{name}] Switching to {direction} addon...")

    current_url = get_current_addon_url(
        base_url, api_key, [debrid_url, fallback_url], timeout
    )

    if current_url == target_url:
        log.info(f"  [{name}] Already has target addon installed, updating state only.")
        state[name] = {"direction": direction}
        return

    if current_url is None:
        log.warning(
            f"  [{name}] Could not read current addon list — proceeding with install anyway."
        )

    ok = (
        switch_addon(
            base_url,
            api_key,
            remove_url=source_url if source_url and current_url is not None else "",
            install_url=target_url,
            timeout=timeout,
        )
        if (source_url and current_url is not None)
        else _install_only(base_url, api_key, target_url, timeout)
    )

    if ok:
        state[name] = {"direction": direction}
        log.info(f"  [{name}] ✓ Switch complete.")
    else:
        log.error(f"  [{name}] ✗ Switch failed — will retry next cycle.")


def _install_only(base_url: str, api_key: str, install_url: str, timeout: int) -> bool:
    try:
        resp = requests.post(
            f"{base_url}/hydra/addons",
            headers=hydra_headers(api_key),
            json={"url": install_url},
            timeout=timeout,
        )
        if not resp.ok:
            log.error(
                f"  [{base_url}] Install failed ({resp.status_code}): {resp.text}"
            )
            return False
        log.info(f"  [{base_url}] Installed: {install_url}")
        return True
    except Exception as e:
        log.error(f"  [{base_url}] Install error: {e}")
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main():
    config = load_config()
    state = load_state()

    aio_url = config["aio_url"]
    debrid_provider = config.get("debrid_provider", "").lower()
    debrid_api_key = config.get("debrid_api_key", "")
    debrid_addon_url = config["debrid_addon_url"]
    debrid_hd_addon_url = config["debrid_hd_addon_url"]
    fallback_addon_url = config["fallback_addon_url"]
    poll_interval = config["poll_interval_seconds"]
    request_timeout = config["request_timeout_seconds"]
    accounts = config["accounts"]

    if not debrid_provider:
        log.error("No DEBRID_PROVIDER set. Supported: torbox, realdebrid")
        sys.exit(1)
    if not debrid_api_key:
        log.error("No DEBRID_API_KEY set.")
        sys.exit(1)

    log.info("Debrid Monitor started.")
    log.info(f"  Provider:        {debrid_provider}")
    log.info(f"  Debrid addon:    {debrid_addon_url}")
    log.info(f"  Debrid HD addon: {debrid_hd_addon_url}")
    log.info(f"  Fallback addon:  {fallback_addon_url}")
    account_summary = [
        f"{a['name']} ({a.get('resolution', 'default')})" for a in accounts
    ]
    log.info(f"  Accounts:        {account_summary}")
    log.info(f"  Poll interval:   {poll_interval}s")

    while True:
        try:
            status = check_debrid_status(
                debrid_provider, debrid_api_key, request_timeout
            )

            if status == "expired":
                log.warning(
                    f"{debrid_provider} account inactive or expired — skipping cycle (billing issue, not an outage)."
                )
            elif status == "down":
                log.info(f"{debrid_provider} status: DOWN")
                for account in accounts:
                    try:
                        resolution = account.get("resolution", "").lower()
                        debrid_url = (
                            debrid_hd_addon_url
                            if resolution == "hd"
                            else debrid_addon_url
                        )
                        process_account(
                            {**account, "aio_url": aio_url},
                            debrid_url,
                            fallback_addon_url,
                            debrid_is_down=True,
                            state=state,
                            timeout=request_timeout,
                        )
                    except Exception as e:
                        log.error(
                            f"Unexpected error processing account {account.get('name', '?')}: {e}"
                        )
                save_state(state)
            else:
                log.info(f"{debrid_provider} status: UP")
                for account in accounts:
                    try:
                        resolution = account.get("resolution", "").lower()
                        debrid_url = (
                            debrid_hd_addon_url
                            if resolution == "hd"
                            else debrid_addon_url
                        )
                        process_account(
                            {**account, "aio_url": aio_url},
                            debrid_url,
                            fallback_addon_url,
                            debrid_is_down=False,
                            state=state,
                            timeout=request_timeout,
                        )
                    except Exception as e:
                        log.error(
                            f"Unexpected error processing account {account.get('name', '?')}: {e}"
                        )
                save_state(state)

        except Exception as e:
            log.error(f"Unexpected error in main loop: {e}")

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
