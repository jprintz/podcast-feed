"""Errors whose message is shown to the user as is (admin UI and MCP client)."""


class PodcastError(Exception):
    """Base for every error with a user-facing message."""


class ConfigError(PodcastError):
    """The environment is missing a setting or has an invalid one."""


class Invalid(PodcastError):
    """The caller asked for something that is not acceptable (a bad link, name or value)."""


class TooLarge(Invalid):
    """An upload is over the size limit (a kind of Invalid that the web app answers with 413)."""


class NotFound(PodcastError):
    """The feed or episode that was asked for does not exist."""


class EpisodeFailure(PodcastError):
    """An episode could not be fetched or converted; str() is the failure reason it shows."""
