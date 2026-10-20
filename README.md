# osu! Playtime Curve

**Live:** https://lminutello.github.io/osu_pp_curve/

How does your osu! pp compare with everyone else who's played as long as you have? Enter your play time and pp (and optionally your global rank) to get:

- **Better than** — the share of players with about your play time who have less pp than you, with an osu!-style rank letter
- **Std devs, typical pp, faster than** — how far above or below the trend you are, and how many players at your pp took longer to get there
- **Global rank and star level** — estimated from your pp, calibrated on real players
- **Project my future** — where you're likely headed if you keep playing, with a realistic range and your next pp milestone
- **Explore the trend** — the median player at any play time, and how fast real players actually improve there

Not affiliated with osu! or ppy Pty Ltd.

## How it works

| Piece | Source | Method |
|---|---|---|
| The curve, bands, percentiles | Newest 3 monthly `random_10000` dumps from [data.ppy.sh](https://data.ppy.sh) (~30,000 players) | ln(pp) fitted as a quadratic in ln(hours); spread measured locally (IQR/1.349 of residuals in 14 bins); percentiles count real players near your hours, weighted by distance |
| Global rank | The same sample's pp distribution | Share of players above your pp × leaderboard size, calibrated on real ranks (the dumps' rank column, or players pulled from the API) with a correction at high pp |
| Star level | ~2,000 players' top plays from the osu! API | Median star rating of each player's top 10 plays without rate/difficulty mods, fitted as a curve in log space |
| Projection | ~2,000 players' histories from the osu! API | Past pp rebuilt from dated top-100 scores, past hours from plays per month. Players' distance from the trend fades as they play; the speed is fitted under several rebuild choices and the site shows the middle with the full range |
| Real pp per hour | Same histories | Median pp gained per hour of play by players near the median, at each stage |

Known limits: if osu! ships a big pp rework, the newest 3 dumps can briefly mix old and new pp values; run *Refresh model* with fewer dumps (`--count 1`) until three post-rework dumps exist. Also, rebuilding past pp from today's top 100 can't see scores a player later beat, so the drift speed and real pp/hour are less certain than everything else. Profiles' 90-day rank history is now saved on new pulls and is the planned fix.

## Repo layout

```
index.html                         the whole site (loads data/model.json)
data/model.json                    everything the page needs, rebuilt monthly
data/history_fit.json              results from the player histories
data/history.jsonl.gz              raw pulled histories (public profile data)
scripts/build_model.py             dumps -> model.json
scripts/fit_histories.py           histories -> history_fit.json
scripts/pull_leaderboard.py        top 10,000 of the real leaderboard -> leaderboard.json (weekly + monthly)
scripts/ranks.py                   ~1,000 real players' current ranks -> rank lookup in model.json (weekly)
scripts/audit_pull.py              independent check data -> the `audit` branch (manual)
scripts/pull_histories.py          osu! API -> history.jsonl.gz
.github/workflows/refresh-model.yml       monthly, automatic
.github/workflows/refresh-histories.yml   manual, needs API secrets
.github/workflows/refresh-ranks.yml       weekly, needs API secrets
.github/workflows/update-me.yml           daily, needs API secrets
.github/workflows/audit.yml               manual accuracy audit, needs API secrets
```

## Setup (one time)

1. **Turn on Pages:** Settings → Pages → Build and deployment → Source: *Deploy from a branch* → Branch: `main`, folder `/ (root)` → Save.
2. **Let workflows commit:** Settings → Actions → General → Workflow permissions → *Read and write permissions* → Save.
3. **(Optional) API secrets for pulling more histories:** create an OAuth app at osu! Settings → OAuth → New OAuth Application (callback URL can be blank), then in this repo go to Settings → Secrets and variables → Actions → New repository secret, and add `OSU_CLIENT_ID` and `OSU_CLIENT_SECRET`.

After that:

- **Monthly:** *Refresh model* runs on the 4th of each month, rebuilds from the newest dumps, and commits `data/model.json`. Pages redeploys on its own. Run it any time from the Actions tab → *Refresh model (monthly)* → *Run workflow*.
- **When you want:** Actions tab → *Pull more player histories (manual)* → *Run workflow*. About 2 seconds per player, so the default 700 players takes ~25 minutes.

## Run it locally

```bash
pip install numpy
python scripts/build_model.py          # downloads the newest 3 dumps into .cache/ (a few hundred MB each)
python -m http.server 8000             # then open http://localhost:8000
```

`python scripts/pull_histories.py --users .cache/users.csv` asks for your API credentials if the environment variables aren't set. Run `build_model.py --write-users .cache/users.csv` first to make that file.

## Credits

Player data: [data.ppy.sh](https://data.ppy.sh) dumps and the [osu! API v2](https://osu.ppy.sh/docs/), used within its rate guideline (≤ 1 request per second). Made by Lukas Minutello.
