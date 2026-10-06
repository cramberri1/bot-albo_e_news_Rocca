"""Bounded Actions recovery; never import the bot, restore state or hide failure."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


WORKFLOW_FILE = "albo_check.yml"
WORKFLOW_NAME = "Albo Pretorio Check"
BRANCH = "main"
DEFAULT_SECONDS = 18_000
MAX_HISTORY_PAGES = 3
RECOVERABLE_CONCLUSIONS = {"failure", "timed_out"}
ACTIVE_STATUSES = {"queued", "in_progress", "waiting", "pending", "requested"}


def worker_seconds(value: str | None) -> int:
    """Keep manual smoke tests bounded; inputs are data, never shell code."""
    if value in (None, ""):
        return DEFAULT_SECONDS
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,5}", value):
        raise ValueError("run_seconds deve contenere solo cifre, tra 60 e 18000")
    seconds = int(value)
    if not 60 <= seconds <= DEFAULT_SECONDS:
        raise ValueError("run_seconds deve essere tra 60 e 18000")
    return seconds


def recovery_title(run_id: int) -> str:
    if type(run_id) is not int or run_id < 1:
        raise ValueError("ID run non valido")
    return f"Albo recovery #{run_id}"


def source_rejection(run: dict, repository: str) -> str | None:
    """Only first-attempt scheduled failures on this repository/main qualify.

    A workflow_dispatch includes manual and recovery runs; both are excluded.
    A re-run retains its original event, so run_attempt also needs a guard.
    Cancelled is excluded to preserve deliberate user stops.
    """
    if run.get("name") != WORKFLOW_NAME:
        return "workflow diverso"
    if run.get("repository", {}).get("full_name", "").casefold() != repository.casefold():
        return "repository diverso"
    if run.get("head_branch") != BRANCH:
        return "branch diverso"
    if run.get("event") != "schedule":
        return "avvio manuale o recupero: nessuna ricorsione"
    if run.get("run_attempt") != 1:
        return "riesecuzione già richiesta"
    if run.get("status") != "completed" or run.get("conclusion") not in RECOVERABLE_CONCLUSIONS:
        return "esito non recuperabile"
    recovery_title(run.get("id"))
    if type(run.get("run_number")) is not int:
        raise ValueError("Numero run mancante")
    created = datetime.fromisoformat(run.get("created_at", "").replace("Z", "+00:00"))
    if created.utcoffset() is None:
        raise ValueError("Data run priva di fuso orario")
    return None


def history_rejection(source: dict, history: list[dict]) -> str | None:
    title = recovery_title(source["id"])
    for run in history:
        if run.get("id") != source["id"] and run.get("status") in ACTIVE_STATUSES:
            # The bot workflow always checks out main and shares its lock even
            # if an owner selects another ref for a manual dispatch.
            return "un worker è già attivo o in coda"
        if run.get("head_branch") != BRANCH:
            continue
        if run.get("event") == "workflow_dispatch" and run.get("display_title") == title:
            return "recupero già richiesto per questa run"
        if run.get("id") == source["id"]:
            continue
        if (run.get("run_number", 0) > source["run_number"]
                and run.get("conclusion") == "success"):
            return "una run successiva è già terminata correttamente"
    return None


class GitHubAPI:
    """Fixed GitHub host and repository paths; no webhook URL is followed."""
    def __init__(self, repository: str, token: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Repository GitHub non valido")
        if not token:
            raise ValueError("Token GitHub mancante")
        self.base = f"https://api.github.com/repos/{repository}/actions"
        self.token = token

    def request(self, path: str, payload: dict | None = None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(self.base + path, data=body, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }, method="GET" if payload is None else "POST")
        # A dispatch is not idempotent: never retry it after an ambiguous error.
        try:
            with urlopen(request, timeout=10) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as error:
            raise RuntimeError(f"GitHub API: HTTP {error.code}") from None
        except URLError:
            raise RuntimeError("GitHub API non raggiungibile; nessun retry automatico del dispatch") from None

    def workflow(self):
        return self.request(f"/workflows/{WORKFLOW_FILE}")

    def run(self, run_id):
        return self.request(f"/runs/{run_id}")

    def history(self, created_at):
        runs = []
        for page in range(1, MAX_HISTORY_PAGES + 1):
            query = urlencode({"branch": BRANCH, "created": f">={created_at}",
                               "per_page": 100, "page": page})
            result = self.request(f"/workflows/{WORKFLOW_FILE}/runs?{query}")
            batch = result["workflow_runs"]
            runs.extend(batch)
            if len(batch) < 100 or len(runs) >= result["total_count"]:
                return runs
        raise RuntimeError("Cronologia oltre il limite: recupero non richiesto senza verifica completa")

    def dispatch(self, run_id):
        return self.request(f"/workflows/{WORKFLOW_FILE}/dispatches", {
            "ref": BRANCH,
            "inputs": {"run_seconds": str(DEFAULT_SECONDS), "recovery_from_run_id": str(run_id)},
        })

    def active_runs(self):
        # Pending runs can predate the failed run: do not use its date filter.
        runs = {}
        for status in sorted(ACTIVE_STATUSES):
            query = urlencode({"status": status, "per_page": 100})
            result = self.request(f"/workflows/{WORKFLOW_FILE}/runs?{query}")
            if result["total_count"] > len(result["workflow_runs"]):
                raise RuntimeError("Coda oltre il limite: recupero non richiesto")
            for run in result["workflow_runs"]:
                runs[run["id"]] = run
        return list(runs.values())


def recover(event: dict, repository: str, api) -> str:
    """Dispatch at most one successor, retaining the failed run unchanged."""
    if event.get("action") != "completed":
        return "evento non conclusivo"
    source = event.get("workflow_run", {})
    rejection = source_rejection(source, repository)
    if rejection:
        return rejection
    workflow = api.workflow()
    if workflow.get("state") != "active":
        return "workflow disabilitato: arresto rispettato"
    if workflow.get("id") != source.get("workflow_id"):
        return "identità workflow diversa"
    # Re-read after queuing: the owner may have already re-run the failed run.
    current = api.run(source["id"])
    rejection = source_rejection(current, repository)
    if rejection:
        return rejection
    history = api.history(source["created_at"]) + api.active_runs()
    rejection = history_rejection(source, history)
    if rejection:
        return rejection
    api.dispatch(source["id"])
    return f"richiesto un worker di recupero per run {source['id']}"


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"duration", "recover"}:
        raise ValueError("Uso: workflow_recovery.py duration|recover")
    if sys.argv[1] == "duration":
        print(worker_seconds(os.environ.get("BOT_SECONDS_INPUT")))
        return
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    repository = os.environ["GITHUB_REPOSITORY"]
    api = GitHubAPI(repository, os.environ.get("GH_TOKEN", ""))
    result = recover(event, repository, api)
    print(result)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(f"Recupero limitato: {result}. La run originale conserva il suo esito.\n")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, KeyError, OSError) as error:
        print(f"Recupero/durata non confermati: {error}", file=sys.stderr)
        raise SystemExit(1)
