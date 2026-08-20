from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.config import Settings
from app.services.catalog_store import CatalogEntryPayload
from app.services.marzban import (
    MarzbanClient,
    SUPPORTED_PROXY_PROTOCOLS,
    build_location_identity,
    ensure_local_proxy_inbounds,
)


@dataclass(slots=True)
class ImportResult:
    entries: list[CatalogEntryPayload]
    skipped_messages: list[str]


class ExternalCatalogImporter:
    def __init__(
        self,
        settings: Settings,
        marzban_client: MarzbanClient | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings
        self._marzban = marzban_client or MarzbanClient(settings)
        self._http = http_client or httpx.Client(
            timeout=settings.external_source_timeout_seconds,
            follow_redirects=True,
        )

    def import_source(self, *, kind: str, source_text: str) -> ImportResult:
        normalized_kind = kind.strip().lower()
        if normalized_kind == "subscription_url":
            body = self._fetch_subscription_text(source_text.strip())
            return self._import_text(body)
        if normalized_kind == "raw_links":
            return self._import_text(source_text)
        raise ValueError(f"Unsupported source kind '{kind}'.")

    def _fetch_subscription_text(self, url: str) -> str:
        response = self._http.get(url)
        response.raise_for_status()
        return response.text

    def _import_text(self, body: str) -> ImportResult:
        configs_or_lines = self._decode_source_body(body)
        if isinstance(configs_or_lines, list) and configs_or_lines and isinstance(
            configs_or_lines[0],
            dict,
        ):
            snapshot = self._marzban.inventory_from_configs(
                configs_or_lines,
                inbound_catalog={},
            )
            return ImportResult(entries=self._snapshot_to_entries(snapshot), skipped_messages=[])

        source_items: list[tuple[str, dict[str, Any]]] = []
        skipped: list[str] = []
        for line in configs_or_lines:
            try:
                source_items.append((line, self._parse_proxy_link(line)))
            except ValueError as exc:
                skipped.append(f"{line[:48]}: {exc}")

        if not source_items:
            detail = skipped[0] if skipped else "No usable links were found."
            raise ValueError(detail)

        source_configs = [config for _raw_link, config in source_items]
        snapshot = self._marzban.inventory_from_configs(
            source_configs,
            inbound_catalog={},
        )
        return ImportResult(
            entries=self._snapshot_to_entries(
                snapshot,
                share_uri_by_identity=self._share_uri_by_identity(source_items),
            ),
            skipped_messages=skipped,
        )

    def _decode_source_body(self, body: str) -> list[dict[str, Any]] | list[str]:
        text = body.strip()
        if not text:
            return []

        json_payload = self._try_json_payload(text)
        if json_payload is not None:
            return json_payload

        raw_text = text
        if "://" not in raw_text:
            decoded = self._try_base64_decode(raw_text)
            if decoded and "://" in decoded:
                raw_text = decoded

        return [line.strip() for line in raw_text.splitlines() if line.strip()]

    @staticmethod
    def _try_json_payload(text: str) -> list[dict[str, Any]] | None:
        try:
            payload = json.loads(text)
        except ValueError:
            return None

        if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
            return [dict(item) for item in payload]
        if isinstance(payload, dict):
            configs = payload.get("configs")
            if isinstance(configs, list) and all(isinstance(item, dict) for item in configs):
                return [dict(item) for item in configs]
        return None

    @staticmethod
    def _try_base64_decode(value: str) -> str | None:
        normalized = value.strip().replace("-", "+").replace("_", "/")
        padding = (-len(normalized)) % 4
        normalized = normalized + ("=" * padding)
        try:
            decoded = base64.b64decode(normalized).decode("utf-8")
        except Exception:
            return None
        return decoded.strip() or None

    def _parse_proxy_link(self, raw_link: str) -> dict[str, Any]:
        link = raw_link.strip()
        if not link:
            raise ValueError("Empty proxy link.")

        scheme = link.split("://", maxsplit=1)[0].lower()
        if scheme == "vmess":
            return self._parse_vmess(link)
        if scheme == "vless":
            return self._parse_vless_or_trojan(link, protocol="vless")
        if scheme == "trojan":
            return self._parse_vless_or_trojan(link, protocol="trojan")
        if scheme == "ss":
            return self._parse_shadowsocks(link)
        if scheme == "socks":
            return self._parse_socks_or_http(link, protocol="socks")
        if scheme == "http":
            return self._parse_socks_or_http(link, protocol="http")
        if scheme in ("wireguard", "wg"):
            return self._parse_wireguard_amnezia(link, protocol="wireguard")
        if scheme in ("amneziawg", "awg", "amnezia"):
            return self._parse_wireguard_amnezia(link, protocol="amneziawg")
        if scheme in ("hysteria", "hysteria2", "hy2"):
            return self._parse_hysteria(link, protocol="hysteria2")
        raise ValueError(f"Unsupported proxy scheme '{scheme}'.")

    def _parse_vmess(self, link: str) -> dict[str, Any]:
        encoded = link.removeprefix("vmess://").strip()
        decoded = self._try_base64_decode(encoded)
        if decoded is None:
            raise ValueError("Invalid vmess payload.")
        payload = json.loads(decoded)
        if not isinstance(payload, dict):
            raise ValueError("Invalid vmess JSON payload.")

        host = str(payload.get("add", "")).strip()
        port = int(str(payload.get("port", "0")).strip() or "0")
        user_id = str(payload.get("id", "")).strip()
        if not host or port <= 0 or not user_id:
            raise ValueError("vmess link is missing host, port, or id.")

        query = {
            "type": [str(payload.get("net", "tcp"))],
            "security": [str(payload.get("tls", "none"))],
            "sni": [str(payload.get("sni", ""))],
            "host": [str(payload.get("host", ""))],
            "path": [str(payload.get("path", ""))],
            "fp": [str(payload.get("fp", ""))],
            "pbk": [str(payload.get("pbk", ""))],
            "sid": [str(payload.get("sid", ""))],
            "serviceName": [str(payload.get("serviceName", ""))],
        }
        outbound = {
            "protocol": "vmess",
            "settings": {
                "vnext": [
                    {
                        "address": host,
                        "port": port,
                        "users": [
                            {
                                "id": user_id,
                                "alterId": int(str(payload.get("aid", "0")) or "0"),
                                "security": str(payload.get("scy", "auto")),
                            }
                        ],
                    }
                ]
            },
            "streamSettings": self._build_stream_settings(query),
        }
        return self._config_from_outbound(
            remarks=str(payload.get("ps", "")).strip() or f"VMESS {host}:{port}",
            outbound=outbound,
        )

    def _parse_vless_or_trojan(self, link: str, *, protocol: str) -> dict[str, Any]:
        parsed = urlparse(link)
        host = parsed.hostname or ""
        port = parsed.port or 0
        secret = unquote(parsed.username or "")
        if not host or port <= 0 or not secret:
            raise ValueError(f"{protocol} link is missing credentials or endpoint.")

        query = parse_qs(parsed.query, keep_blank_values=True)
        network = self._first(query, "type", default="tcp")
        stream_settings = self._build_stream_settings(query, default_network=network)

        if protocol == "vless":
            user: dict[str, Any] = {
                "id": secret,
                "encryption": self._first(query, "encryption", default="none"),
            }
            flow = self._first(query, "flow")
            if flow:
                user["flow"] = flow
            outbound = {
                "protocol": "vless",
                "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
                "streamSettings": stream_settings,
            }
        else:
            outbound = {
                "protocol": "trojan",
                "settings": {
                    "servers": [{"address": host, "port": port, "password": secret}]
                },
                "streamSettings": stream_settings,
            }
        remarks = unquote(parsed.fragment) or f"{protocol.upper()} {host}:{port}"
        return self._config_from_outbound(remarks=remarks, outbound=outbound)

    def _parse_hysteria(self, link: str, *, protocol: str) -> dict[str, Any]:
        parsed = urlparse(link)
        host = parsed.hostname or ""
        port = parsed.port or 0
        secret = unquote(parsed.password or parsed.username or "")
        if not host or port <= 0 or not secret:
            raise ValueError(f"{protocol} link is missing credentials or endpoint.")

        query = parse_qs(parsed.query, keep_blank_values=True)
        sni = self._first(query, "sni", default=host)
        insecure = self._first(query, "insecure", default="0")
        obfs = self._first(query, "obfs", default="")
        obfs_password = self._first(query, "obfs-password", default="")
        
        outbound = {
            "protocol": protocol,
            "settings": {
                "servers": [{"address": host, "port": port, "password": secret}]
            },
            "streamSettings": {
                "network": "udp",
                "security": "tls",
                "tlsSettings": {
                    "serverName": sni,
                    "allowInsecure": insecure in ("1", "true", "yes"),
                }
            },
        }
        if obfs and obfs != "none":
            outbound["settings"]["obfs"] = obfs
            outbound["settings"]["obfs_password"] = obfs_password

        remarks = unquote(parsed.fragment) or f"{protocol.upper()} {host}:{port}"
        return self._config_from_outbound(remarks=remarks, outbound=outbound)

    def _parse_shadowsocks(self, link: str) -> dict[str, Any]:
        raw = link.removeprefix("ss://").strip()
        body, _, fragment = raw.partition("#")
        body, _, _plugin = body.partition("?")
        decoded_authority = body
        if "@" not in decoded_authority:
            decoded_authority = self._try_base64_decode(body) or body

        credentials, separator, endpoint = decoded_authority.rpartition("@")
        if not separator:
            raise ValueError("Invalid shadowsocks authority.")
        method, separator, password = credentials.partition(":")
        if not separator:
            decoded_credentials = self._try_base64_decode(credentials)
            if decoded_credentials is None or ":" not in decoded_credentials:
                raise ValueError("Invalid shadowsocks credentials.")
            method, _, password = decoded_credentials.partition(":")
        host, separator, port_text = endpoint.rpartition(":")
        if not separator:
            raise ValueError("Invalid shadowsocks endpoint.")
        port = int(port_text)
        outbound = {
            "protocol": "shadowsocks",
            "settings": {
                "servers": [
                    {
                        "address": host.strip("[]"),
                        "port": port,
                        "method": method,
                        "password": password,
                    }
                ]
            },
            "streamSettings": {"network": "tcp", "security": "none"},
        }
        remarks = unquote(fragment) or f"SS {host}:{port}"
        return self._config_from_outbound(remarks=remarks, outbound=outbound)

    def _parse_socks_or_http(self, link: str, *, protocol: str) -> dict[str, Any]:
        parsed = urlparse(link)
        host = parsed.hostname or ""
        port = parsed.port or 0
        if not host or port <= 0:
            raise ValueError(f"{protocol} link is missing host or port.")
        server: dict[str, Any] = {"address": host, "port": port}
        if parsed.username:
            server["users"] = [
                {
                    "user": unquote(parsed.username),
                    "pass": unquote(parsed.password or ""),
                }
            ]
        outbound = {
            "protocol": protocol,
            "settings": {"servers": [server]},
            "streamSettings": {"network": "tcp", "security": "none"},
        }
        remarks = unquote(parsed.fragment) or f"{protocol.upper()} {host}:{port}"
        return self._config_from_outbound(remarks=remarks, outbound=outbound)

    def _parse_wireguard_amnezia(self, link: str, *, protocol: str) -> dict[str, Any]:
        parsed = urlparse(link)
        host = parsed.hostname or ""
        port = parsed.port or 0
        secret = unquote(parsed.username or "")
        
        # If the authority is base64 encoded JSON (e.g., standard warp/xray wireguard link)
        if not host and not secret:
            raw = link.partition("://")[2].partition("#")[0]
            decoded = self._try_base64_decode(raw)
            if decoded:
                try:
                    payload = json.loads(decoded)
                    if isinstance(payload, dict):
                        # Construct a basic JSON valid outbound
                        return self._config_from_outbound(
                            remarks=unquote(parsed.fragment) or protocol.upper(),
                            outbound=payload
                        )
                except Exception:
                    pass

        if not host or port <= 0:
            raise ValueError(f"{protocol} link is missing host or port.")

        # Basic wg://pubkey@host:port format fallback
        query = parse_qs(parsed.query, keep_blank_values=True)
        outbound = {
            "protocol": protocol,
            "settings": {
                "secretKey": self._first(query, "privateKey", default=""),
                "peers": [{"endpoint": f"{host}:{port}", "publicKey": secret}]
            },
            "streamSettings": {"network": "udp", "security": "none"},
        }

        # Storing raw INI or other parameters inside config JSON for Flutter to use later
        if "reserved" in query:
            outbound["settings"]["reserved"] = self._first(query, "reserved")

        remarks = unquote(parsed.fragment) or f"{protocol.upper()} {host}:{port}"
        return self._config_from_outbound(remarks=remarks, outbound=outbound)

    def _build_stream_settings(
        self,
        query: dict[str, list[str]],
        *,
        default_network: str | None = None,
    ) -> dict[str, Any]:
        network = self._first(query, "type", default=default_network or "tcp").lower()
        security = self._first(query, "security", default="none").lower()
        settings: dict[str, Any] = {"network": network, "security": security}
        server_name = self._first(query, "sni") or self._first(query, "host")
        fingerprint = self._first(query, "fp")
        alpn = self._first(query, "alpn")

        if security == "tls":
            tls_settings: dict[str, Any] = {}
            if server_name:
                tls_settings["serverName"] = server_name
            if fingerprint:
                tls_settings["fingerprint"] = fingerprint
            if alpn:
                tls_settings["alpn"] = [item.strip() for item in alpn.split(",") if item.strip()]
            if tls_settings:
                settings["tlsSettings"] = tls_settings

        if security == "reality":
            reality_settings: dict[str, Any] = {}
            if server_name:
                reality_settings["serverName"] = server_name
            if fingerprint:
                reality_settings["fingerprint"] = fingerprint
            public_key = self._first(query, "pbk")
            short_id = self._first(query, "sid")
            spider_x = self._first(query, "spx")
            if public_key:
                reality_settings["publicKey"] = public_key
            if short_id:
                reality_settings["shortId"] = short_id
            if spider_x:
                reality_settings["spiderX"] = spider_x
            if reality_settings:
                settings["realitySettings"] = reality_settings

        if network == "ws":
            ws_settings: dict[str, Any] = {}
            path = self._first(query, "path")
            header_host = self._first(query, "host")
            if path:
                ws_settings["path"] = path
            if header_host:
                ws_settings["headers"] = {"Host": header_host}
            if ws_settings:
                settings["wsSettings"] = ws_settings
        elif network == "grpc":
            grpc_settings: dict[str, Any] = {}
            service_name = self._first(query, "serviceName")
            mode = self._first(query, "mode")
            if service_name:
                grpc_settings["serviceName"] = service_name
            if mode:
                grpc_settings["multiMode"] = mode.lower() == "multi"
            if grpc_settings:
                settings["grpcSettings"] = grpc_settings
        elif network in {"http", "xhttp", "splithttp", "httpupgrade"}:
            http_settings: dict[str, Any] = {}
            path = self._first(query, "path")
            header_host = self._first(query, "host")
            if path:
                http_settings["path"] = path
            if header_host:
                http_settings["host"] = [header_host]
            if http_settings:
                settings["httpSettings"] = http_settings
        return settings

    @staticmethod
    def _config_from_outbound(*, remarks: str, outbound: dict[str, Any]) -> dict[str, Any]:
        return ensure_local_proxy_inbounds(
            {"remarks": remarks, "outbounds": [outbound]},
        )

    @staticmethod
    def _first(
        query: dict[str, list[str]],
        key: str,
        *,
        default: str = "",
    ) -> str:
        values = query.get(key)
        if not values:
            return default
        return str(values[0]).strip() or default

    def _share_uri_by_identity(
        self,
        source_items: list[tuple[str, dict[str, Any]]],
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for raw_link, config in source_items:
            for outbound in self._iter_proxy_outbounds(config):
                endpoint = self._extract_endpoint(outbound)
                if endpoint is None:
                    continue
                host, port = endpoint
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
                result.setdefault(identity_key, raw_link.strip())
        return result

    @classmethod
    def _iter_proxy_outbounds(cls, config: dict[str, Any]) -> list[dict[str, Any]]:
        outbounds = config.get("outbounds")
        if not isinstance(outbounds, list):
            return []

        candidates: list[dict[str, Any]] = []
        for outbound in outbounds:
            if not isinstance(outbound, dict):
                continue
            protocol = cls._normalize_protocol(outbound.get("protocol"))
            if protocol not in SUPPORTED_PROXY_PROTOCOLS:
                continue
            if cls._extract_endpoint(outbound) is None:
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
    def _snapshot_to_entries(
        snapshot,
        *,
        share_uri_by_identity: dict[str, str] | None = None,
    ) -> list[CatalogEntryPayload]:
        payloads: list[CatalogEntryPayload] = []
        share_uris = share_uri_by_identity or {}
        for location in snapshot.locations:
            identity_key = build_location_identity(
                protocol=location.protocol,
                host=location.host,
                port=location.port,
                network=location.network,
                security_mode=location.security_mode,
            )
            payloads.append(
                CatalogEntryPayload(
                    fingerprint=identity_key,
                    identity_key=identity_key,
                    location=location,
                    config_variants=dict(snapshot.config_by_location_id[location.id]),
                    share_uri=share_uris.get(identity_key),
                )
            )
        return payloads
