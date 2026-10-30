from typing import List, Literal, Optional

from pydantic import BaseModel


class FluxImageReq(BaseModel):
    prompt: str
    model_name: Literal["flux-dev", "flux-pro", "flux-schnell"]
    # negative_prompt: Optional[str] = None
    # width: int = 512
    # height: int = 512
    # num_images: int = 1
    # guidance_scale: float = 7.5
    # num_inference_steps: int = 50
    num_outputs: Optional[int] = None
    image: Optional[str] = None
    seed: Optional[int] = None
    guidance: Optional[int] = None


class FluxImage(BaseModel):
    url: str
    seed: int


class FluxImagesResponse(BaseModel):
    images: List[FluxImage]
    prompt: str
