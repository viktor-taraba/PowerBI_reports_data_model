import argparse
import hashlib
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Optional
import pandas as pd
import pyodbc
import config

try:
    from pbixray import PBIXRay, LiveConnectionError, NoEmbeddedModelError
except ImportError:
    print("pbixray is not installed. Run: pip install pbixray", file=sys.stderr)
    raise


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("pbix_extractor")


# Helpers
def sha256_of_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute SHA-256 of a file for dedup / change-detection purposes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def df_col(df: pd.DataFrame, *candidates: str) -> Optional[str]:
    """Return the first column name in `df` that matches one of `candidates`
    (case-insensitive), or None if none exist. Used because pbixray's exact
    dataframe column names can vary slightly between versions."""
    lower_map = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def safe_get(df: pd.DataFrame, row, *candidates: str):
    col = df_col(df, *candidates)
    if col is None:
        return None
    val = row[col]
    if pd.isna(val):
        return None
    return val


def df_to_json(df: Optional[pd.DataFrame]) -> Optional[str]:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None
    # default=str handles Timestamps / other non-JSON-native types
    return json.dumps(df.to_dict(orient="records"), default=str)


def safe_executemany(cursor, query: str, rows: list):
    """
    Bypasses pyodbc batch type inference bugs.
    Sorts rows so the most populated row comes first, helping SQL Server guess
    the correct types. Falls back to row-by-row execution if it still fails.
    """
    if not rows:
        return

    # Trick 1: Sort rows so the row with the FEWEST 'None' values is at the top.
    # This almost always guarantees pyodbc sees the real string/int types first.
    rows_sorted = sorted(rows, key=lambda r: sum(val is None for val in r))

    try:
        cursor.executemany(query, rows_sorted)
    except pyodbc.Error:
        # Trick 2: If mutually exclusive sparse columns still cause a crash,
        # fall back to executing row-by-row, which completely avoids the batch inference issue.
        for row in rows:
            cursor.execute(query, row)


# SQL insert helpers
def insert_file_record(cursor, file_path: Path) -> int:
    """Upsert keyed on the persisted hash of FilePath (FilePathHash), since
    FilePath itself is too wide (NVARCHAR(1024)) to carry a unique index."""
    stat = file_path.stat()
    path_str = str(file_path)
    cursor.execute(
        """
        MERGE dbo.PbixFiles AS target
        USING (SELECT CONVERT(VARBINARY(32), HASHBYTES('SHA2_256', ?)) AS FilePathHash) AS src
        ON target.FilePathHash = src.FilePathHash
        WHEN MATCHED THEN
            UPDATE SET FileName = ?, FilePath = ?, FileSizeBytes = ?, FileHash = ?,
                       ScanStartedAt = SYSDATETIME(), ScanFinishedAt = NULL,
                       Status = 'Pending', ErrorMessage = NULL
        WHEN NOT MATCHED THEN
            INSERT (FileName, FilePath, FileSizeBytes, FileHash, Status)
            VALUES (?, ?, ?, ?, 'Pending')
        OUTPUT inserted.FileId;
        """,
        path_str,
        file_path.name,
        path_str,
        stat.st_size,
        sha256_of_file(file_path),
        file_path.name,
        path_str,
        stat.st_size,
        sha256_of_file(file_path),
    )
    file_id = cursor.fetchone()[0]
    return file_id


def finalize_file_record(cursor, file_id: int, status: str, error_message: Optional[str] = None):
    cursor.execute(
        """
        UPDATE dbo.PbixFiles
        SET Status = ?, ErrorMessage = ?, ScanFinishedAt = SYSDATETIME()
        WHERE FileId = ?
        """,
        status,
        error_message,
        file_id,
    )


def clear_previous_extract(cursor, file_id: int):
    """Wipe any previously loaded child rows for this file so re-running
    the script on an updated .pbix doesn't duplicate data."""
    for table in (
        "ModelColumns", "ModelTables", "DaxMeasures", "DaxCalculatedTables",
        "PowerQueries", "MParameters", "ModelRelationships",
        "ModelColumnStatistics", "ModelMetadata", "ModelRawExtracts",
    ):
        cursor.execute(f"DELETE FROM dbo.{table} WHERE FileId = ?", file_id)


def insert_tables_and_columns(cursor, file_id: int, model: PBIXRay):
    schema_df = model.schema
    table_names = list(model.tables) if model.tables is not None else []

    # Try to get row counts from statistics, keyed by table name
    row_counts = {}
    stats_df = getattr(model, "statistics", None)
    if isinstance(stats_df, pd.DataFrame) and not stats_df.empty:
        tname_col = df_col(stats_df, "TableName", "Table")
        rows_col = df_col(stats_df, "RowCount", "Cardinality", "Rows")
        if tname_col and rows_col:
            for _, r in stats_df.iterrows():
                row_counts.setdefault(r[tname_col], safe_get(stats_df, r, "RowCount", "Cardinality", "Rows"))

    calc_table_names = set()
    dax_tables_df = getattr(model, "dax_tables", None)
    if isinstance(dax_tables_df, pd.DataFrame) and not dax_tables_df.empty:
        tname_col = df_col(dax_tables_df, "TableName", "Name")
        if tname_col:
            calc_table_names = set(dax_tables_df[tname_col].tolist())

    for t in table_names:
        cursor.execute(
            """
            INSERT INTO dbo.ModelTables (FileId, TableName, IsCalculatedTable, TableRowCount)
            VALUES (?, ?, ?, ?)
            """,
            file_id, t, 1 if t in calc_table_names else 0, row_counts.get(t),
        )

    if isinstance(schema_df, pd.DataFrame) and not schema_df.empty:
        tname_col = df_col(schema_df, "TableName", "Table")
        cname_col = df_col(schema_df, "ColumnName", "Column")
        dtype_col = df_col(schema_df, "PandasDataType", "DataType", "Type")
        hidden_col = df_col(schema_df, "IsHidden", "Hidden")
        key_col = df_col(schema_df, "IsKey", "Key")
        sortby_col = df_col(schema_df, "SortByColumn", "SortBy")

        rows = []
        for _, r in schema_df.iterrows():
            rows.append((
                file_id,
                r[tname_col] if tname_col else None,
                r[cname_col] if cname_col else None,
                str(r[dtype_col]) if dtype_col and not pd.isna(r[dtype_col]) else None,
                bool(r[hidden_col]) if hidden_col and not pd.isna(r[hidden_col]) else None,
                bool(r[key_col]) if key_col and not pd.isna(r[key_col]) else None,
                r[sortby_col] if sortby_col and not pd.isna(r[sortby_col]) else None,
            ))
        safe_executemany(
            cursor,
            """
            INSERT INTO dbo.ModelColumns
                (FileId, TableName, ColumnName, DataType, IsHidden, IsKey, SortByColumn)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def insert_dax_measures(cursor, file_id: int, model: PBIXRay):
    df = getattr(model, "dax_measures", None)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return
    tname_col = df_col(df, "TableName", "Table")
    mname_col = df_col(df, "MeasureName", "Name")
    expr_col = df_col(df, "Expression", "DAXExpression")
    fmt_col = df_col(df, "FormatString", "Format")
    hidden_col = df_col(df, "IsHidden", "Hidden")
    desc_col = df_col(df, "Description")

    rows = []
    for _, r in df.iterrows():
        rows.append((
            file_id,
            r[tname_col] if tname_col else None,
            r[mname_col] if mname_col else None,
            r[expr_col] if expr_col else None,
            r[fmt_col] if fmt_col and not pd.isna(r[fmt_col]) else None,
            bool(r[hidden_col]) if hidden_col and not pd.isna(r[hidden_col]) else None,
            r[desc_col] if desc_col and not pd.isna(r[desc_col]) else None,
        ))
    safe_executemany(
        cursor,
        """
        INSERT INTO dbo.DaxMeasures
            (FileId, TableName, MeasureName, Expression, FormatString, IsHidden, Description)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def insert_dax_calculated_tables(cursor, file_id: int, model: PBIXRay):
    df = getattr(model, "dax_tables", None)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return
    tname_col = df_col(df, "TableName", "Name")
    expr_col = df_col(df, "Expression")
    rows = [(file_id, r[tname_col] if tname_col else None,
              r[expr_col] if expr_col else None) for _, r in df.iterrows()]
    safe_executemany(
        cursor,
        "INSERT INTO dbo.DaxCalculatedTables (FileId, TableName, Expression) VALUES (?, ?, ?)",
        rows,
    )


def insert_power_query(cursor, file_id: int, model: PBIXRay):
    df = getattr(model, "power_query", None)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return
    tname_col = df_col(df, "TableName", "Name")
    expr_col = df_col(df, "Expression")
    rows = [(file_id, r[tname_col] if tname_col else None,
              r[expr_col] if expr_col else None) for _, r in df.iterrows()]
    safe_executemany(
        cursor,
        "INSERT INTO dbo.PowerQueries (FileId, TableName, Expression) VALUES (?, ?, ?)",
        rows,
    )


def insert_m_parameters(cursor, file_id: int, model: PBIXRay):
    df = getattr(model, "m_parameters", None)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return
    pname_col = df_col(df, "ParameterName", "Name")
    desc_col = df_col(df, "Description")
    expr_col = df_col(df, "Expression")
    mod_col = df_col(df, "ModifiedTime", "ModifiedDate")

    rows = []
    for _, r in df.iterrows():
        rows.append((
            file_id,
            r[pname_col] if pname_col else None,
            r[desc_col] if desc_col and not pd.isna(r[desc_col]) else None,
            r[expr_col] if expr_col else None,
            r[mod_col] if mod_col and not pd.isna(r[mod_col]) else None,
        ))
    safe_executemany(
        cursor,
        """
        INSERT INTO dbo.MParameters (FileId, ParameterName, Description, Expression, ModifiedTime)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )


def insert_relationships(cursor, file_id: int, model: PBIXRay):
    df = getattr(model, "relationships", None)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return
    from_t = df_col(df, "FromTableName", "FromTable")
    from_c = df_col(df, "FromColumnName", "FromColumn")
    to_t = df_col(df, "ToTableName", "ToTable")
    to_c = df_col(df, "ToColumnName", "ToColumn")
    card = df_col(df, "Cardinality")
    xfilter = df_col(df, "CrossFilterDirection", "FilterDirection")
    active = df_col(df, "IsActive", "Active")

    rows = []
    for _, r in df.iterrows():
        rows.append((
            file_id,
            r[from_t] if from_t else None,
            r[from_c] if from_c else None,
            r[to_t] if to_t else None,
            r[to_c] if to_c else None,
            str(r[card]) if card and not pd.isna(r[card]) else None,
            str(r[xfilter]) if xfilter and not pd.isna(r[xfilter]) else None,
            bool(r[active]) if active and not pd.isna(r[active]) else None,
        ))
    safe_executemany(
        cursor,
        """
        INSERT INTO dbo.ModelRelationships
            (FileId, FromTable, FromColumn, ToTable, ToColumn, Cardinality, CrossFilterDirection, IsActive)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def insert_column_statistics(cursor, file_id: int, model: PBIXRay):
    df = getattr(model, "statistics", None)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return
    tname_col = df_col(df, "TableName", "Table")
    cname_col = df_col(df, "ColumnName", "Column")
    card_col = df_col(df, "Cardinality")
    total_col = df_col(df, "TotalSize", "TotalSizeBytes")
    data_col = df_col(df, "DataSize", "DataSizeBytes")
    dict_col = df_col(df, "DictionarySize", "DictionarySizeBytes")
    enc_col = df_col(df, "Encoding")

    rows = []
    for _, r in df.iterrows():
        rows.append((
            file_id,
            r[tname_col] if tname_col else None,
            r[cname_col] if cname_col else None,
            int(r[card_col]) if card_col and not pd.isna(r[card_col]) else None,
            int(r[total_col]) if total_col and not pd.isna(r[total_col]) else None,
            int(r[data_col]) if data_col and not pd.isna(r[data_col]) else None,
            int(r[dict_col]) if dict_col and not pd.isna(r[dict_col]) else None,
            str(r[enc_col]) if enc_col and not pd.isna(r[enc_col]) else None,
        ))
    safe_executemany(
        cursor,
        """
        INSERT INTO dbo.ModelColumnStatistics
            (FileId, TableName, ColumnName, Cardinality, TotalSizeBytes, DataSizeBytes, DictionarySizeBytes, Encoding)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def insert_generic_metadata(cursor, file_id: int, model: PBIXRay):
    meta = getattr(model, "metadata", None)
    rows = []
    if isinstance(meta, pd.DataFrame) and not meta.empty:
        key_col = df_col(meta, "Key", "Name", "MetadataKey")
        val_col = df_col(meta, "Value", "MetadataValue")
        for _, r in meta.iterrows():
            rows.append((file_id, str(r[key_col]) if key_col else "unknown",
                          str(r[val_col]) if val_col and not pd.isna(r[val_col]) else None))
    elif isinstance(meta, dict):
        for k, v in meta.items():
            rows.append((file_id, str(k), str(v)))
    if rows:
        safe_executemany(
            cursor,
            "INSERT INTO dbo.ModelMetadata (FileId, MetadataKey, MetadataValue) VALUES (?, ?, ?)",
            rows,
        )


def insert_raw_extracts(cursor, file_id: int, model: PBIXRay):
    """Store a full JSON dump of every relevant dataframe as a safety net,
    so nothing structural is lost even across pbixray version differences."""
    extract_names = [
        "schema", "relationships", "dax_measures", "dax_tables",
        "power_query", "m_parameters", "statistics",
    ]
    rows = []
    for name in extract_names:
        df = getattr(model, name, None)
        json_blob = df_to_json(df) if isinstance(df, pd.DataFrame) else None
        if json_blob is not None:
            rows.append((file_id, name, json_blob))
    if rows:
        safe_executemany(
            cursor,
            "INSERT INTO dbo.ModelRawExtracts (FileId, ExtractName, ExtractJson) VALUES (?, ?, ?)",
            rows,
        )


# Per-file processing
def process_file(conn: pyodbc.Connection, file_path: Path, on_disk: bool):
    cursor = conn.cursor()
    file_id = insert_file_record(cursor, file_path)
    conn.commit()

    try:
        clear_previous_extract(cursor, file_id)

        model = PBIXRay(str(file_path), on_disk=on_disk)
        try:
            log.info("START      insert_tables_and_columns <- %s", file_path)
            insert_tables_and_columns(cursor, file_id, model)
            log.info("START      insert_dax_measures <- %s", file_path)
            insert_dax_measures(cursor, file_id, model)
            log.info("START      insert_dax_calculated_tables <- %s", file_path)
            insert_dax_calculated_tables(cursor, file_id, model)
            log.info("START      insert_power_query <- %s", file_path)
            insert_power_query(cursor, file_id, model)
            log.info("START      insert_m_parameters <- %s", file_path)
            insert_m_parameters(cursor, file_id, model)
            log.info("START      insert_relationships <- %s", file_path)
            insert_relationships(cursor, file_id, model)
            log.info("START      insert_column_statistics <- %s", file_path)
            insert_column_statistics(cursor, file_id, model)
            log.info("START      insert_generic_metadata <- %s", file_path)
            insert_generic_metadata(cursor, file_id, model)
            log.info("START      insert_raw_extracts <- %s", file_path)
            insert_raw_extracts(cursor, file_id, model)
        finally:
            close = getattr(model, "close", None)
            if callable(close):
                close()

        log.info("START      finalize_file_record")
        finalize_file_record(cursor, file_id, "Success")
        conn.commit()
        log.info("OK      %s", file_path)
        return True

    except LiveConnectionError as e:
        conn.rollback()
        finalize_file_record(cursor, file_id, "LiveConnection",
                              f"connection_type={e.connection_type}, database={getattr(e, 'database_name', None)}")
        conn.commit()
        log.warning("LIVE    %s (thin report, no embedded model: %s)", file_path, e.connection_type)
        return False

    except NoEmbeddedModelError as e:
        conn.rollback()
        finalize_file_record(cursor, file_id, "NoModel", str(e))
        conn.commit()
        log.warning("NOMODEL %s", file_path)
        return False

    except Exception as e:
        conn.rollback()
        finalize_file_record(cursor, file_id, "Error", f"{e}\n{traceback.format_exc()}")
        conn.commit()
        log.error("FAIL    %s -> %s", file_path, e)
        return False


def main():
    parser = argparse.ArgumentParser(description="Extract PBIX structure/metadata into SQL Server.")
    parser.add_argument("--folder", default=None,
                         help="Root folder to scan recursively for .pbix files "
                              "(defaults to PBIX_FOLDER in .env/environment)")
    parser.add_argument("--on-disk", action="store_true", default=None,
                         help="Use pbixray on_disk=True (memory-mapped) mode for very large models "
                              "(defaults to PBIX_ON_DISK in .env/environment)")
    parser.add_argument("--pattern", default="*.pbix", help="Glob pattern for files (default *.pbix)")
    args = parser.parse_args()

    settings = config.load_settings()

    folder = args.folder or settings.pbix_folder
    if not folder:
        log.error("No folder specified. Pass --folder or set PBIX_FOLDER in .env.")
        sys.exit(1)

    on_disk = settings.pbix_on_disk if args.on_disk is None else args.on_disk

    root = Path(folder)
    if not root.exists():
        log.error("Folder does not exist: %s", root)
        sys.exit(1)

    files = sorted(root.rglob(args.pattern))
    log.info("Found %d file(s) under %s", len(files), root)
    if not files:
        return

    conn = pyodbc.connect(settings.connection_string, autocommit=False)
    conn.timeout = settings.db_connection_timeout
    try:
        # speeds up executemany() significantly with recent pyodbc/ODBC drivers
        conn.cursor().fast_executemany = True
    except Exception:
        pass

    success = errors = skipped = 0
    try:
        for i, file_path in enumerate(files, start=1):
            log.info("[%d/%d] Processing %s", i, len(files), file_path.name)
            try:
                # Check the boolean returned by process_file
                is_success = process_file(conn, file_path, on_disk)
                if is_success:
                    success += 1
                else:
                    errors += 1
            except Exception:
                # This now only triggers if something completely fatal 
                # happens outside of the handled process_file logic
                errors += 1
                log.error("Unhandled error on %s:\n%s", file_path, traceback.format_exc())
    finally:
        conn.close()

    log.info("Done. success=%d errors=%d total=%d", success, errors, len(files))


if __name__ == "__main__":
    main()