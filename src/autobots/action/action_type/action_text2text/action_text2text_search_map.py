# from typing import Literal, Type

# from loguru import logger
# from pydantic import BaseModel

# from src.autobots.action.action.common_action_models import TextObj, TextObjs
# from src.autobots.action.action_type.abc.ActionABC import (
#     ActionABC,
#     ActionConfigCreateType,
#     ActionConfigType,
#     ActionConfigUpdateType,
#     ActionInputType,
#     ActionOutputType,
# )
# from src.autobots.action.action_type.action_types import ActionType
# from src.autobots.conn.duckduckgo.duckduckgo import get_duckduckgo
# from src.autobots.conn.duckduckgo.duckduckgo_model import SearchMapsParams
# from src.autobots.data_model.context import Context
# from src.autobots.server_sent_events.server_sent_events import Notify


# class SearchMapsConfig(BaseModel):
#     search_type: Literal["maps"] = "maps"
#     search_params: SearchMapsParams


# class ActionText2TextSearchMaps(
#     ActionABC[SearchMapsConfig, SearchMapsConfig, SearchMapsConfig, TextObj, TextObjs]
# ):
#     type = ActionType.text2text_search_maps

#     @staticmethod
#     def get_config_create_type() -> Type[ActionConfigCreateType]:
#         return SearchMapsConfig

#     @staticmethod
#     def get_config_update_type() -> Type[ActionConfigUpdateType]:
#         return SearchMapsConfig

#     @staticmethod
#     def get_config_type() -> Type[ActionConfigType]:
#         return SearchMapsConfig

#     @staticmethod
#     def get_input_type() -> Type[ActionInputType]:
#         return TextObj

#     @staticmethod
#     def get_output_type() -> Type[ActionOutputType]:
#         return TextObjs

#     @staticmethod
#     def get_description() -> str:
#         return (
#             "Performs a DuckDuckGo Maps search using the provided parameters and "
#             "returns location search results as text objects. The tool queries "
#             "DuckDuckGo Maps with the given keywords, location settings, and "
#             "other map-specific parameters, then formats each result as a text "
#             "entry.\n\n"
#             "Input:\n"
#             "  {\n"
#             '      "search_type": "maps",\n'
#             '      "search_params": {\n'
#             '          "keywords": "coffee shops near central park",\n'
#             '          "place": "name of places or location",\n'
#             '          "max_results": "number of maximum result duckduckgo will return",\n'
#             "      }\n"
#             "  }\n\n"
#             '  search_type: Must be "maps". Used to specify which DuckDuckGo '
#             "               search API to invoke.\n\n"
#             "  search_params: A structured set of DuckDuckGo Maps parameters such "
#             "                 as keywords, region, bounding box, or other query "
#             "                 options supported by DuckDuckGo. Keywords determine "
#             "                 what the tool will search for.\n\n"
#             "Output:\n"
#             "  A list of text objects, each containing the string representation "
#             "  of an individual Maps search result (title, body, link, and other "
#             "  information depending on the result). If an unexpected error "
#             "  occurs, the exception is returned."
#         )

#     # def __init__(self, action_config: TextObj, user: UserORM | None = None):
#     #     super().__init__(action_config=action_config, user=user)

#     async def run_action(
#         self, ctx: Context, action_input: TextObj, notify: Notify | None = None
#     ) -> TextObjs | Exception:
#         text_objs = TextObjs(texts=[])
#         try:
#             duckduckgo = get_duckduckgo()
#             self.action_config.search_params.keywords = (
#                 f"{self.action_config.search_params.keywords} {action_input.text}"
#             )
#             search_res = await duckduckgo.search_maps(self.action_config.search_params)
#             for search in search_res:
#                 # text = f"title: {search.title}\nbody: {search.body}\nhref: {search.href}"
#                 text = str(search)
#                 text_objs.texts.append(TextObj(text=text))
#             return text_objs
#         except Exception as e:
#             logger.bind(ctx=ctx).exception(str(e))
#             return e

#     # async def run_tool(
#     #     self, action_config: SearchMapsConfig, ctx: Context, notify: Notify | None = None
#     # ) -> TextObjs:
#     #     try:
#     #         duckduckgo = get_duckduckgo()

#     #         # don't mutate action_config either - it's simple and straight forward
#     #         params = action_config.search_params.model_copy(deep=True)
#     #         params.keywords = params.keywords.strip()

#     #         search_res = await duckduckgo.search_maps(params)

#     #         text_objs = TextObjs(texts=[])
#     #         for search in search_res:
#     #             text_objs.texts.append(TextObj(text=str(search)))

#     #         return text_objs

#     #     except Exception as e:
#     #         logger.bind(ctx=ctx).exception(str(e))
#     #         return TextObjs(texts=[TextObj(text=f"ERROR: {e}")])
