#!/usr/bin/env python3
"""Bon-Drucker: liest neue Bestellungen direkt aus der Datenbank, legt Druckaufträge
an, druckt Bons (virtuell, USB oder Netzwerk) und zeigt sie live in einer Web-Ansicht.

Start ohne Datenbank (nur Oberfläche mit Testbon):   python bon_drucker.py --demo
Start mit Datenbank:                                  python bon_drucker.py
Oberfläche:                                           http://127.0.0.1:8765

Konfiguration über Umgebungsvariablen (siehe .env.example).
"""
import json
import os
import queue
import sys
import textwrap
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

# .env neben dem Skript automatisch laden (Variablen aus dem Terminal haben Vorrang)
_env_file = Path(__file__).with_name(".env")
if _env_file.exists():
    for _raw in _env_file.read_text(encoding="utf-8-sig").splitlines():
        _raw = _raw.strip()
        if _raw and not _raw.startswith("#") and "=" in _raw:
            _key, _value = _raw.split("=", 1)
            os.environ.setdefault(_key.strip(), _value.strip().strip('"').strip("'"))

# --- Konfiguration ----------------------------------------------------------
DEMO_ONLY = "--demo" in sys.argv
DATABASE_URL = os.environ.get("DATABASE_URL")
PRINTER = os.environ.get("PRINTER", "virtual")        # virtual | usb | network
WIDTH = int(os.environ.get("RECEIPT_WIDTH", "42"))    # 58 mm ≈ 32, 80 mm ≈ 42-48
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "2"))
MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "6"))
LINE_MS = int(os.environ.get("LINE_MS", "140"))       # Tempo der Animation
UI_HOST = os.environ.get("UI_HOST", "127.0.0.1")
UI_PORT = int(os.environ.get("UI_PORT", "8765"))
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "LIEFERDIENST")
TZ_NAME = os.environ.get("TZ_NAME", "Europe/Berlin")
STATE = Path(os.environ.get("STATE_DIR", "."))
MAX_ATTEMPTS = 5


def env_list(name, default):
    return [x.strip().lower() for x in os.environ.get(name, default).split(",") if x.strip()]


# Welche Bestellungen gedruckt werden: bezahlt ODER Barzahlung, nicht storniert.
# Die Werte (paid, cash, cancelled) an deine echten Einträge anpassen.
PAID_STATUSES = env_list("PAID_STATUSES", "paid")
CASH_METHODS = env_list("CASH_METHODS", "cash,cash_on_delivery")
SKIP_STATUSES = env_list("SKIP_STATUSES", "cancelled")

# --- SQL --------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS print_jobs (
  id          SERIAL PRIMARY KEY,
  order_id    UUID NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
  status      TEXT NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending', 'printing', 'printed', 'failed')),
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT,
  claimed_at  TIMESTAMPTZ,
  printed_at  TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)"""

ENQUEUE_SQL = """
INSERT INTO print_jobs (order_id)
SELECT o.id FROM orders o
WHERE o.created_at > now() - make_interval(hours => %(hours)s::int)
  AND lower(o.status::text) <> ALL(%(skip)s::text[])
  AND (lower(o.payment_status) = ANY(%(paid)s::text[])
       OR lower(o.payment_method) = ANY(%(cash)s::text[]))
  AND NOT EXISTS (SELECT 1 FROM print_jobs j WHERE j.order_id = o.id)
ON CONFLICT (order_id) DO NOTHING"""

# Ein einziges Statement mit SKIP LOCKED: kein Auftrag wird doppelt vergeben.
# Hängengebliebene Aufträge (Absturz mitten im Druck) werden nach 2 Minuten freigegeben.
CLAIM_SQL = """
UPDATE print_jobs
SET status = 'printing', claimed_at = now(), attempts = attempts + 1
WHERE id IN (
  SELECT id FROM print_jobs
  WHERE (status = 'pending'
         OR (status = 'printing' AND claimed_at < now() - interval '2 minutes'))
    AND attempts < %s
  ORDER BY created_at
  LIMIT 5
  FOR UPDATE SKIP LOCKED
)
RETURNING id, order_id"""

ORDER_SQL = """
SELECT o.id, o.order_type, o.subtotal, o.tax, o.total, o.delivery_fee,
       o.customer_name, o.customer_phone, o.customer_note,
       o.payment_method, o.payment_status, o.created_at,
       a.street, a.house_number, a.postal_code, a.city, a.notes AS address_notes
FROM orders o
LEFT JOIN delivery_addresses a ON a.id = o.delivery_address_id
WHERE o.id = %s"""

# ANNAHME: Tabelle order_items mit den Spalten name, quantity, price (Einzelpreis).
# Weicht dein Schema ab (z. B. Name aus menu_items), nur diese Abfrage anpassen.
ITEMS_SQL = """
SELECT product_name_snapshot AS name,
       quantity,
       unit_price_snapshot AS price
FROM order_items
WHERE order_id = %s
"""
MARK_PRINTED_SQL = """
UPDATE print_jobs SET status = 'printed', printed_at = now(), last_error = NULL
WHERE id = %s"""

MARK_FAILED_SQL = """
UPDATE print_jobs
SET status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END,
    last_error = %s
WHERE id = %s"""


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --- Schutz vor doppeltem Druck (falls das Melden an die DB scheitert) --------
PRINTED_FILE = STATE / "printed_orders.txt"
_printed = set(PRINTED_FILE.read_text().split()) if PRINTED_FILE.exists() else set()


def already_printed(order_id):
    return str(order_id) in _printed


def remember(order_id):
    _printed.add(str(order_id))
    with PRINTED_FILE.open("a") as f:
        f.write(f"{order_id}\n")


# --- Bon-Layout ---------------------------------------------------------------
def eur(x):
    return f"{float(x):.2f}".replace(".", ",") + " €"


def two_col(left, right):
    return left[: WIDTH - len(right) - 1].ljust(WIDTH - len(right)) + right


def item_rows(qty, name, price):
    first_w = WIDTH - len(price) - 1
    parts = textwrap.wrap(f"{qty}x {name}", first_w, subsequent_indent="   ") or [""]
    return [parts[0].ljust(WIDTH - len(price)) + price] + parts[1:]


def render(d):
    """Gibt eine Liste von Zeilen zurück: {'t': Text, 's': normal|bold|title}."""
    lines = []

    def add(text="", style="normal"):
        lines.append({"t": text, "s": style})

    rule = "-" * WIDTH
    add(BUSINESS_NAME[:WIDTH].center(WIDTH), "title")
    add(d["typeLabel"].center(WIDTH), "bold")
    add(rule)
    add(two_col(f"Nr. {d['orderNo']}", d["time"]), "bold")
    add(rule)
    if d["customer"]:
        for row in textwrap.wrap(d["customer"], WIDTH):
            add(row)
    if d["phone"]:
        add(f"Tel: {d['phone']}")
    for row in d["address"]:
        for part in textwrap.wrap(row, WIDTH):
            add(part)
    if d["addressNotes"]:
        for part in textwrap.wrap(f"Hinweis: {d['addressNotes']}", WIDTH):
            add(part)
    add(rule)
    for it in d["items"]:
        for row in item_rows(it["qty"], it["name"], eur(it["total"])):
            add(row)
    add(rule)
    add(two_col("Zwischensumme", eur(d["subtotal"])))
    if d["fee"]:
        add(two_col("Lieferung", eur(d["fee"])))
    if d["tax"]:
        add(two_col("MwSt.", eur(d["tax"])))
    add(two_col("SUMME", eur(d["total"])), "bold")
    add(rule)
    add(d["payment"], "bold")
    if d["note"]:
        add("Anmerkung:", "bold")
        for part in textwrap.wrap(d["note"], WIDTH):
            add(part)
    add()
    add("Guten Appetit!".center(WIDTH))
    add()
    add()
    return lines


def build_payload(o, items):
    try:
        created = o["created_at"].astimezone(ZoneInfo(TZ_NAME))
    except Exception:
        created = o["created_at"].astimezone()

    order_type = (o["order_type"] or "").lower()
    type_label = {"delivery": "LIEFERUNG", "pickup": "ABHOLUNG"}.get(order_type, order_type.upper())
    paid = (o["payment_status"] or "").lower() in PAID_STATUSES
    cash = (o["payment_method"] or "").lower() in CASH_METHODS
    if paid:
        payment = "BEREITS BEZAHLT"
    elif cash:
        payment = f"BAR KASSIEREN: {eur(o['total'])}"
    else:
        payment = "ZAHLUNG OFFEN"

    address = []
    if o.get("street"):
        address = [f"{o['street']} {o['house_number']}", f"{o['postal_code']} {o['city']}"]

    d = {
        "orderNo": str(o["id"])[:8].upper(),
        "time": created.strftime("%d.%m.%Y %H:%M"),
        "typeLabel": type_label,
        "customer": o["customer_name"] or "",
        "phone": o["customer_phone"] or "",
        "address": address,
        "addressNotes": o.get("address_notes") or "",
        "note": o["customer_note"] or "",
        "items": [
            {"qty": int(i["quantity"]), "name": str(i["name"]),
             "total": Decimal(i["price"]) * int(i["quantity"])}
            for i in items
        ],
        "subtotal": o["subtotal"], "fee": o["delivery_fee"], "tax": o["tax"],
        "total": o["total"], "payment": payment,
    }
    return {
        "id": str(o["id"]),
        "orderNo": d["orderNo"],
        "time": created.strftime("%H:%M"),
        "total": eur(o["total"]),
        "width": WIDTH,
        "lineMs": LINE_MS,
        "lines": render(d),
    }


def demo_payload():
    o = {
        "id": uuid.uuid4(), "order_type": "delivery",
        "subtotal": Decimal("27.40"), "tax": Decimal("1.79"),
        "total": Decimal("30.90"), "delivery_fee": Decimal("3.50"),
        "customer_name": "Max Müller", "customer_phone": "0221 1234567",
        "customer_note": "Bitte ohne Zwiebeln, Klingel defekt: anrufen.",
        "payment_method": "cash", "payment_status": "awaiting",
        "created_at": datetime.now(timezone.utc),
        "street": "Hauptstraße", "house_number": "12", "postal_code": "50667",
        "city": "Köln", "address_notes": "3. OG, links",
    }
    items = [
        {"name": "Döner Teller", "quantity": 2, "price": Decimal("9.50")},
        {"name": "Café Latte mit Hafermilch und extra Sirup", "quantity": 1, "price": Decimal("3.90")},
       
    ]
    return build_payload(o, items)


# --- Live-Ansicht (Server-Sent Events) ----------------------------------------
class Hub:
    def __init__(self):
        self.clients, self.history, self.lock = [], [], threading.Lock()

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.clients.append(q)
            return q, list(self.history)

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def publish(self, payload):
        with self.lock:
            self.history = (self.history + [payload])[-30:]
            for q in self.clients:
                q.put(payload)


hub = Hub()
UI_FILE = Path(__file__).with_name("receipt_ui.html")


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/":
            body = UI_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/events":
            self.stream_events()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/demo":
            threading.Thread(target=virtual_print, args=(demo_payload(),), daemon=True).start()
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def stream_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q, history = hub.subscribe()
        try:
            self.wfile.write(sse("history", history))
            self.wfile.flush()
            while True:
                try:
                    item = q.get(timeout=15)
                    self.wfile.write(sse("receipt", item))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.unsubscribe(q)


# --- Drucken ------------------------------------------------------------------
def virtual_print(payload):
    """Zeigt den Bon nur in der Oberfläche und wartet so lange, wie ein echter Druck dauert."""
    hub.publish(payload)
    time.sleep(len(payload["lines"]) * LINE_MS / 1000 + 4)


def hardware_print(lines):
    from escpos.printer import Network, Usb  # type: ignore # nur nötig, wenn ein echter Drucker genutzt wird

    profile = os.environ.get("PRINTER_PROFILE", "default")
    if PRINTER == "usb":
        p = Usb(int(os.environ["USB_VENDOR"], 16), int(os.environ["USB_PRODUCT"], 16), profile=profile)
    else:
        p = Network(os.environ["PRINTER_HOST"], profile=profile)
    try:  # Drucker pro Auftrag neu öffnen: übersteht Ausstecken und Neustart
        for line in lines:
            p.set(align="left", bold=line["s"] in ("title", "bold"))
            p.text(line["t"] + "\n")
        p.cut()
    finally:
        p.close()


def deliver(payload):
    if PRINTER in ("usb", "network"):
        hardware_print(payload["lines"])
        hub.publish(payload)  # Oberfläche spiegelt den echten Druck
    else:
        virtual_print(payload)


# --- Datenbank-Schleife ---------------------------------------------------------
def connect():
    import psycopg
    from psycopg.rows import dict_row

    # prepare_threshold=None: nötig für Neon-Pooler (pgbouncer)
    return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row, prepare_threshold=None)


def process_job(conn, job):
    job_id, order_id = job["id"], job["order_id"]
    try:
        order = conn.execute(ORDER_SQL, (order_id,)).fetchone()
        if not order:
            raise RuntimeError("Bestellung nicht gefunden")
        items = conn.execute(ITEMS_SQL, (order_id,)).fetchall()
        payload = build_payload(order, items)
        if not already_printed(order_id):
            deliver(payload)
            remember(order_id)
        conn.execute(MARK_PRINTED_SQL, (job_id,))
        log(f"Bon gedruckt: Bestellung {payload['orderNo']}")
    except Exception as e:
        log(f"Auftrag {job_id} fehlgeschlagen: {e}")
        try:
            conn.execute(MARK_FAILED_SQL, (MAX_ATTEMPTS, str(e)[:500], job_id))
        except Exception:
            pass


def worker():
    conn = None
    while True:
        try:
            if conn is None or conn.closed:
                conn = connect()
                conn.execute(SCHEMA_SQL)
                log("Mit der Datenbank verbunden, warte auf Bestellungen")
            conn.execute(ENQUEUE_SQL, {
                "hours": MAX_AGE_HOURS, "skip": SKIP_STATUSES,
                "paid": PAID_STATUSES, "cash": CASH_METHODS,
            })
            for job in conn.execute(CLAIM_SQL, (MAX_ATTEMPTS,)).fetchall():
                process_job(conn, job)
        except Exception as e:
            log(f"Datenbankfehler, neuer Versuch in {POLL_SECONDS:g} s: {e}")
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            conn = None
        time.sleep(POLL_SECONDS)


def main():
    if not DEMO_ONLY and not DATABASE_URL:
        sys.exit("DATABASE_URL fehlt. Zum Ausprobieren ohne Datenbank: python bon_drucker.py --demo")
    server = ThreadingHTTPServer((UI_HOST, UI_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"Live-Ansicht: http://{UI_HOST}:{UI_PORT}  (Drucker: {PRINTER})")
    try:
        if DEMO_ONLY:
            log("Demo-Modus: In der Oberfläche auf „Testbon“ klicken. Beenden mit Strg+C.")
            while True:
                time.sleep(3600)
        else:
            worker()
    except KeyboardInterrupt:
        log("Beendet")


if __name__ == "__main__":
    main()