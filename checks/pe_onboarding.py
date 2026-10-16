"""Reject PE onboarding requests that cannot produce a valid backbone.

Runs on every proposed change for each ``TopologyPeOnboarding`` in the
``pe_onboardings`` group. The schema already enforces presence and
uniqueness; what it cannot express is the cross-node shape the generator
needs: two distinct uplinks, each a PE of the same backbone with a port to
spare, and a name that no other device already uses.

A request whose status is ``decommissioned`` is skipped. The check runs for
every member of the group, so validating a retired request would fail every
later proposed change.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

MIN_UPLINKS = 2
RETIRED_STATUS = "decommissioned"


def _value(node: dict[str, Any] | None, attribute: str) -> Any:
    """Return ``node[attribute].value`` or ``None`` when either level is missing."""
    return ((node or {}).get(attribute) or {}).get("value")


class PeOnboardingCheck(InfrahubCheck):
    """A request names at least two distinct PEs of its backbone, each with a free port."""

    query = "pe_onboarding_check"

    async def validate(self, data: dict[str, Any]) -> None:  # type: ignore[override]
        """Log one error per violated rule.

        Args:
            data: Result of the ``pe_onboarding_check`` GraphQL query.
        """
        for edge in data.get("TopologyPeOnboarding", {}).get("edges", []):
            request = edge["node"]
            if _value(request, "status") == RETIRED_STATUS:
                continue
            name = request["name"]["value"]
            backbone = (request.get("backbone") or {}).get("node") or {}
            backbone_name = _value(backbone, "name")
            backbone_pes = {
                pe["node"]["name"]["value"] for pe in (backbone.get("pes") or {}).get("edges", [])
            }
            uplinks = [up["node"] for up in (request.get("uplinks") or {}).get("edges", [])]
            uplink_names = [up["name"]["value"] for up in uplinks]
            self._validate_name_is_free(request, data)
            self._validate_materialisation(request, backbone_pes, set(uplink_names), data)

            if len(set(uplink_names)) < MIN_UPLINKS:
                self.log_error(
                    message=(
                        f"{name}: needs at least {MIN_UPLINKS} distinct uplinks, got {uplink_names}"
                    )
                )
            if name in uplink_names:
                self.log_error(message=f"{name}: cannot uplink to itself")
            for uplink in uplinks:
                self._validate_uplink(name, uplink, backbone_name, backbone_pes)

    def _validate_name_is_free(self, request: dict[str, Any], data: dict[str, Any]) -> None:
        """Reject a request named after a device that the request did not create.

        The generator refuses to adopt such a device, because adopting it would
        let the generator's cleanup delete it when the request is deleted. The
        catalog form already rejects the name; this covers requests loaded from
        YAML or created in the web UI, and says why the generator stopped.
        """
        name = request["name"]["value"]
        own_device_id = ((request.get("device") or {}).get("node") or {}).get("id")
        for edge in (data.get("DcimDevice") or {}).get("edges", []):
            if edge["node"]["id"] != own_device_id:
                self.log_error(
                    message=(
                        f"{name}: a device named {name} already exists and was not created by "
                        "this request; rename the request"
                    )
                )
                return

    def _validate_materialisation(
        self,
        request: dict[str, Any],
        backbone_pes: set[str],
        uplinks: set[str],
        data: dict[str, Any],
    ) -> None:
        """Reject a request whose generator has not completed before merge.

        Allow additional core links: a later onboarding can claim a spare port
        on an already-onboarded PE without invalidating its original request.
        """
        name = request["name"]["value"]
        device = (request.get("device") or {}).get("node")
        core_count = int((data.get("InterfacePhysical") or {}).get("count", 0))
        session_count = int((data.get("RoutingBGPSession") or {}).get("count", 0))
        if (
            _value(request, "status") != "active"
            or _value(device, "name") != name
            or name not in backbone_pes
            or core_count < len(uplinks)
            or session_count < len(backbone_pes - {name})
        ):
            self.log_error(message=f"{name}: PE onboarding is not fully materialised")

    def _validate_uplink(
        self,
        name: str,
        uplink: dict[str, Any],
        backbone_name: str | None,
        backbone_pes: set[str],
    ) -> None:
        """Log errors for one uplink: wrong role, wrong backbone, or no port to claim."""
        uplink_name = uplink["name"]["value"]
        if uplink_name == name:
            return
        if _value(uplink, "role") != "pe":
            self.log_error(message=f"{name}: uplink {uplink_name} is not a PE")
        if uplink_name not in backbone_pes:
            self.log_error(
                message=f"{name}: uplink {uplink_name} is not in backbone {backbone_name}"
            )
        ports = [p["node"] for p in (uplink.get("interfaces") or {}).get("edges", [])]
        has_free_port = any(_value(port, "status") == "free" for port in ports)
        already_claimed = any(_value(port, "description") == f"To {name}" for port in ports)
        if not has_free_port and not already_claimed:
            self.log_error(message=f"{name}: uplink {uplink_name} has no free port")
