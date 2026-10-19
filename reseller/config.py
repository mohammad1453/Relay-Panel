import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


@dataclass
class Config:
    db: str
    origin: str
    production: bool = True
    xui_url: str = ""
    xui_token: str = ""
    xui_username: str = ""
    xui_password: str = ""
    link_host: str = ""
    sub_url: str = ""
    timezone: str = "Asia/Tehran"
    session_seconds: int = 43200
    idle_seconds: int = 1800
    sync_seconds: int = 120
    connect_timeout: float = 5
    timeout: float = 20
    xui_database_paths: str = ""
    base_path: str = ""
    public_http: bool = False

    @classmethod
    def env(cls):
        return cls(db=os.environ.get("APP_DB", ""), origin=os.environ.get("APP_ORIGIN", ""),
                   production=os.getenv("APP_ENV", "production") == "production",
                   xui_url=os.getenv("XUI_URL", ""), xui_token=os.getenv("XUI_TOKEN", ""),
                   xui_username=os.getenv("XUI_USERNAME", ""), xui_password=os.getenv("XUI_PASSWORD", ""),
                   link_host=os.getenv("XUI_LINK_HOST", ""), sub_url=os.getenv("XUI_SUB_URL", ""),
                   timezone=os.getenv("APP_TIMEZONE", "Asia/Tehran"),
                   session_seconds=int(os.getenv("SESSION_SECONDS", "43200")),
                   idle_seconds=int(os.getenv("SESSION_IDLE_SECONDS", "1800")),
                   sync_seconds=int(os.getenv("SYNC_SECONDS", "120")),
                   connect_timeout=float(os.getenv("XUI_CONNECT_TIMEOUT", "5")),
                   timeout=float(os.getenv("XUI_TIMEOUT", "20")),
                   xui_database_paths=os.getenv("XUI_DATABASE_PATHS", ""),
                   base_path=os.getenv("APP_BASE_PATH", ""),
                   public_http=os.getenv("APP_ENV", "production") == "public-http")

    def validate(self):
        if not self.db or not self.origin:
            raise ValueError("APP_DB and APP_ORIGIN are required")
        origin = urlsplit(self.origin)
        if origin.scheme not in ("http", "https") or not origin.hostname or origin.path not in ("", "/") or origin.username or origin.password or origin.query or origin.fragment:
            raise ValueError("APP_ORIGIN must be an absolute origin")
        if self.production and origin.scheme != "https":
            raise ValueError("Production requires HTTPS APP_ORIGIN")
        if self.public_http and (origin.scheme != "http" or origin.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ValueError("Public HTTP mode requires a non-loopback HTTP APP_ORIGIN")
        if self.base_path and (not self.base_path.startswith("/") or self.base_path.endswith("/") or
                               any(part in ("", ".", "..") for part in self.base_path.split("/")[1:]) or
                               not all(c.isalnum() or c in "-_/" for c in self.base_path)):
            raise ValueError("APP_BASE_PATH must look like /relay-panel")
        # Public HTTP is reachable from the internet, so it keeps every
        # configuration check that production mode has.
        strict = self.production or self.public_http
        if strict and not self.xui_database_paths:
            raise ValueError("Declare XUI_DATABASE_PATHS after inspecting the existing installation")
        raw_path = Path(self.db).absolute()
        path = raw_path.resolve()
        if path.name != "app.db" or raw_path.is_symlink() or raw_path != path:
            raise ValueError("Use a dedicated application-owned app.db")
        for other in self.xui_database_paths.split(","):
            if other and other != "external" and path == Path(other).resolve():
                raise ValueError("Application database conflicts with 3X-UI database")
        if self.xui_url:
            url = urlsplit(self.xui_url)
            if url.username or url.password or url.query or url.fragment or not url.hostname:
                raise ValueError("Invalid XUI_URL")
            if url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")):
                raise ValueError("XUI_URL requires HTTPS except on loopback")
        if strict and (not self.xui_url or not (self.xui_token or self.xui_username and self.xui_password)):
            raise ValueError("Configure master URL and authentication")
        if self.sub_url:
            sub = urlsplit(self.sub_url)
            if sub.scheme != "https" or not sub.hostname or sub.username or sub.password or sub.query or sub.fragment:
                raise ValueError("XUI_SUB_URL must be an HTTPS URL without credentials, query or fragment")
        if not 30 <= self.sync_seconds <= 86400 or not 1 <= self.connect_timeout <= self.timeout <= 120:
            raise ValueError("Invalid synchronization/timeout configuration")
        if not 60 <= self.idle_seconds <= self.session_seconds <= 604800:
            raise ValueError("Invalid session lifetime")
        ZoneInfo(self.timezone)
        return self
