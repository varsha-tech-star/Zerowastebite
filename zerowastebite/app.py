import hmac, logging, os, re, secrets, sqlite3, time, uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from flask import (Flask, g, render_template, request, redirect, url_for,
                   session, flash, abort, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash

BASE = Path(__file__).parent
DB = BASE / "zerowastebite.db"
UPLOADS = BASE / "static" / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def load_secret():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    f = BASE / ".secret_key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
    return f.read_text().strip()

app = Flask(__name__)
app.config.update(SECRET_KEY=load_secret(), MAX_CONTENT_LENGTH=4 * 1024 * 1024,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=8 * 3600)

CATEGORIES = ["Bakery", "Café", "Juice bar", "Canteen", "Restaurant"]
PHONE_RE = re.compile(r"^[6-9]\d{9}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_ACTIVE_PER_PHONE = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, emoji TEXT NOT NULL,
  address TEXT NOT NULL, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS bags (
  id INTEGER PRIMARY KEY, vendor_id INTEGER NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
  title TEXT NOT NULL, description TEXT NOT NULL, image_url TEXT,
  price REAL NOT NULL CHECK(price >= 0), original_value REAL NOT NULL CHECK(original_value >= price),
  quantity INTEGER NOT NULL CHECK(quantity >= 0),
  pickup_start INTEGER NOT NULL, pickup_end INTEGER NOT NULL CHECK(pickup_end > pickup_start));
CREATE TABLE IF NOT EXISTS reservations (
  id INTEGER PRIMARY KEY, bag_id INTEGER NOT NULL REFERENCES bags(id) ON DELETE CASCADE,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  code TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'reserved' CHECK(status IN ('reserved','collected','cancelled')),
  created_at INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_bag ON reservations(bag_id, customer_id) WHERE status != 'cancelled';
CREATE INDEX IF NOT EXISTS idx_bags_live ON bags(pickup_end, quantity);
CREATE INDEX IF NOT EXISTS idx_res_bag ON reservations(bag_id);
"""

class Fail(Exception):
    """A user-facing validation problem."""

# ---------- database ----------
def connect():
    c = sqlite3.connect(DB, isolation_level=None)  # explicit transactions
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c

def db():
    if "db" not in g:
        g.db = connect()
    return g.db

@app.teardown_appcontext
def close_db(_):
    c = g.pop("db", None)
    if c:
        c.close()

@contextmanager
def tx():
    """BEGIN IMMEDIATE takes the write lock up front, so check-then-update is race-free."""
    c = db(); c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK"); raise

def seed():
    c = connect(); c.executescript(SCHEMA)
    if c.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]:
        c.close(); return
    pw = generate_password_hash("demo123")
    vendors = [
        ("Crumb & Co Bakery", "Bakery", "🥐", "12 MG Road", "crumb@demo.com"),
        ("Brew Lab Café", "Café", "☕", "4th Block, Koramangala", "brew@demo.com"),
        ("Green Bowl Juice Bar", "Juice bar", "🥗", "Indiranagar 100ft Road", "green@demo.com"),
        ("Canteen 4 · Campus", "Canteen", "🍱", "North Gate, Main Campus", "canteen@demo.com"),
        ("Dosa Diaries", "Restaurant", "🥞", "Jayanagar 4th Block", "dosa@demo.com"),
        ("Sweet Tooth Patisserie", "Bakery", "🧁", "Church Street", "sweet@demo.com"),
    ]
    for v in vendors:
        c.execute("INSERT INTO vendors(name,category,emoji,address,email,password_hash) VALUES(?,?,?,?,?,?)", (*v, pw))
    now, M = int(time.time()), 60
    img = lambda kw, n: f"https://loremflickr.com/640/480/{kw}?lock={n}"
    bags = [  # vendor, title, desc, price, worth, qty, start, end (minutes from now), photo keywords
        (1, "Pastry Surprise", "Three croissants, muffins or danishes from today's bake.", 60, 240, 5, -10, 120, "croissant,pastry"),
        (1, "Sourdough & Buns", "A rustic loaf plus four soft buns, baked this morning.", 50, 180, 3, 60, 180, "bread,bakery"),
        (2, "Sandwich + Cookie", "One freshly made sandwich and a giant cookie.", 90, 320, 4, -5, 80, "sandwich"),
        (2, "Cold Brew + Muffin", "A bottled cold brew with a blueberry muffin.", 85, 300, 2, 30, 200, "coffee,muffin"),
        (3, "Fruit Bowl Duo", "Two cut-fruit bowls, still crisp and cold.", 70, 260, 6, -15, 50, "fruit,bowl"),
        (4, "Thali Box", "A full meal box: rice, dal, sabzi and roti.", 55, 140, 8, 0, 240, "indian,thali"),
        (4, "Snack Box", "Samosas, a sandwich and a drink.", 40, 120, 2, 0, 25, "samosa"),
        (5, "Dosa Combo", "Masala dosa with chutneys and a filter coffee.", 80, 260, 4, -20, 95, "dosa"),
        (6, "Cupcake Box", "Four frosted cupcakes in assorted flavours.", 99, 360, 3, 20, 150, "cupcake"),
        (6, "Brownie Bites", "Eight fudgy brownie squares.", 75, 280, 5, -5, 40, "brownie,chocolate"),
    ]
    for i, (vid, t, d, p, w, q, s, e, kw) in enumerate(bags, 1):
        c.execute("INSERT INTO bags(vendor_id,title,description,image_url,price,original_value,quantity,"
                  "pickup_start,pickup_end) VALUES(?,?,?,?,?,?,?,?,?)",
                  (vid, t, d, img(kw, i), p, w, q, now + s * M, now + e * M))
    names = ["Aarav Sharma", "Diya Patel", "Rohan Iyer", "Meera Nair", "Kabir Singh", "Ananya Rao",
             "Vihaan Gupta", "Isha Menon", "Arjun Reddy", "Saanvi Kapoor", "Aditya Joshi", "Navya Krishnan",
             "Reyansh Das", "Kiara Mehta", "Dhruv Bansal", "Tara Pillai", "Yash Verma", "Pooja Hegde",
             "Karthik Subramanian", "Sneha Kulkarni", "Imran Khan", "Lakshmi Prasad"]
    for i, n in enumerate(names, 1):
        c.execute("INSERT INTO customers(name,phone) VALUES(?,?)", (n, f"98765{i:05d}"))
    res = [(1, 1, "CR7K", "reserved"), (1, 2, "PQ2M", "collected"), (1, 9, "PA5W", "collected"),
           (3, 3, "SW9A", "reserved"), (3, 10, "SW4H", "collected"), (5, 4, "FB3X", "reserved"),
           (6, 5, "TH8D", "collected"), (6, 6, "TH4N", "reserved"), (6, 11, "TH2Q", "collected"),
           (7, 7, "SN6R", "reserved"), (2, 8, "BB1Z", "reserved"), (8, 12, "DD7P", "reserved"),
           (8, 13, "DD3C", "collected"), (9, 14, "CU8L", "reserved"), (10, 15, "BR5V", "reserved"),
           (10, 16, "BR9T", "collected"), (4, 17, "CB4G", "reserved"), (5, 18, "FB6J", "cancelled"),
           (6, 19, "TH7S", "collected"), (3, 20, "SW2E", "reserved")]
    for bag, cust, code, st in res:
        c.execute("INSERT INTO reservations(bag_id,customer_id,code,status,created_at) VALUES(?,?,?,?,?)",
                  (bag, cust, code, st, now - 300))
    c.close()

# ---------- security & helpers ----------
def csrf():
    session.setdefault("csrf", secrets.token_hex(16))
    return session["csrf"]
app.jinja_env.globals["csrf"] = csrf

@app.before_request
def csrf_guard():
    if request.method == "POST":
        sent, real = request.form.get("csrf", ""), session.get("csrf", "")
        if not real or not hmac.compare_digest(sent.encode(), real.encode()):
            abort(400)

@app.after_request
def headers(r):
    r.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                      "Referrer-Policy": "same-origin"})
    return r

@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(413)
@app.errorhandler(500)
def error(e):
    msgs = {400: "Your session expired. Go back, refresh and try again.", 404: "We couldn't find that page.",
            413: "That photo is too large. Keep it under 4 MB.", 500: "Something went wrong on our side."}
    code = getattr(e, "code", 500)
    if code == 500:
        logging.exception("Unhandled error")
    return render_template("error.html", code=code, msg=msgs.get(code, "Error")), code

@app.template_filter("clock")
def clock(ts):
    return datetime.fromtimestamp(ts).strftime("%I:%M %p").lstrip("0")

def new_code(c):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(4))
        if not c.execute("SELECT 1 FROM reservations WHERE code=?", (code,)).fetchone():
            return code

def save_image(f):
    if not f or not f.filename:
        return None
    head = f.read(12); f.seek(0)
    kinds = {"jpg": head[:2] == b"\xff\xd8", "png": head[:4] == b"\x89PNG",
             "webp": head[:4] == b"RIFF" and head[8:12] == b"WEBP"}
    ext = next((k for k, ok in kinds.items() if ok), None)
    if not ext:
        raise Fail("Photos must be JPG, PNG or WebP images.")
    name = f"{uuid.uuid4().hex}.{ext}"
    f.save(UPLOADS / name)
    return f"/static/uploads/{name}"

# ---------- buyer routes ----------
LIVE_SQL = """SELECT b.*, v.name AS vendor, v.category, v.emoji, v.address
              FROM bags b JOIN vendors v ON v.id=b.vendor_id
              WHERE b.quantity>0 AND b.pickup_end>? ORDER BY b.pickup_end"""

@app.route("/")
def index():
    bags = db().execute(LIVE_SQL, (int(time.time()),)).fetchall()
    return render_template("index.html", bags=bags, now=int(time.time()),
                           total_left=sum(b["quantity"] for b in bags),
                           categories=sorted({b["category"] for b in bags}))

@app.route("/api/bags")
def api_bags():
    rows = db().execute(LIVE_SQL, (int(time.time()),)).fetchall()
    return jsonify([{k: r[k] for k in ("id", "title", "vendor", "price", "quantity", "pickup_end")} for r in rows])

@app.route("/healthz")
def healthz():
    db().execute("SELECT 1"); return {"status": "ok"}

@app.post("/reserve/<int:bag_id>")
def reserve(bag_id):
    name, phone = request.form.get("name", "").strip(), request.form.get("phone", "").strip()
    try:
        if not 2 <= len(name) <= 60:
            raise Fail("Enter your name (2 to 60 characters).")
        if not PHONE_RE.match(phone):
            raise Fail("Enter a valid 10-digit mobile number.")
        now = int(time.time())
        with tx() as c:
            cust = c.execute("SELECT id FROM customers WHERE phone=?", (phone,)).fetchone()
            cid = cust["id"] if cust else c.execute(
                "INSERT INTO customers(name,phone) VALUES(?,?)", (name, phone)).lastrowid
            active = c.execute("""SELECT COUNT(*) FROM reservations r JOIN bags b ON b.id=r.bag_id
                WHERE r.customer_id=? AND r.status='reserved' AND b.pickup_end>?""", (cid, now)).fetchone()[0]
            if active >= MAX_ACTIVE_PER_PHONE:
                raise Fail(f"You can hold up to {MAX_ACTIVE_PER_PHONE} active reservations at a time.")
            if c.execute("SELECT 1 FROM reservations WHERE bag_id=? AND customer_id=? AND status!='cancelled'",
                         (bag_id, cid)).fetchone():
                raise Fail("You already reserved this bag. Check your pickup code.")
            if c.execute("UPDATE bags SET quantity=quantity-1 WHERE id=? AND quantity>0 AND pickup_end>?",
                         (bag_id, now)).rowcount == 0:
                raise Fail("Sorry, that bag just sold out or its pickup window closed.")
            code = new_code(c)
            c.execute("INSERT INTO reservations(bag_id,customer_id,code,created_at) VALUES(?,?,?,?)",
                      (bag_id, cid, code, now))
        logging.info("Reserved bag %s code %s", bag_id, code)
        return redirect(url_for("pickup", code=code))
    except Fail as e:
        flash(str(e), "error")
        return redirect(url_for("index"))

def get_reservation(code):
    return db().execute("""
        SELECT r.code, r.status, c.name AS customer, b.title, b.price, b.pickup_start, b.pickup_end,
               v.name AS vendor, v.address, v.emoji
        FROM reservations r JOIN bags b ON b.id=r.bag_id JOIN vendors v ON v.id=b.vendor_id
        JOIN customers c ON c.id=r.customer_id WHERE r.code=?""", (code.upper(),)).fetchone()

@app.route("/pickup/<code>")
def pickup(code):
    r = get_reservation(code) or abort(404)
    return render_template("pickup.html", r=r, now=int(time.time()))

@app.post("/pickup/<code>/cancel")
def cancel(code):
    with tx() as c:
        row = c.execute("""SELECT r.id, r.bag_id FROM reservations r JOIN bags b ON b.id=r.bag_id
                           WHERE r.code=? AND r.status='reserved' AND b.pickup_end>?""",
                        (code.upper(), int(time.time()))).fetchone()
        if row:
            c.execute("UPDATE reservations SET status='cancelled' WHERE id=?", (row["id"],))
            c.execute("UPDATE bags SET quantity=quantity+1 WHERE id=?", (row["bag_id"],))
            flash("Reservation cancelled. The bag is back on the shelf for others.", "ok")
        else:
            flash("This reservation can't be cancelled any more.", "error")
    return redirect(url_for("pickup", code=code))

@app.route("/track", methods=["GET", "POST"])
def track():
    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        if get_reservation(code):
            return redirect(url_for("pickup", code=code))
        flash("No reservation found for that code.", "error")
    return render_template("track.html")

# ---------- vendor routes ----------
FAILS = {}  # ip -> (count, last_time): simple brute-force throttle

def current_vendor():
    vid = session.get("vendor_id")
    return db().execute("SELECT * FROM vendors WHERE id=?", (vid,)).fetchone() if vid else None

@app.route("/vendor/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr
    if request.method == "POST":
        n, t = FAILS.get(ip, (0, 0))
        if n >= 5 and time.time() - t < 600:
            flash("Too many attempts. Try again in 10 minutes.", "error")
            return render_template("login.html")
        v = db().execute("SELECT * FROM vendors WHERE email=?", (request.form.get("email", "").strip().lower(),)).fetchone()
        if v and check_password_hash(v["password_hash"], request.form.get("password", "")):
            FAILS.pop(ip, None); session.clear(); session.permanent = True
            session["vendor_id"] = v["id"]
            return redirect(url_for("dashboard"))
        FAILS[ip] = (n + 1, time.time())
        flash("Email or password is incorrect.", "error")
    return render_template("login.html")

@app.route("/vendor/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        try:
            name, addr, email = f.get("name", "").strip(), f.get("address", "").strip(), f.get("email", "").strip().lower()
            if not 2 <= len(name) <= 60 or not addr:
                raise Fail("Enter your shop name and address.")
            if f.get("category") not in CATEGORIES:
                raise Fail("Pick a shop type.")
            if not EMAIL_RE.match(email):
                raise Fail("Enter a valid email address.")
            if len(f.get("password", "")) < 8:
                raise Fail("Use a password with at least 8 characters.")
            emoji = {"Bakery": "🥐", "Café": "☕", "Juice bar": "🥤", "Canteen": "🍱", "Restaurant": "🍽️"}[f["category"]]
            with tx() as c:
                vid = c.execute("INSERT INTO vendors(name,category,emoji,address,email,password_hash) VALUES(?,?,?,?,?,?)",
                                (name, f["category"], emoji, addr, email, generate_password_hash(f["password"]))).lastrowid
            session.clear(); session.permanent = True; session["vendor_id"] = vid
            return redirect(url_for("dashboard"))
        except sqlite3.IntegrityError:
            flash("That email is already registered. Log in instead.", "error")
        except Fail as e:
            flash(str(e), "error")
    return render_template("register.html", categories=CATEGORIES)

@app.route("/vendor/logout")
def logout():
    session.clear(); return redirect(url_for("index"))

@app.route("/vendor")
def dashboard():
    v = current_vendor()
    if not v:
        return redirect(url_for("login"))
    c, now = db(), int(time.time())
    bags = c.execute("SELECT * FROM bags WHERE vendor_id=? ORDER BY pickup_end DESC", (v["id"],)).fetchall()
    res = c.execute("""SELECT r.id, r.code, r.status, c.name, c.phone, b.title, b.pickup_end
        FROM reservations r JOIN bags b ON b.id=r.bag_id JOIN customers c ON c.id=r.customer_id
        WHERE b.vendor_id=? AND r.status!='cancelled' ORDER BY r.status DESC, r.id DESC""", (v["id"],)).fetchall()
    saved = c.execute("""SELECT COALESCE(SUM(b.original_value-b.price),0) FROM reservations r
        JOIN bags b ON b.id=r.bag_id WHERE b.vendor_id=? AND r.status='collected'""", (v["id"],)).fetchone()[0]
    stats = {"live": sum(b["quantity"] for b in bags if b["pickup_end"] > now),
             "reserved": sum(r["status"] == "reserved" for r in res),
             "collected": sum(r["status"] == "collected" for r in res), "saved": saved}
    return render_template("dashboard.html", v=v, bags=bags, res=res, stats=stats, now=now)

@app.post("/vendor/post-bag")
def post_bag():
    v = current_vendor() or abort(401)
    f, today, now = request.form, datetime.now(), int(time.time())
    def at(s):
        h, m = map(int, s.split(":"))
        return int(today.replace(hour=h, minute=m, second=0, microsecond=0).timestamp())
    try:
        title, desc = f.get("title", "").strip(), f.get("description", "").strip()
        price, worth, qty = float(f["price"]), float(f["original_value"]), int(f["quantity"])
        start, end = at(f["start"]), at(f["end"])
        if not 3 <= len(title) <= 60 or not 1 <= len(desc) <= 200:
            raise Fail("Give the bag a name (3-60 characters) and a short description.")
        if not (1 <= qty <= 100 and 0 < price <= worth <= 10000):
            raise Fail("Check the numbers: the price can't be higher than the bag's worth.")
        if end <= start or end <= now:
            raise Fail("The pickup end time must be after the start time and still in the future.")
        image = save_image(request.files.get("photo"))
        with tx() as c:
            c.execute("INSERT INTO bags(vendor_id,title,description,image_url,price,original_value,quantity,"
                      "pickup_start,pickup_end) VALUES(?,?,?,?,?,?,?,?,?)",
                      (v["id"], title, desc, image, price, worth, qty, start, end))
        flash("Bag posted. Buyers can see it now.", "ok")
    except (KeyError, ValueError):
        flash("Please fill in every field with valid values.", "error")
    except Fail as e:
        flash(str(e), "error")
    return redirect(url_for("dashboard"))

@app.post("/vendor/collect/<int:rid>")
def collect(rid):
    v = current_vendor() or abort(401)
    db().execute("""UPDATE reservations SET status='collected' WHERE id=? AND status='reserved'
                    AND bag_id IN (SELECT id FROM bags WHERE vendor_id=?)""", (rid, v["id"]))
    return redirect(url_for("dashboard"))

@app.post("/vendor/close/<int:bag_id>")
def close_bag(bag_id):
    v = current_vendor() or abort(401)
    db().execute("UPDATE bags SET pickup_end=MAX(pickup_start+1, ?) WHERE id=? AND vendor_id=?",
                 (int(time.time()), bag_id, v["id"]))
    flash("Listing closed.", "ok")
    return redirect(url_for("dashboard"))

if __name__ == "__main__":
    seed()
    app.run(debug=True)
