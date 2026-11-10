"""Database access shared by the operations scripts."""

import os

import psycopg2


def connect():
    return psycopg2.connect(os.environ["BILLING_DSN"])


def fetch_all(sql, params=()):
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def execute(sql, params=()):
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount
