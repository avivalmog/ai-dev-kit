# Migration rules

Every file in `migrations/` follows these rules.

1. A migration never drops a column in the same release that stops writing it. Stop writing first; drop in a
   later migration.
2. A new `NOT NULL` column has a server-side default, so existing rows stay valid.
3. Every migration has a `downgrade()` that undoes exactly what `upgrade()` did.
4. An index on a table over a million rows is created `CONCURRENTLY`.
