# data/db_connection.py
# SQL Server connection helper using pymssql (no pyodbc / ODBC dependency)

import os
import time
from typing import Any, Dict, Iterable, List
import pymssql
from config.settings import settings
from config.logger import get_logger
from services.sql_validator import validate_sql

logger = get_logger(__name__)


def get_db_connection():
    """
    Create and return a pymssql connection to SQL Server using environment settings.

    Expects environment variables:
      - DB_SERVER: SQL Server hostname or IP
      - DB_DATABASE: Database name
      - DB_USER: SQL login username
      - DB_PASSWORD: SQL login password

    For remote deployments (Render), provide valid SQL credentials.
    Windows integrated authentication is NOT available on Linux.
    """

    server = settings.DB_SERVER
    database = settings.DB_DATABASE
    user = settings.DB_USER or os.getenv('DB_USER')
    password = settings.DB_PASSWORD or os.getenv('DB_PASSWORD')

    logger.info(
        "Creating SQL Server connection (server=%s, database=%s, user_set=%s)",
        server,
        database,
        bool(user and password),
    )

    # pymssql.connect(host, user, password, database)
    try:
        if user and password:
            conn = pymssql.connect(host=server, user=user, password=password, database=database, timeout=settings.DB_CONNECTION_TIMEOUT_SECONDS)
        else:
            logger.warning("DB_USER/DB_PASSWORD not set — attempting connection without credentials (may fail on Render).")
            conn = pymssql.connect(host=server, database=database, timeout=settings.DB_CONNECTION_TIMEOUT_SECONDS)

    logger.info(
        "Creating SQL Server connection (server=%s, database=%s, user_set=%s)",
        server,
        database,
        bool(user and password),
    )

    # pymssql.connect(host, user, password, database)
    try:
        if user and password:
            conn = pymssql.connect(host=server, user=user, password=password, database=database, timeout=settings.DB_CONNECTION_TIMEOUT_SECONDS)
        else:
            logger.warning("DB_USER/DB_PASSWORD not set — attempting connection without credentials (may fail on Render).")
            conn = pymssql.connect(host=server, database=database, timeout=settings.DB_CONNECTION_TIMEOUT_SECONDS)

        return conn
    except Exception:
        logger.exception("pymssql connection failed — verify DB_SERVER, DB_DATABASE, DB_USER, DB_PASSWORD and network access")
        raise


def _split_table_name(table_name: str) -> tuple[str, str]:
    cleaned = table_name.replace("[", "").replace("]", "")
    parts = cleaned.split(".")
    if len(parts) == 2:
        return parts[0], parts[1]
    return "dbo", cleaned


def get_available_tables() -> List[str]:
    """Return all base tables in the current SQL Server database."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        logger.info("Fetching available SQL tables from INFORMATION_SCHEMA.TABLES")
        cursor.execute(
            """
            SELECT TABLE_SCHEMA, TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_SCHEMA, TABLE_NAME
            """
        )
        rows = cursor.fetchall()
        tables = [f"{schema}.{name}" for schema, name in rows]
        logger.info("Available SQL tables count=%d", len(tables))
        logger.info("Available SQL tables sample=%s", tables[:20])
        return tables
    finally:
        cursor.close()
        conn.close()


def get_table_columns(table_name: str) -> List[str]:
    """Return all column names for a specific table."""
    schema_name, bare_table_name = _split_table_name(table_name)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        logger.info("Fetching columns for table=%s", table_name)
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (schema_name, bare_table_name),
        )
        rows = cursor.fetchall()
        columns = [column for (column,) in rows]
        logger.info("Columns for table=%s: %s", table_name, columns)
        return columns
    finally:
        cursor.close()
        conn.close()


def validate_table_exists(table_name: str) -> bool:
    """Check if a base table exists in the database."""
    schema_name, bare_table_name = _split_table_name(table_name)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT 1
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND TABLE_TYPE = 'BASE TABLE'
            """,
            (schema_name, bare_table_name),
        )
        exists = cursor.fetchone() is not None
        logger.info("validate_table_exists(%s) -> %s", table_name, exists)
        return exists
    finally:
        cursor.close()
        conn.close()


def validate_columns_exist(table_name: str, columns: Iterable[str]) -> bool:
    """Check if all requested columns exist in a table."""
    existing_columns = {column.lower() for column in get_table_columns(table_name)}
    requested_columns = [str(column).strip().strip("[]") for column in columns]
    missing_columns = [column for column in requested_columns if column.lower() not in existing_columns]
    is_valid = not missing_columns
    logger.info(
        "validate_columns_exist(table=%s, columns=%s) -> %s missing=%s",
        table_name,
        requested_columns,
        is_valid,
        missing_columns,
    )
    return is_valid


def test_db_connection() -> bool:
    """
    Verify the SQL Server connection using `SELECT 1`.

    Returns:
        bool: True when the connection and query succeed.
    """
    logger.info("Starting SQL connection test using SELECT 1")
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 AS ok")
        row = cursor.fetchone()
        success = bool(row and row[0] == 1)
        logger.info("SQL connection test succeeded: %s", success)
        return success
    except Exception:
        logger.exception("SQL connection test failed")
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
            logger.info("SQL connection test connection closed")


def execute_query(sql_query: str, params: tuple = None) -> List[Dict[str, Any]]:
    """
    Executes a SQL query after validation.
    Validates query for security (no DROP/DELETE/INSERT, etc.) before execution.
    Logs execution time and errors.

    Args:
        sql_query (str): The SQL query to execute.
        params (tuple, optional): Parameters to bind to the query.

    Returns:
        List[Dict[str, Any]]: Query results.
    
    Raises:
        ValueError: If SQL validation fails
    """
    from decimal import Decimal
    
    logger.info("SQL execution requested")
    logger.info("SQL statement: %s", sql_query)
    logger.info("SQL params: %s", params)

    # Validate SQL before execution
    is_valid, error_msg = validate_sql(sql_query)
    if not is_valid:
        logger.warning(f"SQL validation failed: {error_msg}")
        raise ValueError(f"SQL validation error: {error_msg}")
    
    start_time = time.time()
    
    conn = get_db_connection()
    logger.info("SQL Server connection established successfully")
    cursor = conn.cursor()
    try:
        if params:
            cursor.execute(sql_query, params)
        else:
            cursor.execute(sql_query)
        
        # Fetch column names
        columns = [column[0] for column in cursor.description] if cursor.description else []
        # Fetch all rows
        rows = cursor.fetchall()
        
        # Convert to list of dicts, handling Decimal and other non-JSON serializable types
        results = []
        for row in rows:
            row_dict = {}
            for col, val in zip(columns, row):
                # Convert Decimal to float for JSON serialization
                if isinstance(val, Decimal):
                    row_dict[col] = float(val)
                else:
                    row_dict[col] = val
            results.append(row_dict)
        
        elapsed_ms = (time.time() - start_time) * 1000
        logger.info("SQL execution completed in %.0fms, returned %d row(s)", elapsed_ms, len(results))
        if results:
            logger.info("SQL execution sample row: %s", results[0])
        
        return results
    except Exception:
        logger.exception("SQL execution failed")
        raise
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        logger.info("SQL Server connection closed")


def get_database_tables() -> List[str]:
    """
    Returns a list of table names available in the current database.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE'"
        )
        rows = cursor.fetchall()
        return [f"{schema}.{name}" for schema, name in rows]
    finally:
        cursor.close()
        conn.close()


def get_database_schema() -> List[str]:
    """
    Returns a concise list of table names with their columns for the current database.
    Optimized for LLM processing - shows key fact and dimension tables only.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
        )
        rows = cursor.fetchall()
        schema_map = {}
        
        # Prioritize fact and dimension tables, limit schema size
        priority_keywords = ['Fact_', 'D_', '_view', 'Detail']
        
        for schema, table, column in rows:
            table_full = f"{schema}.{table}"
            # Skip if we have too many tables (limit to ~15 key tables)
            if len(schema_map) >= 15 and table_full not in schema_map:
                continue
            
            if table_full not in schema_map:
                schema_map[table_full] = []
            schema_map[table_full].append(f"[{column}]")
        
        # Format as: [dbo].[TableName]([Col1], [Col2], ...)
        result = []
        for table_full, columns in sorted(schema_map.items()):
            parts = table_full.split('.')
            formatted_table = f"[{parts[0]}].[{parts[1]}]"
            # Limit columns shown per table (show first 15)
            limited_cols = columns[:15]
            result.append(f"{formatted_table}({', '.join(limited_cols)})")
        
        return result
    finally:
        cursor.close()
        conn.close()


def get_database_identifiers() -> List[str]:
    """
    Returns a list of database identifiers including table and column names.
    Includes bracketed names for exact matching.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
        )
        rows = cursor.fetchall()
        identifiers = set()
        for schema, table, column in rows:
            identifiers.add(schema.upper())
            identifiers.add(table.upper())
            identifiers.add(column.upper())
            identifiers.add(f"{schema}.{table}".upper())
            identifiers.add(f"[{schema}].[{table}]".upper())
            identifiers.add(f"[{schema}]".upper())
            identifiers.add(f"[{column}]".upper())
        return sorted(identifiers)
    finally:
        cursor.close()
        conn.close()


def get_database_tables() -> List[str]:
    """
    Returns a list of table names available in the current database.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE'"
        )
        rows = cursor.fetchall()
        return [f"{schema}.{name}" for schema, name in rows]
    finally:
        cursor.close()
        conn.close()


def get_database_schema() -> List[str]:
    """
    Returns a concise list of table names with their columns for the current database.
    Optimized for LLM processing - shows key fact and dimension tables only.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
        )
        rows = cursor.fetchall()
        schema_map = {}
        
        # Prioritize fact and dimension tables, limit schema size
        priority_keywords = ['Fact_', 'D_', '_view', 'Detail']
        
        for schema, table, column in rows:
            table_full = f"{schema}.{table}"
            # Skip if we have too many tables (limit to ~15 key tables)
            if len(schema_map) >= 15 and table_full not in schema_map:
                continue
            
            if table_full not in schema_map:
                schema_map[table_full] = []
            schema_map[table_full].append(f"[{column}]")
        
        # Format as: [dbo].[TableName]([Col1], [Col2], ...)
        result = []
        for table_full, columns in sorted(schema_map.items()):
            parts = table_full.split('.')
            formatted_table = f"[{parts[0]}].[{parts[1]}]"
            # Limit columns shown per table (show first 15)
            limited_cols = columns[:15]
            result.append(f"{formatted_table}({', '.join(limited_cols)})")
        
        return result
    finally:
        cursor.close()
        conn.close()

def get_database_identifiers() -> List[str]:
    """
    Returns a list of database identifiers including table and column names.
    Includes bracketed names for exact matching.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
        )
        rows = cursor.fetchall()
        identifiers = set()
        for schema, table, column in rows:
            identifiers.add(schema.upper())
            identifiers.add(table.upper())
            identifiers.add(column.upper())
            identifiers.add(f"{schema}.{table}".upper())
            identifiers.add(f"[{schema}].[{table}]".upper())
            identifiers.add(f"[{schema}]".upper())
            identifiers.add(f"[{column}]".upper())
        return sorted(identifiers)
    finally:
        cursor.close()
        conn.close()