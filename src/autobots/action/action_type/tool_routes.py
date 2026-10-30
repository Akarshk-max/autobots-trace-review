from src.autobots.action.action_type.action_audio2text.action_audio2text_transcription_deepgram import (
    ActionAudio2TextTranscriptionDeepgram,
)
from src.autobots.action.action_type.action_audio2text.action_audio2text_translation_openai import (
    ActionAudio2TextTranslationOpenai,
)
from src.autobots.action.action_type.action_llm.action_llm import ActionLlm
from src.autobots.action.action_type.action_multimodal.action_multimodal_google_search.action_multimodal_exa import (
    ActionMultimodalExa,
)
from src.autobots.action.action_type.action_multimodal.action_multimodal_google_search.action_multimodal_google_search_cse import (
    ActionMultimodalGoogleSearchMain,
)
from src.autobots.action.action_type.action_multimodal.action_multimodal_graph_rag_query import (
    ActionGraphRAGQuery,
)
from src.autobots.action.action_type.action_multimodal.action_multimodal_sandbox_query import (
    ActionMultimodalSandboxQuery,
)
from src.autobots.action.action_type.action_text2audio.action_text2audio_speech_deepgram import (
    ActionText2AudioSpeechDeepgram,
)
from src.autobots.action.action_type.action_text2audio.action_text2audio_speech_openai import (
    ActionText2AudioSpeechOpenai,
)
from src.autobots.action.action_type.action_text2img.action_gemini_nano_image_generation import (
    ActionImageGenerateGemini,
)
from src.autobots.action.action_type.action_text2img.action_text2img_dalle_openai_v2 import (
    ActionGenImageDalleOpenAiV2,
)
from src.autobots.action.action_type.action_text2img.action_text2img_flux import ActionGenImageFlux
from src.autobots.action.action_type.action_text2text.action_autobots import ActionAutobots
from src.autobots.action.action_type.action_text2text.action_csv_merge import ActionCSVMerge
from src.autobots.action.action_type.action_text2text.action_data_plane.action_data_plane_v2 import (
    ActionDataPlaneV2,
)
from src.autobots.action.action_type.action_text2text.action_extract_urls import ActionExtractURLs
from src.autobots.action.action_type.action_text2text.action_file_uploader_claude import (
    ActionFileUploaderClaude,
)
from src.autobots.action.action_type.action_text2text.action_file_uploader_gemini import (
    ActionFileUploaderGemini,
)
from src.autobots.action.action_type.action_text2text.action_file_uploader_openai import (
    ActionFileUploaderOpenai,
)
from src.autobots.action.action_type.action_text2text.action_json_flatten import ActionJsonFlatten
from src.autobots.action.action_type.action_text2text.action_json_query import ActionJsonReader
from src.autobots.action.action_type.action_text2text.action_multimodal_llm_chat_with_vector_search_openai import (
    ActionMultiModalLlmChatWithVectorSearchOpenai,
)
from src.autobots.action.action_type.action_text2text.action_playwright_login import (
    ActionPlaywrightLogin,
)
from src.autobots.action.action_type.action_text2text.action_playwright_scrap import (
    ActionPlaywrightScrape,
)
from src.autobots.action.action_type.action_text2text.action_sql_runner.action_db_sql_runner import (
    ActionDBSqlQueryRunner,
)
from src.autobots.action.action_type.action_text2text.action_sql_runner.action_sql_runner import (
    ActionSqlRunner,
)
from src.autobots.action.action_type.action_text2text.action_text2text_api import ActionText2textAPI
from src.autobots.action.action_type.action_text2text.action_text2text_daytona import (
    ActionText2TextDaytona,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    ActionText2TextLlmChatOpenai,
)
from src.autobots.action.action_type.action_text2text.action_text2text_pdf_parser import (
    ActionPDFParser,
)
from src.autobots.action.action_type.action_text2text.action_text2text_perplexity_search import (
    ActionText2TextPerplexitySearch,
)
from src.autobots.action.action_type.action_text2text.action_text2text_read_api_file import (
    ActionText2textReadApiFile,
)

# from src.autobots.action.action_type.action_text2text.action_text2text_search_map import (
#     ActionText2TextSearchMaps,
# )
from src.autobots.action.action_type.action_text2text.action_text2text_search_web import (
    ActionText2TextSearchWeb,
)

# from src.autobots.action.action_type.action_text2text.action_text2text_read_urls import (
#     ActionText2TextReadUrl,
# )
from src.autobots.action.action_type.action_text2text.action_urls_to_json import ActionURLsToJSON
from src.autobots.action.action_type.action_text2video.action_gemini_omni_video_generation import (
    ActionVideoGenerateGeminiOmni,
)
from src.autobots.action.action_type.action_text2video.action_gemini_veo_video_generation import (
    ActionVideoGenerateGeminiVeo,
)
from src.autobots.action.action_type.action_text2video.action_grok_video import (
    ActionVideoGenerateGrok,
)
from src.autobots.action.action_type.action_text2video.action_text2video_search_video import (
    ActionText2VideoSearchVideo,
)
from src.autobots.files.file_converter import FileConverter
from src.autobots.llm.tools.action_chat_history_query_claude import ActionChatHistoryQueryClaude
from src.autobots.llm.tools.action_chat_history_query_grok import ActionChatHistoryQueryGrok
from src.autobots.llm.tools.action_chat_history_query_openai import ActionChatHistoryQueryOpenai
from src.autobots.llm.tools.action_csv_perquet import ActionCSVToParquet
from src.autobots.llm.tools.action_file_search_store_gemini import (
    ActionFileSearchStoreGemini,
)
from src.autobots.llm.tools.action_graph_run_tools import ActionGraphRunTools
from src.autobots.llm.tools.action_graph_tools import ActionGraphTools
from src.autobots.llm.tools.action_parquet_inspector import ActionParquetInspector
from src.autobots.llm.tools.action_run_tools import ActionRunTools
from src.autobots.llm.tools.action_team_tools import ActionTeamTools
from src.autobots.llm.tools.action_tool_results_query_claude import ActionToolResultsQueryClaude
from src.autobots.llm.tools.action_tools import ActionTools
from src.autobots.llm.tools.agent_chat import AgentsChat
from src.autobots.llm.tools.base64_encoder_decoder import base64_decoder, base64_encoder
from src.autobots.llm.tools.base64_encoder_decoder_extended import (
    base64_decoder_extended,
    base64_encoder_extended,
)
from src.autobots.llm.tools.coding_assistant import coding_assistant
from src.autobots.llm.tools.csv_merge import csv_merge
from src.autobots.llm.tools.file_search_tool import file_search_tool
from src.autobots.llm.tools.file_tools import file_system_tool
from src.autobots.llm.tools.get_date_time import get_date_time
from src.autobots.llm.tools.merge_pdf import merge_pdf
from src.autobots.llm.tools.ocr_tool import ocr_tool
from src.autobots.llm.tools.openai_assistant_tool import openai_assistant
from src.autobots.llm.tools.openai_vector_store_tool import openai_vector_store_tool
from src.autobots.llm.tools.openrouter_inspect_tool import openrouter_inspect
from src.autobots.llm.tools.pdf_highlight import pdf_highlight
from src.autobots.llm.tools.prepare_claude_working_set import claude_container_file_attach
from src.autobots.llm.tools.pty_tools import (
    close_pty_session,
    read_pty_output,
    send_pty_input,
    start_pty_session,
)
from src.autobots.llm.tools.read_files_tool import read_files_from_user_system
from src.autobots.llm.tools.run_config_tool import run_config_tool
from src.autobots.llm.tools.schedules_tools import SchedulerTools
from src.autobots.llm.tools.sleep_tool import sleep
from src.autobots.llm.tools.terminal_tool import run_terminal_command
from src.autobots.llm.tools.tool_save_files import save_files_tool
from src.autobots.llm.tools.tool_save_outlook_mail_attachment import (
    save_outlook_email_attachment_tool,
)
from src.autobots.llm.tools.tools_map import ToolRegistry
from src.autobots.llm.tools.webhook_tools import WebhookTools
from src.autobots.skills.skills_tools import (
    create_skill,
    delete_skill,
    get_skill,
    get_system_skill,
    list_skills,
    list_system_skills,
    update_skill,
)


def register_tools():
    # ToolRegistry.register("google search", ActionMultimodalGoogleSearchMain)
    ToolRegistry.register("api", ActionText2textAPI)
    ToolRegistry.register("data_plane_v2", ActionDataPlaneV2)
    ToolRegistry.register("database", ActionDBSqlQueryRunner)
    # ToolRegistry.register("get date time", get_date_time)

    ToolRegistry.register("search_web", ActionText2TextSearchWeb)
    ToolRegistry.register("perplexity_search" , ActionText2TextPerplexitySearch)
    ToolRegistry.register("read_url", ActionPlaywrightScrape)
    ToolRegistry.register("vector_store_handler", ActionMultiModalLlmChatWithVectorSearchOpenai)
    # ToolRegistry.register("google_search", ActionMultimodalGoogleSearch)
    # ToolRegistry.register("serp_google_search", ActionMultimodalSerpGoogleSearch)
    ToolRegistry.register("api_call", ActionText2textAPI)
    ToolRegistry.register("sleep", sleep)
    ToolRegistry.register("pdf_highlight", pdf_highlight)
    ToolRegistry.register("run_config_tool", run_config_tool)
    ToolRegistry.register("read_api_file", ActionText2textReadApiFile)
    ToolRegistry.register("base64_encoder", base64_encoder)
    ToolRegistry.register("base64_decoder", base64_decoder)
    ToolRegistry.register("base64_encoder_extended", base64_encoder_extended)
    ToolRegistry.register("base64_decoder_extended", base64_decoder_extended)
    ToolRegistry.register("get_date_time", get_date_time)
    ToolRegistry.register("audio_transcription", ActionAudio2TextTranscriptionDeepgram)
    ToolRegistry.register("ocr_tool", ocr_tool)
    ToolRegistry.register("file_search_tool", file_search_tool)
    ToolRegistry.register("gemini_file_search_store_tool", ActionFileSearchStoreGemini)

    ToolRegistry.register("llm_chat", ActionText2TextLlmChatOpenai)
    ToolRegistry.register("text_to_speech", ActionText2AudioSpeechOpenai)
    ToolRegistry.register("text_to_speech_deepgram", ActionText2AudioSpeechDeepgram)
    ToolRegistry.register("audio_translate", ActionAudio2TextTranslationOpenai)
    ToolRegistry.register("openai_assistant", openai_assistant)
    ToolRegistry.register("coding_assistant", coding_assistant)
    ToolRegistry.register("csv_xls_doc_ppt_to_txt", FileConverter.csv_xls_doc_ppt_to_txt)
    ToolRegistry.register("image_generator", ActionGenImageFlux)
    ToolRegistry.register("image_generator_openai", ActionGenImageDalleOpenAiV2)
    ToolRegistry.register("video_search", ActionText2VideoSearchVideo)
    # ToolRegistry.register("maps_search", ActionText2TextSearchMaps)
    ToolRegistry.register("exa_search", ActionMultimodalExa)
    ToolRegistry.register("sql_runner", ActionSqlRunner)
    ToolRegistry.register("pdf_parser", ActionPDFParser)
    ToolRegistry.register("json_flatten", ActionJsonFlatten)
    ToolRegistry.register("json_reader" , ActionJsonReader)
    ToolRegistry.register("csv_to_parquet", ActionCSVToParquet)
    ToolRegistry.register("parquet_inspector", ActionParquetInspector)
    ToolRegistry.register("tool_results_query_claude", ActionToolResultsQueryClaude)
    ToolRegistry.register("chat_history_query_claude", ActionChatHistoryQueryClaude)
    ToolRegistry.register("chat_history_query_openai", ActionChatHistoryQueryOpenai)
    ToolRegistry.register("grok_chat_history_query", ActionChatHistoryQueryGrok)
    ToolRegistry.register("claude_container_file_attach", claude_container_file_attach)
    # ToolRegistry.register("csv_merge_v1", ActionCSVMerge)
    ToolRegistry.register("file_uploader_openai", ActionFileUploaderOpenai)
    ToolRegistry.register("file_uploader_gemini", ActionFileUploaderGemini)
    ToolRegistry.register("file_uploader_claude" , ActionFileUploaderClaude)
    ToolRegistry.register("action_llm", ActionLlm)
    ToolRegistry.register("save_files_tool", save_files_tool)
    ToolRegistry.register("save_outlook_email_attachment_tool", save_outlook_email_attachment_tool)
    ToolRegistry.register("save_urls", ActionExtractURLs)
    ToolRegistry.register("urls_to_json", ActionURLsToJSON)
    ToolRegistry.register("create_skill", create_skill)
    ToolRegistry.register("get_skill", get_skill)
    ToolRegistry.register("update_skill", update_skill)
    ToolRegistry.register("delete_skill", delete_skill)
    ToolRegistry.register("list_skills", list_skills)
    ToolRegistry.register("list_system_skills", list_system_skills)
    ToolRegistry.register("get_system_skill", get_system_skill)
    ToolRegistry.register("database_tool", ActionDBSqlQueryRunner)
    ToolRegistry.register("gemini_nano_banana_image_generation_tool", ActionImageGenerateGemini)
    ToolRegistry.register("gemini_veo_video_generation" , ActionVideoGenerateGeminiVeo)
    ToolRegistry.register("gemini_omni_video_generation", ActionVideoGenerateGeminiOmni)
    ToolRegistry.register("grok_video_generation" , ActionVideoGenerateGrok)
    # Action Tools
    ToolRegistry.register("get_action_types", ActionTools.get_action_types)
    ToolRegistry.register("get_action_data_types", ActionTools.get_action_data_types)
    ToolRegistry.register("get_tools", ActionTools.get_tools)
    ToolRegistry.register("get_database_connection_names", ActionTools.get_database_connection_names)
    ToolRegistry.register("get_mcp_server_names", ActionTools.get_mcp_server_names)
    ToolRegistry.register("enable_mcp", ActionTools.enable_mcp)
    ToolRegistry.register("get_connected_apps", ActionTools.get_connected_apps)
    # ToolRegistry.register("get_connected_app_details", ActionTools.get_connected_app_details)
    ToolRegistry.register("create_action", ActionTools.create_action)
    ToolRegistry.register("get_action", ActionTools.get_action)
    ToolRegistry.register("find_action", ActionTools.find_action)
    ToolRegistry.register("update_action", ActionTools.update_action)
    ToolRegistry.register("run_action", ActionTools.run_action)
    ToolRegistry.register("get_action_run", ActionRunTools.get_action_run)
    ToolRegistry.register("get_action_run_status", ActionRunTools.get_action_run_status)
    ToolRegistry.register("list_action_run", ActionRunTools.list_action_run)

    # Action Graph Tools
    ToolRegistry.register("create_action_graph", ActionGraphTools.create_action_graph)
    ToolRegistry.register("get_action_graph", ActionGraphTools.get_action_graph)
    ToolRegistry.register("find_action_graph", ActionGraphTools.find_action_graph)
    ToolRegistry.register("update_action_graph", ActionGraphTools.update_action_graph)
    ToolRegistry.register("run_action_graph", ActionGraphTools.run_action_graph)
    ToolRegistry.register("get_action_graph_run", ActionGraphRunTools.get_action_graph_run)
    ToolRegistry.register(
        "get_action_graph_run_status", ActionGraphRunTools.get_action_graph_run_status
    )
    ToolRegistry.register("list_action_graph_run", ActionGraphRunTools.list_action_graph_run)

    # Scheduler Tools
    ToolRegistry.register("create_schedule", SchedulerTools.create_schedule)
    ToolRegistry.register("get_schedule", SchedulerTools.get_schedule)
    ToolRegistry.register("update_schedule", SchedulerTools.update_schedule)
    ToolRegistry.register("find_schedule", SchedulerTools.find_schedule)
    ToolRegistry.register("delete_schedule", SchedulerTools.delete_schedule)

    # Webhook Tools
    ToolRegistry.register("create_webhook", WebhookTools.create_webhook)
    ToolRegistry.register("get_webhook", WebhookTools.get_webhook)
    ToolRegistry.register("update_webhook", WebhookTools.update_webhook)
    ToolRegistry.register("find_webhook", WebhookTools.find_webhook)
    ToolRegistry.register("delete_webhook", WebhookTools.delete_webhook)

    # Agent chat
    ToolRegistry.register("delegate", AgentsChat.delegate)
    ToolRegistry.register("csv_merge", csv_merge)
    ToolRegistry.register("csv_merge_1", ActionCSVMerge)
    ToolRegistry.register("merge_pdf", merge_pdf)
    ToolRegistry.register("google_search_main", ActionMultimodalGoogleSearchMain)
    # Ingestion/RAG Tools
    ToolRegistry.register("knowledge_base_query", ActionGraphRAGQuery)
    ToolRegistry.register("sandbox_file_query", ActionMultimodalSandboxQuery)
    ToolRegistry.register("openai_vector_store_tool", openai_vector_store_tool)
    ToolRegistry.register("openrouter_inspect", openrouter_inspect)
    # playwright scrap tool
    ToolRegistry.register("read_web_url" , ActionPlaywrightScrape)
    ToolRegistry.register("playwright_login", ActionPlaywrightLogin)
    # Terminal command execution tool
    ToolRegistry.register("run_terminal_command", run_terminal_command)
    # Read files from user's local machine
    ToolRegistry.register("read_files_from_user_system", read_files_from_user_system)
    # File system / editing tools
    ToolRegistry.register("file_system_tool", file_system_tool)
    # Interactive PTY session tools
    ToolRegistry.register("start_pty_session", start_pty_session)
    ToolRegistry.register("send_pty_input", send_pty_input)
    ToolRegistry.register("read_pty_output", read_pty_output)
    ToolRegistry.register("close_pty_session", close_pty_session)
    # Daytona remote code interpreter (single multi-operation tool)
    ToolRegistry.register("daytona_sandbox", ActionText2TextDaytona)
    # Autobots API tool
    ToolRegistry.register("autobots_api", ActionAutobots)
    
    # Action Team Tools
    ToolRegistry.register("create_action_team", ActionTeamTools.create_action_team)
    ToolRegistry.register("update_action_team", ActionTeamTools.update_action_team)
    ToolRegistry.register("list_action_teams", ActionTeamTools.list_action_teams)
    ToolRegistry.register("get_action_team", ActionTeamTools.get_action_team)
    ToolRegistry.register("run_action_team", ActionTeamTools.run_action_team)
    ToolRegistry.register("delete_action_team", ActionTeamTools.delete_action_team)
    ToolRegistry.register("get_action_team_run_status", ActionTeamTools.get_action_team_run_status)
    ToolRegistry.register("get_action_team_run_result", ActionTeamTools.get_action_team_run_result)