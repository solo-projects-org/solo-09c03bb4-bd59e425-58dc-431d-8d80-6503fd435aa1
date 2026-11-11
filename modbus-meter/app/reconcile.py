"""对账引擎：逐行校验输入，逐段生成 CSV。

输入约定（总表与所有支路表相同）：

* 无表头，每行 3 列：``UTC 采样秒,高字,低字``；若首行是常见表头
  （如 ``timestamp,high,low``），会自动跳过；
* 采样秒为非负整数，且同一文件内严格递增；
* 高字、低字均为 ``0..65535`` 的 16 位无符号整数；
* 计数 = ``(高字 << 16) | 低字``，沿用试采接口的高字在前解码。

处理方式：

* 以所有表都有读数的时刻（交集）的相邻两个时刻划段；
* 每段差额 = 总表增量 - 各支路增量之和；
* 从段首到段尾逐表检查相邻读数，任一计数下降即把该表标为异常，
  该段只列异常表文件名、不给出差额；
* 输入逐行读入，报告逐段写入，内存占用与表数量同阶、与行数无关。
"""

from __future__ import annotations

import csv
import heapq
import io
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, TextIO

from fastapi import UploadFile

from .registers import decode_unsigned

REPORT_HEADER = [
    "segment_start_utc",
    "segment_end_utc",
    "difference",
    "abnormal_files",
]
MAX_REGISTER = 0xFFFF


class CsvInputError(ValueError):
    """输入 CSV 存在坏行或采样秒未严格递增。"""

    def __init__(self, filename: str, line_no: int, reason: str) -> None:
        self.filename = filename
        self.line_no = line_no
        self.reason = reason
        super().__init__(f"文件 {filename}，第 {line_no} 行：{reason}")


class NotReconcilable(ValueError):
    """共同采样时刻不足两个，无法形成任何对账段。"""

    def __init__(self, common_count: int) -> None:
        self.common_count = common_count
        super().__init__(
            "无法对账：所有表都有读数的共同采样时刻不足 2 个"
            f"（实际 {common_count} 个）"
        )


@dataclass(frozen=True)
class TableInput:
    """一份待对账的 CSV（第 0 份固定为总表）。"""

    filename: str
    upload: UploadFile


def display_name(upload: UploadFile, fallback: str) -> str:
    """取用于报错和异常列出的文件名（去掉客户端可能带上的路径）。"""
    raw = upload.filename or fallback
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or fallback


@contextmanager
def _text_stream(upload: UploadFile) -> Iterator[TextIO]:
    """从上传文件当前位置重开一个 UTF-8 文本流（兼容 BOM）。

    ``detach`` 只解除文本包装，不关闭 Starlette 持有的底层临时文件，
    以便随后再次从头读取。
    """
    upload.file.seek(0)
    wrapper = io.TextIOWrapper(
        upload.file, encoding="utf-8-sig", newline=""
    )
    try:
        yield wrapper
    finally:
        wrapper.detach()


def _parse_int(
    token: str, *, label: str, filename: str, line_no: int
) -> int:
    text = token.strip()
    if not text:
        raise CsvInputError(filename, line_no, f"{label}为空")
    if not re.fullmatch(r"[0-9]+", text):
        raise CsvInputError(
            filename, line_no, f"{label}不是十进制整数：{token!r}"
        )
    return int(text, 10)


def _is_header_row(row: list[str]) -> bool:
    """宽松识别一行常见表头；不匹配时仍按普通数据行校验。"""
    if len(row) != 3:
        return False
    normalized = [
        re.sub(r"[^0-9a-z一-鿿]+", "", cell.strip().lower())
        for cell in row
    ]
    second, high, low = normalized
    if not (second and high and low):
        return False
    return (
        any(key in second for key in ("time", "ts", "utc", "second", "秒"))
        and any(key in high for key in ("hi", "high", "高"))
        and any(key in low for key in ("lo", "low", "低"))
    )


def _validate_row(
    row: list[str], filename: str, line_no: int
) -> tuple[int, int, int]:
    if not row:
        raise CsvInputError(filename, line_no, "空行")
    if len(row) != 3:
        raise CsvInputError(
            filename,
            line_no,
            f"应为 3 列（UTC 采样秒,高字,低字），实际 {len(row)} 列",
        )
    second = _parse_int(
        row[0], label="UTC 采样秒", filename=filename, line_no=line_no
    )
    if second < 0:
        raise CsvInputError(filename, line_no, f"UTC 采样秒不能为负数：{second}")
    high = _parse_int(
        row[1], label="高字", filename=filename, line_no=line_no
    )
    low = _parse_int(
        row[2], label="低字", filename=filename, line_no=line_no
    )
    for label, word in (("高字", high), ("低字", low)):
        if not 0 <= word <= MAX_REGISTER:
            raise CsvInputError(
                filename,
                line_no,
                f"{label}必须是 0..{MAX_REGISTER} 的 16 位无符号整数：{word}",
            )
    return second, high, low


def validate_csv(upload: UploadFile, filename: str) -> None:
    """逐行校验一份 CSV；发现第一个错误即抛出，不保留任何行。"""
    previous_second: int | None = None
    try:
        with _text_stream(upload) as stream:
            reader = csv.reader(stream)
            try:
                for row in reader:
                    line_no = reader.line_num
                    if line_no == 1 and _is_header_row(row):
                        continue
                    second, _, _ = _validate_row(row, filename, line_no)
                    if previous_second is not None and second <= previous_second:
                        raise CsvInputError(
                            filename,
                            line_no,
                            "采样秒未严格递增："
                            f"上一行 {previous_second}，本行 {second}",
                        )
                    previous_second = second
            except csv.Error as exc:
                raise CsvInputError(
                    filename,
                    max(reader.line_num, 1),
                    f"CSV 格式错误：{exc}",
                ) from exc
    except UnicodeDecodeError as exc:
        raise CsvInputError(
            filename, 1, f"不是有效的 UTF-8 文本：{exc.reason}"
        ) from exc


def _iter_readings(upload: UploadFile, filename: str) -> Iterator[tuple[int, int]]:
    """逐行产出 ``(采样秒, 计数)``；调用前必须已通过 ``validate_csv``。"""
    with _text_stream(upload) as stream:
        reader = csv.reader(stream)
        for row in reader:
            line_no = reader.line_num
            if line_no == 1 and _is_header_row(row):
                continue
            second, high, low = _validate_row(row, filename, line_no)
            yield second, decode_unsigned([high, low])


def write_report(tables: list[TableInput], out: TextIO) -> int:
    """按时间做多路归并，逐段写入报告；返回共同采样时刻数。

    ``tables[0]`` 必须是总表，其余为支路表。只保留每份表最近一个读数
    和当前段的状态，因此内存占用不随输入行数增长。
    """
    n = len(tables)
    if n < 2:
        raise ValueError("对账至少需要 1 份总表和 1 份支路表")

    readers = [_iter_readings(t.upload, t.filename) for t in tables]
    heap: list[tuple[int, int, int]] = []
    for index, reader in enumerate(readers):
        try:
            second, value = next(reader)
        except StopIteration:
            continue
        heapq.heappush(heap, (second, index, value))

    last_value: list[int | None] = [None] * n
    segment_start: int | None = None
    segment_start_values: list[int | None] = [None] * n
    abnormal: set[int] = set()
    common_count = 0

    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(REPORT_HEADER)

    while heap:
        second = heap[0][0]
        present: set[int] = set()

        # 取出该时刻所有表的读数，并按文件时间顺序推入下一行。
        while heap and heap[0][0] == second:
            _, index, value = heapq.heappop(heap)
            present.add(index)

            # 段内相邻读数出现下降即记为异常表；段首不跨段比较。
            if (
                segment_start is not None
                and last_value[index] is not None
                and value < last_value[index]
            ):
                abnormal.add(index)
            last_value[index] = value

            try:
                next_second, next_value = next(readers[index])
            except StopIteration:
                continue
            heapq.heappush(heap, (next_second, index, next_value))

        if len(present) != n:
            continue

        common_count += 1
        if segment_start is None:
            # 第一个共同时刻只作为段首。
            segment_start = second
            segment_start_values = list(last_value)
            abnormal.clear()
            continue

        end = second
        if abnormal:
            names = ";".join(tables[i].filename for i in sorted(abnormal))
            writer.writerow([segment_start, end, "", names])
        else:
            main_delta = last_value[0] - segment_start_values[0]  # type: ignore[operator]
            branch_delta = sum(
                last_value[i] - segment_start_values[i]  # type: ignore[operator]
                for i in range(1, n)
            )
            writer.writerow([segment_start, end, main_delta - branch_delta, ""])

        segment_start = end
        segment_start_values = list(last_value)
        abnormal.clear()

    return common_count


def safe_unlink(path: str) -> None:
    """删除临时报告文件；文件已被清理时忽略。"""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
