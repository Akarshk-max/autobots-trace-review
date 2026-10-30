import asyncio
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import unquote, urlparse

import aiohttp
from loguru import logger
from pydantic import BaseModel, Field, ValidationError
from unstructured_client import UnstructuredClient
from unstructured_client.models import shared
from unstructured_client.models.errors import SDKError
from unstructured_client.models.operations import PartitionRequest, PartitionResponse

from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


class PartitionParametersParams(BaseModel):
    # files: Optional[Files]
    # r"""The file to extract"""
    chunking_strategy: Optional[Literal["by_title"]] = Field(default=None)
    r"""Use one of the supported strategies to chunk the returned elements. Currently supports: by_title"""
    combine_under_n_chars: Optional[int] = Field(default=None)
    r"""If chunking strategy is set, combine elements until a section reaches a length of n chars. Default: 500"""
    coordinates: Optional[bool] = Field(default=None)
    r"""If true, return coordinates for each element. Default: false"""
    encoding: Optional[str] = Field(default=None)
    r"""The encoding method used to decode the text input. Default: utf-8"""
    extract_image_block_types: Optional[List[str]] = Field(default=None)
    r"""The types of elements to extract, for use in extracting image blocks as base64 encoded data stored in metadata fields"""
    gz_uncompressed_content_type: Optional[str] = Field(default=None)
    r"""If file is gzipped, use this content type after unzipping"""
    hi_res_model_name: Optional[
        Literal["detectron2_onnx", "yolox", "yolox_quantized", "chipper"]
    ] = Field(default=None)
    r"""The name of the inference model used when strategy is hi_res"""
    """https://unstructured-io.github.io/unstructured/best_practices/models.html
    List of Available Models in the Partitions:
    1. detectron2_onnx is a Computer Vision model by Facebook AI that provides object detection and segmentation algorithms with ONNX Runtime. It is the fastest model with the hi_res strategy.
    2. yolox is a single-stage real-time object detector that modifies YOLOv3 with a DarkNet53 backbone.
    3. yolox_quantized: runs faster than YoloX and its speed is closer to Detectron2.
    4. chipper (beta version): the Chipper model is Unstructured’s in-house image-to-text model based on transformer-based Visual Document Understanding (VDU) models.
    """
    include_page_breaks: Optional[bool] = Field(default=None)
    r"""If True, the output will include page breaks if the filetype supports it. Default: false"""
    languages: Optional[List[str]] = Field(default=None)
    r"""The languages present in the document, for use in partitioning and/or OCR"""
    max_characters: Optional[int] = Field(default=None)
    r"""If chunking strategy is set, cut off new sections after reaching a length of n chars (hard max). Default: 1500"""
    multipage_sections: Optional[bool] = Field(default=None)
    r"""If chunking strategy is set, determines if sections can span multiple sections. Default: true"""
    new_after_n_chars: Optional[int] = Field(default=None)
    r"""If chunking strategy is set, cut off new sections after reaching a length of n chars (soft max). Default: 1500"""
    output_format: Optional[str] = Field(default=None)
    r"""The format of the response. Supported formats are application/json and text/csv. Default: application/json."""
    overlap: Optional[int] = Field(default=None)
    r"""A prefix of this many trailing characters from prior text-split chunk is applied to second and later chunks formed from oversized elements by text-splitting. Default: None"""
    overlap_all: Optional[bool] = Field(default=None)
    r"""When True, overlap is also applied to 'normal' chunks formed by combining whole elements. Use with caution as this can introduce noise into otherwise clean semantic units. Default: None"""
    pdf_infer_table_structure: Optional[bool] = Field(default=None)
    r"""If True and strategy=hi_res, any Table Elements extracted from a PDF will include an additional metadata field, 'text_as_html', where the value (string) is a just a transformation of the data into an HTML <table>."""
    skip_infer_table_types: Optional[List[str]] = Field(default=None)
    r"""The document types that you want to skip table extraction with. Default: ['pdf', 'jpg', 'png']"""
    split_pdf_page: Optional[bool] = Field(default=None)
    r"""Should the pdf file be split at client. Ignored on backend."""
    strategy: Optional[Literal["fast", "hi_res", "auto"]] = Field(default=None)
    r"""The strategy to use for partitioning PDF/image. Options are fast, hi_res, auto. Default: auto"""
    xml_keep_tags: Optional[bool] = Field(default=None)
    r"""If True, will retain the XML tags in the output. Otherwise it will simply extract the text from within the tags. Only applies to partition_xml."""


class PartitionResponseElementMetadata(BaseModel):
    filename: Optional[str]
    filetype: Optional[str]
    page_number: Optional[int] = None
    languages: Optional[List[str]] = []
    links: Optional[List[Any]] = None


class PartitionResponseElement(BaseModel):
    type: Optional[str]
    element_id: Optional[str]
    metadata: Optional[PartitionResponseElementMetadata]
    text: Optional[str]


class UnstructuredIO:
    def __init__(
        self, unstructured_api_key: str, unstructured_url: str, timeout_seconds: int = 300
    ):
        self.api_key = unstructured_api_key
        self.server_url = unstructured_url
        self.client = UnstructuredClient(
            api_key_auth=unstructured_api_key,
            server_url=unstructured_url,
            timeout_ms=timeout_seconds * 1000,  # 5-minute per-request timeout
        )
        # Add a semaphore to act as a lock, serializing access to the non-thread-safe SDK client.
        self._sdk_lock = asyncio.Semaphore(1)

    async def partition_from_remote_url(
        self, remote_url: str, partition_parameters: PartitionParametersParams
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Processes a file from a remote URL by streaming it into memory and sending
        it to the Unstructured API. This avoids writing to local disk.
        """
        logger.info(f"Initiating stream-based processing for remote URL: {remote_url}")
        try:
            # 1. Stream the file content from the URL into a bytes object.
            file_content = await self._stream_download_file(remote_url)
            if file_content is None:
                logger.error(f"Failed to download content from {remote_url}")
                return None

            # 2. Extract a filename from the URL.
            filename = self._extract_filename_from_url(remote_url)

            # 3. Call the existing, working method for processing file content.
            #    This reuses the logic we already know works.
            res: PartitionResponse | None = await self._get_PartitionResponse(
                await self._build_PartitionRequest(
                    file=(filename, file_content), partition_parameters_params=partition_parameters
                )
            )

            # 4. Extract and return the elements.
            if res and res.elements:
                return res.elements
            else:
                logger.warning(f"Partitioning returned no elements for {remote_url}")
                return []

        except Exception as e:
            logger.error(f"An unexpected error occurred in partition_from_remote_url: {e}")
            return None

    async def partition_from_content(
        self, file_content: bytes, filename: str, partition_parameters: PartitionParametersParams
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Processes file content provided directly as bytes and sends it to the Unstructured API.
        This avoids needing a remote URL or local file path.
        """
        logger.info(f"Initiating content-based processing for filename: {filename}")
        try:
            # We already have the content, so we just build the request and call the API.
            res: PartitionResponse | None = await self._get_PartitionResponse(
                await self._build_PartitionRequest(
                    file=(filename, file_content), partition_parameters_params=partition_parameters
                )
            )

            if res and res.elements:
                return res.elements
            else:
                logger.warning(
                    f"Partitioning returned no elements for content-based file {filename}"
                )
                return []

        except Exception as e:
            logger.error(f"An unexpected error occurred in partition_from_content: {e}")
            return None

    async def _stream_download_file(
        self, url: str, max_size: int = 200 * 1024 * 1024, max_attempts: int = 10
    ) -> Optional[bytes]:
        """
        Efficiently downloads a file from a URL by streaming, with a size limit.
        """
        for attempt in range(max_attempts):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=300) as response:
                        response.raise_for_status()

                        content_length = response.headers.get("content-length")
                        if content_length and int(content_length) > max_size:
                            logger.error(
                                f"File at {url} is too large: {content_length} bytes (max: {max_size})"
                            )
                            return None

                        file_bytes = await response.read()

                        if len(file_bytes) > max_size:
                            logger.error(
                                f"File at {url} exceeded size limit during download: {len(file_bytes)} bytes"
                            )
                            return None

                        logger.info(f"Successfully downloaded {len(file_bytes)} bytes from {url}")
                        return file_bytes

            except aiohttp.ClientResponseError as e:
                # This handles eventual consistency issues with S3 (403/404)
                if e.status in (403, 404) and attempt < max_attempts - 1:
                    wait_time = 2**attempt  # Exponential backoff
                    logger.warning(
                        f"Got status {e.status} downloading {url}. Retrying in {wait_time}s... (Attempt {attempt + 1}/{max_attempts})"
                    )
                    await asyncio.sleep(wait_time)
                    continue
                logger.error(f"HTTP error downloading {url} after {max_attempts} attempts: {e}")
                return None
            except Exception as e:
                logger.error(f"Generic error downloading {url}: {e}")
                return None
        return None  # Should only be reached if all attempts fail

    def _extract_filename_from_url(self, url: str) -> str:
        """Extracts a filename from a URL, providing a default if needed."""
        try:
            path = urlparse(url).path
            filename = unquote(path.split("/")[-1])
            return filename if filename else "unknown_file"
        except Exception:
            return "unknown_file"

    async def get_file_chunks(
        self, file: Tuple[str, bytes], partition_parameters_params: PartitionParametersParams
    ) -> List[str]:
        strings = []
        try:
            req = await self._build_PartitionRequest(file, partition_parameters_params)
            res: PartitionResponse | None = await self._get_PartitionResponse(req)
            elements = await self.get_file_partition_elements(res)

            for element in elements:
                strings.append(element.text)

            if partition_parameters_params.combine_under_n_chars == 0:
                # return full text if chunk_size is 0
                string = "".join(strings)
                return [string]
            else:
                return strings
        except Exception as e:
            logger.error(str(e))
        return strings

    async def _build_PartitionRequest(
        self, file: Tuple[str, bytes], partition_parameters_params: PartitionParametersParams
    ) -> PartitionRequest:
        filename, content = file

        req = shared.PartitionParameters(
            # Note that this currently only supports a single file
            # files=file,
            files=shared.Files(
                content=content,
                file_name=filename,
            ),
            # Other partition params
            **partition_parameters_params.model_dump(exclude_none=True),
            # chunking_strategy="by_title",
            # combine_under_n_chars=chunk_size,
            # pdf_infer_table_structure=True,
            # hi_res_model_name=None,
            # strategy="hi_res",
        )
        req1 = PartitionRequest(
            partition_parameters=req,
        )
        return req1

    async def _get_PartitionResponse(self, req: PartitionRequest) -> PartitionResponse | None:
        res: PartitionResponse | None = None
        try:
            async with self._sdk_lock:
                # Use asyncio.to_thread to prevent the synchronous SDK call
                # from blocking the event loop.
                res: PartitionResponse = await asyncio.to_thread(
                    self.client.general.partition, request=req
                )
        except SDKError as e:
            logger.error(str(e))
        except Exception as e:
            logger.error(str(e))
        if not res or res.status_code != 200:
            logger.error(
                f"Error in extracting data from file {req.partition_parameters.files.file_name}"
            )
        return res
        # res_str = ("\n\n".join([str(el) for el in res.elements]))

    async def get_file_partition_elements(
        self, partition_response: PartitionResponse | None
    ) -> List[PartitionResponseElement]:
        elements = []
        for element_dict in partition_response.elements:
            try:
                element = PartitionResponseElement.model_validate(element_dict)
                elements.append(element)
            except ValidationError as e:
                logger.error(str(e))
            except Exception as e:
                logger.error(str(e))
        return elements


def get_unstructured_io(
    ctx: Context,
    user: UserORM,
    api_key: str,
    api_uri: str,
    notify: Notify = None,
    settings: Settings = SettingsProvider.sget(),
) -> UnstructuredIO:
    return UnstructuredIO(
        unstructured_api_key=api_key,
        unstructured_url=api_uri,
    )
