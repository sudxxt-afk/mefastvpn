from __future__ import annotations

import hashlib
import json
import socket
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.schemas.contracts import VpnLocation


class MarzbanError(RuntimeError):
    pass


SUPPORTED_PROXY_PROTOCOLS = frozenset({
    "http",
    "hysteria",
    "hysteria2",
    "shadowsocks",
    "socks",
    "trojan",
    "vless",
    "vmess",
    "wireguard",
    "amneziawg",
    "amnezia",
    "openvpn",
})
DEFAULT_ROUTING_PROFILE = "balanced"
AVAILABLE_ROUTING_PROFILES = (
    DEFAULT_ROUTING_PROFILE,
    "strict",
    "bypass_local",
)

_DEFAULT_LOCAL_PROXY_INBOUNDS = (
    {
        "tag": "socks",
        "port": 10808,
        "listen": "127.0.0.1",
        "protocol": "socks",
        "sniffing": {
            "enabled": True,
            "destOverride": ["http", "tls"],
            "routeOnly": False,
        },
        "settings": {
            "auth": "noauth",
            "udp": True,
            "allowTransparent": False,
        },
    },
    {
        "tag": "http",
        "port": 10809,
        "listen": "127.0.0.1",
        "protocol": "http",
        "sniffing": {
            "enabled": True,
            "destOverride": ["http", "tls"],
            "routeOnly": False,
        },
        "settings": {
            "auth": "noauth",
            "udp": True,
            "allowTransparent": False,
        },
    },
)


def normalize_routing_profile(profile: str | None) -> str:
    candidate = (profile or DEFAULT_ROUTING_PROFILE).strip().lower().replace("-", "_")
    if candidate not in AVAILABLE_ROUTING_PROFILES:
        supported = ", ".join(AVAILABLE_ROUTING_PROFILES)
        raise ValueError(f"Unsupported routing profile '{profile}'. Use one of: {supported}.")
    return candidate


def build_location_identity(
    *,
    protocol: str,
    host: str,
    port: int,
    network: str,
    security_mode: str,
) -> str:
    return (
        f"{str(protocol).strip().lower()}|"
        f"{str(host).strip().lower()}|"
        f"{int(port)}|"
        f"{str(network).strip().lower()}|"
        f"{str(security_mode).strip().lower()}"
    )


def build_location_id(
    *,
    protocol: str,
    host: str,
    port: int,
    network: str,
    security_mode: str,
) -> str:
    value = build_location_identity(
        protocol=protocol,
        host=host,
        port=port,
        network=network,
        security_mode=security_mode,
    )
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def ensure_local_proxy_inbounds(config: dict[str, Any]) -> dict[str, Any]:
    inbounds = config.get("inbounds")
    normalized_inbounds: list[dict[str, Any]] = []
    existing_protocols: set[str] = set()

    if isinstance(inbounds, list):
        for inbound in inbounds:
            if not isinstance(inbound, dict):
                continue
            normalized = deepcopy(inbound)
            protocol = str(normalized.get("protocol", "")).strip().lower()
            if protocol in {"socks", "http"}:
                normalized["listen"] = "127.0.0.1"
            normalized_inbounds.append(normalized)
            if protocol:
                existing_protocols.add(protocol)

    for template in _DEFAULT_LOCAL_PROXY_INBOUNDS:
        protocol = str(template["protocol"]).strip().lower()
        if protocol in existing_protocols:
            continue
        normalized_inbounds.append(deepcopy(template))

    config["inbounds"] = normalized_inbounds
    return config


@dataclass(slots=True)
class InventorySnapshot:
    locations: list[VpnLocation]
    config_by_location_id: dict[str, dict[str, str]]
    default_location_id: str
    fetched_at: datetime
    stale: bool = False
    server_ip: str | None = None
    user_status: str = "active"
    user_expire: int | None = None
    default_routing_profile: str = DEFAULT_ROUTING_PROFILE
    available_routing_profiles: tuple[str, ...] = AVAILABLE_ROUTING_PROFILES


_GEOIP_CACHE: dict[str, dict[str, str]] = {}


def _resolve_geoip(host: str) -> dict[str, str]:
    """Resolve host IP to country/city/countryCode via ip-api.com."""
    if host in _GEOIP_CACHE:
        return _GEOIP_CACHE[host]

    result = {"country": "Global", "city": host, "countryCode": ""}
    try:
        resp = httpx.get(
            f"http://ip-api.com/json/{host}?fields=status,country,city,countryCode",
            timeout=5.0,
        )
        data = resp.json()
        if data.get("status") == "success":
            result["country"] = data.get("country", "Global")
            result["city"] = data.get("city", host)
            result["countryCode"] = data.get("countryCode", "")
    except Exception:
        pass

    _GEOIP_CACHE[host] = result
    return result


def _country_code_to_flag(code: str) -> str:
    """Convert ISO 3166-1 alpha-2 country code to flag emoji."""
    if not code or len(code) != 2:
        return "\U0001F310"  # 🌐
    return chr(0x1F1E6 + ord(code[0].upper()) - ord('A')) + chr(0x1F1E6 + ord(code[1].upper()) - ord('A'))


class MarzbanClient:
    def __init__(self, settings: Settings, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = http_client or httpx.Client(
            base_url=settings.marzban_base_url.rstrip("/"),
            verify=settings.marzban_verify_ssl,
            timeout=settings.marzban_timeout_seconds,
        )
        self._cached_admin_token: str | None = None

    def get_system_stats(self) -> dict[str, Any]:
        return self._request("GET", "/api/system")

    def get_inbounds(self) -> dict[str, list[dict[str, Any]]]:
        payload = self._request("GET", "/api/inbounds")
        if not isinstance(payload, dict):
            raise MarzbanError("Unexpected Marzban inbounds response.")
        return {str(key): list(value) for key, value in payload.items()}

    def get_hosts(self) -> dict[str, list[dict[str, Any]]]:
        payload = self._request("GET", "/api/hosts")
        if not isinstance(payload, dict):
            raise MarzbanError("Unexpected Marzban hosts response.")
        return {str(key): list(value) for key, value in payload.items()}

    def get_user(self, username: str) -> dict[str, Any]:
        payload = self._request("GET", f"/api/user/{username}")
        if not isinstance(payload, dict):
            raise MarzbanError("Unexpected Marzban user response.")
        return payload

    def create_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", "/api/user", json=payload)
        if not isinstance(result, dict):
            raise MarzbanError("Unexpected Marzban create user response.")
        return result

    def update_user(self, username: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request("PUT", f"/api/user/{username}", json=payload)
        if not isinstance(result, dict):
            raise MarzbanError("Unexpected Marzban update user response.")
        return result

    def delete_user(self, username: str, *, ignore_missing: bool = True) -> None:
        headers = {"Authorization": f"Bearer {self._get_admin_token()}"}
        response = self._client.request("DELETE", f"/api/user/{username}", headers=headers)
        if response.status_code == 401 and not self._settings.marzban_admin_token:
            self._cached_admin_token = None
            headers["Authorization"] = f"Bearer {self._get_admin_token()}"
            response = self._client.request("DELETE", f"/api/user/{username}", headers=headers)
        if ignore_missing and response.status_code == 404:
            return
        self._raise_for_status(response)

    def ensure_managed_user(self, *, username: str, note: str) -> dict[str, Any]:
        inbounds = self.get_inbounds()
        desired_inbounds = {
            protocol: [str(item["tag"]) for item in entries]
            for protocol, entries in inbounds.items()
            if entries
        }
        desired_proxies = {protocol: {} for protocol in desired_inbounds}

        try:
            user = self.get_user(username)
        except MarzbanError as exc:
            if "404" not in str(exc):
                raise
            payload = {
                "username": username,
                "status": "active",
                "expire": 0,
                "data_limit": 0,
                "data_limit_reset_strategy": "no_reset",
                "proxies": desired_proxies,
                "inbounds": desired_inbounds,
                "note": note,
            }
            return self.create_user(payload)

        current_inbounds = {
            protocol: list(tags)
            for protocol, tags in user.get("inbounds", {}).items()
            if isinstance(tags, list)
        }
        current_proxies = user.get("proxies", {})
        needs_update = False

        if current_inbounds != desired_inbounds:
            needs_update = True

        if set(current_proxies.keys()) != set(desired_proxies.keys()):
            needs_update = True

        if user.get("note") != note:
            needs_update = True

        if not needs_update:
            return user

        payload = {
            "proxies": {
                protocol: current_proxies.get(protocol, {})
                for protocol in desired_inbounds
            },
            "inbounds": desired_inbounds,
            "note": note,
        }
        return self.update_user(username, payload)

    def sync_user_access(
        self,
        *,
        username: str,
        display_name: str,
        is_active: bool,
        expires_at: datetime | None,
    ) -> dict[str, Any]:
        note = f"managed-by-speedy:{display_name}"
        desired_expire = (
            int(expires_at.astimezone(UTC).timestamp())
            if expires_at is not None
            else 0
        )

        if is_active:
            user = self.ensure_managed_user(username=username, note=note)
            payload: dict[str, Any] = {}
            if str(user.get("status", "")).lower() != "active":
                payload["status"] = "active"
            if int(user.get("expire") or 0) != desired_expire:
                payload["expire"] = desired_expire
            if payload:
                user = self.update_user(username, payload)
            return user

        try:
            user = self.get_user(username)
        except MarzbanError as exc:
            if "404" in str(exc):
                return {
                    "username": username,
                    "status": "disabled",
                    "expire": int(datetime.now(UTC).timestamp()),
                    "note": note,
                }
            raise

        payload = {
            "status": "disabled",
            "expire": int(datetime.now(UTC).timestamp()),
            "note": note,
        }
        return self.update_user(username, payload)

    def build_inventory(self, *, username: str, display_name: str) -> InventorySnapshot:
        managed_user = self.ensure_managed_user(
            username=username,
            note=f"managed-by-speedy:{display_name}",
        )
        subscription_configs = self._fetch_subscription_config(managed_user)
        expire = managed_user.get("expire")
        return self.inventory_from_configs(
            subscription_configs,
            inbound_catalog=self._build_inbound_catalog(self.get_inbounds()),
            user_status=str(managed_user.get("status", "active")),
            user_expire=int(expire) if isinstance(expire, int) and expire > 0 else None,
        )

    def get_subscription_url(self, *, username: str, display_name: str) -> str:
        managed_user = self.ensure_managed_user(
            username=username,
            note=f"managed-by-speedy:{display_name}",
        )
        subscription_url = managed_user.get("subscription_url")
        if not isinstance(subscription_url, str) or not subscription_url.strip():
            raise MarzbanError("Marzban user has no subscription URL.")
        return subscription_url.strip()

    def get_subscription_text(self, *, username: str, display_name: str) -> str:
        managed_user = self.ensure_managed_user(
            username=username,
            note=f"managed-by-speedy:{display_name}",
        )
        subscription_url = managed_user.get("subscription_url")
        if not isinstance(subscription_url, str) or not subscription_url.strip():
            raise MarzbanError("Marzban user has no subscription URL.")

        path = urlparse(subscription_url).path.rstrip("/") or "/"
        response = self._client.get(path)
        self._raise_for_status(response)
        text = response.text.strip()
        if not text:
            raise MarzbanError("Marzban subscription is empty.")
        return text

    def inventory_from_configs(
        self,
        subscription_configs: list[dict[str, Any]],
        *,
        inbound_catalog: dict[tuple[str, int], str] | None = None,
        user_status: str = "active",
        user_expire: int | None = None,
    ) -> InventorySnapshot:
        catalog = inbound_catalog or {}
        now = datetime.now(UTC)
        latency_cache: dict[tuple[str, int], tuple[bool, int]] = {}
        seen_endpoints: set[str] = set()
        locations: list[VpnLocation] = []
        config_by_location_id: dict[str, dict[str, str]] = {}
        server_ip: str | None = None

        for index, config in enumerate(subscription_configs):
            for outbound in self._iter_proxy_outbounds(config):
                endpoint = self._extract_endpoint(outbound)
                if endpoint is None:
                    continue

                host, port = endpoint
                server_ip = server_ip or host
                endpoint_key = (host, port)
                if endpoint_key not in latency_cache:
                    latency_cache[endpoint_key] = self._probe_endpoint(host, port)
                online, latency_ms = latency_cache[endpoint_key]

                protocol = self._normalize_protocol(outbound.get("protocol"))
                stream_settings = outbound.get("streamSettings", {}) or {}
                network = str(stream_settings.get("network", "tcp"))
                security_mode = str(stream_settings.get("security", "none"))
                identity_key = build_location_identity(
                    protocol=protocol,
                    host=host,
                    port=port,
                    network=network,
                    security_mode=security_mode,
                )
                if identity_key in seen_endpoints:
                    continue
                seen_endpoints.add(identity_key)
                display = self._display_name(
                    protocol=protocol,
                    port=port,
                    host=host,
                    network=network,
                    security_mode=security_mode,
                    inbound_catalog=catalog,
                )
                remarks = str(config.get("remarks", "")).strip()
                if remarks:
                    display = remarks[:120]
                geo = _resolve_geoip(host)
                location_id = build_location_id(
                    protocol=protocol,
                    host=host,
                    port=port,
                    network=network,
                    security_mode=security_mode,
                )

                locations.append(
                    VpnLocation(
                        id=location_id,
                        display_name=display,
                        country=geo["country"],
                        city=geo["city"],
                        flag_emoji=_country_code_to_flag(geo["countryCode"]),
                        host=host,
                        port=port,
                        protocol=protocol,
                        network=network,
                        security_mode=security_mode,
                        node_name=f"Primary {index + 1}",
                        online=online,
                        latency_ms=latency_ms,
                        recommended=not locations,
                        premium_only=False,
                        last_synced_at=now,
                    )
                )
                config_by_location_id[location_id] = self._build_config_variants(
                    source_config=config,
                    outbound=outbound,
                )

        if not locations:
            raise MarzbanError("Marzban returned no usable outbound configurations.")

        return InventorySnapshot(
            locations=locations,
            config_by_location_id=config_by_location_id,
            default_location_id=locations[0].id,
            fetched_at=now,
            stale=False,
            server_ip=server_ip,
            user_status=user_status,
            user_expire=user_expire,
            default_routing_profile=DEFAULT_ROUTING_PROFILE,
            available_routing_profiles=AVAILABLE_ROUTING_PROFILES,
        )

    def _fetch_subscription_config(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        subscription_url = user.get("subscription_url")
        if not isinstance(subscription_url, str) or not subscription_url.strip():
            raise MarzbanError("Marzban user has no subscription URL.")

        path = urlparse(subscription_url).path.rstrip("/")
        client_type = self._settings.marzban_subscription_client_type.strip("/")
        response = self._client.get(f"{path}/{client_type}")
        self._raise_for_status(response)
        payload = response.json()
        if not isinstance(payload, list):
            raise MarzbanError("Unexpected Marzban subscription payload.")
        return [item for item in payload if isinstance(item, dict)]

    def _iter_proxy_outbounds(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        outbounds = config.get("outbounds")
        if not isinstance(outbounds, list):
            return []

        candidates: list[dict[str, Any]] = []
        for outbound in outbounds:
            if not isinstance(outbound, dict):
                continue
            protocol = self._normalize_protocol(outbound.get("protocol"))
            if protocol not in SUPPORTED_PROXY_PROTOCOLS:
                continue
            if self._extract_endpoint(outbound) is None:
                continue
            candidates.append(outbound)
        return candidates

    @staticmethod
    def _normalize_protocol(value: Any) -> str:
        return str(value or "unknown").strip().lower()

    @staticmethod
    def _extract_endpoint(outbound: dict[str, Any]) -> tuple[str, int] | None:
        settings = outbound.get("settings", {})
        if not isinstance(settings, dict):
            return None

        for key in ("vnext", "servers"):
            items = settings.get(key)
            if isinstance(items, list) and items:
                first = items[0]
                if not isinstance(first, dict):
                    continue
                host = first.get("address")
                port = first.get("port")
                if isinstance(port, str):
                    try:
                        port = int(port)
                    except ValueError:
                        port = None
                if isinstance(host, str) and isinstance(port, int):
                    return host, port

        host = settings.get("address")
        port = settings.get("port")
        if isinstance(port, str):
            try:
                port = int(port)
            except ValueError:
                port = None
        if isinstance(host, str) and isinstance(port, int):
            return host, port

        peers = settings.get("peers")
        if isinstance(peers, list) and peers:
            first = peers[0]
            if isinstance(first, dict):
                endpoint = first.get("endpoint")
                if isinstance(endpoint, str):
                    host, separator, port_text = endpoint.rpartition(":")
                    if separator:
                        host = host.strip().strip("[]")
                        try:
                            port = int(port_text)
                        except ValueError:
                            port = None
                        if host and isinstance(port, int):
                            return host, port
        return None

    @staticmethod
    def _location_id(
        *,
        protocol: str,
        host: str,
        port: int,
        network: str,
        security_mode: str,
    ) -> str:
        return build_location_id(
            protocol=protocol,
            host=host,
            port=port,
            network=network,
            security_mode=security_mode,
        )

    @staticmethod
    def _build_inbound_catalog(
        inbounds: dict[str, list[dict[str, Any]]],
    ) -> dict[tuple[str, int], str]:
        catalog: dict[tuple[str, int], str] = {}
        for protocol, entries in inbounds.items():
            for entry in entries:
                port = entry.get("port")
                if not isinstance(port, int):
                    continue
                tag = entry.get("tag")
                if isinstance(tag, str) and tag.strip():
                    catalog[(protocol, port)] = tag.strip()
        return catalog

    def _display_name(
        self,
        *,
        protocol: str,
        port: int,
        host: str,
        network: str,
        security_mode: str,
        inbound_catalog: dict[tuple[str, int], str],
    ) -> str:
        inbound_tag = inbound_catalog.get((protocol, port))
        if inbound_tag:
            return inbound_tag

        parts = [protocol.upper(), network.upper()]
        if security_mode != "none":
            parts.append(security_mode.upper())
        return f"{' '.join(parts)} {host}:{port}"

    def _build_config_variants(
        self,
        *,
        source_config: dict[str, Any],
        outbound: dict[str, Any],
    ) -> dict[str, str]:
        variants: dict[str, str] = {}
        for profile in AVAILABLE_ROUTING_PROFILES:
            normalized_config = self._build_xray_config(
                source_config=source_config,
                outbound=outbound,
                routing_profile=profile,
            )
            variants[profile] = json.dumps(normalized_config, ensure_ascii=False)
        return variants

    def _build_xray_config(
        self,
        *,
        source_config: dict[str, Any],
        outbound: dict[str, Any],
        routing_profile: str,
    ) -> dict[str, Any]:
        profile = normalize_routing_profile(routing_profile)
        config = deepcopy(source_config)
        proxy_outbound = deepcopy(outbound)
        self._normalize_current_xray_transport(proxy_outbound)
        source_outbounds = config.get("outbounds")
        if not isinstance(source_outbounds, list):
            source_outbounds = []

        self._ensure_outbound_tag(proxy_outbound, preferred="proxy")
        selected_signature = self._outbound_signature(outbound)
        preserved_outbounds: list[dict[str, Any]] = []
        selected_removed = False
        direct_tag: str | None = None
        block_tag: str | None = None

        for existing in source_outbounds:
            if not isinstance(existing, dict):
                continue
            if not selected_removed and self._outbound_signature(existing) == selected_signature:
                selected_removed = True
                continue

            preserved = deepcopy(existing)
            protocol = self._normalize_protocol(preserved.get("protocol"))
            tag = preserved.get("tag")
            if protocol == "freedom" and isinstance(tag, str) and tag.strip():
                direct_tag = direct_tag or tag.strip()
            if protocol == "blackhole" and isinstance(tag, str) and tag.strip():
                block_tag = block_tag or tag.strip()
            preserved_outbounds.append(preserved)

        used_tags = {
            tag.strip()
            for outbound_item in [proxy_outbound, *preserved_outbounds]
            for tag in [outbound_item.get("tag")]
            if isinstance(tag, str) and tag.strip()
        }
        direct_tag = direct_tag or self._unique_tag("direct", used_tags)
        used_tags.add(direct_tag)
        block_tag = block_tag or self._unique_tag("block", used_tags)

        config["outbounds"] = [proxy_outbound, *preserved_outbounds]
        if not any(
            self._normalize_protocol(item.get("protocol")) == "freedom"
            and item.get("tag") == direct_tag
            for item in config["outbounds"]
            if isinstance(item, dict)
        ):
            config["outbounds"].append({"protocol": "freedom", "tag": direct_tag})
        if not any(
            self._normalize_protocol(item.get("protocol")) == "blackhole"
            and item.get("tag") == block_tag
            for item in config["outbounds"]
            if isinstance(item, dict)
        ):
            config["outbounds"].append({"protocol": "blackhole", "tag": block_tag})

        config["routing"] = self._build_routing_section(
            profile,
            existing_routing=config.get("routing"),
            direct_tag=direct_tag,
            block_tag=block_tag,
        )
        return ensure_local_proxy_inbounds(config)

    @staticmethod
    def _normalize_current_xray_transport(outbound: dict[str, Any]) -> None:
        """Normalize transport/reality keys for released Xray clients.

        Released cores up to and including v26.3.x read ``network`` and
        ``publicKey``; the ``method`` alias for ``network`` exists only in
        unreleased main, so it is folded back into the stable spelling.
        ``httpSettings`` was replaced by ``xhttpSettings`` in v26.x.
        """
        stream = outbound.get("streamSettings")
        if not isinstance(stream, dict):
            return

        network = str(stream.get("network") or stream.get("method") or "").lower()
        if network in {"raw", "xhttp"}:
            stream["network"] = network
            stream.pop("method", None)

        reality = stream.get("realitySettings")
        if isinstance(reality, dict):
            public_key = reality.get("publicKey")
            if not public_key:
                legacy_password = reality.pop("password", None)
                if legacy_password:
                    reality["publicKey"] = legacy_password
            else:
                reality.pop("password", None)

        if network == "xhttp" and "xhttpSettings" not in stream:
            http_settings = stream.pop("httpSettings", None)
            if isinstance(http_settings, dict):
                stream["xhttpSettings"] = http_settings

    @staticmethod
    def _build_routing_section(
        profile: str,
        *,
        existing_routing: dict[str, Any] | None = None,
        direct_tag: str,
        block_tag: str,
    ) -> dict[str, Any]:
        routing = deepcopy(existing_routing) if isinstance(existing_routing, dict) else {}
        existing_rules = routing.get("rules")
        if not isinstance(existing_rules, list):
            existing_rules = []

        rules: list[dict[str, Any]] = [
            {
                "type": "field",
                "outboundTag": direct_tag,
                "ip": ["geoip:private"],
            },
            {
                "type": "field",
                "outboundTag": block_tag,
                "protocol": ["bittorrent"],
            },
        ]

        if profile in {DEFAULT_ROUTING_PROFILE, "bypass_local"}:
            rules.insert(
                1,
                {
                    "type": "field",
                    "outboundTag": direct_tag,
                    "domain": [
                        "geosite:private",
                        "domain:localhost",
                        "domain:local",
                    ],
                },
            )

        if profile == "bypass_local":
            rules.insert(
                2,
                {
                    "type": "field",
                    "outboundTag": direct_tag,
                    "domain": ["keyword:lan"],
                },
            )

        routing["domainStrategy"] = routing.get("domainStrategy") or "IPIfNonMatch"
        routing["rules"] = [*rules, *existing_rules]
        return routing

    @staticmethod
    def _ensure_outbound_tag(outbound: dict[str, Any], *, preferred: str) -> str:
        tag = outbound.get("tag")
        if isinstance(tag, str) and tag.strip():
            return tag.strip()

        outbound["tag"] = preferred
        return preferred

    @staticmethod
    def _outbound_signature(outbound: dict[str, Any]) -> str:
        return json.dumps(outbound, sort_keys=True, ensure_ascii=False)

    @staticmethod
    def _unique_tag(preferred: str, used_tags: set[str]) -> str:
        if preferred not in used_tags:
            return preferred

        index = 1
        while f"{preferred}_{index}" in used_tags:
            index += 1
        return f"{preferred}_{index}"

    @staticmethod
    def _location_parts(*, host: str, display_name: str) -> tuple[str, str]:
        if " - " in display_name:
            country, city = display_name.split(" - ", maxsplit=1)
            if country.strip() and city.strip():
                return country.strip(), city.strip()
        return "Global", host

    @staticmethod
    def _flag_label(country: str) -> str:
        normalized = country.replace("-", " ").strip()
        if not normalized:
            return "GL"
        if normalized.lower() == "global":
            return "GL"

        words = [word for word in normalized.split() if word]
        if len(words) == 1:
            return words[0][:2].upper()
        return "".join(word[0].upper() for word in words[:2])

    @staticmethod
    def _probe_endpoint(host: str, port: int) -> tuple[bool, int]:
        started = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=2):
                duration = int((time.perf_counter() - started) * 1000)
                return True, max(duration, 1)
        except OSError:
            return False, 0

    def probe_endpoint(self, host: str, port: int) -> tuple[bool, int]:
        return self._probe_endpoint(host, port)

    def list_users(self, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        response = self._request("GET", "/api/users", params={"offset": offset, "limit": limit})
        return response if isinstance(response, dict) else {"users": response}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._get_admin_token()}"
        response = self._client.request(method, path, headers=headers, **kwargs)
        if response.status_code == 401 and not self._settings.marzban_admin_token:
            self._cached_admin_token = None
            headers["Authorization"] = f"Bearer {self._get_admin_token()}"
            response = self._client.request(method, path, headers=headers, **kwargs)
        self._raise_for_status(response)
        return response.json()

    def _get_admin_token(self) -> str:
        if self._settings.marzban_admin_token.strip():
            return self._settings.marzban_admin_token.strip()
        if self._cached_admin_token is not None:
            return self._cached_admin_token
        if (
            not self._settings.marzban_admin_username.strip()
            or not self._settings.marzban_admin_password.strip()
        ):
            raise MarzbanError(
                "Marzban admin credentials are not configured. "
                "Set SPEEDY_MARZBAN_ADMIN_TOKEN or username/password."
            )

        response = self._client.post(
            "/api/admin/token",
            data={
                "username": self._settings.marzban_admin_username,
                "password": self._settings.marzban_admin_password,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self._raise_for_status(response)
        payload = response.json()
        token = payload.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise MarzbanError("Marzban admin token response is malformed.")
        self._cached_admin_token = token
        return token

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip()
            raise MarzbanError(
                f"Marzban request failed with {exc.response.status_code}: {detail}"
            ) from exc


def stale_copy(snapshot: InventorySnapshot) -> InventorySnapshot:
    stale_locations = [
        location.model_copy(update={"stale": True})
        for location in snapshot.locations
    ]
    return InventorySnapshot(
        locations=stale_locations,
        config_by_location_id=dict(snapshot.config_by_location_id),
        default_location_id=snapshot.default_location_id,
        fetched_at=snapshot.fetched_at,
        stale=True,
        server_ip=snapshot.server_ip,
        user_status=snapshot.user_status,
        user_expire=snapshot.user_expire,
        default_routing_profile=snapshot.default_routing_profile,
        available_routing_profiles=snapshot.available_routing_profiles,
    )
