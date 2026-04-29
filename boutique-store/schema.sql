-- ============================================================
--  Layan Store — Database Schema
--  Run this once on the server to create all tables
--  The app will auto-create the admin user on first start
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Products
CREATE TABLE IF NOT EXISTS products (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    category   TEXT    NOT NULL DEFAULT '',
    price      REAL    NOT NULL,
    cost_price REAL    NOT NULL DEFAULT 0,
    stock      INTEGER NOT NULL DEFAULT 0
);

-- Transactions (sales + returns)
CREATE TABLE IF NOT EXISTS transactions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id     INTEGER NOT NULL,
    product_name   TEXT    NOT NULL,
    qty            INTEGER NOT NULL,
    total          REAL    NOT NULL,
    cost_total     REAL    NOT NULL DEFAULT 0,
    type           TEXT    NOT NULL CHECK(type IN ('sale','return')),
    returned       INTEGER NOT NULL DEFAULT 0,
    customer_name  TEXT    NOT NULL DEFAULT '',
    customer_phone TEXT    NOT NULL DEFAULT '',
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Business goals
CREATE TABLE IF NOT EXISTS goals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    product_id INTEGER,
    target_qty INTEGER NOT NULL DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Personal goals / to-do list
CREATE TABLE IF NOT EXISTS personal_goals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT    NOT NULL,
    achieved   INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Admin users
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT    NOT NULL UNIQUE,
    password   TEXT    NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
