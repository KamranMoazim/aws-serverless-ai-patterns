#!/usr/bin/env python3
"""Seed the Invoices table with sample data so the agent has something to query.

Usage:
    python scripts/seed_invoices.py
"""
import boto3

INVOICES = [
    {"invoice_id": "INV-1001", "customer_name": "Acme Corp",   "invoice_date": "2025-03-04", "amount": 12500, "status": "PAID"},
    {"invoice_id": "INV-1002", "customer_name": "Acme Corp",   "invoice_date": "2025-03-19", "amount": 4800,  "status": "UNPAID"},
    {"invoice_id": "INV-1003", "customer_name": "Globex Inc",  "invoice_date": "2025-03-22", "amount": 9200,  "status": "UNPAID"},
    {"invoice_id": "INV-1004", "customer_name": "Acme Corp",   "invoice_date": "2025-04-02", "amount": 3100,  "status": "PAID"},
    {"invoice_id": "INV-1005", "customer_name": "Initech",     "invoice_date": "2025-04-11", "amount": 15750, "status": "OVERDUE"},
    {"invoice_id": "INV-1006", "customer_name": "Globex Inc",  "invoice_date": "2025-04-18", "amount": 6400,  "status": "PAID"},
]


def main():
    table = boto3.resource("dynamodb").Table("lambda-as-mcp-tool-invoices")
    with table.batch_writer() as batch:
        for inv in INVOICES:
            batch.put_item(Item=inv)
    print(f"Seeded {len(INVOICES)} invoices into 'Invoices'.")


if __name__ == "__main__":
    main()
