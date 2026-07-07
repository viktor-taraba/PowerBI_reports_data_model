"""
config.py

Loads configuration from environment variables (and a local .env file, if
present via python-dotenv) and builds the pyodbc connection string. Keeps
credentials out of shell history / process list and out of source control.
"""
import os
from dataclasses import dataclass
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads .env from the current working directory if present
except ImportError:
    pass  # python-dotenv not installed -> falls back to real env vars only


def _get_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y")


@dataclass(frozen=True)
class Settings:
    db_driver: str
    db_server: str
    db_database: str
    db_auth_mode: str          # "sql" or "windows"
    db_uid: Optional[str]
    db_pwd: Optional[str]
    db_encrypt: bool
    db_trust_server_certificate: bool
    db_connection_timeout: int
    pbix_folder: Optional[str]
    pbix_on_disk: bool

    @property
    def connection_string(self) -> str:
        driver = self.db_driver.strip("{}")
        parts = [
            f"Driver={{{driver}}}",
            f"Server={self.db_server}",
            f"Database={self.db_database}",
        ]
        if self.db_auth_mode == "windows":
            parts.append("Trusted_Connection=yes")
        else:
            if not self.db_uid or not self.db_pwd:
                raise ValueError(
                    "DB_AUTH_MODE=sql requires DB_UID and DB_PWD to be set (.env or environment)."
                )
            parts.append(f"UID={self.db_uid}")
            parts.append(f"PWD={self.db_pwd}")
        parts.append(f"Encrypt={'yes' if self.db_encrypt else 'no'}")
        parts.append(f"TrustServerCertificate={'yes' if self.db_trust_server_certificate else 'no'}")
        parts.append(f"Connection Timeout={self.db_connection_timeout}")
        return ";".join(parts)


def load_settings() -> Settings:
    server = os.getenv("DB_SERVER")
    auth_mode = os.getenv("DB_AUTH_MODE", "windows").strip().lower()

    if not server:
        raise ValueError("DB_SERVER is required (set it in .env or the environment).")
    if auth_mode not in ("sql", "windows"):
        raise ValueError("DB_AUTH_MODE must be 'sql' or 'windows'.")

    return Settings(
        db_driver=os.getenv("DB_DRIVER", "ODBC Driver 18 for SQL Server"),
        db_server=server,
        db_database=os.getenv("DB_DATABASE", "PbixMetadata"),
        db_auth_mode=auth_mode,
        db_uid=os.getenv("DB_UID"),
        db_pwd=os.getenv("DB_PWD"),
        db_encrypt=_get_bool("DB_ENCRYPT", True),
        db_trust_server_certificate=_get_bool("DB_TRUST_SERVER_CERTIFICATE", False),
        db_connection_timeout=int(os.getenv("DB_CONNECTION_TIMEOUT", "30")),
        pbix_folder=os.getenv("PBIX_FOLDER"),
        pbix_on_disk=_get_bool("PBIX_ON_DISK", False),
    )