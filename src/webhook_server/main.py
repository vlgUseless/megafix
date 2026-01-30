import hashlib
import hmac
import json

from fastapi import FastAPI, Header, HTTPException, Request, Response
from redis import Redis
from rq import Queue

from agent_core.runner import handle_issue_opened_job
from agent_core.settings import get_settings

app = FastAPI()

settings = get_settings()
redis_conn = Redis.from_url(settings.redis_url)
q = Queue(settings.rq_queue, connection=redis_conn)


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> None:
    # GitHub: "sha256=<hex>"
    if not secret:
        raise HTTPException(status_code=500, detail="WEBHOOK_SECRET not set")
    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(
            status_code=401, detail="Missing/invalid X-Hub-Signature-256"
        )

    their_sig = signature_header.split("=", 1)[1]
    mac = hmac.new(secret.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
    our_sig = mac.hexdigest()

    if not hmac.compare_digest(our_sig, their_sig):
        raise HTTPException(status_code=401, detail="Bad signature")


def acquire_delivery_lock(delivery_id: str) -> bool:
    # SET key value NX EX ttl — рекомендуемый паттерн атомарного дедуп/локов
    key = f"delivery:{delivery_id}"
    return bool(redis_conn.set(key, "1", nx=True, ex=settings.delivery_ttl_sec))


@app.post("/webhook")
async def webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
):
    body = await request.body()
    verify_signature(settings.webhook_secret or "", body, x_hub_signature_256)

    payload = json.loads(body.decode("utf-8"))

    if x_github_event == "ping":
        return {"ok": True}

    if x_github_event == "issues" and payload.get("action") == "opened":
        repo = payload["repository"]["full_name"]
        issue_number = payload["issue"]["number"]
        installation_id = payload["installation"]["id"]

        if x_github_delivery and not acquire_delivery_lock(x_github_delivery):
            # уже видели эту доставку
            return Response(status_code=202)

        q.enqueue(
            handle_issue_opened_job,
            repo,
            issue_number,
            installation_id,
            x_github_delivery or None,
            job_timeout=settings.rq_job_timeout,
            result_ttl=settings.rq_result_ttl,
            failure_ttl=settings.rq_failure_ttl,
        )
        return Response(status_code=202)

    return {"ignored": True}
