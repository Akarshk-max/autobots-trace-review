import asyncio
from collections.abc import AsyncGenerator


class IntTimesStrGen:
    @staticmethod
    async def repeat_string_generator() -> AsyncGenerator[str | None, int | None]:
        string_to_repeat = "Hello"
        # count = None
        result = string_to_repeat
        while True:
            count = yield result
            print(f"count: {count}")
            result = await IntTimesStrGen.repeat_string(string_to_repeat, count)

    @staticmethod
    async def repeat_string(string_to_repeat: str, count: int) -> str:
        return string_to_repeat * count


async def main() -> None:
    gen = IntTimesStrGen.repeat_string_generator()
    print(await gen.asend(None))  # Start the generator and get the initial string
    print(await gen.asend(3))  # Send 3 and get the string repeated 3 times
    print(await gen.asend(2))  # Send 2 and get the string repeated 2 times
    print(await gen.asend(5))  # Send 5 and get the string repeated 5 times


asyncio.run(main())
"""
Output:
-------
Hello
count: 3
HelloHelloHello
count: 2
HelloHello
count: 5
HelloHelloHelloHelloHello
"""
