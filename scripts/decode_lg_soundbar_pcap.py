#!/usr/bin/env python3
"""Decrypt LG soundbar TCP/9741 frames captured in a PCAP file."""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from scapy.all import IP, TCP, PcapReader, Raw

KEY = b"T^&*J%^7tr~4^%^&I(o%^!jIJ__+a0 k"
IV = b"'%^Ur7gy$~t+f)%@"
HEADER = 0x10
MAX_FRAME_SIZE = 1024 * 1024
KST = ZoneInfo("Asia/Seoul")


@dataclass
class DirectionBuffer:
    """Minimal in-order TCP reassembly for one connection direction."""

    data: bytearray = field(default_factory=bytearray)
    next_seq: int | None = None
    frame_time: float | None = None
    gaps: int = 0

    def add(self, seq: int, payload: bytes, timestamp: float) -> None:
        if self.next_seq is None:
            self.next_seq = seq
        if seq < self.next_seq:
            overlap = self.next_seq - seq
            if overlap >= len(payload):
                return
            payload = payload[overlap:]
            seq = self.next_seq
        elif seq > self.next_seq:
            self.data.clear()
            self.frame_time = None
            self.gaps += 1
            self.next_seq = seq

        if not self.data:
            self.frame_time = timestamp
        self.data.extend(payload)
        self.next_seq = seq + len(payload)


def decrypt_frame(ciphertext: bytes) -> dict[str, Any]:
    decryptor = Cipher(algorithms.AES(KEY), modes.CBC(IV)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    pad_len = padded[-1]
    if pad_len < 1 or pad_len > 16 or padded[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("invalid PKCS#7 padding")
    return json.loads(padded[:-pad_len].decode("utf-8"))


def pop_frames(buffer: DirectionBuffer) -> list[tuple[float | None, dict[str, Any]]]:
    frames: list[tuple[float | None, dict[str, Any]]] = []
    while buffer.data:
        if buffer.data[0] != HEADER:
            try:
                next_header = buffer.data.index(HEADER, 1)
            except ValueError:
                buffer.data.clear()
                buffer.frame_time = None
                break
            del buffer.data[:next_header]
        if len(buffer.data) < 5:
            break

        length = int.from_bytes(buffer.data[1:5], "big")
        if length <= 0 or length % 16 or length > MAX_FRAME_SIZE:
            del buffer.data[0]
            continue
        total = 5 + length
        if len(buffer.data) < total:
            break

        ciphertext = bytes(buffer.data[5:total])
        del buffer.data[:total]
        try:
            message = decrypt_frame(ciphertext)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as err:
            message = {"_decode_error": str(err), "_ciphertext_length": length}
        frames.append((buffer.frame_time, message))
        buffer.frame_time = None
    return frames


def endpoint_label(ip: str, target_ip: str) -> str:
    if ip == target_ip:
        return "SOUNDBAR"
    return "APP/CLIENT"


def ipv4_address(value: str) -> str:
    """Validate and normalize an IPv4 address supplied at runtime."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError as err:
        raise argparse.ArgumentTypeError(str(err)) from err
    if address.version != 4:
        raise argparse.ArgumentTypeError("an IPv4 address is required")
    return str(address)


def decode(path: Path, target_ip: str, json_lines: bool) -> int:
    buffers: dict[tuple[str, int, str, int], DirectionBuffer] = {}
    decoded = 0
    gaps = 0

    with PcapReader(str(path)) as packets:
        for packet in packets:
            if IP not in packet or TCP not in packet:
                continue
            ip = packet[IP]
            tcp = packet[TCP]
            if target_ip not in {ip.src, ip.dst} or 9741 not in {tcp.sport, tcp.dport}:
                continue

            key = (ip.src, int(tcp.sport), ip.dst, int(tcp.dport))
            if int(tcp.flags) & 0x02:
                buffers.pop(key, None)
            if Raw not in packet:
                continue

            payload = bytes(packet[Raw].load)
            if not payload:
                continue
            stream = buffers.setdefault(key, DirectionBuffer())
            old_gaps = stream.gaps
            stream.add(int(tcp.seq), payload, float(packet.time))
            gaps += stream.gaps - old_gaps

            for frame_time, message in pop_frames(stream):
                decoded += 1
                timestamp = datetime.fromtimestamp(
                    frame_time if frame_time is not None else float(packet.time), KST
                ).isoformat(timespec="milliseconds")
                record = {
                    "time": timestamp,
                    "direction": f"{endpoint_label(ip.src, target_ip)} -> {endpoint_label(ip.dst, target_ip)}",
                    "source": f"{ip.src}:{tcp.sport}",
                    "destination": f"{ip.dst}:{tcp.dport}",
                    "message": message,
                }
                if json_lines:
                    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
                else:
                    command = str(message.get("cmd", "?"))
                    kind = str(message.get("msg", "?"))
                    data = message.get("data", {})
                    result = message.get("result")
                    suffix = f" result={result}" if result is not None else ""
                    print(
                        f"{timestamp}  {record['direction']:<24} "
                        f"{command:<10} {kind}{suffix}"
                    )
                    print(f"  {json.dumps(data, ensure_ascii=False, sort_keys=True)}")

    if not json_lines:
        print(f"\nDecoded frames: {decoded}; TCP gaps/resets: {gaps}", file=sys.stderr)
    return 0 if decoded else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pcap", type=Path)
    parser.add_argument("--target-ip", required=True, type=ipv4_address)
    parser.add_argument("--json-lines", action="store_true")
    args = parser.parse_args()
    if not args.pcap.is_file():
        parser.error(f"PCAP does not exist: {args.pcap}")
    return decode(args.pcap, args.target_ip, args.json_lines)


if __name__ == "__main__":
    raise SystemExit(main())
