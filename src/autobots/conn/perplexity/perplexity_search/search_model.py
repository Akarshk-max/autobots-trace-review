import re
from datetime import datetime
from typing import List, Literal

from pydantic import BaseModel, ConfigDict

SearchRecency = Literal["hour", "day", "week", "month", "year"]
class PerplexitySearchPage(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str | None = None
    url: str | None = None
    snippet: str | None = None
    date: str | None = None
    last_updated: str | None = None


class PerplexitySearchReq(BaseModel):
    query: str | List[str]

    country: str | None = None
    max_results: int | None = 10
    max_tokens: int | None = 10_000
    max_tokens_per_page: int | None = 4096

    search_language_filter: List[str] | None = None
    search_domain_filter: List[str] | None = None

    last_updated_after_filter: str | None = None
    last_updated_before_filter: str | None = None
    search_after_date_filter: str | None = None
    search_before_date_filter: str | None = None
    search_recency_filter: SearchRecency | None = None

    @staticmethod
    def _ensure_date_format(value: str, field_name: str) -> None:
        try:
            # Accepts both 3/1/2025 and 03/01/2025
            datetime.strptime(value, "%m/%d/%Y")
        except ValueError as exc:
            raise ValueError(f"{field_name} must use MM/DD/YYYY format") from exc

    @staticmethod
    def _validate_domain_filter_entry(value: str) -> None:
        if not value:
            raise ValueError("search_domain_filter entries must be non-empty")

        candidate = value[1:] if value.startswith("-") else value
        candidate = candidate.strip()

        if not candidate:
            raise ValueError("search_domain_filter entries must be non-empty")

        if len(candidate) > 253:
            raise ValueError("Each search_domain_filter entry must be at most 253 characters")

        if "://" in candidate or "/" in candidate:
            raise ValueError(
                "search_domain_filter entries must be bare domains/domain parts/TLDs, not full URLs or paths"
            )

        if re.search(r"\s", candidate):
            raise ValueError("search_domain_filter entries must not contain whitespace")

    def is_multi_query(self) -> bool:
        return isinstance(self.query, list)

    def sanitize(self) -> None:
        if isinstance(self.query, str):
            self.query = self.query.strip()
            if not self.query:
                raise ValueError("query must be a non-empty string")
        else:
            normalized_queries = [q.strip() for q in self.query if isinstance(q, str) and q.strip()]
            if not normalized_queries:
                raise ValueError("query must contain at least one non-empty string")
            if len(normalized_queries) > 5:
                raise ValueError("Search API supports at most 5 queries in one request")
            self.query = normalized_queries

        if self.country is not None:
            self.country = self.country.strip().upper()
            if not re.fullmatch(r"[A-Z]{2}", self.country):
                raise ValueError("country must be a 2-letter ISO 3166-1 alpha-2 code")

        if self.max_results is not None and not (1 <= self.max_results <= 20):
            raise ValueError("max_results must be between 1 and 20")

        if self.max_tokens is not None and not (1 <= self.max_tokens <= 1_000_000):
            raise ValueError("max_tokens must be between 1 and 1,000,000")

        if self.max_tokens_per_page is not None and not (1 <= self.max_tokens_per_page <= 1_000_000):
            raise ValueError("max_tokens_per_page must be between 1 and 1,000,000")

        if self.search_language_filter:
            if len(self.search_language_filter) > 20:
                raise ValueError("search_language_filter supports at most 20 entries")
            normalized_languages: list[str] = []
            for value in self.search_language_filter:
                normalized = (value or "").strip().lower()
                if not re.fullmatch(r"[a-z]{2}", normalized):
                    raise ValueError(
                        "search_language_filter entries must be 2-letter ISO 639-1 codes like 'en' or 'fr'"
                    )
                normalized_languages.append(normalized)
            self.search_language_filter = normalized_languages

        if self.search_domain_filter:
            if len(self.search_domain_filter) > 20:
                raise ValueError("search_domain_filter supports at most 20 entries")

            normalized_domains: list[str] = []
            for value in self.search_domain_filter:
                normalized = (value or "").strip().lower()
                self._validate_domain_filter_entry(normalized)
                normalized_domains.append(normalized)

            has_allow = any(not value.startswith("-") for value in normalized_domains)
            has_deny = any(value.startswith("-") for value in normalized_domains)
            if has_allow and has_deny:
                raise ValueError(
                    "search_domain_filter must use either allowlist OR denylist mode, not both"
                )

            self.search_domain_filter = normalized_domains

        date_fields = {
            "last_updated_after_filter": self.last_updated_after_filter,
            "last_updated_before_filter": self.last_updated_before_filter,
            "search_after_date_filter": self.search_after_date_filter,
            "search_before_date_filter": self.search_before_date_filter,
        }
        for field_name, value in date_fields.items():
            if value:
                self._ensure_date_format(value, field_name)