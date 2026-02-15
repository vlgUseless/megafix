import hashlib
import hmac
import json
import logging

from fastapi import FastAPI, Header, HTTPException, Request, Response
from redis import Redis
from rq import Queue

from agent_core.runner import handle_issue_opened_job
from agent_core.settings import get_settings
from reviewer_agent.runner import handle_review_job

app = FastAPI()
LOG = logging.getLogger(__name__)

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
    # SET key value NX EX ttl — атомарный паттерн дедуп/локов
    key = f"delivery:{delivery_id}"
    return bool(redis_conn.set(key, "1", nx=True, ex=settings.delivery_ttl_sec))


def _enqueue_review_job(
    repo: str,
    installation_id: int,
    *,
    pr_number: int | None = None,
    head_sha: str | None = None,
    run_id: int | None = None,
    conclusion: str | None = None,
    base_branch: str | None = None,
    delivery_id: str | None = None,
) -> None:
    job = q.enqueue(
        handle_review_job,
        repo,
        installation_id,
        pr_number=pr_number,
        head_sha=head_sha,
        run_id=run_id,
        conclusion=conclusion,
        base_branch=base_branch,
        delivery_id=delivery_id,
        job_timeout=settings.rq_job_timeout,
        result_ttl=settings.rq_result_ttl,
        failure_ttl=settings.rq_failure_ttl,
    )
    LOG.info(
        "Queued review job: repo=%s pr=%s run_id=%s sha=%s delivery=%s job_id=%s",
        repo,
        pr_number,
        run_id,
        head_sha,
        delivery_id,
        job.id,
    )


def _enqueue_issue_job(
    repo: str,
    issue_number: int,
    installation_id: int,
    *,
    delivery_id: str | None = None,
) -> None:
    job = q.enqueue(
        handle_issue_opened_job,
        repo,
        issue_number,
        installation_id,
        delivery_id,
        job_timeout=settings.rq_job_timeout,
        result_ttl=settings.rq_result_ttl,
        failure_ttl=settings.rq_failure_ttl,
    )
    LOG.info(
        "Queued issue job: repo=%s issue=%s delivery=%s job_id=%s",
        repo,
        issue_number,
        delivery_id,
        job.id,
    )


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
        LOG.debug("Received ping delivery=%s", x_github_delivery or None)
        return {"ok": True}

    if x_github_delivery and not acquire_delivery_lock(x_github_delivery):
        # уже видели эту доставку
        LOG.info(
            "Ignoring duplicate delivery: event=%s delivery=%s",
            x_github_event,
            x_github_delivery,
        )
        return Response(status_code=202)

    if x_github_event == "issues":
        action = payload.get("action")
        repo = payload["repository"]["full_name"]
        issue_number = payload["issue"]["number"]
        installation_id = payload["installation"]["id"]
        if action in {"opened", "reopened"}:
            _enqueue_issue_job(
                repo,
                issue_number,
                installation_id,
                delivery_id=x_github_delivery or None,
            )
            return Response(status_code=202)
        if action == "edited":
            changes = payload.get("changes", {})
            if "title" in changes or "body" in changes:
                _enqueue_issue_job(
                    repo,
                    issue_number,
                    installation_id,
                    delivery_id=x_github_delivery or None,
                )
                return Response(status_code=202)
        LOG.debug(
            "Ignored issues event: action=%s repo=%s issue=%s",
            action,
            repo,
            issue_number,
        )

    if x_github_event == "workflow_run" and payload.get("action") == "completed":
        repo = payload["repository"]["full_name"]
        installation_id = payload["installation"]["id"]
        run = payload.get("workflow_run", {})
        if run.get("event") != "pull_request" or not run.get("pull_requests"):
            LOG.debug(
                "Ignored workflow_run: event=%s pull_requests=%s",
                run.get("event"),
                bool(run.get("pull_requests")),
            )
            return {"ignored": True}
        run_id = run.get("id")
        head_sha = run.get("head_sha")
        conclusion = run.get("conclusion")
        pull_requests = run.get("pull_requests", [])
        pr_number = pull_requests[0]["number"] if pull_requests else None
        base_branch = (
            pull_requests[0].get("base", {}).get("ref") if pull_requests else None
        )

        _enqueue_review_job(
            repo,
            installation_id,
            pr_number=pr_number,
            head_sha=head_sha,
            run_id=run_id,
            conclusion=conclusion,
            base_branch=base_branch,
            delivery_id=x_github_delivery or None,
        )
        return Response(status_code=202)

    LOG.debug(
        "Ignored webhook event: event=%s action=%s delivery=%s",
        x_github_event,
        payload.get("action"),
        x_github_delivery or None,
    )
    return {"ignored": True}
