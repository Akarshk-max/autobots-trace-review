import asyncio
import base64
import json
import random
import string
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import List, Literal, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from unstructured_client.models import operations, shared
from unstructured_client.models.shared.partition_parameters import Strategy

from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.chroma.chroma import Chroma, Document
from src.autobots.conn.chroma.multi_vector import BaseStore, MultiVectorRetriever
from src.autobots.conn.openai.openai_client import get_openai
from src.autobots.conn.unstructured_io.unstructured_io import UnstructuredIO
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.datastore.data_provider import DataProvider
from src.autobots.user.user_orm_model import UserORM


class SearchResult(BaseModel):
    """A standardized search result model for different vector backends."""

    data: str
    sources: Optional[str] = None
    score: Optional[float] = None
    metadata: Optional[dict] = None


def summarize_element(
    ctx: Context,
    db: AsyncIOMotorDatabase,
    user: UserORM,
    element,
    openai_provider: Literal["azure", "openai"],
):
    prompt_text = f"""You are an assistant tasked with summarizing tables and text for retrieval. \
These summaries will be embedded and used to retrieve the raw text or table elements. \
Give a concise summary of the table or text that is well optimized for retrieval. Table or text: {element}"""

    # Call the OpenAI API to generate the summary
    client = asyncio.run(AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider))
    response = client.client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": "You are an assistant tasked with summarizing tables and text for retrieval.",
            },
            {"role": "user", "content": prompt_text},
        ],
        temperature=0,
        max_tokens=1000,
    )  # Adjust the token limit based on your needs)

    # Extract and return the summary from the response
    summary = response.choices[0].message.content.strip()
    return summary


def summarize_text(
    ctx: Context,
    db: AsyncIOMotorDatabase,
    user: UserORM,
    texts,
    max_concurrency=5,
    openai_provider: Literal["azure", "openai"] = "azure",
):
    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        futures = [
            executor.submit(summarize_element, ctx, db, user, text, openai_provider)
            for text in texts
        ]
        summaries = [future.result() for future in futures]
    return summaries


# Categorize elements by type
def categorize_elements(raw_pdf_elements):
    """
    Categorize extracted elements from a PDF into tables and texts.
    raw_pdf_elements: List of unstructured.documents.elements
    """
    tables = []
    texts = []
    img_base64_files = []

    for element in raw_pdf_elements:
        # print(element)
        if element["type"] == "Image":
            img_base64_files.append(element["metadata"]["image_base64"])
            # name= uuid.uuid4()
            # with open(str(name)+".jpg", 'wb') as file:
            #     file.write(base64.b64decode(element["metadata"]["image_base64"]))
        if element["type"] == "Table":
            tables.append(element["metadata"]["text_as_html"])
        if element["type"] in ["NarrativeText", "UncategorizedText"]:
            texts.append(element["text"])

    return texts, tables, img_base64_files


# texts_4k_token =texts


def encode_image(image_path):
    """Getting the base64 string"""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


# def image_summarize(img_base64, prompt, openai_provider):
#     """Make image summary"""
#
#     response = Appget_openai(provider=openai_provider).client.chat.completions.create(
#         model="gpt-4o",
#         messages=[
#             {"role": "system", "content": prompt},
#             {"role": "user", "content": [
#                 {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"}}
#             ]}
#         ]
#     )
#
#     return response.choices[0].message.content.strip()
#


def generate_img_summaries(img_base64_list):
    """
    Generate summaries and base64 encoded strings for images
    path: Path to list of .jpg files extracted by Unstructured
    """

    # Store base64 encoded images
    # img_base64_list = []

    # Store image summaries
    image_summaries = []

    # Prompt
    prompt = """You are an assistant tasked with summarizing images for retrieval. \
    These summaries will be embedded and used to retrieve the raw image. \
    Give a concise summary of the image that is well optimized for retrieval."""
    assert prompt
    # Apply to images
    # for img_file in sorted(os.listdir(path)):
    #     if img_file.endswith(".jpg"):
    # img_path = os.path.join(path, img_file)
    # base64_image = encode_image(img_path)
    # img_base64_list.append(base64_image)
    for base64_image in img_base64_list:
        image_summaries.append("dummy")

    return img_base64_list, image_summaries


# Image summaries


def generate_text_summaries(
    ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, texts, tables, summarize_texts=False
):
    """
    Summarize text elements
    texts: List of str
    tables: List of str
    summarize_texts: Bool to summarize texts
    """

    # # Prompt
    # prompt_text = """You are an assistant tasked with summarizing tables and text for retrieval. \
    # These summaries will be embedded and used to retrieve the raw text or table elements. \
    # Give a concise summary of the table or text that is well optimized for retrieval. Table or text: {element} """

    # Text summary chain

    # Initialize empty summaries
    text_summaries = []
    table_summaries = []

    # Apply to text if texts are provided and summarization is requested
    if texts and summarize_texts:
        text_summaries = summarize_text(ctx=ctx, db=db, user=user, texts=texts)
    elif texts:
        text_summaries = texts

    # Apply to tables if tables are provided
    if tables:
        table_summaries = summarize_text(ctx=ctx, db=db, user=user, texts=tables)

    return text_summaries, table_summaries


async def create_multi_vector_retriever(
    filename, vectorstore, text_summaries, texts, table_summaries, tables, image_summaries, images
):
    """
    Create retriever that indexes summaries, but returns raw images or texts
    """
    store = BaseStore()
    # Initialize the storage layer
    id_key = "doc_id"

    # Create the multi-vector retriever
    retriever = MultiVectorRetriever(
        vectorstore=vectorstore,
        docstore=store,
        id_key=id_key,
    )

    # Helper function to add documents to the vectorstore and docstore
    async def add_documents(retriever, doc_summaries, doc_contents, i, total_id):
        summary_docs = []
        doc_ids = [str(uuid.uuid4()) for _ in doc_contents]
        for j, s in enumerate(doc_summaries):
            i += 1
            summary_docs.append(Document(page_content=s, metadata={id_key: doc_ids[j]}))
        await retriever.vectorstore.add_documents(summary_docs)
        return doc_ids, i
        # await retriever.docstore.mset(list(zip(doc_ids, doc_contents)))

    text_ids, table_ids, image_ids = [], [], []
    # Add texts, tables, and images
    # Check that text_summaries is not empty before adding
    total_id = len(texts + tables + images)
    i = 0
    # if text_summaries:
    text_ids, i = await add_documents(retriever, texts, texts, i, total_id)
    # Check that table_summaries is not empty before adding
    # if table_summaries:
    if len(tables) > 0:
        table_ids, i = await add_documents(retriever, tables, tables, i, total_id)
    # Check that image_summaries is not empty before adding
    if image_summaries:
        image_ids, i = await add_documents(retriever, image_summaries, images, i, total_id)

    return retriever, text_ids, table_ids, image_ids


# The vectorstore to use to index the summaries


class MultiDataStore:
    def __init__(self, s3, openai_provider: Literal["azure", "openai"]):
        self.persist_directory = "db"
        self.s3 = s3
        self.openai_provider = openai_provider

    def init(self, id):
        self.name = id

        self.trace = "".join(random.choices(string.hexdigits, k=9))
        # Id for the datastore is unique identifier for the datastore
        self.id = self.trace

        return self

    def hydrate(self, datastore_id: str):
        self.id = datastore_id
        # Get Trace from datastore_id
        self.trace = datastore_id.split("-")[-1]
        # Get name from datastore_id
        self.name = datastore_id.replace(f"-{self.trace}", "")

        return self

    def _datastore_identifier(self) -> str:
        return SettingsProvider.sget().DATASTORE_IDENTIFIER

    def _get_s3_basepath(self) -> str:
        return f"{self._datastore_identifier()}/{self.id}"

    async def _put_data(self, ctx: Context, data: str, id: str) -> None:
        await self.s3.put(ctx=ctx, data=data, filename=f"{self._get_s3_basepath()}/{id}")

    async def get_texts_4k(self, joined_texts):
        texts = []
        texts_4k_token_agen = DataProvider.create_data_chunks(
            joined_texts, DataProvider.read_data_line_by_line, 4000
        )
        async for text in texts_4k_token_agen:
            texts.append(text)
        return texts

    async def put_data(
        self, ctx: Context, user: UserORM, db: AsyncIOMotorDatabase, file: Tuple[str, bytes]
    ):
        unstructured_io_client: UnstructuredIO = await AppConn.get_unstructured_io(
            ctx=ctx,
            user=user,
            db=db,
        )
        s = unstructured_io_client.client
        filename, content = file
        req = shared.PartitionParameters(
            files=shared.Files(
                content=content,
                file_name=filename,
            ),
            strategy=Strategy.HI_RES,
            languages=["eng"],
            extract_image_block_types=["Image"],
        )
        raw_pdf_elements = s.general.partition(
            request=operations.PartitionRequest(partition_parameters=req)
        )

        # # Get text, tables
        texts, tables, img_base64_list = categorize_elements(raw_pdf_elements.elements)

        # joined_texts = " ".join(texts)

        # texts=["In an era dominated by screens and digital content, the simple act of reading a book may seem quaint or even obsolete. Yet, the intrinsic value of books has endured throughout centuries, transcending the rapid changes in technology and lifestyle. Reading books is not just a leisure activity; it is a multifaceted endeavor that offers profound benefits for our cognitive abilities, emotional health, and social skills. This essay delves into the myriad advantages of reading, underscoring its importance in our increasingly digital world."]
        # # Optional: Enforce a specific token size for texts

        vectorstore = Chroma(
            collection_name=self.id,
            embedding_function=get_openai(provider=self.openai_provider).openai_embeddings,
            persist_directory=self.persist_directory,
        )
        # texts_4k_token = await get_texts_4k(joined_texts)
        # text_summaries, table_summaries = generate_text_summaries(
        #     texts_4k_token, None, summarize_texts=True
        # )
        img_base64_list, image_summaries = generate_img_summaries(img_base64_list)
        retriever, text_ids, table_ids, image_ids = await create_multi_vector_retriever(
            filename,
            vectorstore,
            None,
            texts,
            None,
            tables,
            image_summaries,
            img_base64_list,
        )
        total_id = len(text_ids + table_ids + image_ids) + 1
        i = 0
        for doc_id, doc_content in list(
            zip(text_ids + table_ids + image_ids, texts + tables + image_summaries)
        ):
            i += 1
            metadata = {"filename": filename, "chunk": str(i), "total_chunks": str(total_id)}
            await self._put_data(ctx, doc_content, doc_id)
            await self._put_data(ctx, json.dumps(metadata), doc_id + "meta")

        return retriever

    async def search(self, query: str, top_k: int = 10) -> List[SearchResult]:
        from loguru import logger

        logger.info(
            f"Starting search with query: '{query}', top_k: {top_k}, datastore_id: {self.id}"
        )

        store = BaseStore()
        # Initialize the storage layer
        id_key = "doc_id"

        try:
            vectorstore = Chroma(
                collection_name=self.id,
                embedding_function=get_openai(provider=self.openai_provider).openai_embeddings,
                persist_directory=self.persist_directory,
            )
            logger.info(f"Chroma vectorstore created successfully for collection: {self.id}")

            # Check if collection exists and has documents
            try:
                collection_info = vectorstore.get()
                doc_count = len(collection_info.get("ids", []))
                logger.info(f"Collection '{self.id}' has {doc_count} documents")
                if doc_count == 0:
                    logger.warning(f"Collection '{self.id}' is empty - no documents to search")
                    return []
            except Exception as e:
                logger.error(f"Error checking collection info: {e}")
                return []

        except Exception as e:
            logger.error(f"Error creating Chroma vectorstore: {e}")
            return []

        retriever = MultiVectorRetriever(
            vectorstore=vectorstore,
            docstore=store,
            id_key=id_key,
        )

        try:
            ids = await retriever._get_relevant_documents(query, top_k)
            logger.info(f"Vector search returned {len(ids)} document IDs")
            if not ids:
                logger.warning("Vector search returned no results")
                return []
        except Exception as e:
            logger.error(f"Error in vector search: {e}")
            return []

        result = []
        sources = ""
        s3_basepath = self._get_s3_basepath()
        logger.info(f"S3 basepath: {s3_basepath}")

        for i, (id, score) in enumerate(ids):
            try:
                logger.info(f"Processing document {i + 1}/{len(ids)}: ID={id}, score={score}")
                res = {}
                data = await self.s3.get(f"{s3_basepath}/{id}")
                metadata = await self.s3.get(f"{s3_basepath}/{id}meta")

                if not data:
                    logger.warning(f"No data found for document ID: {id}")
                    continue

                sources += metadata
                sources += ","
                res["data"] = data
                res["sources"] = metadata
                res["score"] = score
                result.append(SearchResult.model_validate(res))
                logger.info(f"Successfully processed document {i + 1}: data length={len(data)}")
            except Exception as e:
                logger.error(f"Error processing document ID {id}: {e}")
                continue

        logger.info(f"Search completed. Returning {len(result)} results")
        return result

    async def msearch(self, query: List[str], top_k: int = 10) -> List[SearchResult]:
        store = BaseStore()
        # Initialize the storage layer
        id_key = "doc_id"
        vectorstore = Chroma(
            collection_name=self.id,
            embedding_function=get_openai(self.openai_provider).openai_embeddings,
            persist_directory=self.persist_directory,
        )
        retriever = MultiVectorRetriever(
            vectorstore=vectorstore,
            docstore=store,
            id_key=id_key,
        )
        result = []

        for q in query:
            ids = await retriever._get_relevant_documents(q, top_k)
            sources = ""
            for id, score in ids:
                res = {}
                data = await self.s3.get(f"{self._get_s3_basepath()}/{id}")
                metadata = await self.s3.get(f"{self._get_s3_basepath()}/{id}meta")
                sources += metadata
                sources += ","
                res["data"] = data
                res["sources"] = metadata
                res["score"] = score
                result.append(SearchResult.model_validate(res))
        # result.append(sources)
        return result
