"""
Campsite availability monitor for Samuel P. Taylor SP – Creekside Loop.

Usage:
  python monitor.py              # Normal run: check, alert, save state
  python monitor.py --dry-run    # Check only; no alerts, no state write
  python monitor.py --test-notify  # Send a test notification and exit
  python monitor.py --list-sites   # Print all unit names returned by the API and exit
"""

import argparse
import json
import logging
import os
import re
import smtplib
import sys
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText

import requests
import yaml
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.yaml")
LOCAL_STATE_PATH = os.path.join(SCRIPT_DIR, "state.json")

GIST_API = "https://api.github.com/gists"
GIST_FILENAME = "campsite-monitor-state.json"


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(level_name: str):
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


log = logging.getLogger(__name__)


# ── Date helpers ──────────────────────────────────────────────────────────────

def resolve_date(value: str) -> date:
    if value == "today":
        return date.today()
    return datetime.strptime(value, "%Y-%m-%d").date()


def date_chunks(start: date, end: date, chunk_days: int):
    """Yield (chunk_start, chunk_end) pairs covering start..end inclusive."""
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=chunk_days), end)
        yield cursor, chunk_end
        cursor = chunk_end


# ── API ───────────────────────────────────────────────────────────────────────

def fetch_availability(facility_id: str, start: date, end: date, timeout: int) -> dict:
    payload = {
        "FacilityId": int(facility_id),
        "StartDate": start.strftime("%m-%d-%Y"),
        "EndDate": end.strftime("%m-%d-%Y"),
        "IsADA": None,
        "MinVehicleLength": None,
        "UnitCategoryId": None,
        "WebOnly": False,
        "UnitTypesGroupIds": [],
        "SleepingUnitId": None,
        "UnitSort": "orderby",
        "InSeasonOnly": True,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (personal campsite monitor; not for commercial use)",
        "Origin": "https://reservecalifornia.com",
        "Referer": "https://reservecalifornia.com/",
    }
    cfg = load_config()
    url = cfg["campground"]["base_url"]

    response = requests.post(url, json=payload, headers=headers, timeout=timeout)

    if response.status_code == 403:
        log.warning("HTTP 403 from API — possibly rate-limited or IP-blocked. Skipping this run.")
        sys.exit(0)
    if response.status_code == 429:
        log.warning("HTTP 429 (Too Many Requests) from API. Skipping this run.")
        sys.exit(0)
    response.raise_for_status()

    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected API response type: {type(data)}")
    # Units live inside Facility.Units in the Tyler Tech API
    facility = data.get("Facility") or {}
    units = facility.get("Units") or {}
    return {"Units": units, "_raw": data}


# ── Site matching ─────────────────────────────────────────────────────────────

def extract_site_number(name: str, target_numbers: list[int]) -> int | None:
    """
    Return the matched site number from a unit name, or None.
    Names are like "Campsite #3", "Tent Campsite #14" — match only the number after #.
    """
    for n in target_numbers:
        if re.search(rf"#0*{n}$", name, re.IGNORECASE):
            return n
    return None


# ── Availability scan ─────────────────────────────────────────────────────────

def scan_chunks(cfg: dict) -> dict[tuple[int, str], bool]:
    """
    Returns a dict of {(site_number, "YYYY-MM-DD"): is_locked} for all available nights.
    is_locked=True means recently cancelled (Lock field set); still worth alerting.
    """
    facility_id = cfg["campground"]["facility_id"]
    target_sites: list[int] = cfg["sites"]
    start_date = resolve_date(cfg["dates"]["start"])
    end_date = resolve_date(cfg["dates"]["end"])
    chunk_days = cfg["dates"]["chunk_days"]
    timeout = cfg["polling"]["request_timeout"]

    chunks = list(date_chunks(start_date, end_date, chunk_days))
    log.info(
        "Starting check — facility %s, sites %s, %d date chunk(s) covering %s → %s",
        facility_id,
        target_sites,
        len(chunks),
        start_date,
        end_date,
    )

    available: dict[tuple[int, str], bool] = {}

    for idx, (chunk_start, chunk_end) in enumerate(chunks, 1):
        log.info("Chunk %d/%d: %s → %s", idx, len(chunks), chunk_start, chunk_end)
        try:
            data = fetch_availability(facility_id, chunk_start, chunk_end, timeout)
        except requests.RequestException as exc:
            log.warning("Network error on chunk %d: %s — skipping chunk.", idx, exc)
            continue
        except ValueError as exc:
            log.warning("Unexpected API response on chunk %d: %s — skipping chunk.", idx, exc)
            continue

        units = data.get("Units") or {}
        if not units:
            log.info("  No units returned in chunk %d.", idx)
            continue

        matched_in_chunk = 0
        for unit_id, unit in units.items():
            name = unit.get("Name", "")
            site_num = extract_site_number(name, target_sites)
            if site_num is None:
                continue

            slices = unit.get("Slices") or {}
            for dt_key, slot in slices.items():
                if not slot.get("IsFree"):
                    continue
                # Parse the date from the key (e.g. "2026-06-01T00:00:00")
                try:
                    night = datetime.fromisoformat(dt_key).date()
                except ValueError:
                    continue
                night_str = night.isoformat()
                is_locked = bool(slot.get("Lock"))
                available[(site_num, night_str)] = is_locked
                matched_in_chunk += 1

        log.info("  Found %d available site-night(s) in chunk %d.", matched_in_chunk, idx)

    return available


# ── State (Gist or local file) ────────────────────────────────────────────────

def _gist_headers() -> dict:
    token = os.environ.get("GIST_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError("GIST_TOKEN (or GITHUB_TOKEN) environment variable not set.")
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


def load_state() -> dict[str, list[str]]:
    """Returns {site_key: [date_str, ...]} of previously alerted site-nights."""
    gist_id = os.environ.get("GIST_ID")
    if gist_id:
        try:
            resp = requests.get(f"{GIST_API}/{gist_id}", headers=_gist_headers(), timeout=10)
            resp.raise_for_status()
            files = resp.json().get("files", {})
            if GIST_FILENAME in files:
                content = files[GIST_FILENAME].get("content", "{}")
                return json.loads(content)
            log.info("State file not yet in Gist — starting fresh.")
            return {}
        except Exception as exc:
            log.warning("Could not read state from Gist (%s) — starting fresh this run.", exc)
            return {}
    # Fall back to local file for testing without Gist
    if os.path.exists(LOCAL_STATE_PATH):
        with open(LOCAL_STATE_PATH) as f:
            return json.load(f)
    return {}


def save_state(state: dict[str, list[str]]):
    gist_id = os.environ.get("GIST_ID")
    if gist_id:
        try:
            payload = {"files": {GIST_FILENAME: {"content": json.dumps(state, indent=2)}}}
            resp = requests.patch(
                f"{GIST_API}/{gist_id}", json=payload, headers=_gist_headers(), timeout=10
            )
            resp.raise_for_status()
            log.info("State saved to Gist.")
        except Exception as exc:
            log.warning("Could not save state to Gist: %s", exc)
    else:
        with open(LOCAL_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
        log.info("State saved to local %s.", LOCAL_STATE_PATH)


def state_key(site_num: int) -> str:
    return f"site_{site_num}"


# ── Notifications ─────────────────────────────────────────────────────────────

def send_ntfy(cfg: dict, site_num: int, night_str: str, is_locked: bool):
    ntfy_cfg = cfg["notifications"]["ntfy"]
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        log.warning("NTFY_TOPIC not set — skipping ntfy notification.")
        return

    booking_url = cfg["campground"]["booking_url"]
    lock_note = " (recently cancelled — grab it fast!)" if is_locked else ""
    message = (
        f"Site {site_num} is available for {night_str}{lock_note}\n"
        f"Book now: {booking_url}"
    )

    try:
        resp = requests.post(
            f"{ntfy_cfg['server']}/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": f"Campsite Alert: {cfg['campground']['name']}",
                "Priority": ntfy_cfg.get("priority", "high"),
                "Tags": ntfy_cfg.get("tags", "tent"),
            },
            timeout=10,
        )
        resp.raise_for_status()
        log.info("  ntfy alert sent: site %d on %s.", site_num, night_str)
    except Exception as exc:
        log.warning("  ntfy notification failed: %s", exc)


def send_email(cfg: dict, site_num: int, night_str: str):
    email_cfg = cfg["notifications"]["email"]
    if not email_cfg.get("enabled"):
        return

    smtp_user = os.environ.get("EMAIL_SMTP_USER", "")
    smtp_pass = os.environ.get("EMAIL_SMTP_PASSWORD", "")
    to_addr = os.environ.get("EMAIL_TO", email_cfg.get("to_addr", ""))

    if not all([smtp_user, smtp_pass, to_addr]):
        log.warning("Email enabled but EMAIL_SMTP_USER / EMAIL_SMTP_PASSWORD / EMAIL_TO not set.")
        return

    booking_url = cfg["campground"]["booking_url"]
    body = (
        f"Good news — Site {site_num} at {cfg['campground']['name']} "
        f"is available for {night_str}.\n\nBook now: {booking_url}"
    )
    msg = MIMEText(body)
    msg["Subject"] = f"Campsite Alert: Site {site_num} on {night_str}"
    msg["From"] = smtp_user
    msg["To"] = to_addr

    try:
        with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [to_addr], msg.as_string())
        log.info("  Email alert sent: site %d on %s.", site_num, night_str)
    except Exception as exc:
        log.warning("  Email notification failed: %s", exc)


def send_alerts(cfg: dict, site_num: int, night_str: str, is_locked: bool, dry_run: bool):
    if dry_run:
        log.info("  [DRY RUN] Would alert: site %d on %s (locked=%s).", site_num, night_str, is_locked)
        return
    if cfg["notifications"]["ntfy"]["enabled"]:
        send_ntfy(cfg, site_num, night_str, is_locked)
    send_email(cfg, site_num, night_str)


# ── List sites helper ─────────────────────────────────────────────────────────

def list_sites(cfg: dict):
    """Print all unit names returned by the API for a short near-future window."""
    start = date.today()
    end = start + timedelta(days=7)
    timeout = cfg["polling"]["request_timeout"]
    facility_id = cfg["campground"]["facility_id"]
    log.info("Fetching unit list from API for %s → %s …", start, end)
    try:
        data = fetch_availability(facility_id, start, end, timeout)
    except requests.RequestException as exc:
        log.error("Network error: %s", exc)
        print("\nCould not reach the API. Check your internet connection and try again.")
        sys.exit(1)
    units = data.get("Units") or {}
    if not units:
        print("No units returned — check facility_id in config.yaml.")
        return
    print(f"\n{'Unit ID':<12} {'Name'}")
    print("-" * 40)
    for uid, unit in sorted(units.items(), key=lambda x: x[1].get("Name", "")):
        print(f"{uid:<12} {unit.get('Name', '(unnamed)')}")
    print(f"\nTotal units: {len(units)}")
    print("\nCheck the names above against your target site numbers.")
    print("If the format looks different from expected, adjust config.yaml or")
    print("open a GitHub issue with the output.")


# ── Test notification ─────────────────────────────────────────────────────────

def test_notify(cfg: dict):
    log.info("Sending test notification …")
    send_ntfy(cfg, site_num=7, night_str="2026-06-15", is_locked=False)
    send_email(cfg, site_num=7, night_str="2026-06-15")
    log.info("Test notification sent. Check your phone and email.")


# ── Main ──────────────────────────────────────────────────────────────────────

def check_env():
    missing = []
    if not os.environ.get("NTFY_TOPIC"):
        missing.append("NTFY_TOPIC")
    if not os.environ.get("GIST_ID"):
        log.info(
            "GIST_ID not set — state will be stored in local %s (fine for local testing).",
            LOCAL_STATE_PATH,
        )
    if missing:
        log.warning(
            "Missing environment variables: %s\n"
            "Notifications may not work. Copy .env.example to .env and fill in values.",
            ", ".join(missing),
        )


def main():
    parser = argparse.ArgumentParser(description="Campsite availability monitor")
    parser.add_argument("--dry-run", action="store_true", help="Check only; no alerts or state write")
    parser.add_argument("--test-notify", action="store_true", help="Send a test notification and exit")
    parser.add_argument("--list-sites", action="store_true", help="Print all API unit names and exit")
    args = parser.parse_args()

    cfg = load_config()
    setup_logging(cfg["logging"]["level"])

    if args.test_notify:
        test_notify(cfg)
        return

    if args.list_sites:
        list_sites(cfg)
        return

    if not args.dry_run:
        check_env()

    # Load previous state: {site_key: [date_str, ...]}
    prev_state: dict[str, list[str]] = load_state()

    # Fetch current availability
    available = scan_chunks(cfg)

    if not available:
        log.info("No availability found this run.")
    else:
        log.info("Total available site-nights found: %d", len(available))

    # Rebuild state as sets for easy lookup
    prev_seen: dict[str, set[str]] = {k: set(v) for k, v in prev_state.items()}
    curr_seen: dict[str, set[str]] = {}

    alert_count = 0
    for (site_num, night_str), is_locked in sorted(available.items()):
        key = state_key(site_num)
        if key not in curr_seen:
            curr_seen[key] = set()
        curr_seen[key].add(night_str)

        # Alert only if this (site, date) is new
        if night_str not in prev_seen.get(key, set()):
            log.info("NEW AVAILABILITY: Site %d on %s (locked=%s)", site_num, night_str, is_locked)
            send_alerts(cfg, site_num, night_str, is_locked, dry_run=args.dry_run)
            alert_count += 1

    if alert_count == 0:
        log.info("No new availability since last run.")

    # Save updated state (only current availability — removed pairs will re-alert if they return)
    new_state = {k: sorted(v) for k, v in curr_seen.items()}

    if args.dry_run:
        log.info("[DRY RUN] State not written.")
    else:
        save_state(new_state)

    log.info("Run complete.")


if __name__ == "__main__":
    main()
