# Kroven energy logger

Polls smart plugs on the LAN and writes real readings into Supabase for LSTM training.

Runs on a machine on the same network as the plugs — not on Railway, which cannot reach your LAN.

## Where the data goes

`energy_readings`, **not** `household_profiles`.

`household_profiles.household_id` is a PRIMARY KEY — one row per household — so a per-minute
logger would collide or overwrite on every write. `energy_readings` is the time-series table,
and it is already what `app/routers/forecast.py` reads and feeds to `predict_next`.

Each row stores `kwh_consumed` as the energy used **during that interval**, derived from the
device's cumulative counter. That is what the model wants; a raw cumulative counter would
just encode the time of day.

## Setup

```bash
pip install -r requirements.txt
cp .env.example ../../.env    # or merge into the backend .env you already have
```

Fill in `KROVEN_HOUSEHOLD_ID` with the id the browser uses, so the app and the logger agree
on whose house this is. Get it from the app's console:

```
localStorage.getItem('kroven_household_id')
```

## Running

```bash
cd backend
python -m tools.energy_logger.main --check   # poll once, print, write nothing
python -m tools.energy_logger.main           # run forever, poll every POLL_SECONDS
```

Leave it running to accumulate toward the 50-hour target. At 60s intervals that is
~3,000 readings. It is safe to stop and restart — the first sample after a restart only
primes the counter and is not written.

## Device support

| Adapter | Status | Notes |
|---|---|---|
| `kasa` | **Blocked upstream** | KP125M at `10.0.0.163` reports `encrypt_type=TPAP`, which python-kasa does not implement ([issue #1590](https://github.com/python-kasa/python-kasa/issues/1590)). A forced KLAP/AES handshake is attempted as a fallback when credentials are present. |
| `shelly` | Built, dormant | Set `SHELLY_HOST` and it activates. Gen1 and Gen2+ are auto-detected. |
| ESP32 CSI | Not built | Slots into `adapters/REGISTRY` like the others. `Reading.watts`/`kwh_today` are optional and `Reading.extra` carries arbitrary payloads, so a non-wattage source needs no changes to the loop or the sink. |

## Adding an adapter

1. Subclass `DeviceAdapter`, set `kind`, implement `async read() -> Reading`.
2. Add it to `REGISTRY` and give it a branch in `build_adapters()`.

`main.py` and `sink.py` never change.

## Behaviour worth knowing

- **Failure isolation.** Each device is polled in its own try/except. A dead plug backs off
  exponentially (up to 15 min) and recovers on its own; it never stops the other devices or
  the process.
- **Counter resets.** Kasa's `consumption_today` resets at midnight. A counter going backwards
  is treated as a reset, not as negative energy.
- **No counter.** If a device reports only watts, energy is integrated over the measured
  elapsed time. The method used is recorded in the log line.
- **Schema drift.** `device_name`, `watts` and `kwh_today` only exist after migration
  `003_energy_readings_device_columns.sql`. The sink detects which columns exist and sends
  only those; until then the device stays identifiable through `source` (`kasa:PS5`).
- **Offline database.** Failed writes are appended to `energy_logger_buffer.jsonl` and retried
  on later polls, so a Supabase outage does not cost collection hours.

## Environment

| Variable | Purpose |
|---|---|
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | Where readings are written |
| `KROVEN_HOUSEHOLD_ID` | Ties readings to a household |
| `KASA_HOST`, `KASA_DEVICE_NAME` | Kasa plug IP and label |
| `KASA_USERNAME`, `KASA_PASSWORD` | TP-Link account; required by newer firmware even on LAN |
| `SHELLY_HOST`, `SHELLY_DEVICE_NAME`, `SHELLY_CHANNEL` | Shelly plug, when it arrives |
| `POLL_SECONDS` | Poll interval, default 60 |

Credentials are read from the environment and never logged.
