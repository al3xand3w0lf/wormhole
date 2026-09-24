"""HTTP routes for the configuration generator.

WHERE THIS IS REACHABLE FROM, AND WHY THAT IS THE WHOLE SECURITY MODEL
    These routes hand out this installation's own connection settings -
    including its shared API key and its remote-CLI token - so that an operator
    does not have to copy them by hand into every station's file. That is the
    point of running the generator on the server the file is for, and it is
    also exactly why it must not be reachable from outside.

    It is therefore mounted on the ADMIN app, which binds to loopback, and the
    documented way in is an SSH tunnel:

        ssh -L 8080:127.0.0.1:<admin port> <user>@<host>
        http://localhost:8080/config/

    TWO INDEPENDENT BOLTS, on purpose. The bind address alone is brittle: one
    day someone sets the admin host to 0.0.0.0 to reach a different endpoint,
    and every secret this router prefills becomes public in the same breath,
    with nothing in the logs to say so. So every route here also refuses a
    request whose client is not loopback. Either bolt alone would do on a good
    day; the pair is what survives a configuration change made for an unrelated
    reason.

    The existing X-API-Key dependency is deliberately NOT applied. The page is
    opened in a browser through the tunnel, and a static page cannot hold the
    key without embedding it in the page it is protecting. Loopback plus SSH is
    the authentication, which is what the deployment guide already prescribes
    for this port.
"""

import io
import ipaddress
import json
import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import service
from .schema import load as load_schema

logger = logging.getLogger(__name__)

router = APIRouter()

STATIC_DIR = __import__("pathlib").Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 256 * 1024          # a config file is a few KB; this is generous


def _require_loopback(request: Request):
    """Second bolt. See the module docstring."""
    host = request.client.host if request.client else None
    try:
        if host is None or not ipaddress.ip_address(host).is_loopback:
            raise ValueError(host)
    except ValueError:
        logger.warning("configgen: refused non-loopback request from %s", host)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The configuration generator is reachable over loopback only. "
                   "Use an SSH tunnel.")


class GenerateRequest(BaseModel):
    role: str = Field(..., min_length=1, max_length=32)
    values: dict = Field(default_factory=dict)


def _prefill(config):
    """This installation's own settings, so they are not typed by hand.

    PUBLIC_HOST is separate from HOST/STREAM_HOST on purpose: those are BIND
    addresses, usually 0.0.0.0, and nothing in them says what a device out in
    the field should dial. Nothing derives one from the other, so if it is not
    configured the field is left blank and flagged rather than guessed - a
    plausible wrong address is worse than an empty one, because it looks
    answered.
    """
    public = (getattr(config, "PUBLIC_HOST", "") or "").strip()
    values = {
        "streaming_server_port": str(config.STREAM_PORT),
        "streaming_cli_secret": config.STREAM_CLI_SECRET or "",
        "http_port": str(getattr(config, "BATCH_PORT", "") or "") or None,
        "http_api_key": config.API_KEY or "",
    }
    if public:
        values["streaming_server_ip"] = public
        values["http_server"] = public
    return {k: v for k, v in values.items() if v is not None}, bool(public)


def build_router(config):
    """Bind the routes to one installation's configuration."""

    @router.get("/", response_class=HTMLResponse, tags=["Config"])
    async def page(request: Request):
        _require_loopback(request)
        with io.open(STATIC_DIR / "index.html", encoding="utf-8") as fh:
            return HTMLResponse(fh.read())

    @router.get("/schema", tags=["Config"])
    async def get_schema(request: Request):
        _require_loopback(request)
        return load_schema().as_form_spec()

    @router.get("/prefill", tags=["Config"])
    async def prefill(request: Request):
        _require_loopback(request)
        values, have_public = _prefill(config)
        return {
            "values": values,
            "public_host_configured": have_public,
            "note": None if have_public else
            "PUBLIC_HOST is not set in .env, so the address devices should dial "
            "is unknown here - HOST and STREAM_HOST are bind addresses. Enter it "
            "by hand, or set PUBLIC_HOST to have it prefilled.",
        }

    @router.get("/defaults/{role}", tags=["Config"])
    async def defaults(request: Request, role: str):
        _require_loopback(request)
        schema = load_schema()
        if role not in schema.roles:
            raise HTTPException(status_code=404, detail="unknown role")
        values = service.form_defaults(schema, role)
        prefilled, _ = _prefill(config)
        service.apply_prefill(schema, role, values, prefilled)
        return {"role": role, "values": values}

    @router.post("/import", tags=["Config"])
    async def import_config(request: Request):
        _require_loopback(request)
        raw = await request.body()
        if len(raw) > MAX_UPLOAD:
            raise HTTPException(status_code=413, detail="file too large")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        schema = load_schema()
        role, values, findings = service.import_text(schema, text)
        return {"role": role, "values": values, "findings": findings}

    @router.post("/generate", tags=["Config"])
    async def generate(request: Request, body: GenerateRequest):
        _require_loopback(request)
        schema = load_schema()
        if body.role not in schema.roles:
            raise HTTPException(status_code=404, detail="unknown role")
        values = {str(k): ("" if v is None else str(v))
                  for k, v in body.values.items()}
        text, findings = service.generate(schema, body.role, values)
        return {
            "role": body.role,
            "file": text,
            "findings": findings,
            "ok": not service.has_errors(findings),
        }

    @router.post("/download", response_class=PlainTextResponse, tags=["Config"])
    async def download(request: Request, body: GenerateRequest):
        """The file itself, as an attachment.

        Rendered straight into the response - never written under the data
        directory. A generated file carries the API key, the CLI token and the
        SIM credentials; leaving copies on the server would put them where the
        upload tree already keeps too much.
        """
        _require_loopback(request)
        schema = load_schema()
        if body.role not in schema.roles:
            raise HTTPException(status_code=404, detail="unknown role")
        values = {str(k): ("" if v is None else str(v))
                  for k, v in body.values.items()}
        text, findings = service.generate(schema, body.role, values)
        if service.has_errors(findings):
            raise HTTPException(
                status_code=422,
                detail=json.loads(json.dumps(
                    [f for f in findings if f["level"] == service.ERROR])))
        return PlainTextResponse(
            text, media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="CONFIG.TXT"'})

    return router
