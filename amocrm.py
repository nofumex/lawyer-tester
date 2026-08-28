from __future__ import annotations

import json
import logging
import time
import re
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
        """Find an exact identity match without ever selecting an ambiguous lead.

        Phone has priority.  Name is a fallback only when phone finds no exact
        match; names are compared as an unordered set of normalised words.
        """
        normal_phone = self._phone(phone)
        if normal_phone:
            matches = self._matching_leads(normal_phone, lambda c: normal_phone in self._contact_phones(c))
            if len(matches) == 1:
                return next(iter(matches))
            if len(matches) > 1:
                LOG.warning("Ambiguous amoCRM phone match (%d leads); not binding", len(matches))
                return None
        name_words = self._name_words(full_name)
        if not name_words:
            return None
        # Search every word: amoCRM may index a contact under any FIO order.
        candidates: set[int] = set()
        for word in name_words:
            candidates |= self._matching_leads(word, lambda c: self._name_words(self._contact_name(c)) == name_words)
        if len(candidates) == 1:
            return next(iter(candidates))
        if len(candidates) > 1:
            LOG.warning("Ambiguous amoCRM name match (%d leads); not binding", len(candidates))
        return None

    @staticmethod
    def _phone(value: str) -> str:
        digits = re.sub(r"\D", "", value)
        if len(digits) == 11 and digits[0] in "78": digits = digits[1:]
        return digits if len(digits) == 10 else ""

    @staticmethod
    def _name_words(value: str) -> tuple[str, ...]:
        return tuple(sorted(re.findall(r"[а-яa-z]+", value.casefold().replace("ё", "е"))))

    @staticmethod
    def _contact_name(contact: dict[str, Any]) -> str:
        values = [str(contact.get("name") or "")]
        for field in contact.get("custom_fields_values") or []:
            if field.get("field_code") in {"FULL_NAME", "NAME"}:
                values.extend(str(v.get("value") or "") for v in field.get("values") or [])
        return " ".join(values)

    def _contact_phones(self, contact: dict[str, Any]) -> set[str]:
        return {self._phone(str(v.get("value") or "")) for field in contact.get("custom_fields_values") or []
                if field.get("field_code") == "PHONE" for v in field.get("values") or []} - {""}

    def _matching_leads(self, query: str, predicate: Any) -> set[int]:
        data = self.request("GET", "/api/v4/leads", params={"query": query, "limit": 250, "with": "contacts"})
        leads = data.get("_embedded", {}).get("leads", [])
        ids = {int(link["id"]) for lead in leads for link in lead.get("_embedded", {}).get("contacts", [])}
        contacts: dict[int, dict[str, Any]] = {}
        for cid in ids:
            item = self.request("GET", f"/api/v4/contacts/{cid}")
            contacts[cid] = item
        return {int(lead["id"]) for lead in leads if any(predicate(contacts[int(link["id"])]) for link in lead.get("_embedded", {}).get("contacts", []) if int(link["id"]) in contacts)}
