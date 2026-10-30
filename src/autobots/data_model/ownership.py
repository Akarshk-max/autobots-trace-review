from src.autobots.integrations.app_types import ExtendedEnum


class Ownership(ExtendedEnum):
    user = "user"
    space = "space"
    system = "system"

    @classmethod
    def _missing_(cls, value):
        if value == "org":
            return cls.space
        return None


DEFAULT_OWNERSHIP = (Ownership.user, Ownership.space)
