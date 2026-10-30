import pytest
from fastapi import UploadFile

from src.autobots.conn.unstructured_io.unstructured_io import (
    PartitionParametersParams,
    get_unstructured_io,
)
from src.autobots.core.settings import SettingsProvider


@pytest.mark.asyncio
async def test_unstructured_io_whole_file_happy_path(ctx, user, db, set_test_settings):
    settings = SettingsProvider.sget()
    ui_client = get_unstructured_io(
        ctx, user, db, settings.UNSTRUCTURED_API_KEY, settings.UNSTRUCTURED_URL
    )

    file_name = "tests/resources/unstructured_io/Runkai Design Documentation for Google Ads.pdf"
    try:
        with open(file_name, mode="rb") as file:
            upload_file = UploadFile(filename=file_name, file=file)
            content = await upload_file.read()

            partition_parameters_params = PartitionParametersParams(
                combine_under_n_chars=0,
                strategy="hi_res",
                hi_res_model_name="yolox",
                pdf_infer_table_structure=True,
            )
            chunks = await ui_client.get_file_chunks(
                (file_name, content), partition_parameters_params
            )
            assert len(chunks) == 1
    except Exception as e:
        assert str(e) is None
