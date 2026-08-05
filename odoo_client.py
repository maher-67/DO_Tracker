"""
Read-only client for pulling delivery order (stock.picking) data from Odoo.

This module intentionally exposes no write/create/unlink methods. It should be
paired with an Odoo API user that itself only has read access to stock.picking
(and res.partner) at the server level -- see README.md for how to set that up.
Even if this code were modified or misused, the Odoo-side permissions are the
real safety net.
"""

import os
import xmlrpc.client
from dataclasses import dataclass

# Confirmed against the live Odoo instance.
OPS_STATUS_FIELD = "hex_ops_state"
OPS_STATUS_VALUE = "in_process"

# Only pull orders whose SOURCE location is one of these (adjust if your
# warehouse codes differ -- check the picking's "Source Location" field).
SOURCE_LOCATION_NAMES = ["WH/Stock", "WH/Output"]

# Confirmed against the live Odoo instance.
PROJECT_MANAGER_FIELD = "x_studio_project_manager"

# The specific products to show on the Inventory tab. Use each product's
# Internal Reference / SKU exactly as it appears in Odoo (Sales > Products,
# the "Internal Reference" field). Add or remove SKUs as needed.
INVENTORY_SKUS = [
    "HEX-G",
    "HEX-X-R",
    "HEX-X-G",
    "HEX-X-B-R",
    "HEX-C-R",
    "HEX-C-G",
    "HEX-C-B-R",
    "HEX-W",
    "HEX-A-1",
    "HEX-A-2",
    "HEX-A-R-1",
    "HEX-A-R-2",
    "HEX-A-C-1",
    "HEX-A-C-2",
    "HEX-N",
    "HEX-P",
    "HEX-M",
    "HEX-T-C",
    "HEX-T-R",
    "HEX-T-ULT",
    "HEX-L-S",
    "HEX-L-Z",
]

# Inventory counts sum stock across this location AND everything nested under
# it (Stock, Output, Input, Quality Control, staging areas, bins, etc.) -- use
# the top-level warehouse location (its complete_name is normally just the
# warehouse's short code, e.g. "WH") rather than trying to enumerate specific
# sub-locations by name, which is easy to undercount. If you have more than
# one warehouse and want inventory scoped to just one of them, list only
# that warehouse's code here.
INVENTORY_LOCATION_NAMES = ["WH"]


def _display_value(raw):
    """Odoo returns many2one fields as [id, name] and plain fields as-is."""
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return raw[1]
    return raw or ""


@dataclass
class DeliveryOrder:
    id: int
    name: str
    partner_name: str
    scheduled_date: str
    state: str
    ops_status: str
    project_manager: str


@dataclass
class InventoryItem:
    product_id: int
    sku: str
    name: str
    on_hand: float
    reserved: float
    available: float
    uom: str


class OdooReadOnlyClient:
    def __init__(self):
        self.url = os.environ["ODOO_URL"].rstrip("/")
        self.db = os.environ["ODOO_DB"]
        self.username = os.environ["ODOO_USERNAME"]
        self.api_key = os.environ["ODOO_API_KEY"]

        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")
        self._uid = None
        self._reserved_field_cache = None

    def _authenticate(self):
        if self._uid is None:
            self._uid = self._common.authenticate(
                self.db, self.username, self.api_key, {}
            )
            if not self._uid:
                raise RuntimeError(
                    "Odoo authentication failed. Check ODOO_URL/ODOO_DB/"
                    "ODOO_USERNAME/ODOO_API_KEY."
                )
        return self._uid

    def _read_only_execute(self, model, method, *args, **kwargs):
        # Guardrail: this client should only ever call read-style methods.
        allowed_methods = {"search", "search_read", "read", "fields_get"}
        if method not in allowed_methods:
            raise PermissionError(
                f"Method '{method}' is not permitted by this read-only client."
            )
        uid = self._authenticate()
        return self._models.execute_kw(
            self.db, uid, self.api_key, model, method, list(args), kwargs
        )

    def get_active_deliveries(self):
        """
        Pull delivery orders (outgoing transfers) that are BOTH in the
        "Ready" state AND have Ops Status = "Shipment in Progress", sourced
        from WH/Stock. Returns a list of DeliveryOrder objects.
        """
        domain = [
            ("picking_type_id.code", "=", "outgoing"),
            ("state", "=", "assigned"),  # "assigned" is Odoo's internal key for "Ready"
            (OPS_STATUS_FIELD, "=", OPS_STATUS_VALUE),
            ("location_id.complete_name", "in", SOURCE_LOCATION_NAMES),
        ]
        fields = [
            "id", "name", "partner_id", "scheduled_date", "state",
            OPS_STATUS_FIELD, PROJECT_MANAGER_FIELD,
        ]

        records = self._read_only_execute(
            "stock.picking", "search_read", domain, fields
        )

        orders = []
        for r in records:
            partner_name = _display_value(r.get("partner_id"))
            orders.append(
                DeliveryOrder(
                    id=r["id"],
                    name=r["name"],
                    partner_name=partner_name,
                    scheduled_date=r.get("scheduled_date") or "",
                    state=r["state"],
                    ops_status=r.get(OPS_STATUS_FIELD) or "",
                    project_manager=_display_value(r.get(PROJECT_MANAGER_FIELD)),
                )
            )
        return orders

    def get_order_lines(self, picking_ids):
        """
        Returns {picking_id: [{"product": ..., "quantity": ..., "uom": ...}, ...]}
        for the given list of stock.picking ids, pulled from stock.move.
        """
        if not picking_ids:
            return {}

        domain = [
            ("picking_id", "in", picking_ids),
            ("state", "!=", "cancel"),
        ]
        fields = ["picking_id", "product_id", "product_uom_qty", "product_uom"]

        records = self._read_only_execute("stock.move", "search_read", domain, fields)

        lines_by_picking = {}
        for r in records:
            picking_id = r["picking_id"][0] if r.get("picking_id") else None
            if picking_id is None:
                continue
            product_name = r["product_id"][1] if r.get("product_id") else "Unknown product"
            uom_name = r["product_uom"][1] if r.get("product_uom") else ""
            lines_by_picking.setdefault(picking_id, []).append(
                {
                    "product": product_name,
                    "quantity": r.get("product_uom_qty", 0),
                    "uom": uom_name,
                }
            )
        return lines_by_picking

    def _resolve_reserved_field(self):
        """
        Different Odoo versions have used different field names for "how
        much of this move's demand is currently backed by reserved stock" on
        stock.move -- check which one this instance actually has, in order
        of preference, and cache the answer.
        """
        if self._reserved_field_cache is not None:
            return self._reserved_field_cache or None

        candidates = ["reserved_availability", "quantity"]
        fields_info = self._read_only_execute("stock.move", "fields_get", [], {"attributes": []})
        for candidate in candidates:
            if candidate in fields_info:
                self._reserved_field_cache = candidate
                return candidate

        self._reserved_field_cache = ""  # remember "none found" so we don't retry every call
        return None

    def get_inventory(self):
        """
        Returns a list of InventoryItem for each SKU in INVENTORY_SKUS.
        on_hand sums physical quantity across INVENTORY_LOCATION_NAMES and
        everything nested underneath. reserved sums only what's committed to
        outgoing delivery orders that haven't shipped yet -- not Odoo's raw
        quant-level reserved figure, which also includes internal transfers,
        manufacturing consumption, etc. Products with no stock at all still
        appear, with zeros, so nothing silently disappears from the list.
        """
        if not INVENTORY_SKUS:
            return []

        products = self._read_only_execute(
            "product.product",
            "search_read",
            [("default_code", "in", INVENTORY_SKUS)],
            ["id", "default_code", "name", "uom_id"],
        )
        if not products:
            return []

        product_ids = [p["id"] for p in products]

        # Resolve INVENTORY_LOCATION_NAMES to actual location ids first, then
        # use child_of so stock sitting anywhere underneath (Stock, Output,
        # Input, bins, staging areas, etc.) is included -- an exact name
        # match on complete_name would silently miss those and undercount.
        top_locations = self._read_only_execute(
            "stock.location",
            "search_read",
            [("complete_name", "in", INVENTORY_LOCATION_NAMES)],
            ["id"],
        )
        location_ids = [loc["id"] for loc in top_locations]
        if not location_ids:
            return []

        quants = self._read_only_execute(
            "stock.quant",
            "search_read",
            [
                ("product_id", "in", product_ids),
                ("location_id", "child_of", location_ids),
            ],
            ["product_id", "quantity"],
        )

        on_hand_totals = {}
        for q in quants:
            pid = q["product_id"][0] if q.get("product_id") else None
            if pid is None:
                continue
            on_hand_totals[pid] = on_hand_totals.get(pid, 0.0) + q.get("quantity", 0.0)

        # "Reserved" here means specifically what's committed to outgoing
        # delivery orders that haven't shipped yet -- not Odoo's raw
        # quant-level reserved_quantity, which also counts reservations from
        # internal transfers, manufacturing, etc.
        #
        # The exact field name for "how much of this move's demand is
        # currently backed by reserved stock" has changed across Odoo
        # versions (reserved_availability in older versions, folded into
        # quantity in more recent ones) -- so we detect which one actually
        # exists on this instance rather than hardcoding one that might not.
        #
        # Scoped to the specific "WH/OUT" operation type (this warehouse's
        # delivery orders) rather than the generic "outgoing" code, which
        # would also match other warehouses' delivery operation types if
        # this Odoo instance ever has more than one.
        delivery_picking_types = self._read_only_execute(
            "stock.picking.type",
            "search_read",
            [
                ("warehouse_id.code", "in", INVENTORY_LOCATION_NAMES),
                ("code", "=", "outgoing"),
            ],
            ["id"],
        )
        delivery_picking_type_ids = [pt["id"] for pt in delivery_picking_types]

        reserved_field = self._resolve_reserved_field()

        if not delivery_picking_type_ids or reserved_field is None:
            reserved_totals = {}
        else:
            moves = self._read_only_execute(
                "stock.move",
                "search_read",
                [
                    ("product_id", "in", product_ids),
                    ("picking_type_id", "in", delivery_picking_type_ids),
                    ("state", "not in", ["done", "cancel"]),
                ],
                ["product_id", reserved_field],
            )
            reserved_totals = {}
            for m in moves:
                pid = m["product_id"][0] if m.get("product_id") else None
                if pid is None:
                    continue
                reserved_totals[pid] = reserved_totals.get(pid, 0.0) + m.get(reserved_field, 0.0)

        items = []
        for p in products:
            on_hand = on_hand_totals.get(p["id"], 0.0)
            reserved = reserved_totals.get(p["id"], 0.0)
            items.append(
                InventoryItem(
                    product_id=p["id"],
                    sku=p.get("default_code") or "",
                    name=p.get("name") or "",
                    on_hand=on_hand,
                    reserved=reserved,
                    available=on_hand - reserved,
                    uom=_display_value(p.get("uom_id")),
                )
            )
        # Keep them in the same order as INVENTORY_SKUS for predictable display.
        order = {sku: i for i, sku in enumerate(INVENTORY_SKUS)}
        items.sort(key=lambda i: order.get(i.sku, len(order)))
        return items
