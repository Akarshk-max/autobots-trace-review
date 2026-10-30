from src.autobots.action.action_type.action_audio2text.action_audio2text_transcription_assemblyai import (
    ActionAudio2TextTranscriptionAssemblyai,
)
from src.autobots.action.action_type.action_audio2text.action_audio2text_transcription_cartesia import (
    ActionAudio2TextTranscriptionCartesia,
)
from src.autobots.action.action_type.action_audio2text.action_audio2text_transcription_deepgram import (
    ActionAudio2TextTranscriptionDeepgram,
)
from src.autobots.action.action_type.action_audio2text.action_audio2text_transcription_openai import (
    ActionAudio2TextTranscriptionOpenai,
)
from src.autobots.action.action_type.action_audio2text.action_audio2text_translation_openai import (
    ActionAudio2TextTranslationOpenai,
)

# from src.autobots.action.action_type.action_img2img.action_img2img_bulkedit_claid import \
# ActionImg2BulkEditClaid
# from src.autobots.action.action_type.action_img2img.action_img2img_photoshoot_claid import \
# ActionImg2ImgPhotoshootClaid
from src.autobots.action.action_type.action_llm.action_llm import ActionLlm
from src.autobots.action.action_type.action_llm.action_llm_objectives.action_llm_objectives_openai import (
    ActionLLMObjectivesOpenai,
)
from src.autobots.action.action_type.action_mock.action_mock import MockAction
from src.autobots.action.action_type.action_multicloud_router.action_multicloud_router import (
    ActionMulticloudRouter,
)
from src.autobots.action.action_type.action_multicloud_router.action_multicloud_router_vector_search import (
    ActionMulticloudRouterSearch,
)
from src.autobots.action.action_type.action_multimodal.action_multimodal_assistant_openai.action_multimodal_assistant_openai import (
    ActionMultimodalAssistantOpenai,
)

# from src.autobots.action.action_type.action_multimodal.action_multimodal_assistant_openai.action_multimodal_assistant_openai_stream import (
#     ActionMultimodalAssistantOpenaiStream,
# )
from src.autobots.action.action_type.action_multimodal.action_multimodal_google_search.action_multimodal_exa import (
    ActionMultimodalExa,
)
from src.autobots.action.action_type.action_multimodal.action_multimodal_google_search.action_multimodal_google_search_cse import (
    ActionMultimodalGoogleSearchMain,
)
from src.autobots.action.action_type.action_multimodal.action_multimodal_google_search.action_multimodal_serp_google_search import (
    ActionMultimodalSerpGoogleSearch,
)
from src.autobots.action.action_type.action_multimodal.action_multimodal_graph_rag_query import (
    ActionGraphRAGQuery,
)
from src.autobots.action.action_type.action_multimodal.action_multimodal_sandbox_query import (
    ActionMultimodalSandboxQuery,
)
from src.autobots.action.action_type.action_skills.action_skills import ActionSkills
from src.autobots.action.action_type.action_text2audio.action_text2audio_speech_cartesia import (
    ActionText2AudioCartesia,
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
from src.autobots.action.action_type.action_text2img.action_grok_text2_img import (
    ActionImageGenerateGrok,
)
from src.autobots.action.action_type.action_text2img.action_text2img_dalle_openai_v2 import (
    ActionGenImageDalleOpenAiV2,
)
from src.autobots.action.action_type.action_text2img.action_text2img_flux import ActionGenImageFlux

# from src.autobots.action.action_type.action_text2img.action_text2img_imagine_midjourney import \
#     ActionText2ImgMidjourney
# from src.autobots.action.action_type.action_text2img.action_text2img_midjourney_button import \
#     ActionText2ImgMidjourneyButton
from src.autobots.action.action_type.action_text2img.action_text2img_search_image import (
    ActionText2ImgSearchImage,
)
from src.autobots.action.action_type.action_text2text.action_api import ActionAPI
from src.autobots.action.action_type.action_text2text.action_autobots import ActionAutobots
from src.autobots.action.action_type.action_text2text.action_convert_file_to_pdf_or_txt import (
    ActionText2TextConvertToPDForText,
)
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
from src.autobots.action.action_type.action_text2text.action_kiwi_scout import ActionKiwiScout
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
from src.autobots.action.action_type.action_text2text.action_text2text_io_mapper.action_text2text_io_mapper import (
    ActionText2TextIOMapper,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_azure_openai import (
    ActionText2TextLlmChatAzureOpenAI,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude import (
    ActionText2TextLlmChatclaude,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude_vertex import (
    ActionText2TextLlmChatClaudeVertex,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_deepseek import (
    ActionText2TextLlmChatDeepSeek,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_gemini import (
    ActionText2TextLlmChatGemini,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_gemini_vertex import (
    ActionText2TextLlmChatGeminiVertex,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_grok import (
    ActionText2TextLlmChatGrok,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    ActionText2TextLlmChatOpenai,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openrouter import (
    ActionText2TextLlmChatOpenRouter,
)

# from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2_stream import (
# ActionText2TextLlmChatOpenaiStream,
# )
# from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_with_vector_search_openai import (
#     ActionText2TextLlmChatWithVectorSearchOpenai,
# )
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_perplexity_sonar import (
    ActionText2TextPerplexitySonar,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_responses_azure_openai import (
    ActionText2TextLlmResponsesAzureOpenAI,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_responses_openai import (
    ActionText2TextLlmResponsesOpenai,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_responses_openai_v2 import (
    ActionText2TextLlmResponsesOpenaiV2,
)

# from src.autobots.action.action_type.action_text2text.action_text2text_llm_responses_openai_stream import (
# ActionText2TextLlmResponsesOpenaiStream,
# )
from src.autobots.action.action_type.action_text2text.action_text2text_pdf_parser import (
    ActionPDFParser,
)
from src.autobots.action.action_type.action_text2text.action_text2text_perplexity_search import (
    ActionText2TextPerplexitySearch,
)
from src.autobots.action.action_type.action_text2text.action_text2text_read_api_file import (
    ActionText2textReadApiFile,
)
from src.autobots.action.action_type.action_text2text.action_text2text_read_urls import (
    ActionText2TextReadUrl,
)

# from src.autobots.action.action_type.action_text2text.action_text2text_search_map import (
#     ActionText2TextSearchMaps,
# )
from src.autobots.action.action_type.action_text2text.action_text2text_search_web import (
    ActionText2TextSearchWeb,
)
from src.autobots.action.action_type.action_text2text.action_text2text_user_input import (
    ActionText2textUserInput,
)
from src.autobots.action.action_type.action_text2text.action_urls_to_json import ActionURLsToJSON
from src.autobots.action.action_type.action_text2text.text2text_llm_chat_grok_collection import (
    ActionText2TextLlmChatGrokCollection,
)
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
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action.action_type.action_video2video.action_video2video_opus import (
    ActionVideo2VideoOpus,
)
from src.autobots.llm.tools.action_csv_perquet import ActionCSVToParquet
from src.autobots.llm.tools.action_parquet_inspector import ActionParquetInspector
from src.autobots.llm.tools.action_tool_results_query_claude import ActionToolResultsQueryClaude

ACTION_MAP = {
    # LLMs
    ActionType.openai_responses: ActionText2TextLlmResponsesOpenai,
    ActionType.openai_responses_v2: ActionText2TextLlmResponsesOpenaiV2,
    ActionType.openai_chat: ActionText2TextLlmChatOpenai,
    ActionType.openai_assistant: ActionMultimodalAssistantOpenai,
    ActionType.gemini: ActionText2TextLlmChatGemini,
    ActionType.openrouter: ActionText2TextLlmChatOpenRouter,
    ActionType.claude: ActionText2TextLlmChatclaude,
    ActionType.grok: ActionText2TextLlmChatGrok,
    ActionType.deepseek: ActionText2TextLlmChatDeepSeek,
    ActionType.azure_chat: ActionText2TextLlmChatAzureOpenAI,
    ActionType.azure_responses: ActionText2TextLlmResponsesAzureOpenAI,
    # multicloud
    ActionType.multicloud_router: ActionMulticloudRouter,
    ActionType.multicloud_router_search: ActionMulticloudRouterSearch,
    # multimodal
    ActionType.multimodal_assistant_openai: ActionMultimodalAssistantOpenai,
    # ActionType.multimodal_assistant_openai_stream: ActionMultimodalAssistantOpenaiStream,
    ActionType.multimodal_sandbox_query: ActionMultimodalSandboxQuery,
    ActionType.multimodal_graph_rag_query: ActionGraphRAGQuery,
    ActionType.multimodal_google_search: ActionMultimodalGoogleSearchMain,
    ActionType.multimodal_serp_google_search: ActionMultimodalSerpGoogleSearch,
    ActionType.multimodal_exa_search: ActionMultimodalExa,
    # text2text
    ActionType.text2text_llm_chat_openai: ActionText2TextLlmChatOpenai,
    # ActionType.text2text_llm_chat_openai_stream: ActionText2TextLlmChatOpenaiStream,
    ActionType.text2text_llm_responses_openai: ActionText2TextLlmResponsesOpenai,
    # ActionType.text2text_llm_responses_openai_stream: ActionText2TextLlmResponsesOpenaiStream,
    ActionType.text2text_llm_chat_claude: ActionText2TextLlmChatclaude,
    ActionType.text2text_llm_chat_openrouter: ActionText2TextLlmChatOpenRouter,
    ActionType.text2text_llm_chat_gemini: ActionText2TextLlmChatGemini,
    ActionType.text2text_llm_chat_deepseek: ActionText2TextLlmChatDeepSeek,
    ActionType.text2text_perplexity_sonar: ActionText2TextPerplexitySonar,
    ActionType.text2text_perplexity_search: ActionText2TextPerplexitySearch,
    ActionType.text2text_llm_chat_claude_vertex: ActionText2TextLlmChatClaudeVertex,
    ActionType.text2text_llm_chat_gemini_vertex: ActionText2TextLlmChatGeminiVertex,
    # ActionType.text2text_llm_chat_with_vector_search_openai: ActionText2TextLlmChatWithVectorSearchOpenai,
    ActionType.multimodal_llm_chat_with_vector_search_openai: ActionMultiModalLlmChatWithVectorSearchOpenai,
    ActionType.llm_objectives_openai: ActionLLMObjectivesOpenai,
    ActionType.text2text_read_url: ActionText2TextReadUrl,
    ActionType.text2text_search_web: ActionText2TextSearchWeb,
    # ActionType.text2text_search_maps: ActionText2TextSearchMaps,
    ActionType.text2text_io_mapper: ActionText2TextIOMapper,
    ActionType.text2text_user_input: ActionText2textUserInput,
    ActionType.text2text_api_call: ActionText2textAPI,
    ActionType.text2text_read_api_file: ActionText2textReadApiFile,
    ActionType.text2text_sql_runner: ActionSqlRunner,
    ActionType.action_text2text_pdf_parser: ActionPDFParser,
    ActionType.action_kiwi_scout: ActionKiwiScout,
    ActionType.text2text_llm_chat_grok: ActionText2TextLlmChatGrok,
    ActionType.action_llm: ActionLlm,
    ActionType.text2text_llm_chat_grok_collections: ActionText2TextLlmChatGrokCollection,
    ActionType.text2text_convert_file_to_pdf_or_txt: ActionText2TextConvertToPDForText,
    ActionType.text2text_daytona: ActionText2TextDaytona,
    ActionType.daytona: ActionText2TextDaytona,
    ActionType.json_flatten: ActionJsonFlatten,
    ActionType.csv_merge: ActionCSVMerge,
    ActionType.extract_urls: ActionExtractURLs,
    ActionType.urls_to_json: ActionURLsToJSON,
    ActionType.file_uploader_openai: ActionFileUploaderOpenai,
    ActionType.file_uploader_gemini: ActionFileUploaderGemini,
    ActionType.file_uploader_claude: ActionFileUploaderClaude,
    # text2img
    ActionType.text2img_dalle_openai: ActionGenImageDalleOpenAiV2,
    ActionType.text2img_search_image: ActionText2ImgSearchImage,
    # ActionType.text2img_imagine_midjourney_ai: ActionText2ImgMidjourney,
    # ActionType.text2img_button_midjourney_ai: ActionText2ImgMidjourneyButton,
    ActionType.text2img_flux: ActionGenImageFlux,
    ActionType.text2img_gemini: ActionImageGenerateGemini,
    ActionType.text2img_grok: ActionImageGenerateGrok,
    # img2img
    # ActionType.image_mixer_stable_diffusion: ActionImageMixerStableDiffusion,
    # ActionType.img2img_edit_openai: ActionImg2ImgEditOpenai,
    # ActionType.img2img_variation_openai: ActionImg2ImgVariationOpenai,
    # ActionType.img2img_bulk_edit_claid: ActionImg2BulkEditClaid,
    # ActionType.img2img_photoshoot_claid: ActionImg2ImgPhotoshootClaid,
    # text2video
    # ActionType.text2video_stable_diffusion: ActionText2VideoStableDiffusion,
    ActionType.text2video_search_video: ActionText2VideoSearchVideo,
    # text2audio
    ActionType.text2audio_speech_openai: ActionText2AudioSpeechOpenai,
    ActionType.text2audio_speech_cartesia: ActionText2AudioCartesia,
    ActionType.text2audio_speech_deepgram: ActionText2AudioSpeechDeepgram,
    # audio2text
    ActionType.audio2text_transcription_openai: ActionAudio2TextTranscriptionOpenai,
    ActionType.audio2text_translation_openai: ActionAudio2TextTranslationOpenai,
    ActionType.audio2text_transcription_assemblyai: ActionAudio2TextTranscriptionAssemblyai,
    ActionType.audio2text_transcription_deepgram: ActionAudio2TextTranscriptionDeepgram,
    ActionType.audio2text_transcription_cartesia: ActionAudio2TextTranscriptionCartesia,
    # video2video
    ActionType.video2video_opus: ActionVideo2VideoOpus,
    ActionType.text2video_grok: ActionVideoGenerateGrok,
    # api
    ActionType.api: ActionAPI,
    # mock
    ActionType.mock_action: MockAction,
    # sql runner action
    ActionType.database: ActionDBSqlQueryRunner,
    ActionType.playwright_scrape: ActionPlaywrightScrape,
    ActionType.playwright_login: ActionPlaywrightLogin,
    ActionType.text2video_gemini_veo_video_generation : ActionVideoGenerateGeminiVeo,
    ActionType.text2video_gemini_omni_video_generation: ActionVideoGenerateGeminiOmni,
    ActionType.action_text2text_json_reader : ActionJsonReader,
    ActionType.action_text2text_csv_to_parquet : ActionCSVToParquet,
    ActionType.action_text2text_parquet_inspector : ActionParquetInspector,
    ActionType.action_skills: ActionSkills,
    ActionType.action_text2text_tool_results_query_claude: ActionToolResultsQueryClaude,
    ActionType.data_plane_v2: ActionDataPlaneV2,
    ActionType.autobots: ActionAutobots,
}
