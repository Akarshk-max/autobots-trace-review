import os

import httpx
from httpx import URL
from loguru import logger
from openai import AsyncAzureOpenAI, AsyncOpenAI
from openai._legacy_response import HttpxBinaryResponseContent
from openai.types.audio import Transcription, Translation
from pydantic import HttpUrl
from tenacity import retry, stop_after_attempt, wait_fixed

from src.autobots.conn.openai.openai_audio.speech_model import SpeechReq
from src.autobots.conn.openai.openai_audio.transcription_model import TranscriptionReq
from src.autobots.conn.openai.openai_audio.translation_model import TranslationReq


class OpenaiAudio:
    def __init__(
        self,
        openai_tts_client: AsyncAzureOpenAI | AsyncOpenAI,
        openai_sst_client: AsyncAzureOpenAI | AsyncOpenAI,
    ):
        self.tts_client = openai_tts_client
        self.sst_client = openai_sst_client

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(3), reraise=True)
    async def speech(self, speech_req: SpeechReq) -> HttpxBinaryResponseContent | None:
        try:
            logger.trace("Starting OpenAI create speech")
            res = await self.tts_client.audio.speech.create(**speech_req.model_dump())
            logger.trace("Completed OpenAI create speech")
            return res
        except Exception as e:
            logger.exception(str(e))

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(3), reraise=True)
    async def transcription(self, transcription_req: TranscriptionReq) -> Transcription | None:
        # create new single directory
        path = "./to_del"
        if not os.path.exists(path):
            os.mkdir(path)
        # build filename
        url = HttpUrl(transcription_req.file_url)
        filename = url.path.replace("/", "")
        full_path_name = f"{path}/{filename}"
        # get data from url and create a file object
        with httpx.Client(follow_redirects=True) as client:
            audio_resp = client.get(url=URL(str(transcription_req.file_url)))
            content = audio_resp.content
        with open(full_path_name, "wb") as binary_file:
            # Write bytes to file
            binary_file.write(content)
        try:
            # read file object and send to openai
            with open(full_path_name, "rb") as binary_file:
                logger.trace("Starting OpenAI create transcription")
                transcription_req.file_url = None
                res = await self.sst_client.audio.transcriptions.create(
                    file=binary_file, **transcription_req.model_dump(exclude_none=True)
                )
                logger.trace("Completed OpenAI create transcription")
            return res
        except Exception as e:
            logger.error(str(e))
        finally:
            # delete the file
            os.remove(full_path_name)

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(3), reraise=True)
    async def translation(self, translation_req: TranslationReq) -> Translation | None:
        # create new single directory
        path = "./to_del"
        if not os.path.exists(path):
            os.mkdir(path)
        # build filename
        url = HttpUrl(translation_req.file_url)
        filename = url.path.replace("/", "")
        full_path_name = f"{path}/{filename}"
        # get data from url and create a file object
        with httpx.Client() as client:
            audio_resp = client.get(url=URL(str(translation_req.file_url)))
            content = audio_resp.content
        with open(full_path_name, "wb") as binary_file:
            # Write bytes to file
            binary_file.write(content)
        try:
            # read file object and send to openai
            with open(full_path_name, "rb") as binary_file:
                logger.trace("Starting OpenAI create translation")
                translation_req.file_url = None
                res = await self.sst_client.audio.translations.create(
                    file=binary_file, **translation_req.model_dump(exclude_none=True)
                )
                logger.trace("Completed OpenAI create translation")
            return res
        except Exception as e:
            logger.error(str(e))
        finally:
            # delete the file
            os.remove(full_path_name)
