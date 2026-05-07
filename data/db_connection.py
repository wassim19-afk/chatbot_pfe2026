# data/db_connection.py
# This module handles database connections and query execution for SQL Server.
# It uses pyodbc for connectivity and includes SQL validation before execution.

import os
import pyodbc
import pyodbc
import time
from typing import List, Dict, Any
from config.settings import settings
from config.logger import get_logger
from services.sql_validator import validate_sql

logger = get_logger(__name__)


def _build_connection_string() -> str:
    driver = settings.DB_DRIVER or os.getenv('DB_DRIVER', 'ODBC Driver 18 for SQL Server')
    encrypt = settings.DB_ENCRYPT or os.getenv('DB_ENCRYPT', 'yes')
    trust_server_certificate = settings.DB_TRUST_SERVER_CERTIFICATE or os.getenv('DB_TRUST_SERVER_CERTIFICATE', 'yes')

    common_parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={settings.DB_SERVER}",
        f"DATABASE={settings.DB_DATABASE}",
        f"Encrypt={encrypt}",
        f"TrustServerCertificate={trust_server_certificate}",
        f"Connection Timeout={settings.DB_CONNECTION_TIMEOUT_SECONDS}",
    ]

    if settings.DB_PASSWORD:
        common_parts.extend([
            f"UID={settings.DB_USERNAME}",
            f"PWD={settings.DB_PASSWORD}",
        ])
    else:
        common_parts.append("Trusted_Connection=yes")

    return ";".join(common_parts) + ";"

def get_db_connection():
    """
    Establishes and returns a connection to the SQL Server database.
    Uses centralized settings for configuration to keep credentials secure.
    """
    logger.info(
        "Creating SQL Server connection (server=%s, database=%s, driver=%s, username_set=%s, password_set=%s)",
        settings.DB_SERVER,
        settings.DB_DATABASE,
        settings.DB_DRIVER,
        bool(settings.DB_USERNAME),
        bool(settings.DB_PASSWORD),
    )

    conn_str = _build_connection_string()
    logger.info(
        "Opening SQL Server connection with DRIVER=%s, Encrypt=%s, TrustServerCertificate=%s, timeout=%s",
        settings.DB_DRIVER,
        settings.DB_ENCRYPT,
        settings.DB_TRUST_SERVER_CERTIFICATE,
        settings.DB_CONNECTION_TIMEOUT_SECONDS,
    )

    try:
        return pyodbc.connect(conn_str)
    except pyodbc.Error:
        logger.exception("pyodbc connection failed")
        raise


def test_db_connection() -> bool:
    """
    Verify the SQL Server connection using `SELECT 1`.

    Returns:
        bool: True when the connection and query succeed.

    Raises:
        pyodbc.Error: If the connection or query fails.
    """
    logger.info("Starting SQL connection test using SELECT 1")
    conn = None
    cursor = None
    try:
        logger.info("Installed pyodbc drivers before test: %s", pyodbc.drivers())
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 AS ok")
        row = cursor.fetchone()
        success = bool(row and row[0] == 1)
        logger.info("SQL connection test succeeded: %s", success)
        return success
    except pyodbc.Error:
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
        columns = [column[0] for column in cursor.description]
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
        cursor.close()
        conn.close()
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