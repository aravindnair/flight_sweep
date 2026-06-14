#!/usr/bin/env python3
r"""
flight_sweep.py  (round-trip)
=============================
Sweep a date range for the cheapest ROUND-TRIP fare on one route using the
`fli` library (Google Flights data). Round-trip is searched directly as a
single product -- NOT as two summed one-ways -- because on full-service
carriers a round-trip is usually far cheaper than two one-way tickets.

Filters every itinerary against your hard constraints (max journey time per
direction, max stops, max layover) and writes the survivors to a CSV,
cheapest-first. Optionally attaches a real booking link per top result.

------------------------------------------------------------------
SETUP
------------------------------------------------------------------
    pip install flights          # PyPI name is 'flights'; import name is `fli`

------------------------------------------------------------------
USAGE  (round-trip, anchored stay length -- the family-trip default)
------------------------------------------------------------------
    python flight_sweep.py \
        --origin AMS --dest BOM \
        --from 2026-07-01 --to 2026-07-31 \   # OUTBOUND date range to sweep
        --stay 21 --stay-flex 3 \             # ~21-night trip, try 18..24 nights
        --adults 2 --children 2 \
        --max-duration 15 \                   # max journey time PER DIRECTION, hours
        --max-connections 1 \                 # 0=nonstop, 1=up to one stop, ...
        --max-layover 4 \                     # max hours for any single layover
        --cabin ECONOMY \
        --airlines KL AF LX LH BA \           # optional carrier allow-list
        --booking-links \                     # optional: real book URL for top rows
        --out ams_bom_rt.csv

USAGE  (full outbound x return grid -- exhaustive, more queries)
        ... --from 2026-07-01 --to 2026-07-07 \
            --return-from 2026-07-22 --return-to 2026-07-28

USAGE  (one-way, if you ever need it)
        ... --oneway --from 2026-07-01 --to 2026-07-31   # ignores stay/return

Open the CSV and sort by `price_num` (it's already pre-sorted cheapest-first).

------------------------------------------------------------------
NOTES
------------------------------------------------------------------
* `--booking-links` makes one extra request for the cheapest option of each
  search, returning the real vendor + bookable URL. Left off, every row still
  carries a Google Flights SEARCH link as a fallback.
* A polite, jittered delay sits between queries. Leave it in.
"""

import argparse
import csv
import sys
import time
import random
from datetime import datetime, timedelta

from fli.models import (
    Airport, Airline, PassengerInfo, SeatType, MaxStops, SortBy,
    TripType, FlightSearchFilters, FlightSegment,
)
from fli.models.google_flights.base import LayoverRestrictions
from fli.search import SearchFlights


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def daterange(start, end):
    for n in range((end - start).days + 1):
        yield start + timedelta(days=n)


def fmt_hm(minutes):
    m = int(round(minutes or 0))
    return f"{m // 60}h {m % 60:02d}m"


def stops_filter(max_conn):
    return {
        0: MaxStops.NON_STOP,
        1: MaxStops.ONE_STOP_OR_FEWER,
        2: MaxStops.TWO_OR_FEWER_STOPS,
    }.get(max_conn, MaxStops.ANY)


def airport(code):
    try:
        return Airport[code.strip().upper()]
    except KeyError:
        sys.exit(f"Unknown airport IATA code: {code!r}")


def airline_list(codes):
    if not codes:
        return None
    out = []
    for c in codes:
        key = c.strip().upper()
        a = getattr(Airline, key, None)
        if a is None and key[0].isdigit():
            a = getattr(Airline, f"_{key}", None)   # 6E -> _6E
        if a is None:
            print(f"  ! Unknown airline code {c!r} -- skipping.", file=sys.stderr)
        else:
            out.append(a)
    return out or None


def build_passengers(adults, children, infants_seat, infants_lap):
    try:
        return PassengerInfo(adults=adults, children=children,
                             infants_in_seat=infants_seat, infants_on_lap=infants_lap)
    except TypeError:
        return PassengerInfo(adults=adults)


def direction_info(result):
    """Pull a tidy dict out of one FlightResult (one direction)."""
    legs = result.legs
    carriers = sorted({l.airline.value for l in legs})
    route = "->".join([legs[0].departure_airport.value]
                      + [l.arrival_airport.value for l in legs])
    lays = result.layovers or []
    lay_str = "; ".join(f"{lv.airport.value} {fmt_hm(lv.duration)}"
                        + ("*" if lv.overnight else "") for lv in lays) or "—"
    max_lay = max((lv.duration for lv in lays), default=0)
    return {
        "airlines": ", ".join(carriers),
        "stops": int(result.stops),
        "duration_min": int(result.duration),
        "duration": fmt_hm(result.duration),
        "route": route,
        "depart": legs[0].departure_datetime.strftime("%Y-%m-%d %H:%M"),
        "arrive": legs[-1].arrival_datetime.strftime("%Y-%m-%d %H:%M"),
        "layovers": lay_str,
        "max_layover_min": max_lay,
    }


def passes(info, max_conn, max_dur_min, max_lay_min):
    if info["stops"] > max_conn:
        return False
    if max_dur_min and info["duration_min"] > max_dur_min:
        return False
    if max_lay_min and info["max_layover_min"] > max_lay_min:
        return False
    return True


def total_price(item):
    results = item if isinstance(item, tuple) else (item,)
    prices = [r.price for r in results if r.price]
    return max(prices) if prices else None


def gflights_url(origin, dest, out_date, ret_date=None):
    if ret_date:
        q = f"Round trip flights from {origin} to {dest} {out_date} to {ret_date}"
    else:
        q = f"Flights from {origin} to {dest} on {out_date}"
    from urllib.parse import quote_plus
    return f"https://www.google.com/travel/flights?q={quote_plus(q)}"


# ----------------------------------------------------------------------
# Build the list of (outbound, return) date pairs to sweep
# ----------------------------------------------------------------------
def date_pairs(args):
    start = datetime.strptime(args.date_from, "%Y-%m-%d").date()
    end = datetime.strptime(args.date_to, "%Y-%m-%d").date()
    if args.oneway:
        return [(d, None) for d in daterange(start, end)]
    pairs = []
    if args.return_from:                         # full grid mode
        rstart = datetime.strptime(args.return_from, "%Y-%m-%d").date()
        rend = datetime.strptime(args.return_to, "%Y-%m-%d").date()
        for o in daterange(start, end):
            for r in daterange(rstart, rend):
                if r > o:
                    pairs.append((o, r))
    else:                                        # anchored stay-length mode
        for o in daterange(start, end):
            for s in range(args.stay - args.stay_flex, args.stay + args.stay_flex + 1):
                if s > 0:
                    pairs.append((o, o + timedelta(days=s)))
    return pairs


# ----------------------------------------------------------------------
# Core sweep
# ----------------------------------------------------------------------
def sweep(args):
    o_ap, d_ap = airport(args.origin), airport(args.dest)
    pax = build_passengers(args.adults, args.children, args.infants_seat, args.infants_lap)
    seat = SeatType[args.cabin.upper()]
    api_stops = stops_filter(args.max_connections)
    allow = airline_list(args.airlines)
    max_dur_min = int(args.max_duration * 60)
    max_lay_min = int(args.max_layover * 60)
    trip = TripType.ONE_WAY if args.oneway else TripType.ROUND_TRIP

    search = SearchFlights()
    pairs = date_pairs(args)
    print(f"Sweeping {args.origin}<->{args.dest}: {len(pairs)} "
          f"{'one-way dates' if args.oneway else 'date pairs'}...", file=sys.stderr)

    rows = []
    for i, (out_d, ret_d) in enumerate(pairs, 1):
        segs = [FlightSegment(departure_airport=[[o_ap, 0]],
                              arrival_airport=[[d_ap, 0]],
                              travel_date=out_d.isoformat())]
        if ret_d:
            segs.append(FlightSegment(departure_airport=[[d_ap, 0]],
                                      arrival_airport=[[o_ap, 0]],
                                      travel_date=ret_d.isoformat()))
        filters = FlightSearchFilters(
            trip_type=trip, passenger_info=pax, flight_segments=segs,
            seat_type=seat, stops=api_stops, max_duration=max_dur_min or None,
            layover_restrictions=LayoverRestrictions(max_duration=max_lay_min) if max_lay_min else None,
            airlines=allow, exclude_basic_economy=not args.include_basic,
            sort_by=SortBy.CHEAPEST,
        )
        try:
            results = search.search(filters, top_n=args.top, currency=args.currency) or []
        except Exception as e:
            print(f"  [{i}/{len(pairs)}] {out_d}->{ret_d}  ERROR: {e}", file=sys.stderr)
            time.sleep(args.delay + random.uniform(0, 1))
            continue

        candidates = []
        for item in results:
            results_t = item if isinstance(item, tuple) else (item,)
            out_info = direction_info(results_t[0])
            ret_info = direction_info(results_t[1]) if len(results_t) > 1 else None
            price = total_price(item)
            if price is None:
                continue
            if not passes(out_info, args.max_connections, max_dur_min, max_lay_min):
                continue
            if ret_info and not passes(ret_info, args.max_connections, max_dur_min, max_lay_min):
                continue
            candidates.append((price, item, out_info, ret_info))

        # optional: real booking link for the single cheapest option of THIS search
        best_link = {}
        if args.booking_links and candidates:
            cheapest = min(candidates, key=lambda c: c[0])
            try:
                opts = search.get_booking_options(cheapest[1], filters, currency=args.currency) or []
                opts.sort(key=lambda o: (not o.is_airline_direct, o.price or 9e9))
                if opts:
                    b = opts[0]
                    best_link = {
                        "id": id(cheapest[1]),
                        "vendor": b.vendor_name or b.vendor_code or "",
                        "fare": b.fare_name or "",
                        "url": b.booking_url or b.google_click_url or "",
                    }
            except Exception as e:
                print(f"      (booking link failed: {e})", file=sys.stderr)
            time.sleep(args.delay + random.uniform(0, 1))

        for price, item, out_info, ret_info in candidates:
            stay = (ret_d - out_d).days if ret_d else ""
            row = {
                "out_date": out_d.isoformat(),
                "ret_date": ret_d.isoformat() if ret_d else "",
                "stay_nights": stay,
                "price": f"{price:,.0f}",
                "price_num": round(price, 2),
                "currency": args.currency,
                "out_airlines": out_info["airlines"],
                "out_stops": out_info["stops"],
                "out_duration": out_info["duration"],
                "out_route": out_info["route"],
                "out_depart": out_info["depart"],
                "out_arrive": out_info["arrive"],
                "out_layovers": out_info["layovers"],
                "ret_airlines": ret_info["airlines"] if ret_info else "",
                "ret_stops": ret_info["stops"] if ret_info else "",
                "ret_duration": ret_info["duration"] if ret_info else "",
                "ret_route": ret_info["route"] if ret_info else "",
                "ret_depart": ret_info["depart"] if ret_info else "",
                "ret_arrive": ret_info["arrive"] if ret_info else "",
                "ret_layovers": ret_info["layovers"] if ret_info else "",
                "booking_vendor": best_link.get("vendor", "") if best_link.get("id") == id(item) else "",
                "booking_fare": best_link.get("fare", "") if best_link.get("id") == id(item) else "",
                "booking_url": best_link.get("url", "") if best_link.get("id") == id(item) else "",
                "google_flights_url": gflights_url(args.origin, args.dest, out_d.isoformat(),
                                                   ret_d.isoformat() if ret_d else None),
            }
            rows.append(row)

        print(f"  [{i}/{len(pairs)}] {out_d}->{ret_d}  "
              f"{len(results)} found, {len(candidates)} kept", file=sys.stderr)
        time.sleep(args.delay + random.uniform(0, 1))

    return rows


def write_csv(rows, path, limit=None):
    if not rows:
        print("No itineraries matched your filters. Loosen them and retry.", file=sys.stderr)
        return
    rows.sort(key=lambda r: r["price_num"])
    total = len(rows)
    if limit and limit > 0:
        rows = rows[:limit]
    fields = ["out_date", "ret_date", "stay_nights", "price", "price_num", "currency",
              "out_airlines", "out_stops", "out_duration", "out_route",
              "out_depart", "out_arrive", "out_layovers",
              "ret_airlines", "ret_stops", "ret_duration", "ret_route",
              "ret_depart", "ret_arrive", "ret_layovers",
              "booking_vendor", "booking_fare", "booking_url", "google_flights_url"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    c = rows[0]
    shown = f"{len(rows)} of {total}" if limit and total > len(rows) else f"{len(rows)}"
    print(f"\nDone. {shown} itineraries -> {path}\n"
          f"Cheapest: {c['currency']} {c['price']}  out {c['out_date']}"
          + (f" / back {c['ret_date']} ({c['stay_nights']}n)" if c['ret_date'] else "")
          + f"  via {c['out_airlines']}", file=sys.stderr)


def parse_args():
    p = argparse.ArgumentParser(description="Sweep Google Flights for the cheapest round trip.")
    p.add_argument("--origin", required=True)
    p.add_argument("--dest", required=True)
    p.add_argument("--from", dest="date_from", required=True, help="Outbound start YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", required=True, help="Outbound end YYYY-MM-DD")
    p.add_argument("--oneway", action="store_true", help="One-way instead of round trip")
    # round-trip: anchored stay length
    p.add_argument("--stay", type=int, default=21, help="Trip length in nights (anchored mode)")
    p.add_argument("--stay-flex", type=int, default=0, help="+/- nights around --stay")
    # round-trip: full grid (overrides anchored if given)
    p.add_argument("--return-from", default=None, help="Return range start (grid mode)")
    p.add_argument("--return-to", default=None, help="Return range end (grid mode)")
    p.add_argument("--adults", type=int, default=1)
    p.add_argument("--children", type=int, default=0)
    p.add_argument("--infants-seat", type=int, default=0)
    p.add_argument("--infants-lap", type=int, default=0)
    p.add_argument("--max-duration", type=float, default=99, help="Max hours per direction")
    p.add_argument("--max-connections", type=int, default=2)
    p.add_argument("--max-layover", type=float, default=24, help="Max hours per layover")
    p.add_argument("--cabin", default="ECONOMY")
    p.add_argument("--airlines", nargs="*", default=None, help="Allow-list of IATA codes, e.g. KL AF LH")
    p.add_argument("--include-basic", action="store_true", help="Include basic-economy fares")
    p.add_argument("--booking-links", action="store_true", help="Fetch a real booking URL per top result")
    p.add_argument("--currency", default="EUR")
    p.add_argument("--top", type=int, default=8, help="Options to pull per search")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap the CSV at the N cheapest rows overall (default: no cap)")
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--out", default="flight_sweep.csv")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    write_csv(sweep(args), args.out, limit=args.limit)
