#!/usr/bin/env python3
"""A synthetic merchant storefront on loopback, for the browser import workflow.

The critical browser workflow imports a merchant's public pages, so it needs public pages. Aiming
it at a real storefront would make this repository's test suite depend on somebody else's website
being up and unchanged, and would put a copy of their markup in this repository. So the storefront
is this: five pages, invented, minimal, and shaped to exercise the four outcomes an import has.

```text
/p/charger    schema.org product data, in stock, one variant
/p/sleeve     a product group with two variants, both in stock
/p/dock       out of stock, which is the one availability that becomes a number by itself
/p/lamp       a price with no currency, which is imported as nothing and reported as a reason
/returns      merchant prose
```

Deliberately a separate process from the API rather than a route inside it. A test fixture served
by the application under test would be a test fixture the application could special case, and the
property being tested is that AgentRank fetches an ordinary web server over an ordinary socket.

It binds loopback, which the import address policy refuses by default. Reaching it at all requires
`AGENTRANK_IMPORT_ALLOWED_NETWORKS`, which `Settings` refuses to load in any environment that is
not development, CI or test. That is not an inconvenience being worked around: it is the boundary
being demonstrated, from the one side that is allowed to see it.
"""

import argparse
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
DEFAULT_PORT = 8002

CHARGER = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>65W Travel Charger</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product",
 "name":"65W Travel Charger","sku":"CHG-65","category":"chargers",
 "description":"A two-port 65W USB-C charger for phones and tablets.",
 "offers":{"@type":"Offer","sku":"CHG-65-BLK","price":"3299.00","priceCurrency":"INR",
           "availability":"https://schema.org/InStock"}}
</script></head>
<body><h1>65W Travel Charger</h1><p>Charges a laptop and a phone at once.</p></body></html>
"""

SLEEVE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Laptop Sleeve</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"ProductGroup",
 "name":"Laptop Sleeve","sku":"SLV","category":"cases",
 "description":"A padded sleeve for a thirteen inch laptop.",
 "hasVariant":[
  {"@type":"Product","name":"Black","sku":"SLV-BLK",
   "offers":{"@type":"Offer","price":"1499.00","priceCurrency":"INR",
             "availability":"https://schema.org/InStock"}},
  {"@type":"Product","name":"Sand","sku":"SLV-SND",
   "offers":{"@type":"Offer","price":"1499.00","priceCurrency":"INR",
             "availability":"https://schema.org/InStock"}}]}
</script></head>
<body><h1>Laptop Sleeve</h1></body></html>
"""

DOCK = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Desk Dock</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product",
 "name":"Desk Dock","sku":"DCK-1","category":"docks",
 "offers":{"@type":"Offer","sku":"DCK-1-A","price":"7999.00","priceCurrency":"INR",
           "availability":"https://schema.org/OutOfStock"}}
</script></head>
<body><h1>Desk Dock</h1></body></html>
"""

# The page that is deliberately not importable. A figure with no currency is not an amount, and an
# importer that read a number here would be inventing the currency.
LAMP = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Desk Lamp</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Desk Lamp","sku":"LMP-1",
 "offers":{"@type":"Offer","price":"2499.00","availability":"https://schema.org/InStock"}}
</script></head>
<body><h1>Desk Lamp</h1><p>Only 2,499 today</p></body></html>
"""

RETURNS = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Returns</title></head>
<body><h1>Returns</h1>
<p>Returns are accepted within 30 days of delivery in original packaging.</p>
<script>window.analytics = "never merchant evidence";</script>
</body></html>
"""

PAGES = {
    "/p/charger": CHARGER,
    "/p/sleeve": SLEEVE,
    "/p/dock": DOCK,
    "/p/lamp": LAMP,
    "/returns": RETURNS,
}


class Storefront(BaseHTTPRequestHandler):
    """An ordinary web server. It answers GET and HEAD and refuses everything else."""

    server_version = "ImportFixture/1"
    # Suppressing the default per request line on stderr, which would interleave with the browser
    # harness output and says nothing a failure would need.
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        body = self._body()
        if body is None:
            self._respond(404, b"<html><body>not found</body></html>")
            return
        self._respond(200, body)

    def do_HEAD(self) -> None:
        body = self._body()
        self._respond(404 if body is None else 200, b"", length=0 if body is None else len(body))

    def _body(self) -> bytes | None:
        if self.path == "/health":
            return b"<html><body>ok</body></html>"
        page = PAGES.get(self.path)
        return None if page is None else page.encode("utf-8")

    def _respond(self, status: int, body: bytes, *, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body) if length is None else length))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    arguments = parser.parse_args()
    server = ThreadingHTTPServer((HOST, arguments.port), Storefront)
    print(f"synthetic storefront on http://{HOST}:{arguments.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
