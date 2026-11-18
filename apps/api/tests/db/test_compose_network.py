"""A fixed address for Postgres, so the pool needs no DNS lookup (issue #50, ADR-0002).

Text checks on the committed files (PyYAML is not a dependency, so Compose is read with regular
expressions). Nothing runs Docker. The design these tests pin:

- `postgres` gets `networks.default.ipv4_address: ${QUORUM_NET_PREFIX:-<default>}.10`;
- `api` gets `extra_hosts: ["postgres:${QUORUM_NET_PREFIX:-<default>}.10"]`;
- the top-level `networks.default.ipam.config` has `subnet: ${QUORUM_NET_PREFIX:-<default>}.0/24`;
- `DATABASE_URL` keeps the host name `postgres` and port 5432, so only name resolution changes;
- `.env.example` names `QUORUM_NET_PREFIX` on one commented line with the same default.

`<default>` is one three-number prefix, the same text everywhere. The test that Compose passes no
`DB_POOL_` setting lives in test_pool_deploy_config.py.
"""

import ipaddress
import re

from db_helpers import REPO_ROOT

PREFIX_TEXT = (
    r"([^}]*)"  # whatever follows ":-" up to the closing brace; its validity is its own test
)
SKIP = r"(?:[ ]*#[^\n]*\n)*"  # comment lines may sit between the lines of one entry
TAIL = r"[ ]*(?:#[^\n]*)?$"  # an optional trailing comment
Q = r"[\"']?"  # the value may be quoted or not
PRIVATE_172 = ipaddress.ip_network("172.16.0.0/12")
THROWAWAY_PREFIX = "172.30.150"  # the measurement project quorum-p150 uses this subnet


def read(path) -> str:
    assert path.is_file(), f"{path.name} not found"
    return path.read_text(encoding="utf-8")


def compose_text() -> str:
    return read(REPO_ROOT / "docker-compose.yml")


def top_level_block(text: str, key: str) -> str:
    """The lines under a top-level `key:` up to the next line that starts in column 0."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line == f"{key}:":
            out = []
            for following in lines[i + 1 :]:
                if following and not following[0].isspace() and not following.startswith("#"):
                    break
                out.append(following)
            return "\n".join(out) + "\n"
    return ""


def service_block(text: str, name: str) -> str:
    """The lines of one service: from `  name:` to the next non-comment line indented 2 or less."""
    lines = top_level_block(text, "services").splitlines()
    for i, line in enumerate(lines):
        if line == f"  {name}:":
            out = []
            for following in lines[i + 1 :]:
                stripped = following.strip()
                if (
                    stripped
                    and not stripped.startswith("#")
                    and len(following) - len(following.lstrip()) <= 2
                ):
                    break
                out.append(following)
            return "\n".join(out) + "\n"
    return ""


def postgres_address_prefix() -> str | None:
    pattern = (
        rf"^    networks:\n{SKIP}      default:\n{SKIP}"
        rf"        ipv4_address: {Q}\$\{{QUORUM_NET_PREFIX:-{PREFIX_TEXT}\}}\.10{Q}{TAIL}"
    )
    match = re.search(pattern, service_block(compose_text(), "postgres"), re.MULTILINE)
    return match.group(1) if match else None


def api_extra_hosts_prefix() -> str | None:
    pattern = (
        rf"^    extra_hosts:\n(?:[ ]+(?:#[^\n]*|- [^\n]*)\n)*?"
        rf"      - {Q}postgres:\$\{{QUORUM_NET_PREFIX:-{PREFIX_TEXT}\}}\.10{Q}{TAIL}"
    )
    match = re.search(pattern, service_block(compose_text(), "api"), re.MULTILINE)
    return match.group(1) if match else None


def subnet_prefix() -> str | None:
    pattern = (
        rf"^  default:\n{SKIP}    ipam:\n{SKIP}      config:\n{SKIP}"
        rf"        - subnet: {Q}\$\{{QUORUM_NET_PREFIX:-{PREFIX_TEXT}\}}\.0/24{Q}{TAIL}"
    )
    match = re.search(pattern, top_level_block(compose_text(), "networks"), re.MULTILINE)
    return match.group(1) if match else None


def env_example_lines() -> list[str]:
    return [
        line
        for line in read(REPO_ROOT / ".env.example").splitlines()
        if "QUORUM_NET_PREFIX" in line
    ]


def as_network(prefix: str | None) -> ipaddress.IPv4Network:
    assert prefix is not None, "no QUORUM_NET_PREFIX default found in the Compose entry"
    try:
        return ipaddress.ip_network(f"{prefix}.0/24")
    except ValueError:
        raise AssertionError(f"{prefix!r} is not a valid three-number prefix") from None


# ---- the three Compose entries --------------------------------------------------------------


def test_postgres_has_a_fixed_address_on_the_default_network():
    assert postgres_address_prefix() is not None, (
        "postgres has no networks.default.ipv4_address "
        "of the form ${QUORUM_NET_PREFIX:-<prefix>}.10"
    )


def test_api_maps_the_name_postgres_to_that_address_with_extra_hosts():
    assert api_extra_hosts_prefix() is not None, (
        "api has no extra_hosts entry of the form postgres:${QUORUM_NET_PREFIX:-<prefix>}.10"
    )


def test_the_default_network_has_a_fixed_subnet_with_the_same_prefix():
    assert subnet_prefix() is not None, (
        "the top-level networks.default.ipam.config has no "
        "subnet ${QUORUM_NET_PREFIX:-<prefix>}.0/24"
    )


def test_every_use_of_the_prefix_in_compose_has_a_default_and_there_are_three():
    """Every use has a default (no bare use, no `:?`): `docker compose` works from a clean clone."""
    code = [
        re.sub(r"[ ]+#.*$", "", line)
        for line in compose_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    uses = [line for line in code if "QUORUM_NET_PREFIX" in line]
    assert len(uses) == 3, f"expected 3 uses of QUORUM_NET_PREFIX, found {len(uses)}"
    assert all(re.search(r"\$\{QUORUM_NET_PREFIX:-[^}]+\}", line) for line in uses)


def test_the_default_prefix_is_the_same_text_in_all_three_entries():
    found = {
        "ipv4_address": postgres_address_prefix(),
        "extra_hosts": api_extra_hosts_prefix(),
        "subnet": subnet_prefix(),
    }
    missing = [name for name, prefix in found.items() if prefix is None]
    assert not missing, f"no default found in: {missing}"
    assert len(set(found.values())) == 1, f"the three defaults differ: {found}"


# ---- what the default must be ---------------------------------------------------------------


def test_the_default_is_a_valid_three_number_prefix():
    prefix = postgres_address_prefix()
    assert prefix is not None, "no QUORUM_NET_PREFIX default found in the postgres entry"
    assert re.fullmatch(r"(0|[1-9]\d{0,2})(\.(0|[1-9]\d{0,2})){2}", prefix), (
        f"{prefix!r} is not three numbers"
    )
    assert all(int(part) <= 255 for part in prefix.split("."))


def test_the_default_is_private_in_172_16_12_and_is_not_the_throwaway_subnet():
    prefix = postgres_address_prefix()
    network = as_network(prefix)
    assert network.subnet_of(PRIVATE_172), f"{network} is not inside {PRIVATE_172}"
    assert prefix != THROWAWAY_PREFIX, (
        "the default must not be the subnet of the measurement project"
    )


def test_the_postgres_address_is_inside_the_subnet():
    address = (
        as_network(postgres_address_prefix()).network_address + 10
    )  # the ".10" of the Compose entry
    subnet = as_network(subnet_prefix())
    assert address in subnet, f"{address} is not inside {subnet}"
    assert address not in (
        subnet.network_address,
        subnet.network_address + 1,
        subnet.broadcast_address,
    ), "the address is the network, gateway or broadcast address"


# ---- what must not change -------------------------------------------------------------------


def test_the_api_database_url_keeps_the_host_name_postgres_and_port_5432():
    """Only name resolution changes: the URL is not rewritten to an IP address."""
    urls = [
        line.strip()
        for line in service_block(compose_text(), "api").splitlines()
        if line.strip().startswith("DATABASE_URL:")
    ]
    assert len(urls) == 1
    assert re.fullmatch(
        r"DATABASE_URL: postgresql://quorum_app:\$\{QUORUM_APP_PASSWORD:\?set it in \.env\}"
        r"@postgres:5432/\$\{POSTGRES_DB\}",
        urls[0],
    ), "DATABASE_URL must still use the host name postgres and port 5432"


# ---- .env.example ---------------------------------------------------------------------------


def test_env_example_has_one_commented_line_with_the_same_default():
    prefix = postgres_address_prefix()
    assert prefix is not None, "no QUORUM_NET_PREFIX default found in the postgres entry"
    lines = env_example_lines()
    assert len(lines) == 1, (
        f"QUORUM_NET_PREFIX must appear exactly once in .env.example, found {len(lines)}"
    )
    assert re.fullmatch(rf"# QUORUM_NET_PREFIX={re.escape(prefix)}", lines[0]), lines[0]
