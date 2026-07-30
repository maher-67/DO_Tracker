"""
Run this first to confirm your read-only Odoo credentials work before
building anything else.

Usage:
    export ODOO_URL="https://yourcompany.odoo.com"
    export ODOO_DB="yourcompany"
    export ODOO_USERNAME="warehouse-app-readonly"
    export ODOO_API_KEY="xxxxxxxxxxxxxxxx"
    python test_connection.py
"""

from odoo_client import OdooReadOnlyClient


def main():
    client = OdooReadOnlyClient()
    orders = client.get_active_deliveries()

    if not orders:
        print("Connected successfully, but no active delivery orders were found.")
        return

    print(f"Connected. Found {len(orders)} active delivery order(s):\n")
    for o in orders:
        print(f"  [{o.id}] {o.name} -- {o.partner_name} -- state={o.state} -- due {o.scheduled_date}")


if __name__ == "__main__":
    main()
