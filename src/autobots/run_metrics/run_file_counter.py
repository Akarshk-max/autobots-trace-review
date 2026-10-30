from typing import Any, Set


class RunFileCounter:

    _URL_KEY = "urls"
    _MAX_DEPTH = 20

    @classmethod
    def _urls(cls, value: Any, depth: int = 0) -> Set[str]:
        if depth > cls._MAX_DEPTH:
            return set()
        found: Set[str] = set()
        if isinstance(value, dict):
            for key, child in value.items():
                if key == cls._URL_KEY and isinstance(child, (list, tuple)):
                    found.update(str(url) for url in child if url)
                else:
                    found |= cls._urls(child, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value:
                found |= cls._urls(child, depth + 1)
        elif hasattr(value, "model_dump"):
            found |= cls._urls(value.model_dump(), depth + 1)
        return found

    @classmethod
    def count(cls, result: Any) -> tuple[int, int]:
        received = cls._urls(getattr(result, "input", None))
        produced = cls._urls(getattr(result, "output", None)) - received
        return len(received), len(produced)
