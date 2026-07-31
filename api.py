"""
API + dashboard server for the warehouse app.

Auth model: one shared password for everyone (set via the SITE_PASSWORD
environment variable). Every project manager sees every order -- the login
is just a basic gate so the URL isn't wide open on the public internet, not
a per-person access boundary.

GET  /login                      -> login page (HTML)
POST /login                      -> submit the shared password
GET  /logout                     -> clear session
GET  /                           -> the dashboard (HTML, requires login)
GET  /orders                     -> all active delivery orders, each with
                                     contents, note count, and local status
GET  /inventory                  -> on-hand quantities for the configured SKUs
GET  /orders/<picking_id>/notes  -> notes for one order
POST /orders/<picking_id>/notes  -> add a note (local DB only, never touches Odoo)
POST /orders/<picking_id>/status -> set the local status (local DB only, never touches Odoo)
GET  /statuses                   -> the list of valid local status values, in order
"""

from dataclasses import asdict
from functools import wraps
import os
import secrets

from flask import Flask, jsonify, request, send_from_directory, session, redirect

import notes_db
from odoo_client import OdooReadOnlyClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, static_folder=STATIC_DIR)
# Set SECRET_KEY as an environment variable in production so sessions survive
# a restart. Falls back to a random key (fine for local testing only).
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "changeme")

odoo = OdooReadOnlyClient()
notes_db.init_db()


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/orders") or request.path == "/statuses":
                return jsonify({"error": "not logged in"}), 401
            return redirect("/login")
        return view_func(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET"])
def login_page():
    return send_from_directory(STATIC_DIR, "login.html")


@app.route("/login", methods=["POST"])
def login_submit():
    data = request.get_json(force=True)
    if data.get("password", "") != SITE_PASSWORD:
        return jsonify({"error": "Incorrect password"}), 401
    session["authenticated"] = True
    return jsonify({"status": "ok"})


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
@login_required
def dashboard():
    return send_from_directory(STATIC_DIR, "dashboard.html")


@app.route("/statuses", methods=["GET"])
@login_required
def list_statuses():
    return jsonify(notes_db.STATUS_OPTIONS)


@app.route("/orders", methods=["GET"])
@login_required
def list_orders():
    orders = odoo.get_active_deliveries()

    note_counts = notes_db.get_note_counts()
    statuses = notes_db.get_all_statuses()
    lines_by_picking = odoo.get_order_lines([o.id for o in orders])

    result = []
    for o in orders:
        order_dict = asdict(o)
        order_dict["note_count"] = note_counts.get(o.id, 0)
        order_dict["contents"] = lines_by_picking.get(o.id, [])
        local = statuses.get(o.id)
        order_dict["local_status"] = local["status"] if local else notes_db.STATUS_OPTIONS[0]
        result.append(order_dict)

    return jsonify(result)


@app.route("/inventory", methods=["GET"])
@login_required
def list_inventory():
    items = odoo.get_inventory()
    return jsonify([asdict(i) for i in items])


@app.route("/orders/<int:picking_id>/notes", methods=["GET"])
@login_required
def list_notes(picking_id):
    return jsonify(notes_db.get_notes_for_picking(picking_id))


@app.route("/orders/<int:picking_id>/notes", methods=["POST"])
@login_required
def create_note(picking_id):
    data = request.get_json(force=True)
    note_text = (data.get("note_text") or "").strip()
    picking_name = data.get("picking_name", "")
    author = (data.get("author") or "").strip()

    if not note_text:
        return jsonify({"error": "note_text is required"}), 400

    notes_db.add_note(picking_id, picking_name, note_text, author)
    return jsonify({"status": "ok"}), 201


@app.route("/orders/<int:picking_id>/status", methods=["POST"])
@login_required
def update_status(picking_id):
    data = request.get_json(force=True)
    status = data.get("status", "")
    picking_name = data.get("picking_name", "")

    try:
        notes_db.set_status(picking_id, picking_name, status)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(debug=True, port=5000)
