"""Load and validate policy files, and the content hash behind `policy_version`.

A policy version is a file `versions/<name>.toml`. Its `policy_version` is `<name>:<sha256>`,
where the hash is taken over a canonical serialization of the VALIDATED parameters, not over the
file bytes: sorted keys, the separators "," and ":", ASCII only, and the excluded categories
sorted. Comments, whitespace, CRLF and the order of keys or categories therefore cannot change it.

LIMIT: the hash covers the parameters, not the rule code. A change in rule behaviour needs a new
version file; the hash cannot detect it.

Loading fails closed. A pinned version whose file no longer has the pinned hash is an error and
nothing is returned.
"""

import hashlib
import json
import re
import tomllib
from pathlib import Path

from pydantic import ValidationError

from app.policy.models import (
    Policy,
    PolicyError,
    PolicyFile,
    PolicyHashMismatchError,
    PolicyNotFoundError,
)

VERSIONS_DIR = Path(__file__).resolve().parent / "versions"

# The version that new decisions are made with. A constant in code, not a setting: a new version
# is a new file plus a one-line change here, reviewed in a pull request.
CURRENT_POLICY_NAME = "v1"

# Checked with fullmatch, never with "$": Python's "$" also matches before a final newline.
_NAME = re.compile(r"v[0-9]+")
_HASH = re.compile(r"[0-9a-f]{64}")


def canonical_hash(params: dict) -> str:
    """SHA-256 of the canonical serialization of validated policy parameters."""
    text = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_name(name: str) -> None:
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise PolicyError("a policy version name must look like v1, v2, v3 ...")


def _load_file(name: str, directory: Path) -> Policy:
    _check_name(name)  # before any path is built: no separator, dot or NUL can reach the path
    path = directory / f"{name}.toml"
    if not path.is_file():
        raise PolicyNotFoundError(f"no policy file for version {name}")
    try:
        document = tomllib.loads(path.read_bytes().decode("utf-8"))
        parameters = PolicyFile.model_validate(document)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise PolicyError(f"policy file {name}.toml is not valid TOML: {exc}") from exc
    except ValidationError as exc:
        raise PolicyError(f"policy file {name}.toml is not a valid policy: {exc}") from exc
    if parameters.version != name:
        raise PolicyError(f"policy file {name}.toml says version {parameters.version}")
    digest = canonical_hash(parameters.model_dump(mode="json"))
    return Policy(version=parameters.version, rules=parameters.rules, content_hash=digest)


def load_current_policy(directory: Path | None = None) -> Policy:
    """Load the current version (CURRENT_POLICY_NAME). The hash is computed, not checked."""
    directory = VERSIONS_DIR if directory is None else directory  # resolved at call time
    return _load_file(CURRENT_POLICY_NAME, directory)


def load_policy(policy_version: str, directory: Path | None = None) -> Policy:
    """Load the version a return was pinned with, and check that its file still has that hash.

    Raises PolicyError for a malformed pin (exactly that class), PolicyNotFoundError when the
    version has no file, and PolicyHashMismatchError when the file changed after the pin.
    """
    directory = VERSIONS_DIR if directory is None else directory  # resolved at call time
    if not isinstance(policy_version, str):
        raise PolicyError("a pinned policy version must be a string")
    name, separator, pinned_hash = policy_version.partition(":")
    if not separator or _NAME.fullmatch(name) is None or _HASH.fullmatch(pinned_hash) is None:
        raise PolicyError("a pinned policy version must look like v1:<64 lowercase hex>")
    policy = _load_file(name, directory)
    if policy.content_hash != pinned_hash:
        raise PolicyHashMismatchError(f"policy file {name}.toml no longer matches its pinned hash")
    return policy
