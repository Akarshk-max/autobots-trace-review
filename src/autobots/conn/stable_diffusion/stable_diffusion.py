# from functools import lru_cache
#
# from fastapi import HTTPException
# from tenacity import retry, stop_after_attempt, wait_fixed
#
# from src.autobots.conn.stable_diffusion.common_models import (
#     StableDiffusionRes, StableDiffusionResStatus)
# from src.autobots.conn.stable_diffusion.fetch_queued_image.fetch_queued_image import (
#     FetchQueuedImagesResModel, fetch_queued_image)
# from src.autobots.conn.stable_diffusion.image_mixer.image_mixer import \
#     image_mixer
# from src.autobots.conn.stable_diffusion.image_mixer.image_mixer_model import (
#     ImageMixerProcessingResModel, ImageMixerReqModel, ImageMixerResError,
#     ImageMixerResModel)
# from src.autobots.conn.stable_diffusion.img2img.img2img import sd_img2img
# from src.autobots.conn.stable_diffusion.img2img.img2img_model import (
#     SDImg2ImgProcessingModel, SDImg2ImgReqModel, SDImg2ImgResError,
#     SDImg2ImgResModel)
# from src.autobots.conn.stable_diffusion.text2img.text2img import text2img
# from src.autobots.conn.stable_diffusion.text2img.text2img_model import (
#     Text2ImgReqModel, Text2ImgResError, Text2ImgResModel,
#     Text2ImgResProcessingModel)
# from src.autobots.conn.stable_diffusion.text2video.text2video import text2video
# from src.autobots.conn.stable_diffusion.text2video.text2video_model import (
#     Text2VideoProcessingResModel, Text2VideoReqModel, Text2VideoResError,
#     Text2VideoResModel)
# from src.autobots.core.settings import Settings, SettingsProvider
#
#
# class StableDiffusion:
#     def __init__(self, stable_diffusion_api_key):
#         self.api_key = stable_diffusion_api_key
#
#     async def text2img(self, req: Text2ImgReqModel) -> StableDiffusionRes:
#         req.key = self.api_key
#         res: Text2ImgResModel | Text2ImgResProcessingModel | Text2ImgResError = await text2img(req)
#         if res.status == StableDiffusionResStatus.success:
#             result = StableDiffusionRes(urls=res.output)
#             return result
#         elif res.status == StableDiffusionResStatus.processing:
#             fetched: StableDiffusionRes = await self.fetch_queued_image(res.id)
#             return fetched
#         return StableDiffusionRes(urls=[], fetch_url=self.get_fetch_url(res.id))
#
#     @retry(stop=stop_after_attempt(3), wait=wait_fixed(30), reraise=True)
#     async def image_mixer(self, req: ImageMixerReqModel) -> StableDiffusionRes:
#         req.key = self.api_key
#         res: (
#             ImageMixerResModel | ImageMixerProcessingResModel | ImageMixerResError
#         ) = await image_mixer(req)
#         if res.status == StableDiffusionResStatus.success:
#             result = StableDiffusionRes(urls=res.output)
#             return result
#         elif res.status == StableDiffusionResStatus.processing:
#             fetched: StableDiffusionRes = await self.fetch_queued_image(res.id)
#             return fetched
#         elif res.status == StableDiffusionResStatus.error:
#             raise HTTPException(503, res.messege)
#         return StableDiffusionRes(urls=[], fetch_url=self.get_fetch_url(res.id))
#
#     async def img2img(self, req: SDImg2ImgReqModel) -> StableDiffusionRes:
#         req.key = self.api_key
#         res: SDImg2ImgResError | SDImg2ImgProcessingModel | SDImg2ImgResModel = await sd_img2img(
#             req
#         )
#         if res.status == StableDiffusionResStatus.success:
#             result = StableDiffusionRes(urls=res.output)
#             return result
#         elif res.status == StableDiffusionResStatus.processing:
#             fetched: StableDiffusionRes = await self.fetch_queued_image(res.id)
#             return fetched
#         elif res.status == StableDiffusionResStatus.error:
#             raise HTTPException(503, res.message)
#         return StableDiffusionRes(urls=[], fetch_url=self.get_fetch_url(res.id))
#
#     async def text2video(self, req: Text2VideoReqModel) -> StableDiffusionRes:
#         req.key = self.api_key
#         res: (
#             Text2VideoResModel | Text2VideoProcessingResModel | Text2VideoResError
#         ) = await text2video(req)
#         if res.status == StableDiffusionResStatus.success:
#             result = StableDiffusionRes(urls=res.output)
#             return result
#         elif res.status == StableDiffusionResStatus.processing:
#             fetched: StableDiffusionRes = await self.fetch_queued_image(res.id)
#             return fetched
#         elif res.status == StableDiffusionResStatus.error:
#             raise HTTPException(503, res.message)
#         return StableDiffusionRes(urls=[], fetch_url=self.get_fetch_url(res.id))
#
#     async def fetch_queued_image(self, id: int) -> StableDiffusionRes:
#         res: FetchQueuedImagesResModel | None = await fetch_queued_image(id, self.api_key, 10, 20)
#         if res:
#             result = StableDiffusionRes(urls=res.output)
#             return result
#         return StableDiffusionRes(urls=[], fetch_url=self.get_fetch_url(id))
#
#     def get_fetch_url(self, id: int):
#         return f"https://stablediffusionapi.com/api/v3/fetch/{id}"
#
#
# @lru_cache
# def get_stable_diffusion(settings: Settings = SettingsProvider.sget()) -> StableDiffusion:
#     return StableDiffusion(settings.STABLE_DIFFUSION_API_KEY)
