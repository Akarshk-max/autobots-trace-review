# from typing import AsyncGenerator, Dict, List, Any, Type
# from loguru import logger
# from motor.motor_asyncio import AsyncIOMotorDatabase
# from openai import AsyncStream
# from openai.types.chat import ChatCompletionToolMessageParam
# from openai.types.responses import Response as OpenAIResponse
# # ResponseChunk doesn't exist yet, use ChatCompletionChunk for streaming
# from openai.types.chat import ChatCompletionChunk as ResponseChunk
# from tenacity import retry, stop_after_attempt, wait_fixed
#
# from src.autobots.conn.openai.azure_openai_client_factory import AzureOpenAIModelClientFactory
# from src.autobots.conn.openai.openai_responses.response_model import ResponseCreate
# from src.autobots.core.logging.app_code import AppCode
# from src.autobots.data_model.context import Context
# from src.autobots.llm.tools.tool_factory import ToolFactory
# from src.autobots.user.user_orm_model import UserORM
#
#
# class OpenaiResponses:
#     def __init__(self, client_factory: Type[AzureOpenAIModelClientFactory]):
#         self.client_factory = client_factory
#
#     @retry(stop=stop_after_attempt(3), wait=wait_fixed(3), reraise=True)
#     async def create(
#         self,
#         response_create: ResponseCreate,
#         user: UserORM | None = None,
#         ctx: Context | None = None,
#         db: AsyncIOMotorDatabase | None = None
#     ) -> OpenAIResponse | AsyncStream[ResponseChunk] | None:
#         """
#         Create a response using the OpenAI Responses API.
#
#         Args:
#             response_create: Configuration for the response creation
#             user: Optional user information
#             ctx: Optional context information
#             db: Optional database connection
#
#         Returns:
#             OpenAIResponse object containing the response
#         """
#         try:
#             logger.bind(ctx=ctx).trace("Starting OpenAI Responses, try: 1")
#
#             # # Store the client in a temporary variable instead of directly in the model
#             # # This avoids serialization issues and prevents the client from being passed to the API
#             # temp_client = self.client
#
#             # Check if we're using the Responses API or Chat Completions
#             using_responses_api = False
#             try:
#                 using_responses_api = hasattr(temp_client, "responses")
#             except (AttributeError, ImportError):
#                 using_responses_api = False
#
#             logger.bind(ctx=ctx).info(f"Using Responses API: {using_responses_api}")
#
#             # Call the response loop with the client passed separately
#             res = await self.response_loop(
#                 ctx=ctx,
#                 response_create=response_create,
#                 user=user,
#                 db=db
#             )
#
#
#             logger.debug(f"res : {res}")
#             logger.trace("Completed OpenAI Responses")
#
#             if isinstance(res, AsyncStream):
#                 return self.yield_response_chunks(res)
#             else:
#                 return res
#         except Exception as e:
#             # Exclude client from the model dump to avoid serialization issues
#             error_log_data = response_create.model_dump(exclude={"client"}, exclude_none=True)
#             logger.bind(ctx=ctx, response_create=error_log_data
#                       ).exception(f"Error in OpenAI Responses: {str(e)}")
#             raise
#
#     async def response_loop(
#         self,
#         ctx: Context,
#         response_create: ResponseCreate,
#         user: UserORM | None = None,
#         db: AsyncIOMotorDatabase | None = None
#     ) -> AsyncStream[ResponseChunk] | OpenAIResponse:
#         """
#         Handle the response loop, including tool calls and follow-up responses.
#
#         Args:
#             ctx: Context information
#             response_create: Configuration for the response creation
#             user: Optional user information
#             db: Optional database connection
#
#         Returns:
#             OpenAIResponse or AsyncStream of ResponseChunk
#         """
#         max_loop = 20
#         curr_loop = 0
#
#         while True:
#             if curr_loop >= max_loop:
#                 logger.bind(ctx=ctx).error(f"Response loop exceeded max loop: {max_loop}")
#                 raise RecursionError({"detail": f"Response loop exceeded max loop: {max_loop}"})
#
#             curr_loop += 1
#             logger.bind(ctx=ctx).info(f"Response Loop: {curr_loop}")
#
#             # Get model-specific configuration
#             response_config = await self.get_model_based_config(ctx=ctx, response_create=response_create)
#             # logger.debug(f"Response Config: {response_config}")
#             # Double-check to ensure client is not in the response_config
#             if "client" in response_config:
#                 logger.bind(ctx=ctx).warning("Client found in response_config, removing it")
#                 response_config.pop("client")
#
#             # Check if we're using the responses API
#             using_responses_api = False
#             try:
#                 # Check if responses API is available
#                 using_responses_api = hasattr(self.client, "responses")
#             except (AttributeError, ImportError):
#                 using_responses_api = False
#             # logger.debug(f"Using Responses API: {using_responses_api}")
#             # For Responses API, we need to convert messages to input
#             if using_responses_api and "messages" in response_config and "input" not in response_config:
#                 logger.bind(ctx=ctx).info("Converting messages to input for Responses API")
#                 response_config["input"] = response_config.pop("messages")
#             # logger.debug(f"Response Config: {response_config}")
#             try:
#                 if using_responses_api:
#                     # Use the responses API
#                     logger.bind(ctx=ctx).info("Using Responses API")
#                     # logger.debug(f"client : {self.client}")
#                     # logger.debug(f"client responses : {self.client.responses}")
#                     # google_tool = {'type': 'function', 'name': 'google_search_main', 'description': 'Browse the internet/Do a google search', 'parameters': {'type': 'object', 'properties': {'engine': {'const': 'google', 'default': 'google', 'title': 'Engine', 'type': 'string'}, 'device': {'anyOf': [{'enum': ['desktop', 'tablet', 'mobile'], 'type': 'null'}, {'type': 'null'}], 'default': None, 'title': 'Device'}, 'no_cache': {'anyOf': [{'type': 'null'}, {'type': 'null'}], 'default': None, 'title': 'No Cache'}, 'output': {'anyOf': [{'enum': ['JSON', 'html'], 'type': 'null'}, {'type': 'null'}], 'default': None, 'title': 'Output'}, 'start': {'anyOf': [{'type': 'null'}, {'type': 'null'}], 'default': None, 'title': 'Start'}, 'num': {'anyOf': [{'type': 'null'}, {'type': 'null'}], 'default': None, 'title': 'Num'}, 'tbm': {'anyOf': [{'enum': ['isch', 'lcl', 'vid', 'nws', 'shop', 'pts'], 'type': 'null'}, {'type': 'null'}], 'default': None, 'title': 'Tbm'}, 'tbs': {'anyOf': [{'type': 'null'}, {'type': 'null'}], 'default': None, 'title': 'Tbs'}, 'safe': {'anyOf': [{'enum': ['active', 'off'], 'type': 'null'}, {'type': 'null'}], 'default': None, 'title': 'Safe'}, 'nfpr': {'anyOf': [{'enum': ['0', '1'], 'type': 'null'}, {'type': 'null'}], 'default': None, 'title': 'Nfpr'}, 'filter': {'anyOf': [{'enum': ['0', '1'], 'type': 'null'}, {'type': 'null'}], 'default': None, 'title': 'Filter'}, 'google_domain': {'anyOf': [{'type': 'null'}, {'type': 'null'}], 'default': None, 'title': 'Google Domain'}, 'gl': {'anyOf': [{'type': 'null'}, {'type': 'null'}], 'default': None, 'title': 'Gl'}, 'hl': {'anyOf': [{'type': 'null'}, {'type': 'null'}], 'default': None, 'title': 'Hl'}, 'cr': {'anyOf': [{'type': 'null'}, {'type': 'null'}], 'default': None, 'title': 'Cr'}, 'lr': {'anyOf': [{'type': 'null'}, {'type': 'null'}], 'default': None, 'title': 'Lr'}, 'location': {'anyOf': [{'type': 'null'}, {'type': 'null'}], 'default': None, 'title': 'Location'}, 'q': {'title': 'Q', 'type': 'string'}}, 'required': ['q']}}
#                     tools = []
#
#                     for tool in response_config["tools"]:
#                         # logger.debug(f"tool : {tool}")
#                         tools.append(self.refine_tool_definition(tool))
#                     # tools = self.refine_tool_definition(response_config["tools"])
#                     response_config["tools"] = tools
#                     logger.debug("response_config : ", response_config)
#                     # logger.debug(f"response_config : {response_config}")
#                     # logger.debug(f"google_tool_2 : {google_tool_2}")
#                     # response_config_1 = {
#                     #     "model" : "gpt-4o",
#                     #     "input" : "What's the latest news about AI?"
#                     # }
#                     # response1 = await self.client.responses.create(
#                     #        **response_config_1
#                     # )
#                     # logger.debug(f"response1 : {response1.output}")
#                     # logger.debug(f"response_config : {response_config}")
#                     response = await self.client.responses.create(**response_config)
#                     # logger.debug(f"Response: {response}")
#                 else:
#                     # Fall back to chat.completions
#                     logger.bind(ctx=ctx).info("Using Chat Completions API")
#                     response = await self.client.chat.completions.create(**response_config)
#             except Exception as e:
#                 logger.bind(ctx=ctx, error=str(e), config_keys=list(response_config.keys())).error(f"Error calling API: {str(e)}")
#                 raise
#
#             # Handle streaming responses
#             if isinstance(response, AsyncStream):
#                 return response
#             # logger.debug(f"response : {using_responses_api}")
#             # Process the response based on API type
#             if using_responses_api:
#                 # logger.debug("hi")
#                 # Handle Responses API response format
#                 if hasattr(response, 'output') and response.output:
#                     output_item = response.output[-1]  # Get the last output item
#
#                     logger.bind(ctx=ctx, response_config=response_config, output_item=output_item.model_dump()
#                             ).info("OpenAI Response Output Item")
#                     # Check if the output is a tool call
#                     if hasattr(output_item, 'arguments'):
#                         messages = await self.run_tools(ctx=ctx, output_item=output_item, user=user, db=db)
#                         response_create.messages += messages
#                     # Check if the output is a message
#                     elif hasattr(output_item, 'content'):
#                         response_create.messages.append({"role": output_item.role, "content": output_item.content[0].text})
#                 else:
#                     logger.bind(ctx=ctx).warning("No valid output in response from Responses API")
#                     return response_create
#             else:
#                 # Handle Chat Completions API response format (original code)
#                 choice = response.choices[-1]
#                 logger.bind(ctx=ctx, response_config=response_config, choice=choice.model_dump()
#                           ).info("OpenAI Response Choice")
#
#                 # Add the response to the message history
#                 response_create.messages.append(choice.message.model_dump(exclude_none=True))
#
#                 # Handle tool calls if present
#                 if choice.message.tool_calls or choice.finish_reason == "tool_calls":
#                     messages = await self.run_tools(ctx=ctx, choice=choice, user=user, db=db)
#                     # logger.debug(f"messages : {messages}")
#                     response_create.messages += messages
#
#                 # Determine if we need to continue the loop
#                 if choice.finish_reason == "tool_calls":
#                     continue
#                 elif choice.finish_reason == "stop":
#                     return response
#                 elif choice.finish_reason == "length":
#                     # Add a system message to continue the response
#                     response_create.messages.append({"role": "system", "content": "Continue"})
#                     continue
#
#             return response_create
#
#
#
#
#     def refine_tool_definition(self,tool):
#         """
#         Cleans up a tool definition by removing unnecessary details.
#         :param tool: (dict) The original tool definition.
#         :return: (dict) The refined tool definition.
#         """
#         try:
#         # tool_type = tool.get("type", "function")
#             tool_type = tool["type"]
#             # logger.debug(f"tool : {tool_type}")
#             if (tool_type == "web_search_preview" or tool_type == "file_search" or tool_type == "computer_use_preview"):
#                 # logger.debug(f"tool : {tool}")
#                 return tool
#             refined_tool = {
#                 "name": tool["name"],
#                 "type": tool.get("type", "function"),
#                 "description": tool.get("description", ""),
#                 "parameters": {
#                     "type": "object",
#                     "properties": {},
#                     "required": tool["parameters"].get("required", [])
#                 }
#             }
#             # logger.debug(f"refined_tool : {refined_tool}")
#             for key, value in tool["parameters"]["properties"].items():
#                 refined_value = {}
#
#                 # Keep title and type
#                 if "title" in value:
#                     refined_value["title"] = value["title"]
#                 if "type" in value:
#                     refined_value["type"] = value["type"]
#
#                 # Keep enum if present
#                 if "enum" in value:
#                     refined_value["enum"] = value["enum"]
#
#                 # Keep default value only if it's not None
#                 if "default" in value and value["default"] is not None:
#                     refined_value["default"] = value["default"]
#
#                 # Handle 'anyOf' structures: extract meaningful values
#                 if "anyOf" in value:
#                     continue
#                     # refined_value.update(self.extract_meaningful_anyof(value["anyOf"]))
#
#                 refined_tool["parameters"]["properties"][key] = refined_value
#
#             return refined_tool
#         except Exception as e:
#             logger.debug(f"tool : {tool}")
#             logger.debug(f"error in changing tool definition {e}")
#     def extract_meaningful_anyof(self,anyof_list):
#         """
#         Extracts meaningful data from an 'anyOf' structure.
#         :param anyof_list: (list) The 'anyOf' list from OpenAI tool schema.
#         :return: (dict) A cleaned-up property definition.
#         """
#         for option in anyof_list:
#             if isinstance(option, dict) and "type" in option and option["type"] != "null":
#                 return option  # Keep the first non-null type
#         return {}
#
#
#     async def yield_response_chunks(self, response_stream: AsyncStream[ResponseChunk]) -> AsyncGenerator[ResponseChunk, None]:
#         """
#         Yield chunks from a streaming response.
#
#         Args:
#             response_stream: AsyncStream of ResponseChunk
#
#         Yields:
#             ResponseChunk objects
#         """
#         try:
#             async for chunk in response_stream:
#                 yield chunk
#         except Exception as e:
#             logger.error(f"Error in streaming response: {str(e)}")
#
#     async def run_tools(
#         self,
#         ctx: Context,
#         output_item,
#         user: UserORM | None = None,
#         db: AsyncIOMotorDatabase | None = None
#     ) -> List[Dict[str, Any]]:
#         """
#         Execute tool calls from the model response.
#
#         Args:
#             ctx: Context information
#             choice: The model's choice containing tool calls
#             user: Optional user information
#             db: Optional database connection
#
#         Returns:
#             List of messages with tool results
#         """
#         messages = []
#         # logger.debug(f"output_item : {output_item}")
#         # for tool_call in output_item:
#             # logger.debug(f"tool_call : {tool_call}")
#         tool_call_id = output_item.call_id
#         tool_name = output_item.name
#         tool_args = output_item.arguments
#         try:
#             # Run the tool
#             tool_output_str: str = await ToolFactory(user=user, db=db).run_tool(ctx, tool_name, tool_args)
#             logger.bind(
#                 ctx=ctx, app_code=AppCode.ACTION, tool=tool_name, tool_input=tool_args, tool_output=tool_output_str
#             ).info(f"Ran tool: {tool_name}")
#             logger.debug(f"tool_output_str : {tool_output_str}")
#             # Add tool call output to messages
#             tool_output_message = ChatCompletionToolMessageParam(
#                 role="tool",
#                 content=tool_output_str,
#                 tool_call_id=tool_call_id
#             )
#             if (isinstance(tool_output_message, dict)):
#                 messages.append(tool_output_message)
#             else:
#                 messages.append(tool_output_message.model_dump(exclude_none=True))
#         except Exception as e:
#             logger.bind(
#                 ctx=ctx, app_code=AppCode.ACTION, tool=tool_name, tool_error=str(e)
#             ).exception(f"Error in tool_call: {str(e)}")
#
#             # Add tool call error to messages
#             tool_error_message = ChatCompletionToolMessageParam(
#                 role="tool",
#                 content=str(e),
#                 tool_call_id=tool_call_id
#             )
#             messages.append(tool_error_message.model_dump(exclude_none=True))
#
#         return messages
#
#
#     @staticmethod
#     async def get_model_based_config(ctx: Context, response_create: ResponseCreate) -> Dict[str, Any]:
#         """
#         Adjust configuration based on the model being used.
#
#         Args:
#             ctx: Context information
#             response_create: Configuration for the response creation
#
#         Returns:
#             Adjusted configuration dictionary
#         """
#         # Explicitly exclude the client attribute to prevent it from being passed to the API
#         response_dict = response_create.model_dump(exclude={"client"}, exclude_none=True)
#
#         # Handle model-specific adjustments
#         match response_create.model:
#             case "gpt-4o-1120":
#                 response_create.model = "gpt-4o"
#                 response_dict.pop("reasoning_effort", None)
#             case "gpt-4o-0806":
#                 response_create.model = "gpt-4o"
#                 response_dict.pop("reasoning_effort", None)
#             case "gpt-4o-0513":
#                 response_create.model = "gpt-4o"
#                 response_dict.pop("reasoning_effort", None)
#             case "o1":
#                 max_tokens = response_dict.pop("max_tokens", None)
#                 if max_tokens:
#                     response_dict["max_completion_tokens"] = max_tokens
#                 response_dict.pop("temperature", None)
#             case "o1-mini":
#                 max_tokens = response_dict.pop("max_tokens", None)
#                 if max_tokens:
#                     response_dict["max_completion_tokens"] = max_tokens
#                 response_dict.pop("temperature", None)
#                 response_dict.pop("tools", None)
#                 response_dict.pop("stop", None)
#             case "o3-mini":
#                 max_tokens = response_dict.pop("max_tokens", None)
#                 if max_tokens:
#                     response_dict["max_completion_tokens"] = max_tokens
#                 response_dict.pop("temperature", None)
#                 response_dict.pop("stop", None)
#             case "deepseek-r1":
#                 response_dict.pop("n", None)
#                 response_dict.pop("reasoning_effort", None)
#                 if "user" in response_dict:
#                     response_dict.pop("user")
#
#         # Remove Responses API specific parameters when using chat.completions
#         response_dict.pop("previous_response_id", None)
#         response_dict.pop("max_tokens", None)
#         response_dict.pop("truncation", None)
#         response_dict.pop("store", None)
#         response_dict.pop("web_search_settings", None)
#         response_dict.pop("file_search_settings", None)
#         response_dict.pop("computer_use_settings", None)
#
#         # Double-check to ensure client is not in the response_dict
#         if "client" in response_dict:
#             response_dict.pop("client")
#
#         return response_dict
#
#     async def create_stream(
#         self,
#         response_create: ResponseCreate,
#         user: UserORM | None = None,
#         ctx: Context | None = None,
#         db: AsyncIOMotorDatabase | None = None
#     ) -> AsyncGenerator[ResponseChunk, None]:
#         """
#         Create a streaming response.
#
#         Args:
#             response_create: Configuration for the response creation
#             user: Optional user information
#             ctx: Optional context information
#             db: Optional database connection
#
#         Returns:
#             AsyncGenerator yielding ResponseChunk objects
#         """
#         try:
#             # Ensure stream is set to True for streaming responses
#             response_create.stream = True
#
#             # Get the response stream
#             stream = await self.create(
#                 response_create=response_create,
#                 user=user,
#                 ctx=ctx,
#                 db=db
#             )
#
#             # Yield chunks from the stream
#             if isinstance(stream, AsyncStream):
#                 async for chunk in stream:
#                     yield chunk
#             else:
#                 logger.bind(ctx=ctx).error("Expected AsyncStream but got a different type")
#         except Exception as e:
#             logger.bind(ctx=ctx).exception(f"Error in create_stream: {str(e)}")
#             raise
#
#     async def create_with_tools(
#         self,
#         response_create: ResponseCreate,
#         tools: List[Dict[str, Any]],
#         tool_choice: str | Dict[str, Any] | None = None,
#         user: UserORM | None = None,
#         ctx: Context | None = None,
#         db: AsyncIOMotorDatabase | None = None
#     ) -> OpenAIResponse | None:
#         """
#         Create a response with function calling capabilities.
#
#         Args:
#             response_create: Configuration for the response creation
#             tools: List of tool definitions
#             tool_choice: Optional specification for tool selection
#             user: Optional user information
#             ctx: Optional context information
#             db: Optional database connection
#
#         Returns:
#             OpenAIResponse object containing the response with tool calls
#         """
#         try:
#             # Set tools and tool_choice in the request
#             response_create.tools = tools
#             if tool_choice:
#                 response_create.tool_choice = tool_choice
#
#             # Create the response
#             response = await self.create(
#                 response_create=response_create,
#                 user=user,
#                 ctx=ctx,
#                 db=db
#             )
#
#             return response
#         except Exception as e:
#             logger.bind(ctx=ctx).exception(f"Error in create_with_tools: {str(e)}")
#             raise
