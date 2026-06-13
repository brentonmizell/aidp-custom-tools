"""
Binary Jupyter message encode/decode for AIDP Spark notebook WebSocket protocol.

AIDP uses a custom binary format (not standard ZMQ wire protocol):
- 8-byte offset count (uint64 LE) = 6
- 6 x 8-byte offset table (uint64 LE each, absolute positions)
- 6 parts: channel, header, parent_header, metadata, content, extra
"""

import json
import struct
import uuid
from datetime import datetime, timezone


JUPYTER_VERSION = "5.3"
USERNAME = "spark_executor"


def encode_binary_message(channel, header, parent_header, metadata, content):
    """Encode a Jupyter message into AIDP binary format.

    Parts: channel(str), header(dict), parent_header(dict),
           metadata(dict), content(dict), extra(empty)
    """
    parts = [
        channel.encode("utf-8"),
        json.dumps(header).encode("utf-8"),
        json.dumps(parent_header).encode("utf-8"),
        json.dumps(metadata).encode("utf-8"),
        json.dumps(content).encode("utf-8"),
        b"",  # extra part expected by AIDP
    ]

    num_parts = len(parts)
    # Header: 8 bytes for count + 8 bytes per offset
    header_size = 8 + num_parts * 8

    # Calculate absolute offsets
    offsets = []
    pos = header_size
    for part in parts:
        offsets.append(pos)
        pos += len(part)

    # Build buffer
    buf = struct.pack("<Q", num_parts)
    for offset in offsets:
        buf += struct.pack("<Q", offset)
    for part in parts:
        buf += part

    return buf


def decode_binary_message(data):
    """Decode an AIDP binary Jupyter message.

    Returns dict with keys: channel, header, parent_header, metadata, content.
    Falls back to JSON parse if data starts with '{'.
    """
    if isinstance(data, str):
        return json.loads(data)

    if not data:
        return None

    # Check if it's plain JSON (starts with '{')
    if data[0:1] == b"{":
        return json.loads(data.decode("utf-8"))

    if len(data) < 8:
        return None

    offset_count = struct.unpack("<Q", data[0:8])[0]

    # Sanity check
    if offset_count == 0 or offset_count > 100:
        return _try_delimiter_parse(data)

    # Read offset table
    offsets = []
    for i in range(offset_count):
        start = 8 + i * 8
        if start + 8 > len(data):
            return None
        offsets.append(struct.unpack("<Q", data[start : start + 8])[0])

    # Add end sentinel
    offsets.append(len(data))

    # Extract parts
    parts = []
    for i in range(len(offsets) - 1):
        part_data = data[offsets[i] : offsets[i + 1]]
        parts.append(part_data)

    result = {}
    if len(parts) > 0:
        result["channel"] = parts[0].decode("utf-8")
    if len(parts) > 1:
        result["header"] = json.loads(parts[1].decode("utf-8"))
    if len(parts) > 2:
        result["parent_header"] = json.loads(parts[2].decode("utf-8"))
    if len(parts) > 3:
        result["metadata"] = json.loads(parts[3].decode("utf-8"))
    if len(parts) > 4:
        result["content"] = json.loads(parts[4].decode("utf-8"))

    return result


def _try_delimiter_parse(data):
    """Fallback: try IDS|MSG delimiter format or line-based JSON."""
    text = data.decode("utf-8", errors="replace")

    # Try IDS|MSG delimiter
    delimiter = "<IDS|MSG>"
    if delimiter in text:
        idx = text.index(delimiter)
        json_parts = text[idx + len(delimiter) :].strip().split("\n")
        if len(json_parts) >= 4:
            return {
                "channel": "shell",
                "header": json.loads(json_parts[0]),
                "parent_header": json.loads(json_parts[1]),
                "metadata": json.loads(json_parts[2]),
                "content": json.loads(json_parts[3]),
            }

    # Try plain JSON lines
    for line in text.strip().split("\n"):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    return None


def make_execute_request(session_id, code):
    """Build an execute_request message and return (msg_id, binary_data)."""
    msg_id = str(uuid.uuid4())

    header = {
        "msg_id": msg_id,
        "username": USERNAME,
        "session": session_id,
        "date": datetime.now(timezone.utc).isoformat(),
        "msg_type": "execute_request",
        "version": JUPYTER_VERSION,
    }

    content = {
        "code": code,
        "silent": False,
        "store_history": True,
        "user_expressions": {},
        "allow_stdin": False,
        "stop_on_error": True,
    }

    data = encode_binary_message("shell", header, {}, {}, content)
    return msg_id, data


def make_kernel_info_request(session_id):
    """Build a kernel_info_request message and return (msg_id, binary_data)."""
    msg_id = str(uuid.uuid4())

    header = {
        "msg_id": msg_id,
        "username": USERNAME,
        "session": session_id,
        "date": datetime.now(timezone.utc).isoformat(),
        "msg_type": "kernel_info_request",
        "version": JUPYTER_VERSION,
    }

    data = encode_binary_message("shell", header, {}, {}, {})
    return msg_id, data
