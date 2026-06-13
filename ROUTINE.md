# Flight Sweep — Claude Code cloud one-off

Runs `flight_sweep.py` on Anthropic-managed infrastructure (no laptop needed)
and drops the resulting CSV straight into Google Drive.

---

## One-time setup

1. Create a **private GitHub repo** containing these three files:
   - `flight_sweep.py`
   - `requirements.txt`
   - `ROUTINE.md` (this file)

2. Go to **claude.ai/code** (or the iOS app) → **New task**.
3. **Connect the repo** you just created.
4. Make sure the **Google Drive** connector is enabled for the session.
5. Paste the task prompt below and **Dispatch**. It runs in Anthropic's cloud
   sandbox and writes the CSV to Drive when finished.

> To make it repeatable later, save the same prompt as a **Routine**
> (`/schedule` in the CLI, or the routine creator on the web). For a true
> one-off, Dispatch is all you need.

---

## Task prompt (paste this into Claude Code)

> Run a one-off flight fare sweep and save the result to Google Drive.
>
> 1. Install dependencies: `pip install -r requirements.txt`
> 2. Run this exact command (edit the parameters first if needed):
>
>    ```
>    python flight_sweep.py \
>      --origin AMS --dest BOM \
>      --from 2026-07-01 --to 2026-08-31 \
>      --adults 2 --children 2 \
>      --max-duration 15 --max-connections 1 --max-layover 4 \
>      --cabin ECONOMY \
>      --out ams_bom_sweep.csv
>    ```
>
> 3. If the run produced rows, upload `ams_bom_sweep.csv` to my Google Drive,
>    into a folder named **"Flight searches"** (create it if it doesn't exist).
>    Prefix the filename with today's date (YYYY-MM-DD).
> 4. Reply with a short summary: how many itineraries matched, and the cheapest
>    one (fare, date, airline, total duration, stops).
> 5. If the sweep returns zero rows or hits an error such as a CAPTCHA /
>    rate-block, say so plainly and stop — do not retry more than twice.

---

## Editable parameters

| Flag | Meaning |
|------|---------|
| `--origin` / `--dest` | IATA codes (AMS, BOM, JFK, ...) |
| `--from` / `--to` | Departure-date range (YYYY-MM-DD) |
| `--adults` / `--children` | Passenger counts |
| `--max-duration` | Max total journey time, hours |
| `--max-connections` | 0 = nonstop, 1 = up to one stop, ... |
| `--max-layover` | Max hours for any single layover |
| `--cabin` | ECONOMY / PREMIUM_ECONOMY / BUSINESS / FIRST |

CSV columns: `departure_date, price, price_num, airlines, stops,
total_duration, route, depart, arrive, layovers, max_layover,
flight_numbers, google_flights_url`. Pre-sorted cheapest-first.
