import base64

from loguru import logger

from src.autobots.action.action.common_action_models import TextObj
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

async def base64_encoder(
    config: TextObj, ctx: Context, app_state: AppState, notify: Notify | None = None
) -> TextObj | Exception:
    """
    Description
    -----------
    Encodes a text string into base64 format and returns the encoded string.
    If an unexpected error occurs, the exception is returned.

    Input
    -----
    config : dict
        A dictionary of arguments supplied by the caller (typically an LLM).
        Expected structure:
            {
                "text": "some string to encode"
            }

        text : str
            The plain-text string that will be converted into a base64-encoded
            representation.

    Output
    ------
    TextObj
        An object containing the base64-encoded string in its `text` field.

        In case of an unexpected error, the exception object is returned.
    """
    try:
        if isinstance(config, dict):
            config = TextObj.model_validate(config)
        encoded = base64.b64encode(bytes(config.text, "utf-8"))
        encoded_str = bytes.decode(encoded, "utf-8")
        return TextObj(text=encoded_str)
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in base64_encoder: {str(e)}")
        return e


async def base64_decoder(
    config: TextObj, ctx: Context, app_state: AppState, notify: Notify | None = None
) -> TextObj | Exception:
    """
    Description
    -----------
    Decodes a base64-encoded string and returns the decoded plain-text string.
    If an unexpected error occurs, the exception is returned.

    Input
    -----
    config : dict
        A dictionary of arguments supplied by the caller (typically an LLM).
        Expected structure:
            {
                "text": "<base64-encoded string>"
            }

        text : str
            A base64-encoded string that will be converted back into its original
            plain-text representation.

    Output
    ------
    TextObj
        An object containing the decoded plain-text string in its `text` field.

        In case of an unexpected error, the exception object is returned.
    """
    try:
        if isinstance(config, dict):
            config = TextObj.model_validate(config)
        decoded = base64.b64decode(config.text)
        decoded_str = bytes.decode(decoded, "utf-8")
        return TextObj(text=decoded_str)
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in base64_decoder: {str(e)}")
        return e
