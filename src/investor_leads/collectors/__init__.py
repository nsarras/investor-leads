"""Collectors fetch public posts from one platform and return normalized Post records."""


class CollectorError(RuntimeError):
    """A platform call failed. The message is safe to show to the user."""


class CollectorAuthError(CollectorError):
    """The API key was rejected or the account cannot make this call."""
