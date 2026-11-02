"""Orders and their customers, stored in SQLite."""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    customer_name TEXT NOT NULL,
    total_cents INTEGER NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open the database and create the tables it needs."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def add_order(conn: sqlite3.Connection, customer_id: int, total_cents: int) -> int:
    """Store a new order for a customer and return its id."""
    (name,) = conn.execute("SELECT name FROM customers WHERE id = ?", (customer_id,)).fetchone()
    cur = conn.execute("INSERT INTO orders (customer_id, customer_name, total_cents) VALUES (?, ?, ?)",
                       (customer_id, name, total_cents))
    return cur.lastrowid


def order_report(conn: sqlite3.Connection) -> list[tuple[int, str, int]]:
    """Every order with its customer's name, for the nightly report."""
    return conn.execute("SELECT id, customer_name, total_cents FROM orders").fetchall()
