# FF USD Alerts: iPhone calendar feed

This is a calendar you subscribe to on your iPhone. It alerts you about these Forex Factory **USD** events:

| Event | Alerts |
|---|---|
| 🚫 Bank holidays | 8:00 PM Pacific the night before |
| 💼 NFP (Non-Farm Employment Change) | 8:00 PM Pacific the night before, plus 30 min before release |
| 📊 CPI (CPI m/m, Core CPI m/m, ...) | same |
| 🏭 PPI | same |
| 🏛 FOMC Meeting Minutes | same |

## How it works
- **Data source:** Forex Factory's official weekly export, `https://nfs.faireconomy.media/ff_calendar_thisweek.json`. It's a raw data file, so search engines don't list it, but it loads if you paste it into a browser.
- **Schedule:** every 6 hours, GitHub Actions runs `build_calendar.py` on GitHub's own servers. **Your PC does not need to be on.**
- **Memory:** events are saved to `data/events.json` because FF only shows the current week.
- **Holiday backstop:** Federal Reserve bank holidays are added a year ahead, so a Monday holiday is never missed.
- **Output:** `docs/ff-usd.ics`, published by GitHub Pages. Your iPhone checks it about every hour. The alerts are built into each event, so the phone fires them itself.

## One-time setup

### 1. Put this folder on GitHub (no git needed)
1. Create a free account at github.com and sign in.
2. Click **+ → New repository**. Name it `ff-alerts` and set it to **Public** (needed for free GitHub Pages). Click **Create repository**.
3. Click **uploading an existing file**. Drag in `build_calendar.py`, `requirements.txt`, `README.md`, and the `data` and `docs` folders. Click **Commit changes**.
4. Browsers often skip the hidden `.github` folder, so create the workflow by hand:
   1. Click **Add file → Create new file**.
   2. Type the name `.github/workflows/update.yml`.
   3. Paste in the contents of this folder's `.github/workflows/update.yml`.
   4. Click **Commit changes**.

### 2. Turn on the feed
1. In the repo, go to **Settings → Pages**. Under *Source*, choose **Deploy from a branch**. Pick branch `main` and folder `/docs`, then click **Save**.
2. Go to the **Actions** tab, pick **Update calendar feed**, and click **Run workflow**. Wait for the green check.
3. After a minute or two, your feed is live at:
   `https://<your-github-username>.github.io/ff-alerts/ff-usd.ics`
   Open it in a browser. It should download or show the calendar text.

### 3. Subscribe on your iPhone
1. Go to **Settings → Apps → Calendar → Calendar Accounts → Add Account → Other → Add Subscribed Calendar**.
2. Paste the feed URL and tap **Next**.
3. Make sure **Remove Alerts is OFF**. If it's on, you won't get any alerts.
4. Tap **Save**.
5. Optional: under **Settings → Apps → Calendar → Calendar Accounts → Fetch New Data**, set Fetch to **Hourly**.
6. Make sure notifications are allowed for the Calendar app.

**Google Calendar (optional, view only):** go to Other calendars → **From URL** and paste the same link. Google drops the alerts and updates slowly, so use the iPhone subscription for alerts.

## Customizing
Edit the top of `build_calendar.py`:
- `ALERT_HOUR_PT`: the hour for the night-before alert (20 = 8 PM).
- `PRE_RELEASE_MINUTES`: how long before a release the second alert fires.
- `NEWS_RULES`: which event titles to include.

To test locally on Windows:
```
pip install tzdata
python build_calendar.py
```
