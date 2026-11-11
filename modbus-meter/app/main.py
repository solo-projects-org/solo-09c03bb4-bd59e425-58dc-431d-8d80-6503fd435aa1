"""FastAPI：单台 Modbus TCP 电表试采接口与 CSV 对账接口。"""

from __future__ import annotations

import os
import tempfile
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import AliasChoices, BaseModel, Field, model_validator
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
from starlette.background import BackgroundTask

from .planner import PlanError, Segment, plan_reads
from .reconcile import (
    CsvInputError,
    NotReconcilable,
    TableInput,
    display_name,
    safe_unlink,
    validate_csv,
    write_report,
)
from .registers import decode_unsigned

DEFAULT_HOST = os.getenv("METER_HOST", "meter")
DEFAULT_PORT = int(os.getenv("METER_PORT", "502"))
DEFAULT_SLAVE = int(os.getenv("METER_SLAVE_ID", "1"))
DEFAULT_TIMEOUT = float(os.getenv("METER_TIMEOUT", "3"))


class PointIn(BaseModel):
    address: int = Field(..., ge=0, description="保持寄存器起始地址（从 0 算）")
    words: int = Field(..., ge=1, le=2, description="字数：1（单字）或 2（双字，高字在前）")


class ReadRequest(BaseModel):
    points: list[PointIn] = Field(..., min_length=1, description="待试采测点，至少 1 个")
    range_start: int = Field(..., ge=0, description="设备可读区间起始地址（含两端）")
    range_end: int = Field(..., ge=0, description="设备可读区间结束地址（含两端）")
    max_read: int = Field(..., ge=1, description="单次读取寄存器字数上限")
    host: str = Field(default=DEFAULT_HOST, description="Modbus TCP 设备地址")
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535, description="Modbus TCP 端口")
    slave_id: int = Field(default=DEFAULT_SLAVE, ge=1, le=247, description="从站 / 单元号")
    timeout: float = Field(default=DEFAULT_TIMEOUT, gt=0, le=30, description="连接与读取超时（秒）")

    @model_validator(mode="after")
    def _check_range(self) -> "ReadRequest":
        if self.range_end < self.range_start:
            raise ValueError("range_end 不能小于 range_start")
        return self


class PointOut(BaseModel):
    index: int = Field(..., description="测点在请求 points 中的下标（从 0 算）")
    address: int
    words: int
    value: int = Field(..., description="无符号计数；双字时按高字在前（big-endian）拼接")


class SegmentOut(BaseModel):
    start: int = Field(..., description="读段起始地址（含）")
    end: int = Field(..., description="读段结束地址（含）")
    count: int = Field(..., description="本次读取的寄存器字数")
    point_indexes: list[int] = Field(..., description="该读段覆盖的测点下标")
    registers: list[int] = Field(..., description="读回的原始 16 位寄存器值（按地址升序）")


class ReadResponse(BaseModel):
    request_count: int = Field(..., description="实际 Modbus 请求次数（已最少化）")
    total_registers: int = Field(..., description="读取寄存器总数（次数相同时已最少化）")
    segments: list[SegmentOut]
    points: list[PointOut] = Field(..., description="按请求下标返回的测点评值")


app = FastAPI(
    title="Modbus TCP 电表试采与用电对账接口",
    version="1.1.0",
    description=(
        "试采：提交测点保持寄存器起始地址（从 0 算）与字数（1 或 2），"
        "并给出设备可读区间（含两端）与单次读取上限。"
        "接口先做读段规划（请求次数最少，次数相同则读取寄存器总数最少），"
        "再逐段读取，返回读段与无符号计数（双字高字在前）。"
        "无可行方案或读取失败时直接报错，不返回部分读数。\n\n"
        "对账：上传网关导出的总表 CSV 与至少一份支路表 CSV，"
        "按所有表都有读数的相邻时刻划段，"
        "输出各段起止秒及“总表增量 - 各支路增量之和”的差额。"
    ),
)


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": "modbus-meter-sampling", "docs": "/docs"}


@app.post(
    "/read",
    response_model=ReadResponse,
    summary="规划读段并对单台 Modbus TCP 电表执行试采",
    responses={
        422: {"description": "请求参数校验失败 / 无可行方案"},
        502: {"description": "连接或 Modbus 读取失败（不会返回部分读数）"},
    },
)
async def read_meter(req: ReadRequest) -> ReadResponse:
    try:
        segments: list[Segment] = plan_reads(
            points=[(p.address, p.words) for p in req.points],
            range_start=req.range_start,
            range_end=req.range_end,
            max_read=req.max_read,
        )
    except PlanError as exc:
        # 规划阶段失败：尚未与设备通信
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    client = AsyncModbusTcpClient(
        host=req.host,
        port=req.port,
        timeout=req.timeout,
    )
    try:
        await client.connect()
        if not client.connected:
            raise HTTPException(
                status_code=502,
                detail=f"连接 Modbus 设备失败：{req.host}:{req.port}",
            )

        seg_out: list[SegmentOut] = []
        # point_index -> 该测点读回的原始寄存器值
        point_words: dict[int, list[int]] = {}

        for seg in segments:
            try:
                rr = await client.read_holding_registers(
                    address=seg.start,
                    count=seg.count,
                    device_id=req.slave_id,
                )
            except (ModbusException, OSError, TimeoutError) as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"读取地址 {seg.start}..{seg.end} 失败：{exc}",
                ) from exc
            if rr.isError():
                raise HTTPException(
                    status_code=502,
                    detail=f"读取地址 {seg.start}..{seg.end} 失败：{rr}",
                )

            registers = list(rr.registers)
            seg_out.append(
                SegmentOut(
                    start=seg.start,
                    end=seg.end,
                    count=seg.count,
                    point_indexes=list(seg.point_indexes),
                    registers=registers,
                )
            )
            for idx in seg.point_indexes:
                p = req.points[idx]
                offset = p.address - seg.start
                point_words[idx] = registers[offset:offset + p.words]
    finally:
        client.close()

    points_out = [
        PointOut(
            index=i,
            address=p.address,
            words=p.words,
            value=decode_unsigned(point_words[i]),
        )
        for i, p in enumerate(req.points)
    ]
    return ReadResponse(
        request_count=len(segments),
        total_registers=sum(s.count for s in segments),
        segments=seg_out,
        points=points_out,
    )


async def _reconcile_response(
    main_upload: UploadFile | None,
    branch_uploads: list[UploadFile] | None,
) -> FileResponse:
    """校验全部上传文件后，以流式方式生成报告，完成后再发送。"""
    if main_upload is None:
        raise HTTPException(
            status_code=422,
            detail="缺少总表 CSV（字段名 main_csv）",
        )
    branches = [item for item in (branch_uploads or []) if item is not None]
    if not branches:
        raise HTTPException(
            status_code=422,
            detail="至少需要一份支路表 CSV（字段名 branch_csvs）",
        )

    tables = [TableInput(display_name(main_upload, "main.csv"), main_upload)]
    for index, upload in enumerate(branches, start=1):
        tables.append(
            TableInput(display_name(upload, f"branch_{index}.csv"), upload)
        )

    # 第一遍：逐行读完并校验所有文件。任一坏行/非递增都会在发送响应前报错。
    try:
        for table in tables:
            validate_csv(table.upload, table.filename)
    except CsvInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 第二遍：逐行读入、逐段写入临时文件；报告不驻留内存。
    fd, report_path = tempfile.mkstemp(prefix="reconcile-", suffix=".csv")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            common_count = write_report(tables, out)
        if common_count < 2:
            raise NotReconcilable(common_count)
    except NotReconcilable as exc:
        safe_unlink(report_path)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CsvInputError as exc:
        safe_unlink(report_path)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except BaseException:
        safe_unlink(report_path)
        raise

    # 所有输入均已读完并校验，报告完整落盘后才开始发送。
    return FileResponse(
        report_path,
        media_type="text/csv",
        filename="reconciliation.csv",
        background=BackgroundTask(safe_unlink, report_path),
    )


@app.post(
    "/reconcile",
    response_class=FileResponse,
    summary="上传总表与支路表 CSV 进行用电对账",
    description=(
        "总表与支路表使用相同计数单位和倍率。每份 CSV 无表头，"
        "每行三列：`UTC 采样秒,高字,低字`（可识别的表头行会自动跳过）；"
        "计数按高字在前解码。"
        "报告 CSV 列出相邻共同采样时刻所划各段的起止秒与差额"
        "（总表增量 - 各支路增量之和）；段内有计数下降的表列入"
        "`abnormal_files` 且该段差额留空。"
    ),
    responses={
        200: {
            "description": "对账报告（text/csv）",
            "content": {"text/csv": {}},
        },
        422: {
            "description": (
                "缺少总表/支路表、坏行、采样秒未严格递增，"
                "或共同采样时刻不足 2 个；detail 含文件名与行号，"
                "不返回部分报告"
            )
        },
    },
)
async def reconcile(
    main_csv: Annotated[
        UploadFile,
        File(
            description="总表 CSV：UTC 采样秒,高字,低字（无表头）",
            validation_alias=AliasChoices(
                "main_csv",
                "main_file",
                "main",
                "total_csv",
                "total",
                "main_meter",
            ),
        ),
    ],
    branch_csvs: Annotated[
        list[UploadFile],
        File(
            description="支路表 CSV，至少一份，格式与总表相同",
            validation_alias=AliasChoices(
                "branch_csvs",
                "branch_files",
                "branches",
                "branch_csv",
                "branch",
                "branch_meters",
                "branch_meter",
            ),
        ),
    ],
) -> FileResponse:
    return await _reconcile_response(main_csv, branch_csvs)


# 兼容另一个常见的路径名；Swagger 中只展示 /reconcile。
app.add_api_route(
    "/reconciliation",
    reconcile,
    methods=["POST"],
    include_in_schema=False,
    response_class=FileResponse,
)
