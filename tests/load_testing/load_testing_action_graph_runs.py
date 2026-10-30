import asyncio
import time

import httpx
import pytest


async def run_graph(identifier: str, auth_token: str) -> str:
    print(f"Running graph {identifier}")
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {auth_token}"}) as session:
        async with session.post(
            "https://dev.api.myautobots.com/v1/action_graphs/67ae80b9a29c11141c391091/async_run?is_rerun_graph=false",
            json={
                "input": {
                    "text": "Extract Contract details. List all data points. And de-duplicate it",
                    "urls": [
                        "https://app-myautobots-public-dev.s3.us-west-2.amazonaws.com/files/e29f3392-6124-40f5-8e24-fd6b5ebff875/A_Flow_Mobz_-_Thrill_Over_Fear-signed_1_1_1.pdf",
                        "https://app-myautobots-public-dev.s3.us-west-2.amazonaws.com/files/e29f3392-6124-40f5-8e24-fd6b5ebff875/TOMA_TOMA_-_Agreement_1_2.pdf",
                        "https://app-myautobots-public-dev.s3.us-west-2.amazonaws.com/files/e29f3392-6124-40f5-8e24-fd6b5ebff875/Lowly_Excl_Lowly_Excl_32Stitches_-_Requiem_for_a_Dream-__Lowly_Palace_Agreement_COVER_1.pdf",
                        "https://app-myautobots-public-dev.s3.us-west-2.amazonaws.com/files/e29f3392-6124-40f5-8e24-fd6b5ebff875/NightBass_NightBass_Exclusive_Agreements_Night_Bass_Ep_Recording_Agreement_-_Drinks_On_Me_-_Teetotal_Ep_3.pdf",
                        "https://app-myautobots-public-dev.s3.us-west-2.amazonaws.com/files/e29f3392-6124-40f5-8e24-fd6b5ebff875/Exclusive_Agreements_Type_1-Exclusive_Recording_Agreement_Blossom_-_Big_Shot_EP_-_DYTAM_1.pdf",
                    ],
                }
            },
        ) as response:
            print(f"Ran graph {identifier}: status {response.status}")
            return await response.json()


@pytest.mark.asyncio
@pytest.mark.skip("Skip load testing")
async def test_load_testing_action_graph_runs(set_test_settings):
    auth_token = ""
    iterations = 5

    start_time = time.time()
    print(f"Starting load testing: {start_time}")
    tasks = [run_graph(identifier=str(i + 1), auth_token=auth_token) for i in range(iterations)]
    responses = await asyncio.gather(*tasks)
    end_time = time.time()
    print(f"Stopping load testing: {end_time}")
    print(f"Time taken: {end_time - start_time}")

    return responses
