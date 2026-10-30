from typing import Type

from loguru import logger
from pydantic import BaseModel, HttpUrl

from src.autobots.action.action.common_action_models import TextObj, TextObjs
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.selenium.selenium import get_selenium
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class ReadUrlConfig(BaseModel):
    url: HttpUrl | None = None
    xpath: str = "/html/body"
    attribute: str = ""


class ActionText2TextReadUrl(
    ActionABC[ReadUrlConfig, ReadUrlConfig, ReadUrlConfig, TextObj, TextObjs]
):  # TODO: add `s` at the end
    type = ActionType.text2text_read_url  # TODO: add `s` at the end

    @staticmethod
    def get_description() -> str:
        return "Scrape data from a URL"

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return ReadUrlConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return ReadUrlConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return ReadUrlConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return TextObj

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return TextObjs

    @staticmethod
    def get_description() -> str:  # noqa F811
        return """
Description:
- Load and scrape content from a public webpage using a headless browser (Selenium).
- Extract text or a specific HTML attribute from the element selected by an XPath expression.
- Useful when web search is not enough and you need to directly extract data from a specific URL.

When to use this tool:
- Use when you need structured or targeted content from a webpage.
- Use when a user provides a direct URL and asks for details from the page (content, metadata, links, images, etc.).
- Use when search engines can"t reach the right data, but the webpage itself contains it.

Input config:
{
  "url": "https://example.com",     # optional if URLs are provided via action_input
  "xpath": "/html/body",            # optional, defaults to full page text
  "attribute": ""                   # optional, empty string → return text content
}

Fields:
- url (string, optional):
    - The primary webpage to scrape.
    - If omitted, the tool tries to find URLs inside the text input.
- xpath (string, optional):
    - XPath selector to target specific content.
    - Default is "/html/body", which returns overall visible text.
- attribute (string, optional):
    - If provided, returns the value of the specified HTML attribute instead of the element text.
    - Examples: "href", "src", "alt"

Output:
- A TextObjs object:
  {
    "texts": [
      { "text": "<scraped content or attribute>" }
    ]
  }
- If multiple URLs are found, output may contain multiple entries.
- If an unexpected error occurs, an exception is returned instead.
"""

    # def __init__(self, action_config: ReadUrlConfig, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: TextObj, notify: Notify | None = None
    ) -> TextObjs | Exception:
        text_objs = TextObjs(texts=[])
        try:
            selenium = get_selenium()
            urls = []
            potential_urls = action_input.text.split(",")
            potential_urls.append(str(self.action_config.url)) if self.action_config.url else None
            for potential_url in potential_urls:
                try:
                    urls.append(HttpUrl(potential_url))
                except Exception as e:
                    logger.bind(ctx=ctx).exception(str(e))
                    pass

            out = await selenium.read_urls(
                urls, self.action_config.xpath, self.action_config.attribute
            )

            # out = await selenium.read_url_v1(
            #     HttpUrl(action_input.text),
            #     self.action_config.xpath,
            #     self.action_config.attribute
            # )
            text_objs.texts.append(TextObj(text=out))
            return text_objs
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    async def run_tool(
        self, action_config: ReadUrlConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> TextObjs | Exception:
        text_objs = TextObjs(texts=[])
        try:
            selenium = get_selenium()
            urls = []
            potential_urls = [action_config.url]
            for potential_url in potential_urls:
                try:
                    urls.append(HttpUrl(potential_url))
                except Exception as e:
                    logger.bind(ctx=ctx).exception(str(e))
                    pass

            out = await selenium.read_urls(urls, action_config.xpath, action_config.attribute)
            text_objs.texts.append(TextObj(text=out))
            return text_objs
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e
