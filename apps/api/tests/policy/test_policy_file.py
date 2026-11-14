"""The policy file: parameters, validation, content hash, pinned loading (#46: AC1, AC2, AC9, AC10).

Sources: ADR-0003 Decision 2 and the choices approved on #46. No database is needed.
"""

import hashlib
import inspect
import re
import tomllib
from pathlib import Path

import pytest
from policy_examples import (
    V1_CANONICAL,
    V1_HASH,
    V1_POLICY_VERSION,
    V1_TEXT,
    canonical_hash,
    doc_from,
    toml_from,
    write_version,
)

import app
from app.policy import (
    CURRENT_POLICY_NAME,
    PolicyError,
    PolicyHashMismatchError,
    PolicyNotFoundError,
    load_current_policy,
    load_policy,
    loader,
)

# ---------- AC1: the v1 file ----------


def test_v1_file_has_the_adr_0003_parameters():
    # Read with tomllib directly, so this does not depend on the loader.
    document = tomllib.loads((loader.VERSIONS_DIR / "v1.toml").read_text(encoding="utf-8"))
    rules = document["rules"]
    assert document["version"] == "v1"
    assert rules["R1"] == {"window_days": 30}
    assert set(rules["R2"]["excluded_categories"]) == {"final_sale", "digital"}
    assert len(rules["R2"]["excluded_categories"]) == 2
    assert rules["R3"] == {"ceiling_minor": 20000, "currency": "USD"}
    assert set(document) == {"version", "rules"} and set(rules) == {"R1", "R2", "R3"}


def test_loaded_v1_policy_has_the_same_parameters(policy):
    assert policy.version == "v1"
    assert policy.rules.R1.window_days == 30
    assert set(policy.rules.R2.excluded_categories) == {"final_sale", "digital"}
    assert policy.rules.R3.ceiling_minor == 20000
    assert policy.rules.R3.currency == "USD"


def test_v1_file_is_inside_the_app_package_so_the_image_carries_it():
    # The Dockerfile copies `app/` into the image, so a data file inside it travels with it.
    package_dir = Path(app.__file__).resolve().parent
    versions_dir = loader.VERSIONS_DIR.resolve()
    assert package_dir in versions_dir.parents
    assert (versions_dir / "v1.toml").is_file()


# ---------- AC2: an invalid file is rejected (the boot test is in test_policy_boot.py) ----------

INVALID_FILES = {
    "missing-version": V1_TEXT.replace('version = "v1"\n', ""),
    "missing-window-days": V1_TEXT.replace("window_days = 30\n", ""),
    "missing-excluded-categories": V1_TEXT.replace(
        'excluded_categories = ["final_sale", "digital"]\n', ""
    ),
    "missing-ceiling": V1_TEXT.replace("ceiling_minor = 20000\n", ""),
    "missing-currency": V1_TEXT.replace('currency = "USD"\n', ""),
    "missing-rule-table": V1_TEXT.split("[rules.R3]")[0],
    "unknown-top-level-key": V1_TEXT.replace('version = "v1"\n', 'version = "v1"\nextra = 1\n'),
    "unknown-key-inside-a-rule": V1_TEXT.replace(
        "window_days = 30\n", "window_days = 30\ngrace = 2\n"
    ),
    "unknown-rule-id": V1_TEXT + "\n[rules.R9]\nthreshold = 1\n",
    "float-ceiling": V1_TEXT.replace("ceiling_minor = 20000\n", "ceiling_minor = 20000.0\n"),
    "bool-ceiling": V1_TEXT.replace("ceiling_minor = 20000\n", "ceiling_minor = true\n"),
    "string-ceiling": V1_TEXT.replace("ceiling_minor = 20000\n", 'ceiling_minor = "20000"\n'),
    "float-window": V1_TEXT.replace("window_days = 30\n", "window_days = 30.0\n"),
    "string-window": V1_TEXT.replace("window_days = 30\n", 'window_days = "30"\n'),
    "categories-not-a-list": V1_TEXT.replace(
        'excluded_categories = ["final_sale", "digital"]', 'excluded_categories = "digital"'
    ),
    "categories-with-a-number": V1_TEXT.replace(
        'excluded_categories = ["final_sale", "digital"]', 'excluded_categories = ["digital", 1]'
    ),
    "negative-window": V1_TEXT.replace("window_days = 30\n", "window_days = -1\n"),
    "negative-ceiling": V1_TEXT.replace("ceiling_minor = 20000\n", "ceiling_minor = -1\n"),
    "lowercase-currency": V1_TEXT.replace('currency = "USD"', 'currency = "usd"'),
    "short-currency": V1_TEXT.replace('currency = "USD"', 'currency = "US"'),
    "duplicate-category": V1_TEXT.replace(
        'excluded_categories = ["final_sale", "digital"]',
        'excluded_categories = ["digital", "digital"]',
    ),
    "category-with-a-space": V1_TEXT.replace(
        'excluded_categories = ["final_sale", "digital"]', 'excluded_categories = ["final sale"]'
    ),
    "category-in-capitals": V1_TEXT.replace(
        'excluded_categories = ["final_sale", "digital"]', 'excluded_categories = ["Digital"]'
    ),
    "bad-version-name": V1_TEXT.replace('version = "v1"', 'version = "1"'),
    "version-differs-from-the-file-name": V1_TEXT.replace('version = "v1"', 'version = "v2"'),
    "toml-syntax-error": V1_TEXT + "this is not toml\n",
}


@pytest.mark.parametrize("text", INVALID_FILES.values(), ids=INVALID_FILES.keys())
def test_invalid_policy_files_are_rejected(tmp_path, text):
    write_version(tmp_path, "v1", text)
    with pytest.raises(PolicyError):
        load_current_policy(tmp_path)


def test_a_64_character_excluded_category_loads(tmp_path):
    # R2 records the matched category in its details, whose strings are at most 64 characters.
    category = "c" * 64
    categories = ("final_sale", category)
    write_version(tmp_path, "v1", toml_from(categories=categories))
    policy = load_current_policy(tmp_path)
    assert category in policy.rules.R2.excluded_categories
    assert policy.policy_version == f"v1:{canonical_hash(doc_from(categories=categories))}"


def test_a_65_character_excluded_category_is_rejected(tmp_path):
    write_version(tmp_path, "v1", toml_from(categories=("final_sale", "c" * 65)))
    with pytest.raises(PolicyError):
        load_current_policy(tmp_path)


def test_the_fixture_text_is_valid_and_has_the_pinned_v1_hash(tmp_path):
    # Guards the cases above: they are edits of a text that does load.
    write_version(tmp_path, "v1", V1_TEXT)
    assert load_current_policy(tmp_path).policy_version == V1_POLICY_VERSION


# ---------- AC10: no float amounts in the file ----------


@pytest.mark.parametrize("literal", ["20000.0", "true", '"20000"', "2e4"])
def test_float_bool_and_string_ceilings_are_rejected_in_the_policy_file(tmp_path, literal):
    write_version(
        tmp_path, "v1", V1_TEXT.replace("ceiling_minor = 20000", f"ceiling_minor = {literal}")
    )
    with pytest.raises(PolicyError):
        load_current_policy(tmp_path)


# ---------- AC9: policy_version is the version plus the content hash ----------


def test_v1_content_hash_is_pinned():
    # The pinned values come from the approved serialization. The first assert ties the hash to
    # the string, so the serialization is fixed independently of the implementation.
    assert hashlib.sha256(V1_CANONICAL.encode("utf-8")).hexdigest() == V1_HASH
    assert canonical_hash(doc_from()) == V1_HASH
    assert load_current_policy().policy_version == V1_POLICY_VERSION


def test_policy_version_is_name_colon_hash(policy):
    assert re.fullmatch(r"v[0-9]+:[0-9a-f]{64}", policy.policy_version)
    assert policy.policy_version == f"{policy.version}:{canonical_hash(doc_from())}"


HASH_PRESERVING_FILES = {
    "crlf-line-endings": V1_TEXT.replace("\n", "\r\n"),
    "comments-and-blank-lines": "# a comment\n\n"
    + V1_TEXT.replace("[rules.R2]", "# another comment\n\n[rules.R2]  # trailing")
    + "\n\n",
    "extra-whitespace": V1_TEXT.replace(" = ", "   =   "),
    "tables-and-keys-in-another-order": (
        'version = "v1"\n\n[rules.R3]\ncurrency = "USD"\nceiling_minor = 20000\n\n'
        '[rules.R2]\nexcluded_categories = ["final_sale", "digital"]\n\n'
        "[rules.R1]\nwindow_days = 30\n"
    ),
    "array-over-several-lines": V1_TEXT.replace(
        'excluded_categories = ["final_sale", "digital"]',
        'excluded_categories = [\n  "final_sale",\n  "digital",\n]',
    ),
}


@pytest.mark.parametrize("text", HASH_PRESERVING_FILES.values(), ids=HASH_PRESERVING_FILES.keys())
def test_comments_whitespace_crlf_and_key_order_do_not_change_the_hash(tmp_path, text):
    write_version(tmp_path, "v1", text)
    assert load_current_policy(tmp_path).policy_version == V1_POLICY_VERSION


def test_excluded_category_order_does_not_change_the_hash(tmp_path):
    write_version(tmp_path, "v1", toml_from(categories=("digital", "final_sale")))
    assert load_current_policy(tmp_path).policy_version == V1_POLICY_VERSION


CHANGED_PARAMETERS = {
    "window-days": {"window_days": 31},
    "added-category": {"categories": ("final_sale", "digital", "clearance")},
    "removed-category": {"categories": ("final_sale",)},
    "ceiling": {"ceiling": 20001},
    "currency": {"currency": "EUR"},
}


@pytest.mark.parametrize("params", CHANGED_PARAMETERS.values(), ids=CHANGED_PARAMETERS.keys())
def test_changing_any_one_parameter_changes_the_hash(tmp_path, params):
    expected = canonical_hash(doc_from(**params))
    assert expected != V1_HASH
    write_version(tmp_path, "v1", toml_from(**params))
    assert load_current_policy(tmp_path).policy_version == f"v1:{expected}"
    # The old pin no longer matches the changed file.
    with pytest.raises(PolicyHashMismatchError):
        load_policy(V1_POLICY_VERSION, directory=tmp_path)


def test_the_version_name_is_part_of_the_hash(tmp_path):
    expected = canonical_hash(doc_from(name="v2"))
    assert expected != V1_HASH
    write_version(tmp_path, "v2", toml_from(name="v2"))
    assert load_policy(f"v2:{expected}", directory=tmp_path).policy_version == f"v2:{expected}"


def test_two_versions_load_from_a_fixture_directory(tmp_path):
    v2_params = {"name": "v2", "ceiling": 30000}
    v2_hash = canonical_hash(doc_from(**v2_params))
    write_version(tmp_path, "v1", V1_TEXT)
    write_version(tmp_path, "v2", toml_from(**v2_params))
    old = load_policy(V1_POLICY_VERSION, directory=tmp_path)  # old versions stay loadable
    new = load_policy(f"v2:{v2_hash}", directory=tmp_path)
    assert (old.policy_version, new.policy_version) == (V1_POLICY_VERSION, f"v2:{v2_hash}")
    assert (old.rules.R3.ceiling_minor, new.rules.R3.ceiling_minor) == (20000, 30000)
    with pytest.raises(PolicyHashMismatchError):  # the pins are not interchangeable
        load_policy(f"v2:{V1_HASH}", directory=tmp_path)


def test_pinned_version_whose_file_changed_fails_closed(tmp_path):
    path = write_version(tmp_path, "v1", V1_TEXT)
    assert load_policy(V1_POLICY_VERSION, directory=tmp_path).policy_version == V1_POLICY_VERSION
    path.write_bytes(toml_from(ceiling=20001).encode("utf-8"))  # a released file was edited
    with pytest.raises(PolicyHashMismatchError):
        load_policy(V1_POLICY_VERSION, directory=tmp_path)


@pytest.mark.parametrize(
    "pinned",
    [
        "",
        "v1",
        f"v1{V1_HASH}",
        "v1:",
        "v1:abc",
        f"v1:{V1_HASH.upper()}",
        f"v1:{V1_HASH}0",
        f"1:{V1_HASH}",
        f"V1:{V1_HASH}",
        f"v1:{V1_HASH[:-1]}g",
        # Path traversal and separators in the name: the name is checked before any file is read.
        f"../v1:{V1_HASH}",
        f"v1/../v1:{V1_HASH}",
        f"v1.toml:{V1_HASH}",
        f"v1/x:{V1_HASH}",
        f"v1\\x:{V1_HASH}",
        f"v1\x00:{V1_HASH}",
        f"v1:{V1_HASH}\x00",
        f"v1:{V1_HASH}\n",
    ],
    ids=lambda pinned: repr(pinned.replace(V1_HASH, "<hash>")),
)
def test_malformed_pinned_versions_are_rejected(tmp_path, pinned):
    # A valid v1.toml also sits one level ABOVE the versions directory, so a pin that escaped the
    # directory (../v1) would find and load a real file instead of failing.
    versions = tmp_path / "versions"
    versions.mkdir()
    write_version(versions, "v1", V1_TEXT)
    write_version(tmp_path, "v1", V1_TEXT)
    with pytest.raises(PolicyError) as caught:
        load_policy(pinned, directory=versions)
    # Exactly PolicyError: the two subclasses must not satisfy this test (a malformed pin is not
    # "not found" and not "hash mismatch": nothing was looked up).
    assert type(caught.value) is PolicyError


def test_unknown_pinned_version_is_not_found(tmp_path):
    write_version(tmp_path, "v1", V1_TEXT)
    with pytest.raises(PolicyNotFoundError):
        load_policy(f"v9:{V1_HASH}", directory=tmp_path)


def test_every_file_in_versions_loads_and_validates():
    files = sorted(loader.VERSIONS_DIR.glob("v*.toml"))
    assert files, "no policy file found"
    for path in files:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        document["rules"]["R2"]["excluded_categories"].sort()
        pinned = f"{path.stem}:{canonical_hash(document)}"
        assert load_policy(pinned).policy_version == pinned


def test_current_policy_name_names_a_loadable_version():
    assert (loader.VERSIONS_DIR / f"{CURRENT_POLICY_NAME}.toml").is_file()
    assert load_current_policy().version == CURRENT_POLICY_NAME


# ---------- the directory default is resolved when the function is called ----------


@pytest.mark.parametrize("function", [load_current_policy, load_policy])
def test_directory_parameter_defaults_to_none(function):
    assert inspect.signature(function).parameters["directory"].default is None


def test_directory_default_is_resolved_at_call_time(monkeypatch, tmp_path):
    other = toml_from(ceiling=30000)
    write_version(tmp_path, "v1", other)
    other_hash = canonical_hash(doc_from(ceiling=30000))
    monkeypatch.setattr(loader, "VERSIONS_DIR", tmp_path)
    assert load_current_policy().policy_version == f"v1:{other_hash}"
    assert load_policy(f"v1:{other_hash}").rules.R3.ceiling_minor == 30000
