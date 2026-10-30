# from functools import lru_cache
#
# from src.autobots.conn.useapi.text2img.text2img import buttonApi, imagineApi
# from src.autobots.conn.useapi.text2img.text2img_model import (
#     DiscordErrorResponse, DiscordJobsApiResponse, DiscordReqModel)
#
#
# class UseApi:
#     async def imagine(self, req: DiscordReqModel) -> DiscordJobsApiResponse | DiscordErrorResponse:
#         res: DiscordJobsApiResponse | DiscordErrorResponse = await imagineApi(req)
#         return res
#
#     async def button(self, req: DiscordReqModel) -> DiscordJobsApiResponse | DiscordErrorResponse:
#         res: DiscordJobsApiResponse | DiscordErrorResponse = await buttonApi(req)
#         return res
#
#
# @lru_cache
# def get_use_api_net() -> UseApi:
#     return UseApi()
