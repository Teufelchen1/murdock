import hmac
import hashlib
import json

from typing import List

import httpx

from fastapi import (
    FastAPI, Request, HTTPException, Security, Depends,
    WebSocket, WebSocketDisconnect
)
from fastapi.security.api_key import APIKeyHeader, APIKey
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from murdock.config import MURDOCK_CONFIG, DB_CONFIG, GITHUB_CONFIG, CI_CONFIG
from murdock.models import (
    FinishedJobModel, JobModel, CategorizedJobsModel, JobQueryModel
)
from murdock.murdock import Murdock
from murdock.log import LOGGER


LOGGER.debug("Configuration:\n"
    f"\nMURDOCK_CONFIG:\n{json.dumps(MURDOCK_CONFIG.dict(), indent=4)}\n"
    f"\nDB_CONFIG:\n{json.dumps(DB_CONFIG.dict(), indent=4)}\n"
    f"\nGITHUB_CONFIG:\n{json.dumps(GITHUB_CONFIG.dict(), indent=4)}\n"
    f"\nCI_CONFIG:\n{json.dumps(CI_CONFIG.dict(), indent=4)}\n"
)

murdock = Murdock()
app = FastAPI(
    debug=MURDOCK_CONFIG.log_level == "DEBUG",
    on_startup=[murdock.init],
    on_shutdown=[murdock.shutdown],
    title="Murdock API",
    description="This is the Murdock API",
    version="1.0.0",
    docs_url="/api", redoc_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount(
    "/results",
    StaticFiles(directory=MURDOCK_CONFIG.work_dir, html=True, check_dir=False),
    name="results",
)


@app.post("/github/webhook", include_in_schema=False)
async def github_webhook_handler(request: Request):
    headers = request.headers
    expected_signature = hmac.new(
        key=bytes(GITHUB_CONFIG.webhook_secret, "utf-8"),
        msg=(body := await request.body()),
        digestmod=hashlib.sha256
    ).hexdigest()
    gh_signature = headers.get(
        "X-Hub-Signature-256"
    ).split('sha256=')[-1].strip()
    if not hmac.compare_digest(gh_signature, expected_signature):
        LOGGER.warning(msg := "Invalid event signature")
        raise HTTPException(status_code=400, detail=msg)

    event_type = headers.get("X-Github-Event")
    if event_type not in MURDOCK_CONFIG.accepted_events:
        raise HTTPException(status_code=400, detail="Unsupported event")

    event_data = json.loads(body.decode())
    ret = None
    if event_type == "pull_request":
        ret = await murdock.handle_pull_request_event(event_data)
    if event_type == "push":
        ret = await murdock.handle_push_event(event_data)
    if ret is not None:
        raise HTTPException(status_code=400, detail=ret)


@app.get("/github/authenticate/{code}", include_in_schema=False)
async def github_authenticate_handler(code: str):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": GITHUB_CONFIG.app_client_id,
                "client_secret": GITHUB_CONFIG.app_client_secret,
                "code": code,
            },
            headers={"Accept": "application/vnd.github.v3+json"}
        )
    return JSONResponse({"token": response.json()["access_token"]})


async def _check_push_permissions(
    token: str = Security(APIKeyHeader(
        name="authorization",
        scheme_name="Github OAuth Token",
        auto_error=False)
    )
):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://api.github.com/repos/{GITHUB_CONFIG.repo}",
            headers={
                "Accept": "application/vnd.github.v3+json",
                "Authorization": f"token {token}"
            }
        )
    if response.status_code != 200:
        LOGGER.warning(f"Cannot fetch push permissions ({response})")

    if response.status_code == 200 and response.json()["permissions"]["push"]:
        return token

    raise HTTPException(status_code=401, detail="Missing push permissions")


@app.get(
    path="/jobs/queued",
    response_model=List[JobModel],
    response_model_exclude_unset=True,
    summary="Return the list of queued jobs",
    tags=["queued jobs"]
)
async def queued_jobs_handler():
    return murdock.get_queued_jobs()


@app.delete(
    path="/jobs/queued/{commit}",
    response_model=JobModel,
    response_model_exclude_unset=True,
    summary="Remove a job from the queue",
    tags=["queued jobs"]
)
async def queued_commit_cancel_handler(
    commit: str, _: APIKey = Depends(_check_push_permissions)
):
    if (job := await murdock.cancel_queued_job_with_commit(commit)) is None:
        raise HTTPException(
            status_code=404, detail=f"No job matching commit '{commit}' found"
        )

    return job.queued_model()


@app.get(
    path="/jobs/building",
    response_model=List[JobModel],
    response_model_exclude_unset=True,
    summary="Return the list of building jobs",
    tags=["building jobs"]
)
async def building_jobs_handler():
    return murdock.get_active_jobs()


@app.put(
    path="/jobs/building/{commit}/status",
    response_model=JobModel,
    response_model_exclude_unset=True,
    summary="Update the status of a building job",
    tags=["building jobs"]
)
async def building_commit_status_handler(request: Request, commit: str):
    if MURDOCK_CONFIG.use_job_token:
        msg = ""
        if (job := murdock.active.search_by_commit_sha(commit)) is None:
            msg = f"No job running for commit {commit}"
        elif "Authorization" not in request.headers:
            msg = "Job token is missing"
        elif (
            "Authorization" in request.headers and
            request.headers["Authorization"] != job.token
        ):
            msg = "Invalid Job token"

        if msg:
            LOGGER.warning(f"Invalid request to control_handler: {msg}")
            raise HTTPException(status_code=400, detail=msg)

    data = await request.json()
    if (job := await murdock.handle_commit_status_data(commit, data)) is None:
        raise HTTPException(
            status_code=404, detail=f"No job matching commit '{commit}' found"
        )

    return job.running_model()


@app.delete(
    path="/jobs/building/{commit}",
    response_model=JobModel,
    response_model_exclude_unset=True,
    summary="Stop a building job",
    tags=["building jobs"]
)
async def building_commit_stop_handler(
    commit: str,
    _: APIKey = Depends(_check_push_permissions)
):
    if (job := await murdock.stop_active_job_with_commit(commit)) is None:
        raise HTTPException(
            status_code=404, detail=f"No job matching commit '{commit}' found"
        )

    return job.running_model()


@app.get(
    path="/jobs/finished",
    response_model=List[FinishedJobModel],
    summary="Return the list of finished jobs sorted by end time, reversed",
    tags=["finished jobs"]
)
async def finished_jobs_handler(query: JobQueryModel = Depends()):
    return await murdock.db.find_jobs(query)


@app.post(
    path="/jobs/finished/{uid}",
    response_model=JobModel,
    response_model_exclude_unset=True,
    summary="Restart a finished job",
    tags=["finished jobs"]
)
async def finished_job_restart_handler(
    uid: str,
    _: APIKey = Depends(_check_push_permissions)
):
    if (job := await murdock.restart_job(uid)) is None:
        raise HTTPException(
            status_code=404, detail=f"Cannot restart job '{uid}'"
        )
    return job.queued_model()


@app.delete(
    path="/jobs/finished",
    response_model=List[FinishedJobModel],
    response_model_exclude_unset=True,
    summary="Removed finished jobs older than 'before' date",
    tags=["finished jobs"]
)
async def finished_job_delete_handler(
    before: str,
    _: APIKey = Depends(_check_push_permissions)
):
    query = JobQueryModel(before=before)
    if not (jobs := await murdock.remove_finished_jobs(query)):
        raise HTTPException(
            status_code=404, detail=f"Found no finished job to remove"
        )
    return jobs


@app.get(
    path="/jobs",
    response_model=CategorizedJobsModel,
    response_model_exclude_unset=True,
    summary="Return the list of all jobs (queued, building, finished)",
    tags=["jobs"]
)
async def jobs_handler(query: JobQueryModel = Depends()):
    return await murdock.get_jobs(query)


@app.websocket("/ws/status")
async def ws_client_handler(websocket: WebSocket):
    LOGGER.debug('websocket opening')
    await websocket.accept()
    LOGGER.debug('websocket connection opened')
    murdock.add_ws_client(websocket)

    try:
        while True:
            _ = await websocket.receive_text()
    except WebSocketDisconnect:
        LOGGER.debug('websocket connection closed')
        murdock.remove_ws_client(websocket)
