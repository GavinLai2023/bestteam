class BestTeamError(Exception):
    """Base exception for all framework errors."""


class ConfigurationError(BestTeamError):
    """Raised when an Agent, Team, or Workflow is misconfigured."""


class EngineError(BestTeamError):
    """Raised when the underlying orchestration engine fails during execution."""
