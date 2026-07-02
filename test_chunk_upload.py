#!/usr/bin/env python3
"""Standalone tests for chunked-upload reassembly in server.py.

Runs the FastAPI app in-process via TestClient — no network, SSL or real
modem needed:

    pip install fastapi aiofiles python-dotenv httpx
    python test_chunk_upload.py

Exit code 0 = all passed, 1 = at least one failure.
"""

import os
import sys
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlencode

# Point the server at a throwaway directory BEFORE importing it.
_tmp = Path(tempfile.mkdtemp(prefix="wormhole_test_"))
os.environ["UPLOAD_DIR"] = str(_tmp / "incoming")
os.environ["DOWNLOAD_DIR"] = str(_tmp / "outgoing")
os.environ["API_KEY"] = "testkey"
os.environ["LOG_FILE"] = str(_tmp / "server.log")
os.environ["ACCESS_LOG_FILE"] = str(_tmp / "access.log")

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)
H = {"X-API-Key": "testkey"}
UP = Path(server.UPLOAD_DIR)
CAP = 1000  # test chunk size

_failures = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _failures.append(name)


def url(**params):
    return "/modem/upload?" + urlencode(params)


def send_chunks(device_id, filename, data, cap=CAP, resend_index=None):
    """Upload `data` in `cap`-sized chunks; optionally re-send one chunk to test
    idempotency. Returns the list of HTTP status codes."""
    parts = [data[i:i + cap] for i in range(0, len(data), cap)]
    n = len(parts)
    codes = []
    for i, part in enumerate(parts):
        r = client.post(url(device_id=device_id, filename=filename, chunk_index=i,
                            chunk_count=n, offset=i * cap, total_size=len(data)),
                        headers=H, content=part)
        codes.append(r.status_code)
        if resend_index is not None and i == resend_index:
            r2 = client.post(url(device_id=device_id, filename=filename, chunk_index=i,
                                chunk_count=n, offset=i * cap, total_size=len(data)),
                            headers=H, content=part)
            codes.append(("resend", r2.status_code))
    return codes


print("== whole-file upload (no chunk params) still works ==")
payload = os.urandom(1000)
r = client.post(url(device_id="dev1", filename="whole.bin"), headers=H, content=payload)
check("whole upload -> 201", r.status_code == 201)
check("whole file bytes match", (UP / "whole.bin").read_bytes() == payload)

print("== chunked upload, 3 chunks, reassembled byte-identical ==")
data = os.urandom(2500)
codes = send_chunks("dev2", "big.ubx", data)
check("chunk status codes == [202, 202, 201]", codes == [202, 202, 201])
check("reassembled bytes match original", (UP / "big.ubx").read_bytes() == data)
check("no leftover .partial", not list(UP.glob("*.partial")))

print("== chunk re-send is idempotent ==")
data2 = os.urandom(2500)
codes = send_chunks("dev3", "retry.ubx", data2, resend_index=1)
check("re-sent chunk answered 202", ("resend", 202) in codes)
check("reassembled after re-send matches", (UP / "retry.ubx").read_bytes() == data2)

print("== missing partial (chunk 1 without chunk 0) -> 409 ==")
r = client.post(url(device_id="dev4", filename="nopart.ubx", chunk_index=1, chunk_count=2,
                    offset=CAP, total_size=2000), headers=H, content=os.urandom(1000))
check("missing partial -> 409", r.status_code == 409)

print("== assembled size mismatch on final chunk -> 422 ==")
d = os.urandom(2000)
client.post(url(device_id="dev5", filename="mm.ubx", chunk_index=0, chunk_count=2,
                offset=0, total_size=9999), headers=H, content=d[:1000])
r = client.post(url(device_id="dev5", filename="mm.ubx", chunk_index=1, chunk_count=2,
                    offset=1000, total_size=9999), headers=H, content=d[1000:])
check("size mismatch -> 422", r.status_code == 422)

print("== auth still enforced ==")
r = client.post(url(device_id="x", filename="a.bin"), content=b"x")
check("no API key -> 401", r.status_code == 401)

shutil.rmtree(_tmp, ignore_errors=True)
print()
if _failures:
    print(f"{len(_failures)} FAILED: {_failures}")
    sys.exit(1)
print("ALL PASSED")
sys.exit(0)
