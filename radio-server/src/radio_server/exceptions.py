"""
Custom exceptions for the radio server.
Provides a clear exception hierarchy for better error handling and debugging.
"""


class RadioServerError(Exception):
    """Base exception for all radio server errors."""

    pass


# Audio Processing Errors
class AudioProcessingError(RadioServerError):
    """Base exception for audio processing errors."""

    pass


class FFmpegError(AudioProcessingError):
    """Exception raised when FFmpeg command fails."""

    pass


class FFmpegTimeoutError(FFmpegError):
    """Exception raised when FFmpeg command times out."""

    pass
