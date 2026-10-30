from typing import List

from pydantic import BaseModel, Field


class KiwiScoutChat(BaseModel):
    """
    Configuration for the output model.
    """

    success: bool = Field(default=False, description="Indicates if the operation was successful.")
    prompt: str = Field(default="", description="The prompt used for the browser action.")
    extracted_content: List[str] = Field(
        default_factory=list, description="The content extracted from the response."
    )
