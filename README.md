# Warehouse status + notes app (starter)

Read-only view of active Odoo delivery orders, with a local notes layer that
never writes back to Odoo.

## 1. Set up a read-only Odoo user

This is the important safety step -- don't skip it even though the code is
already read-only.

1. In Odoo: Settings -> Users & Companies -> Users -> New
   - Name: `Warehouse App (Read Only)`
   - Login: e.g. `warehouse-app-readonly`
2. Create (or reuse) a security group that grants **read-only** access to
   `stock.picking` and `res.partner` -- no create, write, or unlink.
   - Settings -> Technical -> Security -> Groups -> New
   - Under "Access Rights", add a line for `stock.picking` with only "Read"
     checked, and the same for `res.partner`.
   - Assign this group to the user created above.
3. Generate an API key for that user: open the user record -> "API Keys" tab
   -> New API Key. Copy it somewhere safe -- Odoo only shows it once.

## 2. Configure environment variables

```bash
export ODOO_URL="https://yourcompany.odoo.com"
export ODOO_DB="yourcompany"
export ODOO_USERNAME="warehouse-app-readonly"
export ODOO_API_KEY="paste-the-api-key-here"
```

## 3. Install dependencies

```bash
pip install -r requirements.txt
```

## 4. Test the connection

```bash
python test_connection.py
```

You should see a list of currently active delivery orders. If authentication
fails, double check the group's access rights and the API key.

## 5. Run the app

```bash
python api.py
```

Then open **http://127.0.0.1:5000** in your browser -- that's the dashboard.
It shows every active delivery order with its customer, scheduled time,
contents (pulled live from Odoo), a progress rail, a dropdown to set the
warehouse's own stage, and a notes thread. Click anywhere on a row to expand
its contents and notes; the dropdown and note box work without expanding.

The API routes behind it, if you want to call them directly:

- `GET /orders` -- active delivery orders pulled live from Odoo, each with
  `contents` (product lines), `note_count`, and `local_status`
- `GET /orders/<picking_id>/notes` -- notes for one order
- `POST /orders/<picking_id>/notes` -- add a note, body:
  `{"note_text": "...", "picking_name": "WH/OUT/00123", "author": "jane"}`
- `POST /orders/<picking_id>/status` -- set the local warehouse stage, body:
  `{"status": "packed", "picking_name": "WH/OUT/00123"}`
- `GET /statuses` -- the list of valid stage values, in order

Notes and statuses both live in `warehouse_notes.db` (SQLite, created
automatically) and are entirely separate from Odoo. Nothing in this project
calls Odoo's `write`, `create`, or `unlink` methods -- the Odoo client in
`odoo_client.py` actively rejects any method outside a small read-only
allowlist as a second layer of protection. The stage options themselves are
defined in `notes_db.py` as `STATUS_OPTIONS` -- edit that list (in order) to
match your actual warehouse process.

**Known limitation:** the dashboard auto-refreshes every 30 seconds, which
will collapse an expanded row and clear an unsent note if you're mid-typing.
Fine for a first pass -- worth revisiting once you're using this daily.

## 6. The shared password

There's one password for everyone, set via an environment variable:

```bash
export SITE_PASSWORD="pick-something-not-guessable"
```

If unset, it defaults to `"changeme"` -- don't leave it that way once this
is public. Every PM sees every order; this is just a basic gate so the URL
isn't wide open to anyone on the internet, not a per-person permission
system. Each person types their own name into the note box when they add a
note (remembered in their browser for next time), so notes still show who
wrote them.

## 7. Hosting it so PMs don't need your laptop running

I'd suggest **Render** (render.com) -- free tier is enough for this, it
deploys straight from a GitHub repo, and you don't have to manage a server.

1. **Put the project in a GitHub repo** (private is fine): create a repo,
   push this folder to it. Don't commit `warehouse_notes.db` or any `.env`
   file with real credentials -- add a `.gitignore` with:
   ```
   warehouse_notes.db
   .env
   venv/
   __pycache__/
   ```
2. **Create a Render account**, then "New +" -> "Web Service" -> connect
   your GitHub repo.
3. **Configure the service:**
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn api:app`
4. **Set environment variables** in Render's dashboard (Settings ->
   Environment): `ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_API_KEY`,
   `SITE_PASSWORD` (the shared password everyone will type in), and
   `SECRET_KEY` (any long random string -- this signs login sessions, so
   keep it private and don't regenerate it casually or everyone gets logged
   out).
5. **Deploy.** Render gives you a URL like `https://your-app.onrender.com`
   -- that's what you share with PMs, along with the `SITE_PASSWORD`,
   instead of localhost.

**Important limitation on Render's free tier:** local disk isn't persistent
across deploys/restarts, so `warehouse_notes.db` (your notes, statuses, and
user accounts) could get wiped whenever the service redeploys or spins down
from inactivity. For a quick pilot this is often fine -- for anything you
depend on daily, either upgrade to a Render plan with a persistent disk, or
swap SQLite for a small hosted Postgres (Render offers a free Postgres
instance too) once you're past the testing phase. Worth flagging before PMs
start relying on their notes sticking around.
