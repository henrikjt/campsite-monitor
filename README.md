# Campsite Availability Monitor

Monitors specific campsites in the **Creekside Loop at Samuel P. Taylor State Park** via ReserveCalifornia and sends a push notification to your phone the moment one opens up. Runs free in the cloud on GitHub Actions every 15 minutes. You book manually after receiving the alert.

**Sites monitored:** 3, 4, 5, 6, 7, 14, 15, 16, 18, 19, 20  
**Date range:** today → July 1, 2026  
**Notification:** [ntfy.sh](https://ntfy.sh) push to your phone (free, no account needed)

---

## How It Works

1. Every 15 minutes GitHub Actions wakes up and runs `monitor.py`
2. The script calls the ReserveCalifornia availability API (no login, no scraping)
3. If any monitored site has a free night that wasn't free last run → sends a push notification
4. State is stored in a private GitHub Gist so duplicate alerts are avoided between runs
5. If availability disappears and later comes back, a new alert is sent

---

## Setup (Step-by-Step)

### Step 1 — Fork or copy this repo to your GitHub account

- Go to this repo on GitHub and click **Fork** (top right)
- Make it a **public repo** so GitHub Actions free minutes aren't capped

> All secrets (your ntfy topic, tokens) are stored in GitHub Encrypted Secrets — they are never visible in the public code.

---

### Step 2 — Install the ntfy app on your phone

ntfy.sh is a free push notification service. No account needed.

**Android:**
1. Install **ntfy** from [Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy) or [F-Droid](https://f-droid.org/packages/io.heckel.ntfy/)

**iPhone:**
1. Install **ntfy** from the [App Store](https://apps.apple.com/us/app/ntfy/id1625396347)

**Subscribe to your topic:**
1. Open the ntfy app
2. Tap **+** (Add subscription)
3. Enter your topic name (you'll generate this in the next step)
4. Tap **Subscribe**

That's it — you'll receive a push notification whenever the monitor finds availability.

---

### Step 3 — Generate a secret ntfy topic name

Your topic name is your only "password" for receiving notifications. Generate a random one:

```bash
python -c "import secrets; print(secrets.token_hex(16))"
```

This gives you something like: `a3f8c2d7e1b094563a2f8e1d7c4b3a09`

**Save this string** — you'll need it in Step 4 and Step 5.

---

### Step 4 — Create a GitHub Gist for state storage

The script stores which sites have already been alerted in a GitHub Gist so you don't get duplicate notifications between runs.

1. Go to [https://gist.github.com](https://gist.github.com)
2. Create a **secret** Gist (click the arrow next to "Create public gist" and choose "Create secret gist")
3. Put anything in the filename field (e.g., `state.json`) and content field (e.g., `{}`)
4. Click **Create secret gist**
5. **Copy the Gist ID** — it's the long string at the end of the URL, e.g.:
   ```
   https://gist.github.com/yourusername/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
                                        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ this part
   ```

---

### Step 5 — Create a GitHub Personal Access Token

The script needs a token to read and write your Gist.

1. Go to [https://github.com/settings/tokens](https://github.com/settings/tokens)
2. Click **Generate new token (classic)**
3. Give it a name like `campsite-monitor-gist`
4. Set **Expiration** to at least 90 days (or "No expiration" for convenience)
5. Check only the **`gist`** checkbox under Select scopes
6. Click **Generate token**
7. **Copy the token immediately** — GitHub will not show it again

---

### Step 6 — Add GitHub Secrets to your forked repo

1. Go to your forked repo on GitHub
2. Click **Settings** → **Secrets and variables** → **Actions**
3. Click **New repository secret** for each of the following:

| Secret Name | Value |
|---|---|
| `NTFY_TOPIC` | The random hex string from Step 3 |
| `GIST_ID` | The Gist ID from Step 4 |
| `GIST_TOKEN` | The personal access token from Step 5 |

---

### Step 7 — Subscribe to your ntfy topic

Back in the ntfy app on your phone:

1. Tap **+**
2. Type in your topic name (the random hex string from Step 3)
3. Tap **Subscribe**

You're now set up to receive alerts.

---

### Step 8 — Verify the setup with a manual trigger

1. Go to your forked repo on GitHub
2. Click the **Actions** tab
3. Click **Campsite Monitor** in the left sidebar
4. Click **Run workflow** → **Run workflow**
5. Wait ~30 seconds, then click the run to see logs

A successful run ends with:
```
Run complete.
```

If you see errors, check the log for which environment variable is missing.

---

### Step 9 — Send a test notification to your phone

You can trigger a test notification by adding `--test-notify` to the run command. To do this:

1. Temporarily edit `.github/workflows/monitor.yml`
2. Change `run: python monitor.py` to `run: python monitor.py --test-notify`
3. Commit and push, then trigger a manual run (Step 8)
4. Check your phone — you should get a test notification within a minute
5. **Revert the change** to `run: python monitor.py` and push again

---

### Step 10 — Confirm correct sites are matched (recommended)

Before relying on the monitor, verify that the right campsites are being detected:

1. Edit `.github/workflows/monitor.yml`: change `python monitor.py` to `python monitor.py --list-sites`
2. Commit, push, and run manually
3. In the logs you'll see all unit names returned by the API for facility 705
4. Confirm sites 3–7 and 14–20 appear as expected
5. **Revert** back to `python monitor.py` and push

> If site names in the API look different from expected (e.g., `"Creek 7"` instead of `"7"` or `"007"`), open `config.yaml` and note the pattern — the developer can add a `name_pattern` override.

---

## Running Locally for Testing

```bash
# Clone your fork
git clone https://github.com/YOUR_USERNAME/YOUR_REPO_NAME
cd campsite-monitor

# Install dependencies (Python 3.11+ required)
pip install -r requirements.txt

# Copy the example env file and fill in your values
cp .env.example .env
# Edit .env with your NTFY_TOPIC, GIST_ID, GIST_TOKEN

# Dry run — checks availability, prints results, NO notifications, NO state write
python monitor.py --dry-run

# Test notification — sends a fake alert to your phone
python monitor.py --test-notify

# List all campsites returned by the API (useful for verifying site name format)
python monitor.py --list-sites

# Normal run (same as what GitHub Actions runs)
python monitor.py
```

---

## Customizing the Monitor

Edit `config.yaml` to change:

| Setting | Location in config.yaml |
|---|---|
| Sites to monitor | `sites:` list |
| End date | `dates.end` |
| API chunk size | `dates.chunk_days` |
| ntfy priority | `notifications.ntfy.priority` |
| Log verbosity | `logging.level` (use `DEBUG` for more detail) |

---

## Monitoring Logs

- Go to your repo → **Actions** tab
- Click the most recent **Campsite Monitor** run
- Click the **check** job to expand logs
- Each run shows which chunks were fetched and whether new availability was found

GitHub keeps logs for **90 days**.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No notifications after availability found | NTFY_TOPIC secret missing or wrong | Re-check Step 6 |
| `State saved to local state.json` | GIST_ID not set | Add GIST_ID secret |
| HTTP 403 from API | Temporary IP block or rate limit | Wait; it will retry next run |
| 0 units returned | Facility ID wrong or API changed | Run `--list-sites` to debug |
| Workflow doesn't run on schedule | GitHub disables schedules on inactive repos | Go to Actions tab and re-enable |

> **GitHub Actions schedule note:** GitHub may pause scheduled workflows if the repo has no commits for 60 days. To keep it active, either push occasional commits or re-enable it manually from the Actions tab.

---

## Risks and Limitations

- **15-minute polling gap:** If a site opens and is booked within 15 minutes, the monitor may miss it. This is inherent to any polling-based approach.
- **API changes:** ReserveCalifornia may update their backend. If the script stops working, check the Issues tab.
- **Not a booking bot:** This script only sends alerts. You book manually on [reservecalifornia.com](https://reservecalifornia.com/park/705).
- **Personal use only:** Do not use this for commercial or high-volume scraping.

---

## Files

```
monitor.py              Main script
config.yaml             Configuration (sites, dates, notifications)
requirements.txt        Python dependencies
.env.example            Template for local environment variables
.gitignore              Excludes .env and local state from git
.github/workflows/
  monitor.yml           GitHub Actions scheduled workflow
```
