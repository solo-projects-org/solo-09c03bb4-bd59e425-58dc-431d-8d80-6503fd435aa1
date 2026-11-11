"""对账 API 集成测试（内存上传，无需 Docker 或电表）。"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import httpx

from app.main import app

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def _file(name: str, content: bytes) -> tuple[str, bytes, str]:
    return (name, content, "text/csv")


def _sample(name: str) -> tuple[str, bytes, str]:
    return _file(name, (SAMPLES / name).read_bytes())


def _files(
    main: tuple[str, bytes, str], *branches: tuple[str, bytes, str]
) -> list[tuple[str, tuple[str, bytes, str]]]:
    """httpx 用重复的 branch_csvs 字段上传多份支路表。"""
    return [("main_csv", main)] + [("branch_csvs", b) for b in branches]


async def test_sample_staggered_sampling_and_midway_decrease() -> None:
    files = _files(
        _sample("main.csv"), _sample("branch_a.csv"), _sample("branch_b.csv")
    )
    async with await _client() as client:
        resp = await client.post("/reconcile", files=files)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "reconciliation.csv" in resp.headers["content-disposition"]

    rows = _rows(resp.text)
    assert rows[0] == [
        "segment_start_utc",
        "segment_end_utc",
        "difference",
        "abnormal_files",
    ]
    # 共同采样时刻为 5600/5660/5720；中间的错开读数不影响划段。
    assert rows[1] == ["1767225600", "1767225660", "20", ""]
    # branch_b 在段内错开采样处从 40200 降到 40100（段尾又回升）：
    # 只列异常表，不给差额。
    assert rows[2] == ["1767225660", "1767225720", "", "branch_b.csv"]
    assert len(rows) == 3


async def test_decrease_at_non_common_reading_is_detected() -> None:
    files = _files(
        _file("main.csv", b"1,0,1000\n3,0,1200\n"),
        _file("branch.csv", b"1,0,500\n2,0,400\n3,0,600\n"),
    )
    async with await _client() as client:
        resp = await client.post("/reconcile", files=files)
    assert resp.status_code == 200
    rows = _rows(resp.text)
    assert rows[1] == ["1", "3", "", "branch.csv"]


async def test_main_decrease_also_marks_segment_abnormal() -> None:
    files = _files(
        _file("main.csv", b"1,0,10\n2,0,5\n"),
        _file("branch.csv", b"1,0,1\n2,0,3\n"),
    )
    async with await _client() as client:
        resp = await client.post("/reconcile", files=files)
    assert resp.status_code == 200
    rows = _rows(resp.text)
    assert rows[1] == ["1", "2", "", "main.csv"]


async def test_bad_row_reports_filename_and_line_without_report() -> None:
    files = _files(
        _file("main.csv", b"1,0,1\n2,0,2,99\n"),
        _file("branch.csv", b"1,0,10\n2,0,11\n"),
    )
    async with await _client() as client:
        resp = await client.post("/reconcile", files=files)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "main.csv" in detail
    assert "第 2 行" in detail
    assert "3 列" in detail


async def test_out_of_range_register_reports_line() -> None:
    files = _files(
        _file("main.csv", b"1,0,1\n2,0,65536\n"),
        _file("branch.csv", b"1,0,10\n2,0,11\n"),
    )
    async with await _client() as client:
        resp = await client.post("/reconcile", files=files)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "main.csv" in detail and "第 2 行" in detail
    assert "65535" in detail


async def test_non_increasing_sample_second_reports_filename_and_line() -> None:
    files = _files(
        _file("main.csv", b"1,0,1\n1,0,2\n"),
        _file("branch.csv", b"1,0,10\n"),
    )
    async with await _client() as client:
        resp = await client.post("/reconcile", files=files)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "main.csv" in detail and "第 2 行" in detail
    assert "未严格递增" in detail


async def test_insufficient_common_timestamps_is_not_reconcilable() -> None:
    files = _files(
        _file("main.csv", b"1,0,1\n2,0,2\n"),
        _file("branch.csv", b"1,0,10\n"),
    )
    async with await _client() as client:
        resp = await client.post("/reconcile", files=files)
    assert resp.status_code == 422
    assert "无法对账" in resp.json()["detail"]
    assert "不足 2 个" in resp.json()["detail"]


async def test_at_least_one_branch_required() -> None:
    files = _files(_file("main.csv", b"1,0,1\n2,0,2\n"))
    async with await _client() as client:
        resp = await client.post("/reconcile", files=files)
    assert resp.status_code == 422
    assert "branch_csvs" in resp.text


async def test_alternate_form_field_names_are_accepted() -> None:
    files = [
        ("main_file", _file("main.csv", b"1,0,100\n2,0,110\n")),
        ("branch_files", _file("branch.csv", b"1,0,50\n2,0,55\n")),
    ]
    async with await _client() as client:
        resp = await client.post("/reconcile", files=files)
    assert resp.status_code == 200
    rows = _rows(resp.text)
    assert rows[1] == ["1", "2", "5", ""]


async def test_recognizable_header_row_is_skipped() -> None:
    files = _files(
        _file("main.csv", b"timestamp,high,low\n1,0,100\n2,0,110\n"),
        _file("branch.csv", b"ts,hi,lo\n1,0,50\n2,0,55\n"),
    )
    async with await _client() as client:
        resp = await client.post("/reconcile", files=files)
    assert resp.status_code == 200
    rows = _rows(resp.text)
    assert rows[1] == ["1", "2", "5", ""]


async def test_malformed_first_row_is_not_treated_as_header() -> None:
    files = _files(
        _file("main.csv", b"nope,0,1\n2,0,2\n"),
        _file("branch.csv", b"1,0,10\n2,0,11\n"),
    )
    async with await _client() as client:
        resp = await client.post("/reconcile", files=files)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "main.csv" in detail and "第 1 行" in detail
