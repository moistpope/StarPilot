"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import socket
import struct

import pytest

from openpilot.starpilot.system.webhud.mdns import build_a_response, decode_name, encode_name, parse_query


def query(name: str, qtype: int = 1, qclass: int = 1, qid: int = 0) -> bytes:
  return struct.pack("!HHHHHH", qid, 0, 1, 0, 0, 0) + encode_name(name) + struct.pack("!HH", qtype, qclass)


class TestMdns:
  def test_parse_query(self):
    assert parse_query(query("sunnypilot.local", qclass=0x8001, qid=7)) == (7, [("sunnypilot.local", 1, 0x8001)])

  def test_responses_and_garbage_ignored(self):
    assert parse_query(build_a_response("sunnypilot.local", "10.0.0.2")) is None
    assert parse_query(b"\x00\x01") is None
    assert parse_query(struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + b"\x05ab") is None

  def test_compressed_names(self):
    # "foo.local" at offset 12, then a pointer back to it
    data = b"\x00" * 12 + encode_name("foo.local") + b"\xc0\x0c"
    name, end = decode_name(data, 12)
    assert name == "foo.local" and end == 12 + len(encode_name("foo.local"))
    name, end = decode_name(data, end)
    assert name == "foo.local" and end == len(data)

  def test_pointer_loop_is_bounded(self):
    data = b"\x00" * 12 + b"\xc0\x0c"
    with pytest.raises(ValueError):
      decode_name(data, 12)

  def test_a_response(self):
    pkt = build_a_response("sunnypilot.local", "192.168.4.7")
    qid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", pkt[:12])
    assert (qid, flags, qd, an) == (0, 0x8400, 0, 1)
    name, off = decode_name(pkt, 12)
    rtype, rclass, ttl, rdlen = struct.unpack("!HHIH", pkt[off:off + 10])
    assert name == "sunnypilot.local" and rtype == 1 and rclass == 0x8001 and rdlen == 4
    assert socket.inet_ntoa(pkt[off + 10:off + 14]) == "192.168.4.7"

  def test_legacy_unicast_response_echoes_question(self):
    pkt = build_a_response("sunnypilot.local", "10.1.2.3", qid=0x1234, question=True, cache_flush=False)
    qid, flags, qd, an = struct.unpack("!HHHH", pkt[:8])
    assert (qid, qd, an) == (0x1234, 1, 1)
    name, off = decode_name(pkt, 12)
    assert name == "sunnypilot.local"
    _, off = decode_name(pkt, off + 4)
    assert struct.unpack("!HH", pkt[off:off + 4]) == (1, 1)   # class without the cache-flush bit
