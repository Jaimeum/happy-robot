"""FMCSA operating-authority verification by MC (docket) number.

The QCMobile record carries several overlapping status fields. What the desk
actually needs to know is "may this carrier legally haul our freight for hire
right now", which is a conjunction:

  allowedToOperate == Y     not shut down
  statusCode == A           the census record is active
  common or contract
  authority == A            for-hire authority actually granted

A carrier can be `allowedToOperate: Y` while holding no for-hire authority at
all, so the last clause is what stops a broker booking an unauthorised carrier.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class FmcsaUnavailable(Exception):
    """FMCSA could not be reached or returned something unusable."""


@dataclass
class CarrierAuthority:
    mc_number: str
    found: bool
    authorised: bool
    legal_name: str | None = None
    dba_name: str | None = None
    dot_number: str | None = None
    city: str | None = None
    state: str | None = None
    status_code: str | None = None
    allowed_to_operate: str | None = None
    common_authority: str | None = None
    contract_authority: str | None = None
    safety_rating: str | None = None
    out_of_service_date: str | None = None
    power_units: int | None = None
    drivers: int | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str | None:
        return self.dba_name or self.legal_name

    def as_audit(self) -> dict[str, object]:
        return {
            "mc_number": self.mc_number,
            "found": self.found,
            "authorised": self.authorised,
            "legal_name": self.legal_name,
            "dot_number": self.dot_number,
            "status_code": self.status_code,
            "allowed_to_operate": self.allowed_to_operate,
            "common_authority": self.common_authority,
            "contract_authority": self.contract_authority,
            "out_of_service_date": self.out_of_service_date,
            "reasons": self.reasons,
        }


def normalise_mc(value: str) -> str:
    """Accept 'MC-123456', 'mc 123456', '123456' and spoken digit runs."""
    return re.sub(r"\D", "", value or "").lstrip("0") or ""


class FmcsaClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def verify(self, mc_number: str) -> CarrierAuthority:
        mc = normalise_mc(mc_number)
        if not mc:
            return CarrierAuthority(
                mc_number=mc_number, found=False, authorised=False,
                reasons=["MC number was empty or not numeric"],
            )

        url = f"{self._settings.fmcsa_base_url.rstrip('/')}/carriers/docket-number/{mc}"
        params = {"webKey": self._settings.fmcsa_api_key}

        try:
            async with httpx.AsyncClient(timeout=self._settings.fmcsa_timeout_seconds) as client:
                response = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise FmcsaUnavailable(f"FMCSA request failed: {exc}") from exc

        if response.status_code == 404:
            return CarrierAuthority(
                mc_number=mc, found=False, authorised=False,
                reasons=["No FMCSA record for that MC number"],
            )
        if response.status_code >= 400:
            raise FmcsaUnavailable(f"FMCSA returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise FmcsaUnavailable("FMCSA returned a non-JSON body") from exc

        content = payload.get("content")
        if not content:
            return CarrierAuthority(
                mc_number=mc, found=False, authorised=False,
                reasons=["No FMCSA record for that MC number"],
            )

        record = content[0] if isinstance(content, list) else content
        carrier = record.get("carrier") or {}
        return self._evaluate(mc, carrier)

    @staticmethod
    def _evaluate(mc: str, carrier: dict) -> CarrierAuthority:
        allowed = (carrier.get("allowedToOperate") or "").upper()
        status_code = (carrier.get("statusCode") or "").upper()
        common = (carrier.get("commonAuthorityStatus") or "").upper()
        contract = (carrier.get("contractAuthorityStatus") or "").upper()
        oos = carrier.get("oosDate")

        reasons: list[str] = []
        if allowed != "Y":
            reasons.append("FMCSA does not list this carrier as allowed to operate")
        if status_code and status_code != "A":
            reasons.append("The carrier's FMCSA record is not active")
        if oos:
            reasons.append(f"Carrier is out of service as of {oos}")
        if common != "A" and contract != "A":
            reasons.append("No active common or contract for-hire authority on file")

        return CarrierAuthority(
            mc_number=mc,
            found=True,
            authorised=not reasons,
            legal_name=carrier.get("legalName"),
            dba_name=carrier.get("dbaName"),
            dot_number=str(carrier["dotNumber"]) if carrier.get("dotNumber") else None,
            city=carrier.get("phyCity"),
            state=carrier.get("phyState"),
            status_code=status_code or None,
            allowed_to_operate=allowed or None,
            common_authority=common or None,
            contract_authority=contract or None,
            safety_rating=carrier.get("safetyRating"),
            out_of_service_date=oos,
            power_units=carrier.get("totalPowerUnits"),
            drivers=carrier.get("totalDrivers"),
            reasons=reasons,
        )
