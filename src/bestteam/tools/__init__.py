from .calculator import calculator
from .file_parser import parse_file
from .http_client import http_get
from .web_search import web_search

REGISTRY: dict = {
    "web_search": web_search,
    "parse_file": parse_file,
    "http_get": http_get,
    "calculator": calculator,
}

__all__ = ["web_search", "parse_file", "http_get", "calculator", "REGISTRY"]
