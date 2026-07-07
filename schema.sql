/* ============================================================
   PBIX Metadata Warehouse - SQL Server DDL
   Stores STRUCTURE and METADATA extracted from .pbix files via
   the pbixray Python package. No actual data rows are stored.
   ============================================================ */

IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name = 'PbixMetadata')
BEGIN
    CREATE DATABASE PbixMetadata;
END
GO

USE PbixMetadata;
GO

/* ---------- 1. One row per scanned .pbix file ---------- */
IF OBJECT_ID('dbo.PbixFiles', 'U') IS NOT NULL DROP TABLE dbo.PbixFiles;
CREATE TABLE dbo.PbixFiles (
    FileId          INT IDENTITY(1,1) PRIMARY KEY,
    FileName        NVARCHAR(255)   NOT NULL,
    FilePath        NVARCHAR(1024)  NOT NULL,
    /* NVARCHAR(1024) is 2048 bytes -- too wide for a unique index key
       (1700-byte limit), and paths can legitimately be long. So we
       uniquely constrain on a persisted hash of the path instead. */
    FilePathHash AS CONVERT(VARBINARY(32), HASHBYTES('SHA2_256', FilePath)) PERSISTED,
    FileSizeBytes   BIGINT          NULL,
    FileHash        CHAR(64)        NULL,        -- SHA-256 of file contents, for change detection
    ScanStartedAt   DATETIME2       NOT NULL DEFAULT SYSDATETIME(),
    ScanFinishedAt  DATETIME2       NULL,
    Status          NVARCHAR(50)    NOT NULL DEFAULT 'Pending',  -- Pending/Success/LiveConnection/NoModel/Error
    ErrorMessage    NVARCHAR(MAX)   NULL,
    CONSTRAINT UQ_PbixFiles_PathHash UNIQUE (FilePathHash)
);
GO

/* ---------- 2. Tables present in the data model ---------- */
IF OBJECT_ID('dbo.ModelTables', 'U') IS NOT NULL DROP TABLE dbo.ModelTables;
CREATE TABLE dbo.ModelTables (
    ModelTableId    INT IDENTITY(1,1) PRIMARY KEY,
    FileId          INT NOT NULL FOREIGN KEY REFERENCES dbo.PbixFiles(FileId) ON DELETE CASCADE,
    TableName       NVARCHAR(255) NOT NULL,
    IsCalculatedTable BIT NOT NULL DEFAULT 0,
    TableRowCount   BIGINT NULL     -- row count from VertiPaq statistics, not the rows themselves
);
CREATE INDEX IX_ModelTables_FileId ON dbo.ModelTables(FileId);
GO

/* ---------- 3. Columns per table ---------- */
IF OBJECT_ID('dbo.ModelColumns', 'U') IS NOT NULL DROP TABLE dbo.ModelColumns;
CREATE TABLE dbo.ModelColumns (
    ModelColumnId   INT IDENTITY(1,1) PRIMARY KEY,
    FileId          INT NOT NULL FOREIGN KEY REFERENCES dbo.PbixFiles(FileId) ON DELETE CASCADE,
    TableName       NVARCHAR(255) NOT NULL,
    ColumnName      NVARCHAR(255) NOT NULL,
    DataType        NVARCHAR(100) NULL,
    IsHidden        BIT NULL,
    IsKey           BIT NULL,
    SortByColumn    NVARCHAR(255) NULL
);
CREATE INDEX IX_ModelColumns_FileId ON dbo.ModelColumns(FileId);
GO

/* ---------- 4. DAX measures ---------- */
IF OBJECT_ID('dbo.DaxMeasures', 'U') IS NOT NULL DROP TABLE dbo.DaxMeasures;
CREATE TABLE dbo.DaxMeasures (
    MeasureId       INT IDENTITY(1,1) PRIMARY KEY,
    FileId          INT NOT NULL FOREIGN KEY REFERENCES dbo.PbixFiles(FileId) ON DELETE CASCADE,
    TableName       NVARCHAR(255) NULL,
    MeasureName     NVARCHAR(255) NOT NULL,
    Expression      NVARCHAR(MAX) NULL,
    FormatString    NVARCHAR(255) NULL,
    IsHidden        BIT NULL,
    Description     NVARCHAR(MAX) NULL
);
CREATE INDEX IX_DaxMeasures_FileId ON dbo.DaxMeasures(FileId);
GO

/* ---------- 5. Calculated tables (DAX-defined tables) ---------- */
IF OBJECT_ID('dbo.DaxCalculatedTables', 'U') IS NOT NULL DROP TABLE dbo.DaxCalculatedTables;
CREATE TABLE dbo.DaxCalculatedTables (
    CalcTableId     INT IDENTITY(1,1) PRIMARY KEY,
    FileId          INT NOT NULL FOREIGN KEY REFERENCES dbo.PbixFiles(FileId) ON DELETE CASCADE,
    TableName       NVARCHAR(255) NOT NULL,
    Expression      NVARCHAR(MAX) NULL
);
CREATE INDEX IX_DaxCalculatedTables_FileId ON dbo.DaxCalculatedTables(FileId);
GO

/* ---------- 6. Power Query (M) code per table ---------- */
IF OBJECT_ID('dbo.PowerQueries', 'U') IS NOT NULL DROP TABLE dbo.PowerQueries;
CREATE TABLE dbo.PowerQueries (
    PowerQueryId    INT IDENTITY(1,1) PRIMARY KEY,
    FileId          INT NOT NULL FOREIGN KEY REFERENCES dbo.PbixFiles(FileId) ON DELETE CASCADE,
    TableName       NVARCHAR(255) NOT NULL,
    Expression      NVARCHAR(MAX) NULL
);
CREATE INDEX IX_PowerQueries_FileId ON dbo.PowerQueries(FileId);
GO

/* ---------- 7. M Parameters ---------- */
IF OBJECT_ID('dbo.MParameters', 'U') IS NOT NULL DROP TABLE dbo.MParameters;
CREATE TABLE dbo.MParameters (
    MParameterId    INT IDENTITY(1,1) PRIMARY KEY,
    FileId          INT NOT NULL FOREIGN KEY REFERENCES dbo.PbixFiles(FileId) ON DELETE CASCADE,
    ParameterName   NVARCHAR(255) NOT NULL,
    Description     NVARCHAR(MAX) NULL,
    Expression      NVARCHAR(MAX) NULL,
    ModifiedTime    DATETIME2 NULL
);
CREATE INDEX IX_MParameters_FileId ON dbo.MParameters(FileId);
GO

/* ---------- 8. Relationships between tables ---------- */
IF OBJECT_ID('dbo.ModelRelationships', 'U') IS NOT NULL DROP TABLE dbo.ModelRelationships;
CREATE TABLE dbo.ModelRelationships (
    RelationshipId  INT IDENTITY(1,1) PRIMARY KEY,
    FileId          INT NOT NULL FOREIGN KEY REFERENCES dbo.PbixFiles(FileId) ON DELETE CASCADE,
    FromTable       NVARCHAR(255) NULL,
    FromColumn      NVARCHAR(255) NULL,
    ToTable         NVARCHAR(255) NULL,
    ToColumn        NVARCHAR(255) NULL,
    Cardinality     NVARCHAR(50) NULL,
    CrossFilterDirection NVARCHAR(50) NULL,
    IsActive        BIT NULL
);
CREATE INDEX IX_ModelRelationships_FileId ON dbo.ModelRelationships(FileId);
GO

/* ---------- 9. VertiPaq column-level statistics (sizes, cardinality) ---------- */
IF OBJECT_ID('dbo.ModelColumnStatistics', 'U') IS NOT NULL DROP TABLE dbo.ModelColumnStatistics;
CREATE TABLE dbo.ModelColumnStatistics (
    ColumnStatId    INT IDENTITY(1,1) PRIMARY KEY,
    FileId          INT NOT NULL FOREIGN KEY REFERENCES dbo.PbixFiles(FileId) ON DELETE CASCADE,
    TableName       NVARCHAR(255) NULL,
    ColumnName      NVARCHAR(255) NULL,
    Cardinality     BIGINT NULL,
    TotalSizeBytes  BIGINT NULL,
    DataSizeBytes   BIGINT NULL,
    DictionarySizeBytes BIGINT NULL,
    Encoding        NVARCHAR(100) NULL
);
CREATE INDEX IX_ModelColumnStatistics_FileId ON dbo.ModelColumnStatistics(FileId);
GO

/* ---------- 10. Generic key/value metadata (model.metadata) ---------- */
IF OBJECT_ID('dbo.ModelMetadata', 'U') IS NOT NULL DROP TABLE dbo.ModelMetadata;
CREATE TABLE dbo.ModelMetadata (
    ModelMetadataId INT IDENTITY(1,1) PRIMARY KEY,
    FileId          INT NOT NULL FOREIGN KEY REFERENCES dbo.PbixFiles(FileId) ON DELETE CASCADE,
    MetadataKey     NVARCHAR(255) NOT NULL,
    MetadataValue   NVARCHAR(MAX) NULL
);
CREATE INDEX IX_ModelMetadata_FileId ON dbo.ModelMetadata(FileId);
GO

/* ---------- 11. Raw fallback: full JSON dump of every extracted dataframe ----------
   Guarantees no structural detail is lost even if a pbixray version
   exposes columns the typed tables above don't anticipate. */
IF OBJECT_ID('dbo.ModelRawExtracts', 'U') IS NOT NULL DROP TABLE dbo.ModelRawExtracts;
CREATE TABLE dbo.ModelRawExtracts (
    RawExtractId    INT IDENTITY(1,1) PRIMARY KEY,
    FileId          INT NOT NULL FOREIGN KEY REFERENCES dbo.PbixFiles(FileId) ON DELETE CASCADE,
    ExtractName     NVARCHAR(100) NOT NULL,   -- e.g. 'schema', 'relationships', 'dax_measures'
    ExtractJson     NVARCHAR(MAX) NULL        -- JSON array of the dataframe's records
);
CREATE INDEX IX_ModelRawExtracts_FileId ON dbo.ModelRawExtracts(FileId);
GO