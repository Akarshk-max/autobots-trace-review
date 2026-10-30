from functools import lru_cache
from typing import List

from loguru import logger
from pydantic import HttpUrl
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.wait import WebDriverWait
from webdriver_manager.firefox import GeckoDriverManager


class Selenium:
    def __init__(self, options: Options = Options()):
        # Do not open browser
        # options.add_argument("--no-sandbox")
        options.add_argument("--headless")
        # options.add_argument("--setDefaultBrowser")
        options.add_argument("--disable-pinch")

        self.browser_options = options
        self.browser_service = FirefoxService(GeckoDriverManager().install())
        # Expensive operation!! so do it once and close resource at the end
        self.driver = self.get_webdriver()

    def get_webdriver(self):
        driver = webdriver.Firefox(
            options=self.browser_options, service=self.browser_service, keep_alive=False
        )
        return driver

    def close_webdriver(self):
        self.driver.quit()

    # Not working yet
    async def refresh_driver_on_resource_overload(self):
        # # gives a single float value
        # cpu_percent = psutil.cpu_percent()
        # # percentage of used RAM
        # used_ram_percent = psutil.virtual_memory().percent
        # if cpu_percent >= 100 or used_ram_percent >= 60:
        #     logger.info("Refreshing web_driver on resource overload")
        #     # replace with new driver
        #     self.close_webdriver()
        #     self.driver = self.get_webdriver()
        pass

    async def read_url_text(self, url: HttpUrl, xpath: str = "/html/body") -> str:
        await self.refresh_driver_on_resource_overload()
        logger.bind(url=url).debug("Reading url")
        # Target URL
        self.driver.get(url.unicode_string())
        # To load entire webpage
        # await asyncio.sleep(5)
        await self.wait_for_all_elements_load(driver=self.driver, xpath=xpath, timeout=10)

        # whole body text
        text: str = self.driver.find_element(By.XPATH, xpath).text
        return text

    async def read_url_v1(
        self, url: HttpUrl, xpath: str = "/html/body", attribute: str = ""
    ) -> str:
        try:
            await self.refresh_driver_on_resource_overload()
            logger.bind(url=url).debug("Reading url")
            # Target URL
            self.driver.get(url.unicode_string())
            # To load entire webpage
            # await asyncio.sleep(5)
            await self.wait_for_all_elements_load(driver=self.driver, xpath=xpath, timeout=10)

            resp: List[str] = []
            if xpath and attribute:
                web_elements = self.driver.find_elements(By.XPATH, xpath)
                for web_element in web_elements:
                    resp = resp + [web_element.get_attribute(attribute)]
            elif xpath and not attribute:
                web_elements = self.driver.find_elements(By.XPATH, xpath)
                for web_element in web_elements:
                    resp = resp + [web_element.text]
            else:
                resp = [self.driver.page_source]

            resp_str = "\n".join(str(x) for x in resp)
            return resp_str
        except Exception as e:
            logger.bind(url=url).error(f"Error while reading url: {str(e)}")
            raise

    async def read_urls(
        self, urls: List[HttpUrl], xpath: str = "/html/body", attribute: str = ""
    ) -> str:
        result = ""
        for url in urls:
            try:
                txt = await self.read_url_v1(url, xpath, attribute)
                result += f"{url.unicode_string()}:\n{txt}\n\n"
            except Exception as e:
                logger.bind(url=url).exception(f"Error while reading url: {str(e)}")
                pass
        return result

    async def read_url(
        self,
        url: HttpUrl,
    ) -> str:
        await self.refresh_driver_on_resource_overload()
        logger.bind(url=url).debug("Reading url")
        # Target URL
        self.driver.get(url.unicode_string())
        # To load entire webpage
        # await asyncio.sleep(5)
        await self.wait_for_page_load(self.driver, timeout=10)

        # whole body text
        html: str = self.driver.page_source
        return html

    def __del__(self):
        # Closing the driver
        self.close_webdriver()

    async def wait_for_page_load(self, driver, timeout=10):
        """Wait until all elements are loaded in the DOM"""
        WebDriverWait(driver, timeout).until(
            expected_conditions.presence_of_all_elements_located(
                (By.XPATH, "//*")
            )  # Wait for all elements in DOM
        )

    async def wait_for_all_elements_load(self, driver, xpath: str, timeout=10):
        """Wait until all elements are loaded in the DOM"""
        WebDriverWait(driver, timeout).until(
            expected_conditions.presence_of_all_elements_located(
                (By.XPATH, xpath)
            )  # Wait for all elements in DOM
        )


@lru_cache
def get_selenium() -> Selenium:
    return Selenium()
