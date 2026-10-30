from typing import Any, Optional, Type

from pydantic import BaseModel
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, DefaultPayloadConverter, PayloadConverter


class PydanticPayloadConverter(PayloadConverter):
    def __init__(self, delegate: PayloadConverter | None = None) -> None:
        self._delegate = delegate or DefaultPayloadConverter()

    def to_payload(self, value: Any) -> Optional[Payload]:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        return self._delegate.to_payload(value)

    def from_payload(self, payload: Payload, type_hint: Optional[Type] = None) -> Any:
        if type_hint and isinstance(type_hint, type) and issubclass(type_hint, BaseModel):
            data = self._delegate.from_payload(payload, dict)
            return type_hint.model_validate(data)
        return self._delegate.from_payload(payload, type_hint)

    def to_payloads(self, values: list[Any]) -> list[Payload]:
        return self._delegate.to_payloads(
            [
                value.model_dump(mode="json") if isinstance(value, BaseModel) else value
                for value in values
            ]
        )

    def from_payloads(
        self,
        payloads: list[Payload],
        type_hints: list[Optional[Type]] | None = None,
    ) -> list[Any]:
        if type_hints is None:
            type_hints = [None] * len(payloads)

        return [
            self.from_payload(payload, type_hint)
            for payload, type_hint in zip(payloads, type_hints, strict=False)
        ]


def get_temporal_data_converter() -> DataConverter:
    return DataConverter(payload_converter_class=PydanticPayloadConverter)
