from .calculator import calculator
from .email_client import email_draft_reply, email_find, email_read
from .file_parser import parse_file
from .google_places import local_business_search
from .http_client import http_get
from .web_search import web_search

REGISTRY: dict = {
    "web_search": web_search,
    "parse_file": parse_file,
    "http_get": http_get,
    "calculator": calculator,
    "email_find": email_find,
    "email_read": email_read,
    "email_draft_reply": email_draft_reply,
    "local_business_search": local_business_search,
}

__all__ = [
    "web_search",
    "parse_file",
    "http_get",
    "calculator",
    "email_find",
    "email_read",
    "email_draft_reply",
    "local_business_search",
    "REGISTRY",
]
