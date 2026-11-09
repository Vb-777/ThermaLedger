#!/usr/bin/env python
"""
MQTT meter simulator - the producer side, standing in for Iot-sensor-sim.

Two modes:

  --mode stream     publish readings at a realistic rate, forever
  --mode burst      publish N readings as fast as the broker accepts them,
                    which is what you run to fill README slot [INGEST_RATE]
  --mode latency    publish one reading, then poll the API until it appears,
                    which fills README slot [E2E_LATENCY]

Run the ingest worker (`manage.py ingest_mqtt`) alongside it.

Usage:
    python scripts/meter_simulator.py --mode burst --count 5000
    python scripts/meter_simulator.py --mode latency --samples 50 \
        --api http://localhost:8000 --token <token>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import statistics
import sys
import time
import urllib.error
import urllib.request

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("paho-mqtt is not installed: pip install -r requirements.txt")

DEFAULT_FLEET = [
    ("acme-steel", "PLANT-01", "MTR-0101"),
    ("acme-steel", "PLANT-01", "MTR-0102"),
    ("acme-steel", "PLANT-02", "MTR-0201"),
    ("acme-steel", "PLANT-03", "MTR-0301"),
    ("borax-chem", "PLANT-01", "MTR-0101"),
    ("borax-chem", "PLANT-02", "MTR-0201"),
    ("veda-textiles", "DYE-01", "MTR-0101"),
    ("veda-textiles", "DYE-02", "MTR-0201"),
]


def connect(args):
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if args.username:
        client.username_pw_set(args.username, args.password)
    client.connect(args.host, args.port, keepalive=30)
    client.loop_start()
    return client


def publish(client, root, tenant, site, meter, cumulative, when, qos=1):
    topic = f"{root}/{tenant}/{site}/{meter}/reading"
    body = json.dumps(
        {
            "ts": when.isoformat().replace("+00:00", "Z"),
            "cumulative": f"{cumulative:.3f}",
            "meter": meter,
            "quality": "good",
        }
    )
    info = client.publish(topic, body, qos=qos)
    return info


def mode_stream(client, args):
    counters = {key: random.uniform(50_000, 300_000) for key in DEFAULT_FLEET}
    sent = 0
    try:
        while True:
            now = dt.datetime.now(dt.timezone.utc)
            for key in DEFAULT_FLEET:
                counters[key] += random.uniform(150, 450)
                publish(client, args.root, *key, counters[key], now)
                sent += 1
            print(f"\rpublished {sent}", end="", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nstopped after {sent} messages")


def mode_burst(client, args):
    counters = {key: random.uniform(50_000, 300_000) for key in DEFAULT_FLEET}
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=args.count)
    started = time.monotonic()
    for index in range(args.count):
        key = DEFAULT_FLEET[index % len(DEFAULT_FLEET)]
        counters[key] += random.uniform(150, 450)
        publish(
            client,
            args.root,
            *key,
            counters[key],
            base + dt.timedelta(seconds=index),
            qos=args.qos,
        )
    elapsed = time.monotonic() - started
    print(
        f"\nPUBLISH RATE: {args.count} messages in {elapsed:.2f}s "
        f"= {args.count / elapsed:.0f} msg/s"
    )
    print("Now read the ingest worker's own throughput line for [INGEST_RATE];")
    print("the worker's number is the one that counts, since it includes the database write.")


def api_latest(api, token, meter_serial):
    request = urllib.request.Request(
        f"{api}/api/v1/consumption?days=1&granularity=hour",
        headers={"Authorization": f"Token {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)
    except urllib.error.URLError:
        return None


def mode_latency(client, args):
    if not args.token:
        sys.exit("--mode latency needs --token (manage.py drf_create_token <user>)")
    samples = []
    counter = random.uniform(500_000, 600_000)
    tenant, site, meter = DEFAULT_FLEET[0]

    for n in range(args.samples):
        counter += random.uniform(150, 450)
        when = dt.datetime.now(dt.timezone.utc)
        start = time.monotonic()
        info = publish(client, args.root, tenant, site, meter, counter, when)
        info.wait_for_publish(timeout=5)

        deadline = start + 10
        seen = False
        while time.monotonic() < deadline:
            body = api_latest(args.api, args.token, meter)
            if body and body.get("points"):
                seen = True
                break
            time.sleep(0.02)
        if seen:
            samples.append((time.monotonic() - start) * 1000)
        print(f"\rsample {n + 1}/{args.samples}", end="", flush=True)
        time.sleep(0.1)

    if not samples:
        sys.exit("\nno samples completed; is the ingest worker running?")
    samples.sort()
    print(
        f"\n\nEND-TO-END LATENCY (publish -> visible in API), n={len(samples)}\n"
        f"  p50 {statistics.median(samples):.0f} ms\n"
        f"  p95 {samples[int(len(samples) * 0.95) - 1]:.0f} ms\n"
        f"  max {samples[-1]:.0f} ms\n"
        "  ^ record these: README slot [E2E_LATENCY]"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["stream", "burst", "latency"], default="stream")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--root", default="thermaledger")
    parser.add_argument("--qos", type=int, default=1, choices=[0, 1, 2])
    parser.add_argument("--interval", type=float, default=2.0, help="stream mode: seconds between rounds")
    parser.add_argument("--count", type=int, default=5000, help="burst mode: messages to send")
    parser.add_argument("--samples", type=int, default=50, help="latency mode: samples to take")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--token", default=None)
    args = parser.parse_args()

    client = connect(args)
    try:
        {"stream": mode_stream, "burst": mode_burst, "latency": mode_latency}[args.mode](client, args)
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
