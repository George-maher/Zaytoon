class ZaytoonError(Exception):
    pass


class ConfigurationError(ZaytoonError):
    pass


class AgentError(ZaytoonError):
    pass


class VoiceError(ZaytoonError):
    pass


class SchedulerError(ZaytoonError):
    pass


class RateLimitError(ZaytoonError):
    pass
