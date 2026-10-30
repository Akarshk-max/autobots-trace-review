from typing import ClassVar, Set

from pydantic import BaseModel, model_validator


class StripDeprecatedModel(BaseModel):
    _deprecated_fields: ClassVar[Set[str]] = set()

    @classmethod
    @model_validator(mode="before")
    def _drop_deprecated(cls, data):
        if isinstance(data, dict):
            for k in getattr(cls, "_deprecated_fields", set()):
                data.pop(k, None)
        return data
