"""KPI configuration for dynamic SQL generation.

This file stores logical KPI definitions without hardcoding SQL table names.
The runtime service resolves the actual SQL Server table/columns by inspecting
schema metadata.
"""

KPI_TABLES = {
    "ca": {
        "label": "Chiffre d'affaires",
        "aliases": ("ca", "chiffre d'affaire", "chiffre d'affaires", "revenue", "sales", "ventes"),
        "table": None,
        "value_columns": ("Sales Amount (Actual)", "Sales Amt_ Incl_ VAT (Actual)", "Amount"),
        "date_columns": ("Posting Date", "Date"),
        "company_columns": ("companyName", "CompanyName", "Company"),
        "use_abs": True,
    },
    "achat": {
        "label": "Achat",
        "aliases": ("achat", "achats", "purchase", "purchases", "buy", "buying"),
        "table": None,
        "value_columns": ("Purchase Amount (Actual)", "Amount"),
        "date_columns": ("Posting Date", "Date"),
        "company_columns": ("companyName", "CompanyName", "Company"),
        "use_abs": True,
    },
    "encaissement": {
        "label": "Encaissement",
        "aliases": ("encaissement", "encaissements", "cash in", "cash-in", "inflow"),
        "table": None,
        "value_columns": ("Amount",),
        "date_columns": ("Posting Date", "Date"),
        "company_columns": ("companyName", "CompanyName", "Company"),
        "use_abs": False,
    },
    "decaissement": {
        "label": "Décaissement",
        "aliases": ("décaissement", "décaissements", "decaissement", "decaissements", "cash out", "cash-out", "outflow"),
        "table": None,
        "value_columns": ("Amount",),
        "date_columns": ("Posting Date", "Date"),
        "company_columns": ("companyName", "CompanyName", "Company"),
        "use_abs": False,
    },
}
