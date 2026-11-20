"""
Odoo connector for the quarterly order forecast dashboard.

Pulls two things from your live Odoo instance:
  1. Monthly sales history per product (from sale.order.line), for trend/seasonality forecasting.
  2. Current inventory snapshot per product (on hand, outgoing confirmed, outgoing unconfirmed).

Setup
-----
1. pip install -r requirements.txt
2. Copy .env.example to .env and fill in your Odoo URL, database name, and API credentials.
   Generate an API key in Odoo: Settings > Users > (your user) > Account Security > API Keys.
   Do not use your login password here - use a scoped API key.
3. Run: python odoo_client.py
   This prints the fetched data as JSON to stdout, and writes it to output/odoo_export.json.

This script is read-only. It never writes back to Odoo.
"""

import os
import json
import statistics
import math
import calendar
import xmlrpc.client
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv
from bom_config import DEVICE_BOMS

load_dotenv()

ODOO_URL = os.environ["ODOO_URL"].rstrip("/")
ODOO_DB = os.environ["ODOO_DB"]
ODOO_USERNAME = os.environ["ODOO_USERNAME"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]

# Comma-separated internal reference codes (default_code) of the devices to track.
PRODUCT_SKUS = [s.strip() for s in os.environ.get("PRODUCT_SKUS", "").split(",") if s.strip()]

# How many months of sales history to pull. 36+ recommended for real seasonality detection.
HISTORY_MONTHS = int(os.environ.get("HISTORY_MONTHS", "36"))

# Warehouse to scope everything to - on-hand, sales history, and outgoing are all filtered
# to this warehouse only. Identified by its warehouse code (e.g. "WH", shown in Odoo under
# Inventory > Configuration > Warehouses).
WAREHOUSE_CODE = os.environ["WAREHOUSE_CODE"]


def connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {})
    if not uid:
        raise RuntimeError(
            "Odoo authentication failed. Check ODOO_URL, ODOO_DB, ODOO_USERNAME, and ODOO_API_KEY."
        )
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def fetch_warehouse(uid, models):
    """Resolve WAREHOUSE_CODE (e.g. "WH") to its stock.warehouse record. Uses
    view_location_id - the umbrella location that contains every sub-location in the
    warehouse (Stock, Input, Output, Quality Control, etc.) - rather than a single
    sub-location, so on-hand reflects the whole warehouse, not just one zone within it.
    """
    warehouses = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "stock.warehouse", "search_read",
        [[["code", "=", WAREHOUSE_CODE]]],
        {"fields": ["id", "name", "code", "view_location_id"]},
    )
    if not warehouses:
        candidates = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY, "stock.warehouse", "search_read",
            [[]], {"fields": ["id", "name", "code"]},
        )
        listing = "\n".join(f'  - code "{c["code"]}" = "{c["name"]}"' for c in candidates)
        raise RuntimeError(
            f'No warehouse found with code "{WAREHOUSE_CODE}". Available warehouses:\n{listing}\n'
            f"Set WAREHOUSE_CODE in .env to one of the codes listed above."
        )
    warehouse = warehouses[0]
    warehouse["location_id"] = warehouse["view_location_id"][0]
    return warehouse


def fetch_products(uid, models, warehouse):
    """Resolve SKUs (default_code) to Odoo product.product IDs, with qty_available
    scoped to the given warehouse's stock location only."""
    domain = [["default_code", "in", PRODUCT_SKUS]]
    fields = ["id", "default_code", "name", "qty_available"]
    products = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "product.product", "search_read", [domain],
        {"fields": fields, "context": {"location": warehouse["location_id"]}},
    )
    return {p["default_code"]: p for p in products}


# Custom Ops Status field on stock.move/stock.picking - "Shipment in Progress" corresponds
# to the "in_process" value on this field.
OUTGOING_STATUS_FIELD = "hex_ops_state"
OUTGOING_CONFIRMED_VALUE = "in_process"


def fetch_outgoing(uid, models, product_ids, warehouse_id, warehouse_location_id):
    """
    Outgoing quantities for the tracked devices, taken only from VALID delivery orders that are
    awaiting shipment: outgoing deliveries from the WH location tree to a customer location
    (Odoo's standard usage == "customer" type, not a specific name) whose native status is
    Waiting or Ready. Odoo's technical names differ from its labels - the UI's "Waiting" is the
    technical value "confirmed" and "Ready" is "assigned". Drafts, deliveries "waiting on another
    operation", and completed or cancelled deliveries are excluded, and quotations are not
    counted at all.

    Those deliveries are then split by the custom Ops Status field (hex_ops_state). It's a
    picking-level field, so it's fetched from stock.picking and joined to the per-product
    quantities on stock.move:
      - Confirmed outgoing   = Ops Status is "in_process" ("Shipment in Progress").
      - Unconfirmed outgoing = any other Ops Status (including none set): the delivery is
        awaiting shipment, but its Ops Status is holding it back from shipping.

    Quantities are the DEMANDED quantity on each line (the "Demand" column in Odoo, not the
    reserved/"Quantity" column), for the tracked devices only. Lines that are themselves cancelled,
    done or draft are ignored even when their delivery is still open.

    Returns (confirmed, unconfirmed, confirmed_orders): the first two are qty totals per
    product id. confirmed_orders is per-product-id detail of exactly which delivery orders (by
    name) and quantities make up the confirmed total, for drill-down display.
    """
    # Odoo's technical state names don't match their UI labels: the UI shows "Waiting" for
    # the technical value "confirmed" (not "waiting" - that technical value actually means
    # "Waiting Another Operation", a different status for multi-step routes). "Ready" in
    # the UI is the technical value "assigned".
    AWAITING_SHIPMENT_STATES = ["confirmed", "assigned"]

    picking_domain = [
        ["picking_type_id.code", "=", "outgoing"],
        ["picking_type_id.warehouse_id", "=", warehouse_id],
        ["location_id", "child_of", warehouse_location_id],
        ["location_dest_id.usage", "=", "customer"],
        ["state", "in", AWAITING_SHIPMENT_STATES],
    ]
    pickings = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "stock.picking", "search_read", [picking_domain],
        {"fields": ["id", "name", "state", OUTGOING_STATUS_FIELD, "partner_id"]},
    )
    picking_info = {p["id"]: p for p in pickings}
    picking_ids = list(picking_info.keys())

    move_domain = [
        ["product_id", "in", product_ids],
        ["picking_id", "in", picking_ids],
        # A delivery can stay open while one of its lines is cancelled: that line is no longer
        # demand, so it must not be counted. Lines already done or still draft aren't open demand either.
        ["state", "not in", ["draft", "cancel", "done"]],
    ]
    moves = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "stock.move", "search_read", [move_domain],
        {"fields": ["product_id", "product_uom_qty", "picking_id"]},
    )

    confirmed = defaultdict(float)
    unconfirmed = defaultdict(float)
    # Per-order detail behind the confirmed totals, so the dashboard can show exactly
    # which delivery orders make up a device's Outgoing Confirmed number. Aggregated by
    # (product, picking) in case a picking has more than one move line for the same
    # product (lot/serial splits).
    confirmed_lines = defaultdict(lambda: defaultdict(float))  # confirmed_lines[product_id][picking_id] += qty
    for m in moves:
        pid = m["product_id"][0]
        picking_id = m["picking_id"][0]
        picking = picking_info.get(picking_id, {})
        is_confirmed = (
            picking.get("state") in AWAITING_SHIPMENT_STATES
            and picking.get(OUTGOING_STATUS_FIELD) == OUTGOING_CONFIRMED_VALUE
        )
        if is_confirmed:
            confirmed[pid] += m["product_uom_qty"]
            confirmed_lines[pid][picking_id] += m["product_uom_qty"]
        else:
            unconfirmed[pid] += m["product_uom_qty"]

    confirmed_orders = {
        product_id: [
            {
                "picking": picking_info[picking_id]["name"],
                "qty": qty,
                "client": (
                    picking_info[picking_id]["partner_id"][1]
                    if picking_info[picking_id].get("partner_id")
                    else "No customer set"
                ),
            }
            for picking_id, qty in by_picking.items()
        ]
        for product_id, by_picking in confirmed_lines.items()
    }

    return confirmed, unconfirmed, confirmed_orders


def fetch_component_stock(uid, models, warehouse):
    """Pulls on-hand quantity for every component referenced anywhere in DEVICE_BOMS,
    scoped to the same warehouse as everything else. Returns id + on-hand keyed by SKU,
    since the id is needed to look up incoming quantity separately."""
    component_skus = sorted({c for parts in DEVICE_BOMS.values() for c, _qty in parts})
    if not component_skus:
        return {}, []
    domain = [["default_code", "in", component_skus]]
    products = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "product.product", "search_read", [domain],
        {"fields": ["id", "default_code", "qty_available"], "context": {"location": warehouse["location_id"]}},
    )
    stock_by_sku = {p["default_code"]: {"id": p["id"], "onHand": p["qty_available"]} for p in products}
    missing = [c for c in component_skus if c not in stock_by_sku]
    return stock_by_sku, missing


def fetch_component_incoming(uid, models, warehouse_id, component_ids):
    """
    Incoming quantity per component = qty on open incoming stock moves (purchase
    receipts not yet done), scoped to this warehouse. Deliberately not using Odoo's
    built-in incoming_qty field on product.product - that field proved inaccurate
    for finished devices earlier (likely aggregating across locations/warehouses in a
    way that didn't match what was expected), so this queries stock.move directly the
    same way fetch_outgoing does, for consistency and to keep the scoping explicit.
    """
    if not component_ids:
        return {}
    move_domain = [
        ["product_id", "in", component_ids],
        ["picking_id.picking_type_id.code", "=", "incoming"],
        ["picking_id.picking_type_id.warehouse_id", "=", warehouse_id],
        ["state", "not in", ["done", "cancel"]],
    ]
    moves = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "stock.move", "search_read", [move_domain],
        {"fields": ["product_id", "product_uom_qty"]},
    )
    incoming = defaultdict(float)
    for m in moves:
        incoming[m["product_id"][0]] += m["product_uom_qty"]
    return incoming


def allocate_buildable(device_boms, component_onhand, weights):
    """
    Splits shared component stock between devices so the buildable numbers add up to something
    that can really be built. Many parts (QR, Date, the SC1800HT base unit...) appear in the BOMs
    of several devices; computing each device's buildable on its own lets every one of them
    "spend" the same stock, so the totals are impossible.

    Method - weighted progressive filling: every device starts at zero builds and all devices
    build together, each at a speed proportional to its weight (popularity). The moment a
    component runs out, every device that needs it stops. The rest keep going, using whatever
    stock is left, until nothing can grow. Consequences:
      - a scarce shared part is split in proportion to the weights (popular devices reserve more);
      - a device held back by its own scarce part stops early and the stock it can't use is
        released to the others, so nothing is stranded;
      - devices that share nothing end up with exactly their stand-alone maximum.

    device_boms:      {device: [(component, qty_per_unit), ...]}
    component_onhand: {component: on-hand qty, or None if the component isn't found in Odoo}
    weights:          {device: popularity weight (> 0)}
    Returns {device: {"units": int, "limitedBy": component or None}} (units rounded down, so the
    reservations never exceed stock).
    """
    boms = {d: [(c, q) for c, q in parts if q > 0] for d, parts in device_boms.items()}
    boms = {d: p for d, p in boms.items() if p}
    original = {}
    for parts in boms.values():
        for c, _ in parts:
            on_hand = component_onhand.get(c)
            original[c] = max(0.0, float(on_hand)) if on_hand is not None else 0.0
    remaining = dict(original)
    built = {d: 0.0 for d in boms}
    limited_by = {}
    active = set(boms)

    while active:
        rate = defaultdict(float)  # stock consumed per unit of "time" by the still-growing devices
        for d in active:
            for c, q in boms[d]:
                rate[c] += q * weights[d]
        t, c_star = min((remaining[c] / r, c) for c, r in rate.items())
        for d in active:
            built[d] += weights[d] * t
        for c, r in rate.items():
            remaining[c] -= r * t
        exhausted = {c for c in rate if remaining[c] <= 1e-9 * max(1.0, original[c])}
        exhausted.add(c_star)  # guarantees progress despite float rounding
        frozen = {d for d in active if any(c in exhausted for c, _ in boms[d])}
        for d in frozen:
            limited_by[d] = next(c for c, _ in boms[d] if c in exhausted)
        active -= frozen

    units = {d: int(math.floor(built[d] + 1e-6)) for d in boms}

    # Rounding sweep: rounding each device's share down throws away a fraction of a unit per
    # device, which can leave a few whole units' worth of parts that some device could still use.
    # Hand those out one unit at a time, most popular device first, until none can build another
    # (otherwise that waste would later be mistaken for extra capacity). Never exceeds stock.
    left = dict(original)
    for d, parts in boms.items():
        for c, q in parts:
            left[c] -= q * units[d]
    order = sorted(boms, key=lambda d: (-weights[d], -(built[d] - units[d])))
    for _ in range(100000):  # guard; the leftovers are tiny so this ends after a few rounds
        progressed = False
        for d in order:
            if all(left[c] >= q - 1e-9 for c, q in boms[d]):
                units[d] += 1
                for c, q in boms[d]:
                    left[c] -= q
                progressed = True
        if not progressed:
            break

    return {d: {"units": units[d], "limitedBy": limited_by.get(d)} for d in boms}


def compute_manufacturable(component_stock, component_incoming, popularity=None):
    """
    Buildable units per device from the components currently on hand.

    Shared components are RESERVED between devices by popularity (see allocate_buildable):
    `popularity` is {device: units shipped over the last 12 months}. Without it (or if nobody
    shipped anything) shared parts are split equally and allocationBasis says so.

    Each device's entry has:
      manufacturableQty  - the reserved share: what this device can build once the shared parts
                           are divided up. This is the number used everywhere on the dashboard,
                           and the shares add up to what can really be built.
      standaloneQty      - what it could build if it had every part to itself (the old number).
      allocationLimit    - the component whose exhaustion stopped this device's reserved builds.
      bottleneckComponent- the scarcest component when the device is considered on its own.
      components         - per component: stock, qty per unit, stand-alone buildable, how many
                           devices use it, and where its stock goes: reserved for this device /
                           for every other device / left over, first from parts on hand, then
                           from parts on order (reservedOthers, leftAfterReserve, incomingReserved,
                           incomingReservedOthers, leftAfterIncoming).
      incomingBuildableQty - EXTRA units this device could build once the parts that are on order
                           arrive, on top of manufacturableQty (second allocation pass, below).
      incomingLimit        - the component that limits those extra builds.
    Incoming parts never change manufacturableQty: that stays what's buildable from parts on
    hand today. They only feed incomingBuildableQty, since incoming stock isn't in hand yet and
    may still slip.
    """
    uses = defaultdict(int)  # how many devices use each component
    for parts in DEVICE_BOMS.values():
        for c, _ in parts:
            uses[c] += 1

    basis = "shipments" if popularity and any(v > 0 for v in popularity.values()) else "equal"
    # Floor of 1 unit so a device that shipped nothing still gets stock when it isn't contested
    # (its weight only matters when it competes for a scarce part, where it then gets ~nothing).
    weights = {d: (max(float(popularity.get(d, 0)), 1.0) if basis == "shipments" else 1.0) for d in DEVICE_BOMS}
    onhand = {c: (info["onHand"] if info is not None else None) for c, info in
              ((c, component_stock.get(c)) for parts in DEVICE_BOMS.values() for c, _ in parts)}
    allocation = allocate_buildable(DEVICE_BOMS, onhand, weights)

    # Second pass - parts on order. The builds reserved above are treated as decided; whatever is
    # left of each component, plus what's incoming, is then shared out the same way (by
    # popularity) for ADDITIONAL builds once the incoming parts arrive. Doing it as a second pass
    # means incoming parts can only add to a device's reserved builds, never take any away, and
    # no component is ever used beyond on hand + incoming.
    used = defaultdict(float)
    for device, parts in DEVICE_BOMS.items():
        units = allocation.get(device, {"units": 0})["units"]
        for c, q in parts:
            used[c] += q * units
    stock_after = {}
    for c, on_hand in onhand.items():
        info = component_stock.get(c)
        incoming_qty = component_incoming.get(info["id"], 0) if info is not None else 0
        stock_after[c] = None if on_hand is None else max(0.0, max(0.0, on_hand) - used[c]) + max(0.0, incoming_qty)
    incoming_allocation = allocate_buildable(DEVICE_BOMS, stock_after, weights)
    used_incoming = defaultdict(float)  # per component: total reserved by ALL devices' extra builds
    for device, parts in DEVICE_BOMS.items():
        extra_units = incoming_allocation.get(device, {"units": 0})["units"]
        for c, q in parts:
            used_incoming[c] += q * extra_units

    result = {}
    for device, parts in DEVICE_BOMS.items():
        alloc = allocation.get(device, {"units": 0, "limitedBy": None})
        extra_units = incoming_allocation.get(device, {"units": 0})["units"]
        limits = []
        for component_sku, qty_per_unit in parts:
            info = component_stock.get(component_sku)
            if info is None:
                limits.append({"component": component_sku, "buildable": 0, "missing": True, "incoming": 0,
                               "usedBy": uses[component_sku], "reserved": 0})
                continue
            on_hand = info["onHand"]
            incoming_qty = component_incoming.get(info["id"], 0)
            buildable = int(on_hand // qty_per_unit)
            limits.append({
                "component": component_sku, "onHand": on_hand,
                "qtyPerUnit": qty_per_unit, "buildable": buildable, "missing": False,
                "incoming": incoming_qty, "usedBy": uses[component_sku],
                # --- where this part's stock goes (all in units of the part) ---
                "reserved": qty_per_unit * alloc["units"],                      # this device, from parts on hand
                "reservedOthers": _clean_qty(used[component_sku] - qty_per_unit * alloc["units"]),  # every other device
                "leftAfterReserve": _clean_qty(max(0.0, max(0.0, on_hand) - used[component_sku])),
                "incomingReserved": qty_per_unit * extra_units,                 # this device, from parts on order
                "incomingReservedOthers": _clean_qty(used_incoming[component_sku] - qty_per_unit * extra_units),
                "leftAfterIncoming": _clean_qty(max(0.0,
                    max(0.0, max(0.0, on_hand) - used[component_sku]) + max(0.0, incoming_qty) - used_incoming[component_sku])),
            })
        standalone = min((l["buildable"] for l in limits), default=0)
        bottleneck = min(limits, key=lambda l: l["buildable"]) if limits else None
        result[device] = {
            "manufacturableQty": alloc["units"],
            "standaloneQty": standalone,
            "allocationLimit": alloc["limitedBy"],
            "allocationBasis": basis,
            "incomingBuildableQty": incoming_allocation.get(device, {"units": 0})["units"],
            "incomingLimit": incoming_allocation.get(device, {"limitedBy": None})["limitedBy"],
            "bottleneckComponent": bottleneck["component"] if bottleneck else None,
            "components": limits,
        }
    return result



def fetch_sales_history(uid, models, product_ids, warehouse_id):
    """Pull sale.order.line rows for the given products over the trailing HISTORY_MONTHS,
    scoped to orders fulfilled from warehouse_id."""
    since = _months_ago(datetime.today(), HISTORY_MONTHS).strftime("%Y-%m-%d")
    domain = [
        ["product_id", "in", product_ids],
        ["order_id.date_order", ">=", since],
        ["order_id.warehouse_id", "=", warehouse_id],
        # Only count confirmed sales orders, not drafts/quotations, so history reflects real demand.
        ["state", "in", ["sale", "done"]],
    ]
    fields = ["product_id", "product_uom_qty", "order_id"]
    lines = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "sale.order.line", "search_read", [domain], {"fields": fields}
    )
    # Need order dates - fetch them in a second pass keyed by sale.order id.
    order_ids = list({l["order_id"][0] for l in lines})
    orders = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "sale.order", "read", [order_ids], {"fields": ["date_order"]}
    )
    order_dates = {o["id"]: o["date_order"] for o in orders}

    for l in lines:
        l["date_order"] = order_dates.get(l["order_id"][0])
    return lines


def aggregate_monthly(lines, sku_by_product_id):
    """Group sale order lines into {sku: [{year, month, qty}, ...]}."""
    buckets = defaultdict(lambda: defaultdict(float))
    for l in lines:
        product_id = l["product_id"][0]
        sku = sku_by_product_id.get(product_id)
        if not sku or not l["date_order"]:
            continue
        dt = datetime.strptime(l["date_order"], "%Y-%m-%d %H:%M:%S")
        buckets[sku][(dt.year, dt.month)] += l["product_uom_qty"]

    series = {}
    for sku, monthly in buckets.items():
        keys = sorted(monthly.keys())
        if not keys:
            series[sku] = []
            continue
        (y0, m0), (y1, m1) = keys[0], keys[-1]
        cur_y, cur_m = y0, m0
        points = []
        while (cur_y, cur_m) <= (y1, m1):
            points.append({"year": cur_y, "month": cur_m, "qty": monthly.get((cur_y, cur_m), 0)})
            cur_m += 1
            if cur_m == 13:
                cur_m = 1
                cur_y += 1
        series[sku] = points
    return series


# Shared across every windowed metric on the Key Metrics tab: (key, amount, unit, label).
# Keys are used in the JSON output and must match exactly what the dashboard's JS expects.
WINDOW_DEFS = [
    ("1d", 1, "days"),
    ("1w", 7, "days"),
    ("1mo", 1, "months"),
    ("3mo", 3, "months"),
    ("6mo", 6, "months"),
    ("12mo", 12, "months"),
]


def _window_cutoff(reference, amount, unit):
    """Computes a cutoff datetime for either a day-based or calendar-month-based window,
    so day/week windows get exact arithmetic and month windows get exact calendar
    subtraction (see _months_ago) rather than a days-per-month approximation."""
    if unit == "days":
        return reference - timedelta(days=amount)
    if unit == "months":
        return _months_ago(reference, amount)
    raise ValueError(f"Unknown window unit: {unit}")


def _months_ago(reference, months):
    """
    Exact calendar-month subtraction (e.g., "12 months ago from today" = same date one
    year ago), rather than an approximation like timedelta(days=months*31). The
    days*31 shortcut drifts by several days per window (12*31=372 days, not 365) - for
    a "how many happened in the last year" count, that drift can shift which orders
    fall just inside or outside the boundary, throwing off an exact reconciliation
    against a manual count. Handles day-of-month overflow (e.g., Aug 31 minus 6 months
    lands on Feb 28/29, not an invalid Feb 31).
    """
    year = reference.year
    month = reference.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(reference.day, calendar.monthrange(year, month)[1])
    return reference.replace(year=year, month=month, day=day)


def _parse_odoo_datetime(value):
    """Odoo returns Datetime fields as 'YYYY-MM-DD HH:MM:SS' but Date fields as plain
    'YYYY-MM-DD' with no time component. Handles both so a field turning out to be one
    type or the other doesn't crash the parse."""
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return datetime.strptime(value, "%Y-%m-%d")


def _summarize_picking_durations(uid, models, picking_data, tracked_id_to_sku):
    """
    Shared tail logic for any "how long did X take" metric keyed by picking id, where
    picking_data is {picking_id: {"days": float, "completed": datetime}}. Handles:
      - IQR-based outlier filtering (see inline comment below)
      - classifying each picking's product composition (device-only / single-SKU)
      - summarizing into every window in WINDOW_DEFS (1 day / 1 week / 1/3/6/12 months)
      - a monthly trend series across the full lookback

    Returns {"windows": {...}, "byMonth": [...], "outlierFilter": {...}}, or the same
    shape with all-empty stats if picking_data is empty.
    """
    tracked_ids = set(tracked_id_to_sku.keys())
    empty = {"avgDays": None, "medianDays": None, "sampleSize": 0}
    if not picking_data:
        return {
            "windows": {key: {"all": empty, "deviceOnly": empty, "byDevice": {}} for key, _, _ in WINDOW_DEFS},
            "byMonth": [],
            "outlierFilter": {"applied": False, "excludedCount": 0, "thresholdDays": None},
        }

    # Filter out statistical outliers - some records have wildly implausible durations,
    # almost certainly data entry errors (backdated corrections, migrated records, etc.)
    # rather than real elapsed time, and left unfiltered they drag the whole average off,
    # especially for the smaller per-device samples. Uses the standard IQR (interquartile
    # range) method rather than a fixed day cutoff, so it adapts to the actual distribution
    # instead of guessing a "too many days" number: anything more than 1.5x the
    # interquartile range above the 75th percentile is excluded. Needs a reasonable sample
    # to compute stable quartiles from - skipped below that.
    all_days = [info["days"] for info in picking_data.values()]
    outlier_filter_info = {"applied": False, "excludedCount": 0, "thresholdDays": None}
    if len(all_days) >= 8:
        q1, _, q3 = statistics.quantiles(all_days, n=4)
        iqr = q3 - q1
        upper_fence = q3 + 1.5 * iqr
        before_count = len(picking_data)
        picking_data = {pid: info for pid, info in picking_data.items() if info["days"] <= upper_fence}
        excluded = before_count - len(picking_data)
        outlier_filter_info = {
            "applied": True,
            "excludedCount": excluded,
            "thresholdDays": round(upper_fence, 1),
        }

    # Determine each picking's product composition, to classify device-only/single-SKU vs mixed.
    moves = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "stock.move", "search_read",
        [[["picking_id", "in", list(picking_data.keys())]]],
        {"fields": ["picking_id", "product_id"]},
    )
    products_by_picking = defaultdict(set)
    for m in moves:
        products_by_picking[m["picking_id"][0]].add(m["product_id"][0])

    for pid, info in picking_data.items():
        products = products_by_picking.get(pid, set())
        info["deviceOnly"] = bool(products) and products.issubset(tracked_ids)
        if len(products) == 1:
            only_id = next(iter(products))
            info["sku"] = tracked_id_to_sku.get(only_id)  # None if that one product isn't tracked
        else:
            info["sku"] = None

    def summarize(records):
        if not records:
            return dict(empty)
        vals = [r["days"] for r in records]
        return {
            "avgDays": round(statistics.mean(vals), 1),
            "medianDays": round(statistics.median(vals), 1),
            "sampleSize": len(vals),
        }

    tracked_skus = sorted(set(tracked_id_to_sku.values()))
    now = datetime.today()
    windows_result = {}
    for key, amount, unit in WINDOW_DEFS:
        cutoff = _window_cutoff(now, amount, unit)
        in_window = [info for info in picking_data.values() if info["completed"] >= cutoff]
        device_only = [info for info in in_window if info["deviceOnly"]]
        by_device = {
            sku: summarize([info for info in in_window if info["sku"] == sku])
            for sku in tracked_skus
        }
        windows_result[key] = {
            "all": summarize(in_window),
            "deviceOnly": summarize(device_only),
            "byDevice": by_device,
        }

    by_month = defaultdict(list)
    by_week = defaultdict(list)
    for info in picking_data.values():
        by_month[(info["completed"].year, info["completed"].month)].append(info["days"])
        # ISO week (Monday-Sunday), not calendar week - standard, unambiguous convention
        iso_year, iso_week, _ = info["completed"].isocalendar()
        by_week[(iso_year, iso_week)].append(info["days"])
    monthly_avg = [
        {"year": y, "month": m, "avgDays": round(statistics.mean(vals), 1), "count": len(vals)}
        for (y, m), vals in sorted(by_month.items())
    ]
    weekly_avg = [
        {"year": y, "week": w, "avgDays": round(statistics.mean(vals), 1), "count": len(vals)}
        for (y, w), vals in sorted(by_week.items())
    ]

    return {
        "windows": windows_result,
        "byMonth": monthly_avg,
        "byWeek": weekly_avg,
        "outlierFilter": outlier_filter_info,
    }


def fetch_delivery_cycle_times(uid, models, warehouse_id, warehouse_location_id, tracked_id_to_sku):
    """
    Measures how long completed outgoing deliveries spent actually in process: from
    the moment the delivery became "Waiting" (assigned_date) to when it was fully
    processed (date_done, "Effective Date" in the UI) - not from record creation,
    which would also count time spent sitting unconfirmed/unassigned beforehand.

    Field name note: "assigned_date_0" / "date_done_0" as given have a "_0" suffix,
    which is typically an artifact Odoo appends to column headers in a CSV/XLSX export
    when two columns would otherwise share the same label - not how the field is
    actually named on the model itself. This tries the field names as given first,
    and falls back to the same names without the "_0" suffix if Odoo rejects them as
    unknown fields, so it works either way without needing another round trip.

    tracked_id_to_sku: {product_id: sku} for every tracked device, used both to
    classify "device-only" orders and to attribute single-device orders to a specific
    SKU for the per-device breakdown.

    Scoping is explicit: source location must be inside the WH location tree (covers
    WH/Stock and any sub-zones from multi-step routes) and destination must be a
    customer location (Odoo's standard usage == "customer" type), rather than relying
    solely on picking_type_id.warehouse_id ownership.

    Returns stats for every window in WINDOW_DEFS (1 day, 1 week, 1/3/6/12 months),
    each split into:
      - "all": every completed outgoing delivery, regardless of what was on it
      - "deviceOnly": deliveries where EVERY line item is one of the tracked device
        SKUs (any mix of tracked devices together still counts here) - no mixing
        with untracked products.
      - "byDevice": deliveries where EVERY line item is the SAME single tracked SKU -
        no mixing with untracked products, and no mixing with other tracked devices
        either. This is the only bucket that can be honestly attributed to one specific
        device, since a delivery's completion timestamp applies to the whole order, not
        per line - any order containing more than one distinct product (tracked or not)
        can't have its duration cleanly assigned to just one of them.
    """
    since_cutoff = _months_ago(datetime.today(), 12)  # longest window in WINDOW_DEFS

    domain = [
        ["picking_type_id.code", "=", "outgoing"],
        ["picking_type_id.warehouse_id", "=", warehouse_id],
        ["location_id", "child_of", warehouse_location_id],
        ["location_dest_id.usage", "=", "customer"],
        ["state", "=", "done"],
    ]

    field_candidates = [("assigned_date_0", "date_done_0"), ("assigned_date", "date_done")]
    pickings = None
    used_fields = None
    for assigned_field, done_field in field_candidates:
        try:
            pickings = models.execute_kw(
                ODOO_DB, uid, ODOO_API_KEY, "stock.picking", "search_read", [domain],
                {"fields": ["id", assigned_field, done_field]},
            )
            used_fields = (assigned_field, done_field)
            break
        except xmlrpc.client.Fault:
            continue

    if pickings is None:
        raise RuntimeError(
            'Could not find "assigned_date"/"date_done" (or the "_0" suffixed variants) '
            "on stock.picking. Check the exact field names in Odoo's developer mode."
        )

    assigned_field, done_field = used_fields
    total_matching_domain = len(pickings)

    # Build each picking's duration/completion date. Every exclusion reason is tracked
    # by completion date (when available) rather than as a single silent/global count,
    # so a manual reconciliation against Odoo has an exact, windowed answer for any gap:
    # "N were excluded for reason X within this specific window" rather than just a
    # vague total across all time.
    picking_data = {}
    missing_assigned_completions = []  # has date_done, but no assigned_date - unusable for duration, still "completed"
    negative_duration_completions = []  # has both dates, but assigned is after completed - bad data
    no_done_date_count = 0  # can't even tell which window this belongs to
    for p in pickings:
        if not p.get(done_field):
            no_done_date_count += 1
            continue
        completed = _parse_odoo_datetime(p[done_field])
        if not p.get(assigned_field):
            missing_assigned_completions.append(completed)
            continue
        assigned = _parse_odoo_datetime(p[assigned_field])
        if completed < since_cutoff:
            continue
        days = (completed - assigned).total_seconds() / 86400
        if days < 0:
            negative_duration_completions.append(completed)
            continue
        picking_data[p["id"]] = {"days": days, "completed": completed}

    # Raw ground-truth count per window: every picking matching the domain that HAS a
    # completion date, regardless of whether its duration was computable - this is the
    # number that should match a manual count of "completed deliveries to customers" in
    # Odoo's UI filtered by the same date range, state, and location.
    now = datetime.today()
    raw_completed_by_window = {}
    for key, amount, unit in WINDOW_DEFS:
        cutoff = _window_cutoff(now, amount, unit)
        raw_count = sum(1 for info in picking_data.values() if info["completed"] >= cutoff)
        raw_count += sum(1 for c in missing_assigned_completions if c >= cutoff)
        raw_count += sum(1 for c in negative_duration_completions if c >= cutoff)
        raw_completed_by_window[key] = raw_count

    result = _summarize_picking_durations(uid, models, picking_data, tracked_id_to_sku)
    result["reconciliation"] = {
        "totalMatchingDomain": total_matching_domain,
        "rawCompletedByWindow": raw_completed_by_window,
        "noDoneDateCount": no_done_date_count,
        "missingAssignedDateCount": len(missing_assigned_completions),
        "negativeDurationCount": len(negative_duration_completions),
    }
    return result


def _age_bucket_key(days):
    """Order age groups: 0-90, 90-180, 180-360 and 360+ days. A boundary day belongs to the older
    group (exactly 90 days is in 90-180)."""
    if days < 90:
        return "under90"
    if days < 180:
        return "90to180"
    if days < 360:
        return "180to360"
    return "over360"


def _clean_qty(value):
    """Whole numbers stay ints; fractional quantities keep 2 decimals."""
    value = float(value)
    return int(value) if value.is_integer() else round(value, 2)


def _bucket_order_ages(picking_data):
    """
    Buckets each picking's "days" value into age ranges (0-90 / 90-180 / 180-360 / 360+ days),
    per window in WINDOW_DEFS, based on the picking's completion date. Each bucket carries
    two measures: the top-level keys count delivery ORDERS, and the nested "units" dict
    counts tracked DEVICE units on those orders (a picking's units all take that
    picking's age). Deliberately does NOT apply the IQR outlier filter used elsewhere -
    a genuinely long-open order is exactly what the oldest bucket is meant to surface.
    """
    now = datetime.today()
    windows_result = {}
    for key, amount, unit in WINDOW_DEFS:
        cutoff = _window_cutoff(now, amount, unit)
        in_window = [info for info in picking_data.values() if info["completed"] >= cutoff]
        counts = {"under90": 0, "90to180": 0, "180to360": 0, "over360": 0}
        units = {"under90": 0, "90to180": 0, "180to360": 0, "over360": 0}
        for info in in_window:
            k = _age_bucket_key(info["days"])
            counts[k] += 1
            units[k] += info.get("units", 0)
        counts["total"] = len(in_window)
        units["total"] = sum(units.values())
        counts["units"] = {k: _clean_qty(v) for k, v in units.items()}
        windows_result[key] = counts
    return windows_result


def _bucket_order_ages_by_week(picking_data):
    """
    Same age-bucket classification as _bucket_order_ages, but grouped by the picking's
    completion week (ISO week, Monday-Sunday) instead of collapsed into fixed windows -
    a week-by-week trend for charting, again with order counts at the top level and
    device-unit counts under "units". Also skips the IQR outlier filter, for the same
    reason: a genuinely long-open order belongs in the >90 day bucket, not filtered out.
    """
    blank = lambda: {"under90": 0, "90to180": 0, "180to360": 0, "over360": 0}
    by_week_orders = defaultdict(blank)
    by_week_units = defaultdict(blank)
    for info in picking_data.values():
        iso_year, iso_week, _ = info["completed"].isocalendar()
        k = _age_bucket_key(info["days"])
        by_week_orders[(iso_year, iso_week)][k] += 1
        by_week_units[(iso_year, iso_week)][k] += info.get("units", 0)

    result = []
    for (y, w), counts in sorted(by_week_orders.items()):
        units = by_week_units[(y, w)]
        entry = {"year": y, "week": w}
        entry.update(counts)
        entry["total"] = sum(counts.values())
        entry["units"] = {k: _clean_qty(v) for k, v in units.items()}
        entry["units"]["total"] = _clean_qty(sum(units.values()))
        result.append(entry)
    return result


def fetch_order_to_fulfillment_times(uid, models, warehouse_id, warehouse_location_id, tracked_id_to_sku):
    """
    Breaks down completed deliveries by how long the underlying SALES ORDER was open
    before it shipped: from the sale.order's date_order (order date) to the linked
    delivery's date_done (Effective Date), bucketed into <30 / 30-60 / 60-90 / >90 days.
    This is a bigger-picture measure than delivery cycle time above - it includes time
    the order spent waiting before the delivery even started processing, not just the
    delivery's own processing time.

    Only pickings with a linked sale order (stock.picking.sale_id, Odoo's standard
    "Source Document" field set when a delivery is generated from a confirmed sales
    order) can be measured this way - pickings created without a sales order behind them
    (manual transfers, etc.) are excluded and don't have a comparable start date.

    Scoping is explicit: source location must be inside the WH location tree and
    destination must be a customer location (Odoo's standard usage == "customer" type),
    rather than relying solely on picking_type_id.warehouse_id ownership.

    Coverage (totalLinkedToSalesOrder / unlinkedCount) is computed PER WINDOW, using each
    delivery's own completion date, so it always matches whatever window's bucket totals
    are being displayed - these used to be a single all-time count regardless of which
    window was selected, which could show a coverage figure that didn't match the bucket
    total next to it.
    """
    since_cutoff = _months_ago(datetime.today(), 12)  # longest window in WINDOW_DEFS

    field_candidates = ["date_done_0", "date_done"]
    linked_domain = [
        ["picking_type_id.code", "=", "outgoing"],
        ["picking_type_id.warehouse_id", "=", warehouse_id],
        ["location_id", "child_of", warehouse_location_id],
        ["location_dest_id.usage", "=", "customer"],
        ["state", "=", "done"],
        ["sale_id", "!=", False],
    ]
    pickings = None
    used_field = None
    for done_field in field_candidates:
        try:
            pickings = models.execute_kw(
                ODOO_DB, uid, ODOO_API_KEY, "stock.picking", "search_read", [linked_domain],
                {"fields": ["id", done_field, "sale_id"]},
            )
            used_field = done_field
            break
        except xmlrpc.client.Fault:
            continue

    if pickings is None:
        raise RuntimeError(
            'Could not find "date_done" (or the "_0" suffixed variant) on stock.picking. '
            "Check the exact field name in Odoo's developer mode."
        )

    # Every linked delivery's completion date, regardless of whether its sale order turned
    # out to have a usable date_order - used for the per-window "how many had a sales order
    # attached at all" coverage figure, which is a broader set than the bucketed ages below.
    linked_completions = []
    for p in pickings:
        if p.get(used_field):
            linked_completions.append(_parse_odoo_datetime(p[used_field]))

    pickings_with_dates = [p for p in pickings if p.get(used_field) and p.get("sale_id")]
    sale_ids = list({p["sale_id"][0] for p in pickings_with_dates})

    sale_orders = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "sale.order", "read", [sale_ids], {"fields": ["date_order"]}
    ) if sale_ids else []
    order_date_by_id = {so["id"]: so["date_order"] for so in sale_orders if so.get("date_order")}

    picking_data = {}
    skipped_no_order_date = 0
    for p in pickings_with_dates:
        so_id = p["sale_id"][0]
        order_date_str = order_date_by_id.get(so_id)
        if not order_date_str:
            skipped_no_order_date += 1
            continue
        ordered = _parse_odoo_datetime(order_date_str)
        completed = _parse_odoo_datetime(p[used_field])
        if completed < since_cutoff:
            continue
        days = (completed - ordered).total_seconds() / 86400
        if days < 0:
            continue  # skip bad data rather than let it skew the buckets
        picking_data[p["id"]] = {"days": days, "completed": completed, "units": 0}

    # Device units per delivery: sum the quantity of TRACKED device products on each
    # picking's move lines (accessories and other untracked products don't count). The
    # quantity field name differs between Odoo versions - "quantity" in 17+, "quantity_done"
    # before that - so try each in turn, falling back to demanded qty as a last resort.
    if picking_data and tracked_id_to_sku:
        tracked_ids = set(tracked_id_to_sku.keys())
        move_rows = None
        qty_field = None
        for candidate in ["quantity", "quantity_done", "product_uom_qty"]:
            try:
                move_rows = models.execute_kw(
                    ODOO_DB, uid, ODOO_API_KEY, "stock.move", "search_read",
                    [[["picking_id", "in", list(picking_data.keys())]]],
                    {"fields": ["picking_id", "product_id", candidate]},
                )
                qty_field = candidate
                break
            except xmlrpc.client.Fault:
                continue
        if move_rows is not None:
            for m in move_rows:
                if m["product_id"] and m["product_id"][0] in tracked_ids:
                    picking_data[m["picking_id"][0]]["units"] += m.get(qty_field) or 0

    # Same pattern for unlinked deliveries (no sale order at all) - need their completion
    # dates too now, not just a single all-time count, to bucket coverage per window.
    unlinked_domain = [
        ["picking_type_id.code", "=", "outgoing"],
        ["picking_type_id.warehouse_id", "=", warehouse_id],
        ["location_id", "child_of", warehouse_location_id],
        ["location_dest_id.usage", "=", "customer"],
        ["state", "=", "done"],
        ["sale_id", "=", False],
    ]
    unlinked_completions = []
    for done_field in field_candidates:
        try:
            unlinked_pickings = models.execute_kw(
                ODOO_DB, uid, ODOO_API_KEY, "stock.picking", "search_read", [unlinked_domain],
                {"fields": [done_field]},
            )
            unlinked_completions = [
                _parse_odoo_datetime(p[done_field]) for p in unlinked_pickings if p.get(done_field)
            ]
            break
        except xmlrpc.client.Fault:
            continue

    now = datetime.today()
    coverage_by_window = {}
    for key, amount, unit in WINDOW_DEFS:
        cutoff = _window_cutoff(now, amount, unit)
        linked_in_window = sum(1 for c in linked_completions if c >= cutoff)
        unlinked_in_window = sum(1 for c in unlinked_completions if c >= cutoff)
        coverage_by_window[key] = {"totalLinkedToSalesOrder": linked_in_window, "unlinkedCount": unlinked_in_window}

    return {
        "windows": _bucket_order_ages(picking_data),
        "byWeek": _bucket_order_ages_by_week(picking_data),
        "coverage": coverage_by_window,
        "skippedMissingOrderDate": skipped_no_order_date,
    }


def fetch_shipped_units(uid, models, warehouse_id, warehouse_location_id, tracked_ids, since):
    """
    Units of each tracked device delivered to customers from this warehouse since `since`:
    completed outgoing pickings (source inside the WH tree, destination a customer location),
    summing the delivered quantity on their move lines. Unlike the sales-order-age metric this
    includes deliveries with no sales order attached - turns should reflect everything that
    physically left the warehouse. Returns ({product_id: units}, quantity_field_used).
    """
    domain = [
        ["picking_type_id.code", "=", "outgoing"],
        ["picking_type_id.warehouse_id", "=", warehouse_id],
        ["location_id", "child_of", warehouse_location_id],
        ["location_dest_id.usage", "=", "customer"],
        ["state", "=", "done"],
    ]
    pickings = None
    done_field = None
    for candidate in ["date_done_0", "date_done"]:
        try:
            pickings = models.execute_kw(
                ODOO_DB, uid, ODOO_API_KEY, "stock.picking", "search_read", [domain],
                {"fields": ["id", candidate]},
            )
            done_field = candidate
            break
        except xmlrpc.client.Fault:
            continue
    if pickings is None:
        raise RuntimeError('Could not read "date_done" on stock.picking for the turns calculation.')

    picking_ids = [
        p["id"] for p in pickings
        if p.get(done_field) and _parse_odoo_datetime(p[done_field]) >= since
    ]
    if not picking_ids or not tracked_ids:
        return {}, None

    # Quantity field name differs between Odoo versions (see fetch_order_to_fulfillment_times)
    for qty_field in ["quantity", "quantity_done", "product_uom_qty"]:
        try:
            moves = models.execute_kw(
                ODOO_DB, uid, ODOO_API_KEY, "stock.move", "search_read",
                [[["picking_id", "in", picking_ids], ["product_id", "in", list(tracked_ids)]]],
                {"fields": ["product_id", qty_field]},
            )
            break
        except xmlrpc.client.Fault:
            moves = None
            continue
    if moves is None:
        raise RuntimeError("Could not read a quantity field on stock.move for the turns calculation.")

    shipped = defaultdict(float)
    for m in moves:
        shipped[m["product_id"][0]] += m.get(qty_field) or 0
    return dict(shipped), qty_field


def fetch_onhand_snapshots(uid, models, warehouse_location_id, product_ids, months=12):
    """
    On-hand per product at today and at the same calendar day of each of the previous
    `months` months (months+1 points), using Odoo's as-of-date stock lookup (the "to_date"
    context key), scoped to the warehouse's location tree. Returns a list of
    (datetime, {product_id: qty}), oldest first.
    """
    now = datetime.today()
    snapshots = []
    for k in range(months, -1, -1):
        at = _months_ago(now, k) if k else now
        ctx = {"location": warehouse_location_id}
        if k:
            ctx["to_date"] = at.strftime("%Y-%m-%d") + " 23:59:59"
        rows = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY, "product.product", "search_read",
            [[["id", "in", list(product_ids)]]],
            {"fields": ["id", "qty_available"], "context": ctx},
        )
        snapshots.append((at, {r["id"]: r["qty_available"] for r in rows}))
    return snapshots


def fetch_reorder_rules(uid, models, warehouse_id, product_ids):
    """
    Odoo's own reordering rules (stock.warehouse.orderpoint) for the tracked devices in this
    warehouse: the configured min/max stock levels, if anyone has set them up. A device's
    max quantity is treated as its PAR level. If a device has several rules in this warehouse
    (e.g. one per sub-location) the quantities are summed, since the dashboard's on-hand is
    warehouse-wide. Rules with a max of 0 are ignored - Odoo uses that for "order up to the
    minimum", which isn't a PAR level. Returns {product_id: {"min": x, "max": y}}.
    """
    rows = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "stock.warehouse.orderpoint", "search_read",
        [[["product_id", "in", list(product_ids)], ["warehouse_id", "=", warehouse_id]]],
        {"fields": ["product_id", "product_min_qty", "product_max_qty"]},
    )
    rules = {}
    for r in rows:
        max_qty = r.get("product_max_qty") or 0
        if max_qty <= 0:
            continue
        entry = rules.setdefault(r["product_id"][0], {"min": 0, "max": 0})
        entry["min"] += r.get("product_min_qty") or 0
        entry["max"] += max_qty
    return rules


# Fields read from product.supplierinfo (the vendor pricelist on a product's Purchase tab). Only the
# ones that exist on this Odoo version are requested.
_SUPPLIERINFO_FIELDS = ["partner_id", "product_tmpl_id", "product_id", "delay", "min_qty", "price",
                        "sequence", "date_start", "date_end"]


def _supplierinfo_applies(rec, product_id, today):
    """True if a vendor pricelist line applies to this product variant today."""
    variant = rec.get("product_id")
    if variant and variant[0] != product_id:
        return False  # the line is for a different variant of the same template
    for key, is_start in (("date_start", True), ("date_end", False)):
        raw = rec.get(key)
        if not raw:
            continue
        try:
            day = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if (is_start and day > today) or (not is_start and day < today):
            return False
    return True


def fetch_vendor_lead_times(uid, models, products_by_sku, component_stock, today=None):
    """
    Each tracked device's and each bill-of-materials part's vendors and delivery lead times, from
    the vendor pricelist on the product's Purchase tab (Odoo's product.supplierinfo, "Delivery
    Lead Time" in days).

    Returns {"byDevice": {sku: [vendor, ...]}, "byComponent": {code: [vendor, ...]}, "asOf": date}
    where vendor = {"vendor": name, "vendorId": id, "leadDays": days or None, "minQty": n,
    "isDefault": bool}. Vendors come in Odoo's own order, so the first one is Odoo's default
    (lowest sequence). Lines that are expired or not yet valid, or that belong to a different
    variant, are left out. A lead time of 0 is reported as None (not set). Each vendor appears
    once, at its highest-priority line. Products with no vendor are simply absent.
    """
    today = today or datetime.today().date()
    wanted = {}  # product id -> [(group, code)]
    for sku, p in products_by_sku.items():
        wanted.setdefault(p["id"], []).append(("byDevice", sku))
    for code, info in component_stock.items():
        wanted.setdefault(info["id"], []).append(("byComponent", code))
    result = {"byDevice": {}, "byComponent": {}, "asOf": today.isoformat()}
    if not wanted:
        return result

    products = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "product.product", "read", [list(wanted)], {"fields": ["product_tmpl_id"]},
    )
    tmpl_by_product = {r["id"]: r["product_tmpl_id"][0] for r in products if r.get("product_tmpl_id")}

    available = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "product.supplierinfo", "fields_get", [], {"attributes": ["type"]},
    )
    for needed in ("partner_id", "product_tmpl_id", "delay"):
        if needed not in available:
            raise RuntimeError(f"this Odoo version has no '{needed}' field on vendor pricelists")
    fields = [f for f in _SUPPLIERINFO_FIELDS if f in available]

    rows = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "product.supplierinfo", "search_read",
        [[["product_tmpl_id", "in", sorted(set(tmpl_by_product.values()))]]], {"fields": fields},
    )
    by_template = {}
    for r in rows:
        if r.get("product_tmpl_id"):
            by_template.setdefault(r["product_tmpl_id"][0], []).append(r)

    for product_id, targets in wanted.items():
        recs = [r for r in by_template.get(tmpl_by_product.get(product_id), []) if _supplierinfo_applies(r, product_id, today)]
        # Odoo's own ordering for vendor lines: sequence, then larger minimum quantity, then price.
        recs.sort(key=lambda r: (r.get("sequence") or 0, -(r.get("min_qty") or 0), r.get("price") or 0, r["id"]))
        vendors, seen = [], set()
        for r in recs:
            partner = r.get("partner_id")
            if not partner or partner[0] in seen:
                continue
            seen.add(partner[0])
            days = r.get("delay")
            vendors.append({
                "vendor": partner[1],
                "vendorId": partner[0],
                "leadDays": int(days) if days and days > 0 else None,
                "minQty": r.get("min_qty") or 0,
                "isDefault": not vendors,
            })
        if vendors:
            for group, code in targets:
                result[group][code] = vendors
    return result


def _vendors_or_error(uid, models, products_by_sku, component_stock):
    """Vendor lead times are optional: if this Odoo user can't read vendor pricelists, or the fields
    differ on this version, report that in the dashboard instead of failing the whole refresh."""
    try:
        return fetch_vendor_lead_times(uid, models, products_by_sku, component_stock)
    except Exception as e:
        return {"error": str(e)[:400]}


def fetch_warehouse_turns(uid, models, warehouse_id, warehouse_location_id, products_by_sku, shipped_lookup=None):
    """
    Unit-based warehouse (inventory) turns over the trailing 12 months:
        turns          = units shipped to customers / average on-hand
        days of stock  = 365 / turns
    Average on-hand is the mean of 13 snapshots (today and the same day of each of the
    previous 12 months) - a snapshot-based approximation, not a continuous daily average.
    Computed per device and for the warehouse overall (total shipped / total average on-hand,
    so high-volume devices carry proportionally more weight in the total).
    """
    months = 12
    since = _months_ago(datetime.today(), months)
    product_ids = [p["id"] for p in products_by_sku.values()]
    shipped_by_id, qty_field = shipped_lookup or fetch_shipped_units(
        uid, models, warehouse_id, warehouse_location_id, set(product_ids), since
    )
    snapshots = fetch_onhand_snapshots(uid, models, warehouse_location_id, product_ids, months)

    # Optional: actual PAR levels from Odoo reordering rules. The Inventory app may not
    # expose them (or they may simply not be set up) - either way turns still work.
    rules, rules_error = {}, None
    try:
        rules = fetch_reorder_rules(uid, models, warehouse_id, product_ids)
    except Exception as e:
        rules_error = str(e)[:200]

    by_device = {}
    total_shipped = 0.0
    total_avg = 0.0
    for sku, p in products_by_sku.items():
        pid = p["id"]
        values = [snap.get(pid, 0) for _, snap in snapshots]
        avg_on_hand = statistics.mean(values) if values else 0
        shipped = shipped_by_id.get(pid, 0)
        turns = (shipped / avg_on_hand) if avg_on_hand > 0 else None
        days = (365 / turns) if turns else None
        total_shipped += shipped
        total_avg += avg_on_hand
        by_device[sku] = {
            "name": p["name"],
            "shipped": _clean_qty(shipped),
            "avgOnHand": round(avg_on_hand, 1),
            "odooPar": _clean_qty(rules[pid]["max"]) if pid in rules else None,
            "odooMin": _clean_qty(rules[pid]["min"]) if pid in rules else None,
            "turns": round(turns, 2) if turns is not None else None,
            "daysOnHand": round(days, 1) if days is not None else None,
        }

    total_turns = (total_shipped / total_avg) if total_avg > 0 else None
    total_days = (365 / total_turns) if total_turns else None

    # Safety net: if Odoo ignored the as-of-date lookup, every snapshot would equal today's
    # on-hand. With a year of shipments that is essentially impossible for real data.
    distinct_snapshots = {tuple(sorted(snap.items())) for _, snap in snapshots}
    history_looks_flat = total_shipped > 0 and len(distinct_snapshots) == 1

    return {
        "periodMonths": months,
        "quantityBasis": qty_field,
        "snapshotCount": len(snapshots),
        "historyLooksFlat": history_looks_flat,
        "reorderRulesError": rules_error,
        "byDevice": by_device,
        "total": {
            "shipped": _clean_qty(total_shipped),
            "avgOnHand": round(total_avg, 1),
            "turns": round(total_turns, 2) if total_turns is not None else None,
            "daysOnHand": round(total_days, 1) if total_days is not None else None,
        },
    }


def fetch_all():
    """Runs the full fetch pipeline against Odoo and returns the export dict.
    Reusable by both the CLI entry point and the dashboard server."""
    uid, models = connect()
    warehouse = fetch_warehouse(uid, models)
    warehouse_id = warehouse["id"]
    products_by_sku = fetch_products(uid, models, warehouse)

    missing = [s for s in PRODUCT_SKUS if s not in products_by_sku]

    product_ids = [p["id"] for p in products_by_sku.values()]
    sku_by_product_id = {p["id"]: sku for sku, p in products_by_sku.items()}

    lines = fetch_sales_history(uid, models, product_ids, warehouse_id)
    monthly_series = aggregate_monthly(lines, sku_by_product_id)

    confirmed_by_id, unconfirmed_by_id, confirmed_orders_by_id = fetch_outgoing(uid, models, product_ids, warehouse_id, warehouse["location_id"])

    component_stock, missing_components = fetch_component_stock(uid, models, warehouse)
    component_ids = [info["id"] for info in component_stock.values()]
    component_incoming = fetch_component_incoming(uid, models, warehouse_id, component_ids)
    # Popularity for splitting shared components: units shipped to customers over the last
    # 12 months. The same lookup feeds the turns calculation below, so it's only done once. If it
    # fails, shared components are split equally instead of failing the whole refresh.
    shipped_lookup, popularity = None, None
    try:
        shipped_lookup = fetch_shipped_units(
            uid, models, warehouse_id, warehouse["location_id"], set(product_ids),
            _months_ago(datetime.today(), 12),
        )
        popularity = {sku: shipped_lookup[0].get(p["id"], 0) for sku, p in products_by_sku.items()}
    except Exception:
        shipped_lookup, popularity = None, None
    manufacturable = compute_manufacturable(component_stock, component_incoming, popularity)

    inventory = {
        sku: {
            "name": p["name"],
            "onHand": p["qty_available"],
            "outgoingConfirmed": confirmed_by_id.get(p["id"], 0),
            "outgoingUnconfirmed": unconfirmed_by_id.get(p["id"], 0),
            "manufacturable": manufacturable.get(sku),
            "confirmedOrders": confirmed_orders_by_id.get(p["id"], []),
        }
        for sku, p in products_by_sku.items()
    }

    # Fill in empty series for any tracked SKU with zero sales history, so the
    # dashboard can flag it for manual input rather than erroring out.
    for sku in PRODUCT_SKUS:
        monthly_series.setdefault(sku, [])

    tracked_id_to_sku = {pid: sku_by_product_id[pid] for pid in product_ids}
    delivery_cycle_times = fetch_delivery_cycle_times(uid, models, warehouse_id, warehouse["location_id"], tracked_id_to_sku)
    order_to_fulfillment_times = fetch_order_to_fulfillment_times(uid, models, warehouse_id, warehouse["location_id"], tracked_id_to_sku)

    # Optional metric: if the as-of-date stock lookup or quantity fields don't work on this
    # Odoo version, report the error in the dashboard instead of failing the whole refresh.
    try:
        warehouse_turns = fetch_warehouse_turns(uid, models, warehouse_id, warehouse["location_id"], products_by_sku, shipped_lookup)
    except Exception as e:
        warehouse_turns = {"error": str(e)}

    vendors = _vendors_or_error(uid, models, products_by_sku, component_stock)

    return {
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "warehouse": warehouse["name"],
        "warehouse_code": WAREHOUSE_CODE,
        "missing_skus": missing,
        "missing_components": missing_components,
        "monthly_series": monthly_series,
        "inventory": inventory,
        "delivery_cycle_times": delivery_cycle_times,
        "order_to_fulfillment_times": order_to_fulfillment_times,
        "warehouse_turns": warehouse_turns,
        "vendors": vendors,
    }


def main():
    output = fetch_all()
    if output["missing_skus"]:
        print(f"Warning: no product found in Odoo for SKUs: {output['missing_skus']}")
    if output["missing_components"]:
        print(f"Warning: no product found in Odoo for component SKUs: {output['missing_components']}")

    os.makedirs("output", exist_ok=True)
    with open("output/odoo_export.json", "w") as f:
        json.dump(output, f, indent=2)

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
