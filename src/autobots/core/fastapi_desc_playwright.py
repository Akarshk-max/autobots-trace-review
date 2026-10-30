from typing import Any, Dict

from pydantic import BaseModel


class FastAPIDescPlaywright(BaseModel):
    title: str = "Autobots-playwright"
    # description = metadata.metadata("app").get("summary")
    description: str = """Autobots-playwright-container enables web scraping seamlessly in containerized environment."""
    # version = metadata.version("app")
    version: str = "0.1.0"
    # terms_of_service="http://example.com/terms/"
    contact: Dict[str, Any] = {
        # "name": metadata.metadata("app").get("author"),
        "name": "Ayush Kumar",
        # "url": "http://x-force.example.com/contact/",
        # "email": metadata.metadata("app").get("Author-email")
        "email": "ayush@runkai.ai",
    }
    license_info: Dict[str, str] = {
        "name": "Apache 2.0",
        "url": "https://www.apache.org/licenses/LICENSE-2.0.html",
    }
    docs_url: str | None = None
    redoc_url: str | None = None
    openapi_url: str | None = None
