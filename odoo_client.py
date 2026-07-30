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

# TODO: fill these in after checking the field in Odoo (debug mode -> hover
# over the "Ops Status" label on a Delivery Order to see the technical name,
# and check Settings > Technical > Fields for the selection's internal value).
OPS_STATUS_FIELD = "hex_ops_state"          # placeholder -- likely wrong, confirm it
OPS_STATUS_VALUE = "in_process"  # placeholder -- likely wrong, confirm it

# Only pull orders whose SOURCE location is WH/Stock (adjust if your
# warehouse code differs -- check the picking's "Source Location" field).
SOURCE_LOCATION_NAME = ["WH/Stock", "WH/Output"]

# TODO: same discovery process as Ops Status -- hover over "Project Manager"
# in debug mode to get the technical field name. If it's a many2one (e.g. a
# linked user or contact) rather than plain text, that's handled below too.
PROJECT_MANAGER_FIELD = "x_studio_project_manager"  # placeholder -- likely wrong, confirm it


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


class OdooReadOnlyClient:
    def __init__(self):
        self.url = os.environ["ODOO_URL"].rstrip("/")
        self.db = os.environ["ODOO_DB"]
        self.username = os.environ["ODOO_USERNAME"]
        self.api_key = os.environ["ODOO_API_KEY"]

        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")
        self._uid = None

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
            ("location_id.complete_name", "=", SOURCE_LOCATION_NAME),
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
