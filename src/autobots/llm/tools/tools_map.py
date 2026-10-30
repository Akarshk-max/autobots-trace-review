TOOLS_MAP = {}


class ToolRegistry:
    @staticmethod
    def register(name, cls):
        TOOLS_MAP[name] = cls

    @staticmethod
    def get(name):
        return TOOLS_MAP[name]

    @staticmethod
    def list():
        tools = []
        for tool in TOOLS_MAP.keys():
            tools += [tool]
        return tools
