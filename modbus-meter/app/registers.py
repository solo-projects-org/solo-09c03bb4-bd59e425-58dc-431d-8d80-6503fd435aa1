"""寄存器解码工具（试采与对账共用）。"""

from __future__ import annotations


def decode_unsigned(words: list[int]) -> int:
    """无符号拼接；双字高字在前（big-endian / 高字在低地址）。"""
    value = 0
    for word in words:
        value = (value << 16) | (word & 0xFFFF)
    return value
