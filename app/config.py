from __future__ import annotations

import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    ingest_token: str
    allowed_hosts: tuple[str, ...]
    max_workers: int
    app_env: str

    @classmethod
    def from_env(cls) -> "Settings":
        hosts = tuple(
            host.strip()
            for host in os.getenv(
                "ALLOWED_HOSTS", "jobhunt.prakhar.wtf,localhost,127.0.0.1,testserver"
            ).split(",")
            if host.strip()
        )
        return cls(
            database_url=_required("DATABASE_URL"),
            ingest_token=_required("INGEST_TOKEN"),
            allowed_hosts=hosts,
            max_workers=min(max(int(os.getenv("SCRAPER_CONCURRENCY", "8")), 1), 8),
            app_env=os.getenv("APP_ENV", "production"),
        )


settings = Settings.from_env()

