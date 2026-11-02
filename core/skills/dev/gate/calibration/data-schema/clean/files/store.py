"""Orders, customers and invoices, stored in SQLite."""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    total_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
    billed_name TEXT NOT NULL  -- the name as printed when issued: a frozen snapshot, never synced
);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open the database and create the tables it needs."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def add_order(conn: sqlite3.Connection, customer_id: int, total_cents: int) -> int:
    """Store a new order for a customer and return its id."""
    cur = conn.execute("INSERT INTO orders (customer_id, total_cents) VALUES (?, ?)", (customer_id, total_cents))
    return cur.lastrowid


def order_report(conn: sqlite3.Connection) -> list[tuple[int, int, int]]:
    """Every order with its customer id, for the nightly report."""
    return conn.execute("SELECT id, customer_id, total_cents FROM orders").fetchall()


def issue_invoice(conn: sqlite3.Connection, order_id: int) -> int:
    """Freeze the customer's current name onto a new invoice for the order, and return its id."""
    (name,) = conn.execute(
        "SELECT c.name FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.id = ?", (order_id,)
    ).fetchone()
    cur = conn.execute("INSERT INTO invoices (order_id, billed_name) VALUES (?, ?)", (order_id, name))
    return cur.lastrowid


def invoice_header(conn: sqlite3.Connection, invoice_id: int) -> tuple[int, str]:
    """The order id and billed name printed at the top of an invoice."""
    return conn.execute("SELECT order_id, billed_name FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
