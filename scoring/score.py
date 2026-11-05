#!/usr/bin/env python3
"""Calculate a driving score for a trip logged by the OBD-READ firmware.

Reads a trip either from the Firebase Realtime Database or from a local JSON
export, detects harsh acceleration / braking, speeding, over-revving and
idling, and turns them into a 0-100 score.

    python3 score.py --file ../data/sample_trip.json
    python3 score.py --trip 1667600000000            # uses FIREBASE_URL / FIREBASE_AUTH
    python3 score.py --latest --write                # score the newest trip and save it

Uses only the Python standard library.
"""

import argparse
import json
import math
import os
import sys
import urllib.parse
import urllib.request

# ---- Thresholds -------------------------------------------------------------
HARSH_ACCEL_MS2 = 3.0      # m/s^2, about 0-60 mph in 9 s
HARSH_BRAKE_MS2 = -3.5     # m/s^2
SPEEDING_KMH = 120.0
HIGH_RPM = 4000.0
MAX_GAP_S = 5.0            # samples further apart than this are a gap, not a reading

# ---- Penalties --------------------------------------------------------------
PENALTY_HARSH_ACCEL = 3.0  # points per event
PENALTY_HARSH_BRAKE = 4.0  # points per event
PENALTY_SPEEDING = 30.0    # points if the whole trip were spent speeding
PENALTY_HIGH_RPM = 20.0    # points if the whole trip were spent above HIGH_RPM
PENALTY_IDLE = 10.0        # points if the whole trip were spent idling
IDLE_ALLOWANCE = 0.15      # this fraction of idling (lights, traffic) is free


# ---- Firebase ---------------------------------------------------------------

def firebase_request(path, method="GET", body=None, params=None):
    base = os.environ.get("FIREBASE_URL")
    if not base:
        sys.exit("Set FIREBASE_URL (and FIREBASE_AUTH if your rules need it).")
    query = dict(params or {})
    if os.environ.get("FIREBASE_AUTH"):
        query["auth"] = os.environ["FIREBASE_AUTH"]
    url = f"{base.rstrip('/')}/{path}.json"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read() or "null")


def latest_trip_id():
    # Trip IDs are start times in epoch ms, so the newest key sorts last.
    trips = firebase_request("trips", params={"shallow": "true"})
    if not trips:
        sys.exit("No trips found in Firebase.")
    return max(trips, key=int)


# ---- Scoring ----------------------------------------------------------------

def load_samples(trip):
    """Return samples sorted by time, skipping any without a time or speed."""
    raw = trip.get("samples") or {}
    # Firebase returns a list when keys are 0..n, a dict otherwise.
    items = raw.values() if isinstance(raw, dict) else [s for s in raw if s]
    samples = [s for s in items
               if s.get("t") is not None and _num(s.get("speed")) is not None]
    return sorted(samples, key=lambda s: s["t"])


def _num(value):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else value


def count_events(flags):
    """Count runs of consecutive True values, so one long stop is one event."""
    events, prev = 0, False
    for flag in flags:
        if flag and not prev:
            events += 1
        prev = flag
    return events


def score_trip(trip):
    samples = load_samples(trip)
    if len(samples) < 2:
        raise ValueError("Trip has fewer than two usable samples.")

    distance_km = 0.0
    duration_s = 0.0
    speeding_s = high_rpm_s = idle_s = 0.0
    accel_flags, brake_flags = [], []
    max_speed = 0.0

    for prev, cur in zip(samples, samples[1:]):
        dt = (cur["t"] - prev["t"]) / 1000.0
        if dt <= 0 or dt > MAX_GAP_S:
            accel_flags.append(False)
            brake_flags.append(False)
            continue

        v0, v1 = _num(prev["speed"]), _num(cur["speed"])
        rpm = _num(cur.get("rpm"))
        duration_s += dt
        distance_km += (v0 + v1) / 2 * dt / 3600.0
        max_speed = max(max_speed, v1)

        accel = (v1 - v0) / 3.6 / dt  # km/h per s -> m/s^2
        accel_flags.append(accel >= HARSH_ACCEL_MS2)
        brake_flags.append(accel <= HARSH_BRAKE_MS2)

        if v1 > SPEEDING_KMH:
            speeding_s += dt
        if rpm is not None and rpm > HIGH_RPM:
            high_rpm_s += dt
        if v1 == 0 and rpm is not None and rpm > 0:
            idle_s += dt

    if duration_s == 0:
        raise ValueError("Trip has no continuous stretches of data.")

    harsh_accels = count_events(accel_flags)
    harsh_brakes = count_events(brake_flags)
    speeding_frac = speeding_s / duration_s
    high_rpm_frac = high_rpm_s / duration_s
    idle_frac = idle_s / duration_s

    penalties = {
        "harsh_acceleration": harsh_accels * PENALTY_HARSH_ACCEL,
        "harsh_braking": harsh_brakes * PENALTY_HARSH_BRAKE,
        "speeding": speeding_frac * PENALTY_SPEEDING,
        "high_rpm": high_rpm_frac * PENALTY_HIGH_RPM,
        "idling": max(0.0, idle_frac - IDLE_ALLOWANCE) * PENALTY_IDLE,
    }
    score = max(0.0, min(100.0, 100.0 - sum(penalties.values())))

    return {
        "score": round(score, 1),
        "distance_km": round(distance_km, 2),
        "duration_min": round(duration_s / 60, 1),
        "max_speed_kmh": round(max_speed, 1),
        "harsh_accelerations": harsh_accels,
        "harsh_brakes": harsh_brakes,
        "speeding_pct": round(speeding_frac * 100, 1),
        "high_rpm_pct": round(high_rpm_frac * 100, 1),
        "idle_pct": round(idle_frac * 100, 1),
        "penalties": {k: round(v, 1) for k, v in penalties.items()},
    }


def print_report(trip_id, result):
    print(f"Trip {trip_id}")
    print(f"  Score:          {result['score']:.1f} / 100")
    print(f"  Distance:       {result['distance_km']} km over {result['duration_min']} min")
    print(f"  Max speed:      {result['max_speed_kmh']} km/h")
    print(f"  Harsh accel:    {result['harsh_accelerations']}")
    print(f"  Harsh braking:  {result['harsh_brakes']}")
    print(f"  Speeding:       {result['speeding_pct']}% of the time")
    print(f"  High RPM:       {result['high_rpm_pct']}% of the time")
    print(f"  Idling:         {result['idle_pct']}% of the time")
    lost = {k: v for k, v in result["penalties"].items() if v}
    if lost:
        print("  Points lost:    " + ", ".join(f"{k} -{v}" for k, v in lost.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="local JSON export of one trip")
    source.add_argument("--trip", help="trip ID to fetch from Firebase")
    source.add_argument("--latest", action="store_true", help="score the newest trip in Firebase")
    parser.add_argument("--write", action="store_true",
                        help="save the result to /trips/<id>/score in Firebase")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args()

    if args.file:
        with open(args.file) as f:
            trip = json.load(f)
        trip_id = os.path.splitext(os.path.basename(args.file))[0]
    else:
        trip_id = latest_trip_id() if args.latest else args.trip
        trip = firebase_request(f"trips/{trip_id}")
        if trip is None:
            sys.exit(f"Trip {trip_id} not found.")

    result = score_trip(trip)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(trip_id, result)

    if args.write:
        if args.file:
            sys.exit("--write needs a Firebase trip (--trip or --latest).")
        firebase_request(f"trips/{trip_id}/score", method="PUT", body=result)
        print(f"Saved to trips/{trip_id}/score")


if __name__ == "__main__":
    main()
