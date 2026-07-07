Reports:
- https://github.com/iBalajiShanmugam/Powerbi/tree/main/projects
- https://github.com/microsoft/powerbi-desktop-samples/tree/main/new-power-bi-service-samples
- https://github.com/microsoft/powerbi-desktop-samples/tree/main/Sample%20Reports
- https://zebrabi.com/templates/
- https://github.com/Dashboard-Design/Power-BI-Design-Files/tree/main/Full%20Dashboards

Scans a folder (recursively) for .pbix files, extracts model STRUCTURE and
METADATA using the `pbixray` package (tables, columns, DAX measures,
calculated tables, Power Query / M code, M parameters, relationships,
VertiPaq column statistics, and generic metadata), and loads everything
into a SQL Server database. Actual data-table ROWS are never read or
stored.

Configuration:
    Connection details come from config.py, which reads environment
    variables (and a local .env file via python-dotenv if present).
    Copy .env.example to .env and fill in your server/database/credentials.

Usage:
    python extract_pbix_to_sql.py --folder "D:\\PowerBI\\Reports" --on-disk

    # Folder can also be set via PBIX_FOLDER in .env, in which case:
    python extract_pbix_to_sql.py

Run schema.sql once against your SQL Server instance before running this.