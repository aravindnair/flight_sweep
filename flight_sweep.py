#!/usr/bin/env python3
"""
flight_sweep.py
===============
Sweep a departure-date range for one route using the `fli` library
(Google Flights data), filter every itinerary against your hard
constraints, and dump the survivors to a CSV you can open and sort by price.

------------------------------------------------------------------
SETUP (run once)
------------------------------------------------------------------
    pip install flights pandas
    # ('flights' is the PyPI name; the import name is `fli`)

------------------------------------------------------------------
USAGE
------------------------------------------------------------------
    python flight_sweep.py \
        --origin AMS --dest BOM \
        --from 2026-07-01 --to 2026-08-31 \
        --adults 2 --children 2 \
        --max-duration 15 \         # max total journey time, hours
        --max-connections 1 \       # 0 = nonstop, 1 = up to one stop, etc.
        --max-layover 4 \           # max hours for ANY single layover
        --cabin ECONOMY \
        --out amsterdam_mumbai.csv

Then: open the CSV in Excel / Numbers / Google Sheets and sort by `price_num`.
(The file is also pre-sorted cheapest-first.)

------------------------------------------------------------------
NOTES
------------------------------------------------------------------
* One-way sweep. For a round trip, run it twice (AMS->BOM, then BOM->AMS)
  and pair up the dates yourself, or ask Claude to extend it.
* `google_flights_url` is a SEARCH link (lands on the results page for that
  route+date) — not a one-click booking link. No Google Flights library can
  produce a durable book-this-exact-fare URL; the tokens are opaque.
* A polite delay sits between queries so you're not hammering Google. Leave
  it in. Raise it if you start seeing failures.
"""

import argparse
import csv
import sys
import time
import random
from datetime import datetime, date, timedelta
from urllib.parse import quote_plus

from fli.models import (
    Airport,
    PassengerInfo,
    SeatType,
    MaxStops,
    SortBy,
    FlightSearchFilters,
    FlightSegment,
)
from fli.search import SearchFlights


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def daterange(start: date, end: date):
    """Yield every date from start to end inclusive."""
    days = (end - start).days
    for n in range(days + 1):
        yield start + timedelta(days=n)


def to_dt(value):
    """Coerce fli leg datetimes (datetime | ISO string | tuple) -> datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, (list, tuple)):
        return datetime(*value[:6])
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    continue
    raise ValueError(f"Cannot parse datetime from: {value!r}")


def fmt_hm(minutes: float) -> str:
    """720 -> '12h 00m'."""
    minutes = int(round(minutes))
    return f"{minutes // 60}h {minutes % 60:02d}m"


def stops_filter(max_connections: int) -> MaxStops:
    if max_connections <= 0:
        return MaxStops.NON_STOP
    if max_connections == 1:
        return MaxStops.ONE_STOP
    return MaxStops.ANY  # we post-filter to the exact integer anyway


def build_passengers(adults, children, infants_seat, infants_lap) -> PassengerInfo:
    """PassengerInfo's optional fields vary by version; degrade gracefully."""
    try:
        return PassengerInfo(
            adults=adults,
            children=children,
            infants_in_seat=infants_seat,
            infants_on_lap=infants_lap,
        )
    except TypeError:
        if children or infants_seat or infants_lap:
            print(
                "  ! This fli version only supports adults; "
                "ignoring children/infants.",
                file=sys.stderr,
            )
        return PassengerInfo(adults=adults)


def airport(code: str) -> "Airport":
    code = code.strip().upper()
    try:
        return Airport[code]
    except KeyError:
        sys.exit(f"Unknown IATA airport code: {code!r}. Use codes like AMS, BOM, JFK.")


def layovers_from_legs(legs):
    """Return list of (airport_code, layover_minutes) between consecutive legs."""
    out = []
    for i in range(len(legs) - 1):
        arr = to_dt(legs[i].arrival_datetime)
        dep = to_dt(legs[i + 1].departure_datetime)
        mins = (dep - arr).total_seconds() / 60.0
        code = getattr(legs[i].arrival_airport, "value", str(legs[i].arrival_airport))
        out.append((code, mins))
    return out


def gflights_url(origin: str, dest: str, d: date) -> str:
    q = f"Flights from {origin} to {dest} on {d.isoformat()}"
    return f"https://www.google.com/travel/flights?q={quote_plus(q)}"


# ----------------------------------------------------------------------
# Core sweep
# ----------------------------------------------------------------------
def sweep(args):
    origin_ap = airport(args.origin)
    dest_ap = airport(args.dest)
    passengers = build_passengers(
        args.adults, args.children, args.infants_seat, args.infants_lap
    )
    seat = SeatType[args.cabin.upper()]
    api_stops = stops_filter(args.max_connections)
    start = datetime.strptime(args.date_from, "%Y-%m-%d").date()
    end = datetime.strptime(args.date_to, "%Y-%m-%d").date()
    max_dur_min = args.max_duration * 60
    max_lay_min = args.max_layover * 60

    search = SearchFlights()
    rows = []
    all_dates = list(daterange(start, end))
    print(
        f"Sweeping {args.origin}->{args.dest}, {len(all_dates)} dates "
        f"({start} to {end})...",
        file=sys.stderr,
    )

    for idx, d in enumerate(all_dates, 1):
        filters = FlightSearchFilters(
            passenger_info=passengers,
            flight_segments=[
                FlightSegment(
                    departure_airport=[[origin_ap, 0]],
                    arrival_airport=[[dest_ap, 0]],
                    travel_date=d.isoformat(),
                )
            ],
            seat_type=seat,
            stops=api_stops,
            sort_by=SortBy.CHEAPEST,
        )

        try:
            flights = search.search(filters) or []
        except Exception as e:  # one bad date shouldn't kill the whole sweep
            print(f"  [{idx}/{len(all_dates)}] {d}  ERROR: {e}", file=sys.stderr)
            time.sleep(args.delay + random.uniform(0, 1))
            continue

        kept = 0
        for f in flights:
            stops = int(getattr(f, "stops", len(f.legs) - 1))
            duration_min = float(getattr(f, "duration", 0) or 0)

            # --- hard filters ---
            if stops > args.max_connections:
                continue
            if max_dur_min and duration_min and duration_min > max_dur_min:
                continue

            lays = layovers_from_legs(f.legs)
            if max_lay_min and any(m > max_lay_min for _, m in lays):
                continue

            carriers = sorted(
                {getattr(l.airline, "value", str(l.airline)) for l in f.legs}
            )
            route = "->".join(
                [getattr(f.legs[0].departure_airport, "value", "")]
                + [getattr(l.arrival_airport, "value", "") for l in f.legs]
            )
            dep_dt = to_dt(f.legs[0].departure_datetime)
            arr_dt = to_dt(f.legs[-1].arrival_datetime)
            lay_str = "; ".join(f"{code} {fmt_hm(m)}" for code, m in lays) or "—"

            rows.append(
                {
                    "departure_date": d.isoformat(),
                    "price_num": round(float(f.price), 2),
                    "price": f"{float(f.price):,.0f}",
                    "airlines": ", ".join(carriers),
                    "stops": stops,
                    "total_duration": fmt_hm(duration_min),
                    "duration_min": int(round(duration_min)),
                    "route": route,
                    "depart": dep_dt.strftime("%Y-%m-%d %H:%M"),
                    "arrive": arr_dt.strftime("%Y-%m-%d %H:%M"),
                    "layovers": lay_str,
                    "max_layover": fmt_hm(max(m for _, m in lays)) if lays else "—",
                    "flight_numbers": " / ".join(
                        f"{getattr(l.airline,'value',l.airline)}{l.flight_number}"
                        for l in f.legs
                    ),
                    "google_flights_url": gflights_url(args.origin, args.dest, d),
                }
            )
            kept += 1

        print(
            f"  [{idx}/{len(all_dates)}] {d}  {len(flights)} found, {kept} kept",
            file=sys.stderr,
        )
        time.sleep(args.delay + random.uniform(0, 1))

    return rows


def write_csv(rows, path):
    if not rows:
        print("No itineraries matched your filters. Loosen them and retry.",
              file=sys.stderr)
        return
    rows.sort(key=lambda r: r["price_num"])  # cheapest first
    fields = [
        "departure_date", "price", "price_num", "airlines", "stops",
        "total_duration", "duration_min", "route", "depart", "arrive",
        "layovers", "max_layover", "flight_numbers", "google_flights_url",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    cheapest = rows[0]
    print(
        f"\nDone. {len(rows)} itineraries written to {path}\n"
        f"Cheapest: {cheapest['price']} on {cheapest['departure_date']} "
        f"via {cheapest['airlines']} ({cheapest['total_duration']}, "
        f"{cheapest['stops']} stop[s])",
        file=sys.stderr,
    )


def parse_args():
    p = argparse.ArgumentParser(description="Sweep Google Flights for the best fares.")
    p.add_argument("--origin", required=True, help="Origin IATA code, e.g. AMS")
    p.add_argument("--dest", required=True, help="Destination IATA code, e.g. BOM")
    p.add_argument("--from", dest="date_from", required=True, help="Start date YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", required=True, help="End date YYYY-MM-DD")
    p.add_argument("--adults", type=int, default=1)
    p.add_argument("--children", type=int, default=0)
    p.add_argument("--infants-seat", type=int, default=0)
    p.add_argument("--infants-lap", type=int, default=0)
    p.add_argument("--max-duration", type=float, default=99,
                   help="Max total journey time in hours")
    p.add_argument("--max-connections", type=int, default=2,
                   help="0 = nonstop, 1 = up to one stop, etc.")
    p.add_argument("--max-layover", type=float, default=24,
                   help="Max hours for ANY single layover")
    p.add_argument("--cabin", default="ECONOMY",
                   help="ECONOMY | PREMIUM_ECONOMY | BUSINESS | FIRST")
    p.add_argument("--delay", type=float, default=2.0,
                   help="Base seconds between date queries (be polite)")
    p.add_argument("--out", default="flight_sweep.csv")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    rows = sweep(args)
    write_csv(rows, args.out)
