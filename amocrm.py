from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)


class AmoError(RuntimeError): pass


@dataclass(slots=True)
class AmoClient:
    base_url: str
    access_token: str
    requests_per_second: float = 4.0
    _last_request: float = field(init=False, default=0.0)

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, body: Any = None) -> Any:
        if not self.base_url or not self.access_token:
            raise AmoError("amoCRM is not configured")
        url = self.base_url.rstrip("/") + path
        if params:
            url += "?" + urlencode(params, doseq=True)
        payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}
        if payload: headers["Content-Type"] = "application/json"
        for attempt in range(5):
            pause = 1 / max(self.requests_per_second, .2) - (time.monotonic() - self._last_request)
            if pause > 0: time.sleep(pause)
            try:
                with urlopen(Request(url, data=payload, headers=headers, method=method), timeout=30) as response:
                    self._last_request = time.monotonic()
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except HTTPError as exc:
                self._last_request = time.monotonic()
                if 500 <= exc.code < 600 and attempt < 4:
                    time.sleep(min(2 ** attempt, 16)); continue
                raise AmoError(f"amoCRM HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:600]}") from exc
            except URLError as exc:
                if attempt < 4: time.sleep(min(2 ** attempt, 16)); continue
                raise AmoError(f"amoCRM network error: {exc}") from exc
        raise AmoError("amoCRM retry limit exceeded")

    def add_note(self, lead_id: int, text: str) -> None:
        self.request("POST", f"/api/v4/leads/{lead_id}/notes", body=[{"note_type":"common", "params":{"text":text}}])

    def move_lead(self, lead_id: int, pipeline_id: int, status_id: int) -> None:
        self.request("PATCH", f"/api/v4/leads/{lead_id}", body={"pipeline_id":pipeline_id, "status_id":status_id})

    def target_stage(self, pipeline_name: str, status_name: str) -> tuple[int, int]:
        data = self.request("GET", "/api/v4/leads/pipelines")
        for pipeline in data.get("_embedded", {}).get("pipelines", []):
            if pipeline.get("name", "").casefold() != pipeline_name.casefold(): continue
            for status in pipeline.get("_embedded", {}).get("statuses", []):
                if status.get("name", "").casefold() == status_name.casefold():
                    return int(pipeline["id"]), int(status["id"])
        raise AmoError(f"Target amoCRM stage not found: {pipeline_name} / {status_name}")

    def find_lead(self, full_name: str, phone: str) -> int | None:
        """Returns an ID only for one unambiguous candidate; never guesses."""
        queries = [x for x in (phone, full_name) if x]
        ids: set[int] | None = None
        for query in queries:
            data = self.request("GET", "/api/v4/leads", params={"query": query, "limit": 50, "with": "contacts"})
            found = {int(x["id"]) for x in data.get("_embedded", {}).get("leads", [])}
            ids = found if ids is None else ids & found
        return next(iter(ids)) if ids and len(ids) == 1 else None
