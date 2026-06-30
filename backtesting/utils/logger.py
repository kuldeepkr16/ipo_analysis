import logging
from rich.logging import RichHandler

_configured = False


def get_logger(name: str, level: str = "INFO") -> logging.Logger:
    global _configured
    if not _configured:
        logging.basicConfig(
            level=level,
            format="%(message)s",
            datefmt="[%X]",
            handlers=[RichHandler(rich_tracebacks=True, markup=True)],
        )
        _configured = True
    return logging.getLogger(name)
