import json
import base64
from urllib.parse import quote, unquote


def encode_channels_payload(channels: list[dict]) -> str:
    """
    Encodes a channel list to a URL-encoded Base64 string matching JS decodeURIComponent(atob(_payload)).
    Protects payload from scanning and corruption.
    """
    raw_json = json.dumps(channels, ensure_ascii=False)
    uri_encoded = quote(raw_json)
    return base64.b64encode(uri_encoded.encode("utf-8")).decode("utf-8")


def decode_channels_payload(payload_str: str) -> list[dict]:
    """
    Decodes a URL-encoded Base64 string back to a list of channel dictionaries.
    Inverse of encode_channels_payload.
    """
    if not payload_str or not isinstance(payload_str, str):
        return []
    try:
        decoded_b64 = base64.b64decode(payload_str.encode("utf-8")).decode("utf-8")
        raw_json = unquote(decoded_b64)
        data = json.loads(raw_json)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def is_valid_base64_payload(payload_str: str) -> bool:
    """Returns True if the payload string is a non-empty, valid encoded channels payload."""
    decoded = decode_channels_payload(payload_str)
    return bool(decoded)
