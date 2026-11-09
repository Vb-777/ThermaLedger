"""MQTT subscriber: broker -> ReadingWriter -> PostgreSQL."""

from __future__ import annotations

import signal
import ssl
import sys
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from metering.ingest import ReadingWriter


class Command(BaseCommand):
    help = "Subscribe to the meter topic tree and persist readings."

    def add_arguments(self, parser):
        parser.add_argument("--host", default=settings.MQTT_HOST)
        parser.add_argument("--port", type=int, default=settings.MQTT_PORT)
        parser.add_argument("--root", default=settings.MQTT_TOPIC_ROOT)
        parser.add_argument("--qos", type=int, default=1, choices=[0, 1, 2])
        parser.add_argument(
            "--tls-ca", default=None, help="CA bundle for broker mutual TLS."
        )
        parser.add_argument("--tls-cert", default=None, help="Client certificate (PEM).")
        parser.add_argument("--tls-key", default=None, help="Client private key (PEM).")
        parser.add_argument(
            "--report-seconds",
            type=float,
            default=10.0,
            help="How often to print throughput, which is the number to record for the README.",
        )
        parser.add_argument(
            "--max-messages",
            type=int,
            default=0,
            help="Stop after N messages. 0 means run forever. Used by the benchmark.",
        )

    def handle(self, *args, **opts):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:  # pragma: no cover
            self.stderr.write("paho-mqtt is not installed; pip install -r requirements.txt")
            sys.exit(1)

        writer = ReadingWriter()
        root = opts["root"]
        topic = f"{root}/+/+/+/reading"
        stop = {"flag": False}
        started = time.monotonic()
        last_report = {"t": started, "n": 0}

        def on_connect(client, _userdata, _flags, reason_code, _properties=None):
            if int(reason_code) != 0:
                self.stderr.write(f"broker refused connection: {reason_code}")
                return
            client.subscribe(topic, qos=opts["qos"])
            self.stdout.write(self.style.SUCCESS(f"subscribed to {topic}"))

        def on_message(_client, _userdata, msg):
            writer.handle_message(msg.topic, msg.payload, root)
            now = time.monotonic()
            total = writer.stats.accepted + writer.stats.duplicates + writer.stats.rejected
            if opts["report_seconds"] and now - last_report["t"] >= opts["report_seconds"]:
                window = now - last_report["t"]
                rate = (total - last_report["n"]) / window if window else 0
                self.stdout.write(
                    f"{total} messages | {rate:.0f} msg/s | "
                    f"accepted={writer.stats.accepted} "
                    f"duplicates={writer.stats.duplicates} "
                    f"rejected={writer.stats.rejected}"
                )
                last_report["t"], last_report["n"] = now, total
            if opts["max_messages"] and total >= opts["max_messages"]:
                stop["flag"] = True

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if settings.MQTT_USERNAME:
            client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
        if opts["tls_ca"]:
            # Per-device certificates carry the meter serial as CN, so the
            # broker authenticates the same identity the readings are filed
            # under. Reused from the torquetrust device-identity model.
            client.tls_set(
                ca_certs=opts["tls_ca"],
                certfile=opts["tls_cert"],
                keyfile=opts["tls_key"],
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLSv1_2,
            )
        client.on_connect = on_connect
        client.on_message = on_message

        signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("flag", True))
        signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))

        client.connect(opts["host"], opts["port"], keepalive=30)
        client.loop_start()
        try:
            while not stop["flag"]:
                time.sleep(0.05)
        finally:
            client.loop_stop()
            client.disconnect()

        elapsed = time.monotonic() - started
        total = writer.stats.accepted + writer.stats.duplicates + writer.stats.rejected
        self.stdout.write(
            self.style.SUCCESS(
                f"\nINGESTION THROUGHPUT: {total} messages in {elapsed:.1f}s "
                f"= {total / elapsed if elapsed else 0:.0f} readings/s\n"
                f"  accepted={writer.stats.accepted} "
                f"duplicates={writer.stats.duplicates} "
                f"rejected={writer.stats.rejected}\n"
                "  ^ record this line: it is README slot [INGEST_RATE]."
            )
        )
