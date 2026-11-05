# OBD-READ

A DIY driving-behavior logger. A microcontroller plugged into a car's OBD-II
port records engine and speed data while you drive and pushes it to Firebase.
A Python script then reads each trip back and gives it a 0–100 driving score.

> This started as a college project. The original code was lost, so this is a
> rewrite of the same idea.

```
 Car OBD-II port ──> ELM327 adapter ──UART──> ESP32 ──WiFi──> Firebase RTDB
                                                                   │
                                          scoring/score.py <───────┘
                                          (writes score back to the trip)
```

## Repository layout

| Path | What it is |
|---|---|
| `firmware/obd_logger/` | Arduino sketch for the ESP32 logger |
| `scoring/score.py` | Driving-score calculator (Python 3, standard library only) |
| `data/sample_trip.json` | A synthetic 8-minute trip for trying the scorer offline |

## Hardware

- ESP32 dev board (any board with WiFi works with small changes)
- ELM327 or STN1110 OBD-II **UART** adapter board
- 12 V → 5 V buck converter, powered from OBD pin 16 (battery +) and pin 4/5 (ground)
- WiFi in the car, usually a phone hotspot

Wiring: adapter TX → ESP32 GPIO16, adapter RX → ESP32 GPIO17, common ground.

## Firmware

1. Install the [ELMduino](https://github.com/PowerBroker2/ELMduino) library from the
   Arduino Library Manager, and ESP32 board support.
2. Copy `firmware/obd_logger/secrets.h.example` to `secrets.h` and fill in your
   WiFi and Firebase details. `secrets.h` is git-ignored.
3. Flash `obd_logger.ino` to the ESP32.

Once a second, the logger reads these PIDs:

| Field | PID | Unit |
|---|---|---|
| `speed` | 0x0D | km/h |
| `rpm` | 0x0C | rev/min |
| `throttle` | 0x11 | % |
| `load` | 0x04 | % (calculated engine load) |
| `coolant` | 0x05 | °C |

It uploads them in batches of 10. Each power-on starts a new trip, keyed by
its start time:

```json
{
  "trips": {
    "1667600000000": {
      "meta":    { "started": 1667600000000, "vehicle": "my-car" },
      "samples": { "0": { "t": 1667600000000, "speed": 0, "rpm": 752, ... }, ... },
      "score":   { "score": 86.1, ... }
    }
  }
}
```

## Driving score

```sh
cd scoring
python3 score.py --file ../data/sample_trip.json   # offline sample

export FIREBASE_URL=https://your-project-default-rtdb.firebaseio.com
export FIREBASE_AUTH=your-database-secret
python3 score.py --latest            # newest trip
python3 score.py --trip 1667600000000 --write   # score a trip and save it to Firebase
```

Every trip starts at 100. Points come off for:

| Behavior | Rule | Penalty |
|---|---|---|
| Harsh acceleration | ≥ 3.0 m/s² between samples | 3 pts per event |
| Harsh braking | ≤ −3.5 m/s² between samples | 4 pts per event |
| Speeding | over 120 km/h | up to 30 pts, scaled by share of trip time |
| Over-revving | over 4000 rpm | up to 20 pts, scaled by share of trip time |
| Excess idling | stopped with the engine running, beyond 15% of the trip | up to 10 pts |

Consecutive harsh samples count as one event. Gaps of more than 5 s between
samples (lost WiFi, a dropped reading) are skipped rather than treated as huge
speed changes. All thresholds live at the top of `score.py`.

Sample output:

```
Trip sample_trip
  Score:          86.1 / 100
  Distance:       9.34 km over 8.3 min
  Max speed:      128.8 km/h
  Harsh accel:    1
  Harsh braking:  1
  Speeding:       22.9% of the time
  High RPM:       0.0% of the time
  Idling:         12.3% of the time
  Points lost:    harsh_acceleration -3.0, harsh_braking -4.0, speeding -6.9
```
