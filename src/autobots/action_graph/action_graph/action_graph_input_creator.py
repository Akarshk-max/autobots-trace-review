import json
from io import BytesIO
from typing import Any, Dict, List

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks, Metadata
from src.autobots.action_graph.action_graph.action_graph_doc_model import DataFlowType
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.excel.read_and_split import pick_spreadsheet_url, split_excel_into_batches
from src.autobots.core.logging.app_code import AppCode
from src.autobots.core.settings import SettingsProvider
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState


class ActionGraphInputCreator:
    @staticmethod
    async def _maybe_convert_to_file(
        ctx: Context,
        app_state: AppState,
        data_block: DataBlock,
        user_files: UserFiles,
    ) -> DataBlock:
        """
        Checks if the text in DataBlock exceeds character limit.
        If yes:
          1. Creates a deep copy of the block.
          2. Extracts URLs embedded in text (if enabled).
          3. Uploads ORIGINAL text to S3 as a file.
          4. Replaces text with a summary and the file URL.
          5. Appends S3 URL to urls list.
        """
        if not data_block.text:
            return data_block

        settings = SettingsProvider.sget()
        char_limit = getattr(settings, "ACTION_GRAPH_MAX_INPUT_CHARS", 75000)

        try:
            text_length = len(data_block.text)
            if text_length <= char_limit:
                return data_block

            logger.bind(ctx=ctx, text_length=text_length, limit=char_limit, code=AppCode.ACTION_GRAPH).info(
                "ActionGraph input exceeded character limit. Converting to file."
            )

            original_text = data_block.text

            new_block = data_block.model_copy(deep=True)

            if new_block.metadata.process_url_in_text:
                new_block.process_url_in_text(ctx)

            filename = f"graph_input_{gen_uuid()}.txt"
            file_obj = BytesIO(original_text.encode("utf-8"))
            file_obj.seek(0)  # Safety: rewind

            uploaded_metas = await user_files.upload_binary(ctx, file_obj, filename)

            file_obj.close()

            if uploaded_metas:
                file_url = str(uploaded_metas[0].url)

                if file_url not in new_block.urls:
                    new_block.urls.append(file_url)

                new_block.text = (
                    f"Input text was too long ({text_length:,} characters) and has been converted to a file.\n"
                    f"Please read and process the content from the attached file URL."
                )

                if new_block.metadata.process_url_in_text:
                    new_block.metadata.process_url_in_text = False

                return new_block
            else:
                logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).warning(
                    "Upload returned empty list; falling back to truncation"
                )

        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).exception(f"Failed to convert large input to file: {str(e)}")

        fallback_limit = 60000  # A safe, hardcoded fallback limit

        fallback_block = data_block.model_copy(deep=True)

        if fallback_block.metadata.process_url_in_text:
            try:
                fallback_block.process_url_in_text(ctx)
                # We leave the flag enabled on fallback because text remains (albeit truncated)
            except Exception:
                pass

        fallback_block.text = (
            f"{data_block.text[:fallback_limit]}\n...[TRUNCATED DUE TO UPLOAD FAILURE]..."
        )
        return fallback_block

    @staticmethod
    async def to_input(
        ctx: Context,
        app_state: AppState,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        upstream_nodes: List[str],
        current_node_action: ActionDoc,
        action_response: Dict[str, ActionDoc],
        separator: str,
        data_flow_type: DataFlowType = DataFlowType.only_output,
        is_loop: bool = False,
        excel_loop: bool = False,
        file_column_index: int = 0,
        api_loop: bool = False,
        batches: int = 0,
        is_split: bool = False,
        file_split: bool = False,
        header: bool = False,
        data_processing: bool = False,
        instruction_column_index: int = 0,
        file_processing: bool = False,
        process_url_in_text: bool = True,
        process_attachment_url: bool = True,
        attachment_requested: int = 0,
    ) -> DataBlock | List[DataBlock] | Dict[str, Any]:
        logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info("Choosing manual path to generate input for Node")

        settings = SettingsProvider.sget()
        s3 = get_public_s3(settings=settings)
        # Using standard UserFiles behavior (files/ folder)
        user_files = UserFiles(user=user, s3=s3, settings=settings)

        # Shared helper for all branches
        def indent_text(text: str, indent: str) -> str:
            # Indent every line in a potentially multi-line string
            return "\n".join(f"{indent}{line}" for line in (text or "").splitlines())

        if api_loop:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info("to_input for using API loop")
            blocks: list[DataBlock] = []

            for upstream_node in upstream_nodes:
                action_doc = action_response.get(upstream_node)
                upstream_title = action_doc.name or "Start"

                logger.bind(
                    ctx=ctx,
                    code=AppCode.ACTION_GRAPH,
                    upstream_action_name=action_doc.name,
                    curr_action=current_node_action.name,
                ).info(
                    f"Creating API loop input for node: {current_node_action.name} "
                    f"from upstream node: {upstream_title}"
                )

                try:
                    action_outputs = DataBlocks.model_validate(action_doc.output)
                    for action_output in action_outputs.blocks:
                        base_text = action_output.text or ""

                        for batch in range(batches):
                            lines: list[str] = []
                            if data_flow_type == DataFlowType.as_is_last_output:
                                lines.append(base_text)
                            else:
                                lines.append(f"## Upstream: {upstream_title} (API Loop)")
                                lines.append("")
                                lines.append(f"### Batch {batch + 1}/{batches}")
                                lines.append("")
                                lines.append("**Base Output:**")
                                lines.append(indent_text(base_text, "    "))
                                lines.append("")
                                lines.append("**Instruction:**")
                                lines.append(
                                    f"    Get Google Sheets data for `batch_id={batch}` using `api_call`."
                                )

                            data_block = DataBlock(
                                text="\n".join(lines),
                                urls=list(action_output.urls) if action_output.urls else [],
                                metadata=Metadata(
                                    process_url_in_text=process_url_in_text,
                                    process_attachment_url=process_attachment_url,
                                    attachment_requested=attachment_requested,
                                ),
                            )
                            data_block = await ActionGraphInputCreator._maybe_convert_to_file(
                                ctx, app_state, data_block, user_files
                            )
                            blocks.append(data_block)
                except Exception as e:
                    logger.bind(
                        ctx=ctx,
                        code=AppCode.ACTION_GRAPH,
                        curr_action_name=current_node_action.name,
                        upstream_action_name=getattr(action_doc, "name", None),
                    ).exception(f"Error in to_input api_loop: {str(e)}")

            return blocks

        elif excel_loop:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info("to_input for using Excel loop")
            blocks: list[DataBlock] = []

            for upstream_node in upstream_nodes:
                action_doc = action_response.get(upstream_node)
                upstream_title = action_doc.name or "Start"

                logger.bind(
                    ctx=ctx,
                    code=AppCode.ACTION_GRAPH,
                    upstream_action_name=action_doc.name,
                    curr_action=current_node_action.name,
                ).info(
                    f"Creating Excel loop input for node: {current_node_action.name} "
                    f"from upstream node: {upstream_title}"
                )

                try:
                    action_outputs = DataBlocks.model_validate(action_doc.output)
                    for action_output in action_outputs.blocks:
                        urls = list(action_output.urls or [])
                        text = action_output.text or ""
                        if not urls and not text:
                            raise ValueError(
                                "excel_loop enabled but upstream block has no urls and no text containing URLs"
                            )
                        spreadsheet_url = pick_spreadsheet_url(urls, text=text)
                        # Split excel into batches (each batch is a single-row dataframe here)
                        excel_batches = split_excel_into_batches(spreadsheet_url, 1, header=header)
                        total_rows = len(excel_batches)

                        for idx, batch in enumerate(excel_batches):
                            lines: list[str] = []
                            if data_flow_type == DataFlowType.as_is_last_output:
                                pass
                            else:
                                lines.append(f"## Upstream: {upstream_title} (Excel Loop)")
                                lines.append("")
                                lines.append(f"### Row {idx + 1}/{total_rows}")
                                lines.append("")

                            data_block = None

                            if data_processing:
                                # Process all columns from the row, not just first column
                                row_data = batch.to_dict(orient="records")[0]
                                base_text = action_output.text or ""
                                if data_flow_type == DataFlowType.as_is_last_output:
                                    lines.append(
                                        indent_text(json.dumps(row_data, indent=2), "    ")
                                    )
                                else:
                                    lines.append("**Base Output:**")
                                    lines.append(indent_text(base_text, "    "))
                                    lines.append("")
                                    lines.append("**Row Data (JSON):**")
                                    lines.append(
                                        indent_text(json.dumps(row_data, indent=2), "    ")
                                    )

                                data_block = DataBlock(
                                    text="\n".join(lines),
                                    metadata=Metadata(
                                        process_url_in_text=process_url_in_text,
                                        process_attachment_url=process_attachment_url,
                                        attachment_requested=attachment_requested,
                                    ),
                                )

                            elif file_processing:
                                instruction = batch.iloc[0][instruction_column_index]
                                file_url = batch.iloc[0][file_column_index]
                                base_text = action_output.text or ""

                                if data_flow_type == DataFlowType.as_is_last_output:
                                    lines.append(f"    {file_url}")
                                else:
                                    lines.append("**Instruction:**")
                                    lines.append(indent_text(str(instruction), "    "))
                                    lines.append("")
                                    lines.append("**File URL:**")
                                    lines.append(f"    {file_url}")
                                    lines.append("")
                                    lines.append("**Base Output:**")
                                    lines.append(indent_text(base_text, "    "))

                                data_block = DataBlock(
                                    text="\n".join(lines),
                                    urls=[file_url],
                                    metadata=Metadata(
                                        process_url_in_text=process_url_in_text,
                                        process_attachment_url=process_attachment_url,
                                        attachment_requested=attachment_requested,
                                    ),
                                )

                            else:
                                file_url = batch.iloc[0][file_column_index]
                                base_text = action_output.text or ""

                                if data_flow_type == DataFlowType.as_is_last_output:
                                    lines.append(f"    {file_url}")
                                else:
                                    lines.append("**Base Output:**")
                                    lines.append(indent_text(base_text, "    "))
                                    lines.append("")
                                    lines.append("**File URL:**")
                                    lines.append(f"    {file_url}")

                                data_block = DataBlock(
                                    text="\n".join(lines),
                                    urls=[file_url],
                                    metadata=Metadata(
                                        process_url_in_text=process_url_in_text,
                                        process_attachment_url=process_attachment_url,
                                        attachment_requested=attachment_requested,
                                    ),
                                )

                            # Apply file conversion check
                            if data_block:
                                data_block = await ActionGraphInputCreator._maybe_convert_to_file(
                                    ctx, app_state, data_block, user_files
                                )
                                blocks.append(data_block)
                except Exception as e:
                    logger.bind(
                        ctx=ctx,
                        code=AppCode.ACTION_GRAPH,
                        curr_action_name=current_node_action.name,
                        upstream_action_name=getattr(action_doc, "name", None),
                    ).exception(f"Error in to_input excel_loop: {str(e)}")

            return blocks

        elif is_loop:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info("to_input for using is_loop")
            blocks: list[DataBlock] = []
            # store (upstream_title, [DataBlock, ...]) when not splitting
            temp_blocks: list[tuple[str, list[DataBlock]]] = []

            for upstream_node in upstream_nodes:
                action_doc = action_response.get(upstream_node)
                upstream_title = action_doc.name or "Start"

                logger.bind(
                    ctx=ctx,
                    code=AppCode.ACTION_GRAPH,
                    upstream_action_name=action_doc.name,
                    curr_action=current_node_action.name,
                ).info(
                    f"Creating loop input for node: {current_node_action.name} "
                    f"from upstream node: {upstream_title}"
                )

                try:
                    action_outputs = DataBlocks.model_validate(action_doc.output)
                    upstream_blocks: list[DataBlock] = []

                    for action_output in action_outputs.blocks:
                        text = (action_output.text or "").strip()

                        # check for urls first TODO: Need a better solution
                        if is_split and not file_split:
                            if not text:
                                continue
                            multiple_objs = text.split(separator)
                            for multiple in multiple_objs:
                                segment = multiple.strip()
                                if not segment:
                                    continue
                                data_block = DataBlock(
                                    text=segment,
                                    metadata=Metadata(
                                        process_url_in_text=process_url_in_text,
                                        process_attachment_url=process_attachment_url,
                                        attachment_requested=attachment_requested,
                                    ),
                                )
                                data_block = await ActionGraphInputCreator._maybe_convert_to_file(
                                    ctx, app_state, data_block, user_files
                                )
                                blocks.append(data_block)

                        elif file_split and action_output.urls:
                            for url in action_output.urls:
                                data_block = DataBlock(
                                    text=text,
                                    urls=[url],
                                    metadata=Metadata(
                                        process_url_in_text=process_url_in_text,
                                        process_attachment_url=process_attachment_url,
                                        attachment_requested=attachment_requested,
                                    ),
                                )
                                data_block = await ActionGraphInputCreator._maybe_convert_to_file(
                                    ctx, app_state, data_block, user_files
                                )
                                blocks.append(data_block)

                        else:
                            # non-splitting path: keep one DataBlock per upstream output
                            data_block = DataBlock(
                                text=text,
                                urls=list(action_output.urls) if action_output.urls else [],
                                metadata=Metadata(
                                    process_url_in_text=process_url_in_text,
                                    process_attachment_url=process_attachment_url,
                                    attachment_requested=attachment_requested,
                                ),
                            )
                            upstream_blocks.append(data_block)

                    if not is_split and not file_split and upstream_blocks:
                        temp_blocks.append((upstream_title, upstream_blocks))

                except Exception as e:
                    logger.bind(
                        ctx=ctx,
                        code=AppCode.ACTION_GRAPH,
                        current_node_name=current_node_action.name,
                        upstream_action_name=getattr(action_doc, "name", None),
                    ).exception(f"Error in to_input is_loop: {str(e)}")

            # Only combine blocks if we're not splitting
            if not is_split and not file_split and temp_blocks:
                # Find the minimum length among all upstream blocks to avoid index errors
                min_length = min(len(blocks_list) for _, blocks_list in temp_blocks)

                # Combine blocks from different upstreams by index
                for i in range(min_length):
                    lines: list[str] = []
                    combined_urls: list[str] = []

                    for upstream_title, upstream_blocks in temp_blocks:
                        block_text = upstream_blocks[i].text or ""
                        block_urls = upstream_blocks[i].urls or []

                        if lines:
                            lines.append("")  # blank line between upstreams

                        if data_flow_type == DataFlowType.as_is_last_output:
                            lines.append(block_text)
                        else:
                            lines.append(f"## Upstream: {upstream_title}")
                            lines.append("")
                            lines.append(indent_text(block_text, "    "))
                        
                        combined_urls.extend(block_urls)

                    combined_text = "\n".join(lines)

                    data_block = DataBlock(
                        text=combined_text,
                        urls=combined_urls,
                        metadata=Metadata(
                            process_url_in_text=process_url_in_text,
                            process_attachment_url=process_attachment_url,
                            attachment_requested=attachment_requested,
                        ),
                    )
                    data_block = await ActionGraphInputCreator._maybe_convert_to_file(
                        ctx, app_state, data_block, user_files
                    )
                    blocks.append(data_block)

            return blocks

        else:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info("to_input default")

            lines: list[str] = []
            urls: list[str] = []
            tabs = -1

            for upstream_node in upstream_nodes:
                tabs += 1
                indent = "    " * tabs  # 4 spaces per level (better than \t for LLMs)

                action_doc = action_response.get(upstream_node)
                logger.bind(
                    ctx=ctx,
                    code=AppCode.ACTION_GRAPH,
                    upstream_action_name=action_doc.name,
                    curr_action=current_node_action.name,
                ).info(
                    f"Creating input for node: {current_node_action.name} "
                    f"from upstream node: {action_doc.name}"
                )
                try:
                    action_results = action_doc.results

                    # Blank line between upstream sections
                    if lines:
                        lines.append("")

                    if data_flow_type != DataFlowType.as_is_last_output:
                        tabs += 1
                        indent = "    " * tabs  # 4 spaces per level (better than \t for LLMs)

                        upstream_title = action_doc.name or "Start"
                        # Level-2 heading for each upstream node
                        lines.append(f"## Upstream: {upstream_title}")

                    for idx, action_result in enumerate(action_results, start=1):
                        try:
                            if data_flow_type == DataFlowType.as_is_last_output:
                                action_result_output = DataBlocks.model_validate(
                                    action_result.output
                                )
                                for jdx, data_block in enumerate(
                                    action_result_output.blocks, start=1
                                ):
                                    if data_block.text:
                                        lines.append(data_block.text)
                                    if data_block.urls:
                                        urls.extend(data_block.urls)
                            else:
                                lines.append("")  # blank line before each run
                                lines.append(f"{indent}### Run {idx}")
                                if data_flow_type == DataFlowType.both_input_output:
                                    # INPUT
                                    action_result_input = DataBlock.model_validate(
                                        action_result.input
                                    )
                                    lines.append(f"{indent}**Input:**")

                                    input_text = action_result_input.text or ""
                                    lines.append(indent_text(input_text, indent + "    "))
                                    urls.extend(action_result_input.urls)

                                # OUTPUT
                                action_result_output = DataBlocks.model_validate(
                                    action_result.output
                                )
                                lines.append("")
                                lines.append(f"{indent}**Output:**")

                                for jdx, data_block in enumerate(
                                    action_result_output.blocks, start=1
                                ):
                                    block_text = data_block.text or ""
                                    # Bullet per block for readability
                                    lines.append(f"{indent}- Block {jdx}:")
                                    lines.append(indent_text(block_text, indent + "    "))
                                    urls.extend(data_block.urls)

                        except Exception as e:
                            logger.bind(
                                ctx=ctx,
                                code=AppCode.ACTION_GRAPH,
                                curr_action_name=current_node_action.name,
                                upstream_action_name=action_doc.name,
                            ).exception(f"Error while creating input from upstream node: {str(e)}")

                except Exception as e:
                    logger.bind(
                        ctx=ctx,
                        code=AppCode.ACTION_GRAPH,
                        curr_action_name=current_node_action.name,
                        upstream_action_name=getattr(action_doc, "name", None),
                    ).exception(f"Error while processing upstream node '{upstream_node}': {str(e)}")

            input_msg = "\n".join(lines)

            data_block = DataBlock(
                text=input_msg,
                urls=urls,
                metadata=Metadata(
                    process_url_in_text=process_url_in_text,
                    process_attachment_url=process_attachment_url,
                    attachment_requested=attachment_requested,
                ),
            )

            data_block = await ActionGraphInputCreator._maybe_convert_to_file(
                ctx, app_state, data_block, user_files
            )

            return data_block