from loguru import logger
from pydantic import BaseModel, Field

from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.terminal.terminal_connection_manager import terminal_manager
from src.autobots.data_model.app_state import AppState

class ReadFilesConfig(BaseModel):
    paths: list[str] = Field(
        ...,
        description=(
            "List of absolute file or folder paths on the user's local machine. "
            "Files are uploaded as-is. Folders are zipped before upload. "
            "Returns S3 URLs for each successfully uploaded file."
        ),
    )
    timeout_sec: int = Field(300, ge=1, le=600)


class ReadFilesOutput(BaseModel):
    urls: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    success: bool = False


async def read_files_from_user_system(
    config: ReadFilesConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
) -> ReadFilesOutput | Exception:
    """
    Description
    -----------
    Reads files or folders from the user's local machine and uploads them to
    cloud storage. Returns publicly accessible S3 URLs for each file.

    Requires the user to have an active CLI agent connection. Folders are
    automatically zipped before upload.

    Input
    -----
    config : dict
        {
            "paths": ["/home/user/report.pdf", "/home/user/project/"],
            "timeout_sec": 300
        }

        paths : list[str]
            Absolute paths to files or folders on the user's machine.
            Files are uploaded directly. Folders are zipped into a .zip file
            before uploading.

        timeout_sec : int
            Maximum time in seconds to wait for the upload to complete.
            Must be between 1 and 600. Defaults to 300.

    Output
    ------
    ReadFilesOutput
        - urls: List of S3 URLs for successfully uploaded files.
        - errors: List of per-path error messages (e.g. "file not found").
        - success: True if at least one file was uploaded successfully.

    Examples
    --------
    Single file: {"paths": ["/home/user/report.pdf"]}
    Multiple files: {"paths": ["/home/user/a.txt", "/home/user/b.csv"]}
    Folder (zipped): {"paths": ["/home/user/project/"]}
    """
    try:
        try:
            agent_id = await terminal_manager.resolve_agent_for_consumer(ctx)
        except ValueError as e:
            return ReadFilesOutput(
                urls=[],
                errors=[str(e)],
                success=False,
            )

        if not agent_id:
            return ReadFilesOutput(
                urls=[],
                errors=[
                    "Error: No unpaired terminal CLI agent connected for this chat. "
                    "Please run the CLI agent on your machine first, or release an existing pairing."
                ],
                success=False,
            )

        result = await terminal_manager.send_read_files_to_agent(
            ctx=ctx,
            agent_id=agent_id,
            paths=config.paths,
            timeout_sec=config.timeout_sec,
        )

        if "error" in result:
            return ReadFilesOutput(
                urls=[],
                errors=[result["error"]],
                success=False,
            )

        if result.get("success") is False and result.get("message"):
            return ReadFilesOutput(
                urls=[],
                errors=[str(result.get("message", ""))],
                success=False,
            )

        urls = result.get("urls", [])
        errors = result.get("errors", [])

        return ReadFilesOutput(
            urls=urls,
            errors=errors,
            success=len(urls) > 0,
        )

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in read_files_from_user_system: {str(e)}")
        return e