"""Private podcast feeds from links to audio and video files."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

try:
    __version__ = package_version("podcast-feed")
except PackageNotFoundError:  # a source checkout that is not installed
    __version__ = "0.0.0"
