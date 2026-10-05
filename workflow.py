#!/usr/bin/env python3
import calendar
import hashlib
import hmac
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "sources"
ICONS = ROOT / "icons"
DATA = ROOT / "data"
INDEX = DATA / "index.json"
STATE = DATA / "state.json"
BADGE = DATA / "badge.json"
OWNERS = DATA / "owners.json"
LEGACY_INDEX = ROOT / "index.json"
LEGACY_STATE = ROOT / "state.json"

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
CATALOG_OWNER = os.environ.get("GITHUB_REPOSITORY_OWNER", "").lower()
CATALOG_REPO = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("CATALOG_BRANCH", "main")

R2_PREFIX = "packs"
R2_DEFAULT_LIMIT_GB = 9.0
R2_PRUNE_DAYS = int(os.environ.get("R2_PRUNE_DAYS", "").strip() or 7)

MAX_ZIPS_PER_RELEASE = 10
MAX_PARTS = MAX_ZIPS_PER_RELEASE
MAX_REPOS_PER_SOURCE = 100
MAX_ZIP_BYTES = 100 * 1024 * 1024
MAX_EXPANDED_BYTES = 250 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_EMOTES = 256
MAX_EVENTS = 10_000
MAX_KEYFRAMES = 20_000
MAX_ANIMATION_SECONDS = 30.0 * 60.0
SCHEMA_VERSION = 1
MAX_ENTRIES = 20000
MAX_ICON_BYTES = 512 * 1024
MAX_RATIO = 200

SOURCE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38})\.json$")
REPO_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
IDENT = re.compile(r"^[a-z0-9_.-]+$")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")
SAFE_PATH = re.compile(r"^[a-z0-9_./-]+$")
RL_NAMESPACE = re.compile(r"^[a-z0-9_.-]+$")
RL_PATH = re.compile(r"^[a-z0-9_./-]+$")
NUMERIC = re.compile(r"^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?[fFdD]?\s*$")
RARITIES = {"LEGENDARY", "RARE", "UNCOMMON", "COMMON", "COMPLEMENTARY"}
EVENT_TYPES = {"play_music", "play_sound", "spawn_particle", "set_skin", "reset_skin", "render_model"}
SUPPORTED_FORMATS = {"1.8.0"}
BONES = {
    "Root", "Head", "RightItem", "LeftItem",
    "Torso", "RightArm", "LeftArm", "RightLeg", "LeftLeg",
    "LowerTorso", "UpperTorso",
    "RightUpperArm", "RightLowerArm", "RightHand",
    "LeftUpperArm", "LeftLowerArm", "LeftHand",
    "RightUpperLeg", "RightLowerLeg", "RightFoot",
    "LeftUpperLeg", "LeftLowerLeg", "LeftFoot",
}

ALLOWED = [
    (re.compile(r"^pack\.json$"), "json"),
    (re.compile(r"^icon\.png$"), "png"),
    (re.compile(r"^emotes/[^/]+/(info|animation|events)\.json$"), "json"),
    (re.compile(r"^assets/audio/.+\.ogg$"), "ogg"),
    (re.compile(r"^assets/textures/.+\.png$"), "png"),
    (re.compile(r"^assets/models/.+\.json$"), "json"),
]
MAGIC = {"png": b"\x89PNG\r\n\x1a\n", "ogg": b"OggS"}


class Gone(Exception):
    pass


class Invalid(Exception):
    pass


def api(path):
    request = urllib.request.Request(API + path, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "universal-emotes-packs",
        **({"Authorization": "Bearer " + TOKEN} if TOKEN else {}),
    })
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (404, 410, 451):
                raise Gone(f"{path}: HTTP {error.code}")
            if error.code in (403, 429) or error.code >= 500:
                time.sleep(5 * (attempt + 1))
                last = error
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as error:
            time.sleep(5 * (attempt + 1))
            last = error
    raise RuntimeError(f"GitHub API unavailable for {path}: {last}")


def download(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "universal-emotes-packs"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise Invalid(f"release zip is larger than {limit // (1024 * 1024)} MB")
    return data


class R2Account:
    def __init__(self, number, raw):
        where = f"R2_ACCOUNTS[{number}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{where} must be an object")
        values = {}
        for field in ("account_id", "access_key_id", "secret_access_key", "bucket", "public_url"):
            value = raw.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{where}.{field} is missing")
            values[field] = value.strip()
        limit = raw.get("limit_gb", R2_DEFAULT_LIMIT_GB)
        if not is_num(limit) or limit <= 0:
            raise ValueError(f"{where}.limit_gb must be a positive number")
        self.number = number + 1
        self.account_id = values["account_id"]
        self.access_key_id = values["access_key_id"]
        self.secret_access_key = values["secret_access_key"]
        self.bucket = values["bucket"]
        self.public_url = values["public_url"].rstrip("/")
        self.limit = int(limit * 1024 ** 3)
        self.objects = None
        self.used = 0

    def label(self):
        return f"R2 account {self.number} ({self.bucket})"


def load_r2_accounts():
    raw = os.environ.get("R2_ACCOUNTS", "").strip()
    if not raw:
        raise ValueError("R2_ACCOUNTS is not set")
    try:
        items = json.loads(raw)
    except ValueError:
        raise ValueError("R2_ACCOUNTS is not valid JSON")
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        raise ValueError("R2_ACCOUNTS must be a list of accounts")
    accounts = [R2Account(number, item) for number, item in enumerate(items)]
    urls = [a.public_url for a in accounts]
    if len(set(urls)) != len(urls):
        raise ValueError("every R2 account needs its own public_url")
    return accounts


R2_ACCOUNTS = []


def _hmac(key, message):
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def r2_request(account, method, key="", query=None, body=b"", headers=None):
    host = f"{account.account_id}.r2.cloudflarestorage.com"
    path = "/" + account.bucket + ("/" + urllib.parse.quote(key, safe="/-_.~") if key else "")
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(str(v), safe='-_.~')}"
        for k, v in sorted((query or {}).items()))
    url = f"https://{host}{path}" + (f"?{canonical_query}" if canonical_query else "")
    payload_hash = hashlib.sha256(body).hexdigest()
    last = None
    for attempt in range(3):
        amz_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        day = amz_date[:8]
        signed_headers = {k.lower(): str(v).strip() for k, v in (headers or {}).items()}
        signed_headers.update({"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash})
        names = sorted(signed_headers)
        canonical = "\n".join([
            method, path, canonical_query,
            "".join(f"{n}:{signed_headers[n]}\n" for n in names),
            ";".join(names), payload_hash,
        ])
        scope = f"{day}/auto/s3/aws4_request"
        string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                                    hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
        signing_key = ("AWS4" + account.secret_access_key).encode("utf-8")
        for part in (day, "auto", "s3", "aws4_request"):
            signing_key = _hmac(signing_key, part)
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        sent = {n: v for n, v in signed_headers.items() if n != "host"}
        sent["authorization"] = (f"AWS4-HMAC-SHA256 Credential={account.access_key_id}/{scope}, "
                                 f"SignedHeaders={';'.join(names)}, Signature={signature}")
        request = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None,
                                         method=method, headers=sent)
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return 404, {}, b""
            if error.code in (429,) or error.code >= 500:
                last = error
                time.sleep(5 * (attempt + 1))
                continue
            detail = error.read()[:300].decode("utf-8", "replace")
            raise RuntimeError(f"{account.label()} {method} {key or account.bucket}: HTTP {error.code} {detail}")
        except (urllib.error.URLError, TimeoutError) as error:
            last = error
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{account.label()} unavailable for {method} {key or account.bucket}: {last}")


def r2_list(account, prefix=""):
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    token = None
    while True:
        query = {"list-type": "2", "prefix": prefix}
        if token:
            query["continuation-token"] = token
        _, _, body = r2_request(account, "GET", query=query)
        root = ET.fromstring(body)
        for item in root.findall("s3:Contents", ns):
            key = item.findtext("s3:Key", default="", namespaces=ns)
            modified = item.findtext("s3:LastModified", default="", namespaces=ns)
            try:
                size = int(item.findtext("s3:Size", default="0", namespaces=ns))
            except ValueError:
                size = 0
            try:
                stamp = calendar.timegm(time.strptime(modified[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                stamp = time.time()
            yield key, size, stamp
        if root.findtext("s3:IsTruncated", default="false", namespaces=ns) != "true":
            return
        token = root.findtext("s3:NextContinuationToken", namespaces=ns)
        if not token:
            return


def r2_inventory(problems):
    for account in R2_ACCOUNTS:
        try:
            account.objects = {key: (size, stamp) for key, size, stamp in r2_list(account)}
            account.used = sum(size for size, _ in account.objects.values())
        except Exception as error:
            account.objects = None
            problems.append(f"{account.label()}: could not be listed, not used this run ({error})")


def r2_key(listed, sha256):
    return f"{R2_PREFIX}/{listed.lower()}/{sha256}.zip"


def locate_url(url):
    if not isinstance(url, str):
        return None
    for account in R2_ACCOUNTS:
        prefix = account.public_url + "/"
        if url.startswith(prefix):
            return account, url[len(prefix):]
    return None


def publish_zip(listed, data, sha256, file_name):
    key = r2_key(listed, sha256)
    for account in R2_ACCOUNTS:
        if account.objects is not None and account.objects.get(key, (None,))[0] == len(data):
            return f"{account.public_url}/{key}"
    target = next((a for a in R2_ACCOUNTS if a.objects is not None and a.used + len(data) <= a.limit), None)
    if target is None:
        raise RuntimeError("every R2 account is full or unavailable; add an account or raise limit_gb")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", file_name) or "bundle.zip"
    r2_request(target, "PUT", key, body=data, headers={
        "Content-Type": "application/zip",
        "Cache-Control": "public, max-age=31536000, immutable",
        "Content-Disposition": f'attachment; filename="{safe_name}"',
        "x-amz-meta-sha256": sha256,
    })
    previous = target.objects.get(key, (0,))[0]
    target.objects[key] = (len(data), time.time())
    target.used += len(data) - previous
    return f"{target.public_url}/{key}"


def prune_r2(referenced, listed):
    cutoff = time.time() - R2_PRUNE_DAYS * 86400 if R2_PRUNE_DAYS > 0 else None
    removed_repos, removed_old = set(), 0
    for account in R2_ACCOUNTS:
        if account.objects is None:
            continue
        for key, (size, modified) in list(account.objects.items()):
            parts = key.split("/")
            if (len(parts) != 4 or parts[0] != R2_PREFIX or not key.endswith(".zip")
                    or (account.number, key) in referenced):
                continue
            repo = f"{parts[1]}/{parts[2]}"
            if repo in listed and (cutoff is None or modified >= cutoff):
                continue
            r2_request(account, "DELETE", key)
            account.objects.pop(key)
            account.used -= size
            if repo in listed:
                removed_old += 1
            else:
                removed_repos.add(repo)
    return sorted(removed_repos), removed_old


def _reject_constant(value):
    raise ValueError(value)


def parse_json(content):
    try:
        return json.loads(content.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None


def is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_int(value):
    return is_num(value) and float(value).is_integer()


def as_float(value):
    if is_num(value):
        number = float(value)
    elif isinstance(value, str) and NUMERIC.match(value):
        number = float(value.strip().rstrip("fFdD"))
    else:
        return None
    return number if math.isfinite(number) else None


def need_string(obj, key, where):
    value = obj.get(key) if isinstance(obj, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise Invalid(f"{where}: {key} is missing or not a text value")
    return value


def optional_string(obj, key, where):
    if key in obj and obj[key] is not None and not isinstance(obj[key], str):
        raise Invalid(f"{where}: {key} must be text")


def split_id(raw, where, field):
    split = raw.find(":")
    if split <= 0 or split == len(raw) - 1:
        raise Invalid(f"{where}: {field} must look like namespace:path")
    namespace, path = raw[:split], raw[split + 1:]
    if not RL_NAMESPACE.match(namespace) or not RL_PATH.match(path):
        raise Invalid(f"{where}: {field} is not a valid id ({raw})")
    return namespace, path


def check_vector(value, size, where):
    if not isinstance(value, list) or len(value) != size or any(as_float(v) is None for v in value):
        raise Invalid(f"{where}: expected {size} numbers")


def check_track(bone, channel, size, where):
    if channel not in bone:
        return
    track = bone[channel]
    if not isinstance(track, dict):
        raise Invalid(f"{where}: {channel} must be an object of keyframes")
    if len(track) > MAX_KEYFRAMES:
        raise Invalid(f"{where}: too many keyframes in {channel} (max {MAX_KEYFRAMES})")
    for time_key, frame in track.items():
        time = as_float(time_key)
        if time is None or time < 0 or time > MAX_ANIMATION_SECONDS:
            raise Invalid(f"{where}: invalid {channel} keyframe time {time_key!r}")
        if isinstance(frame, dict):
            vector = next((frame[k] for k in ("vector", "post", "pre") if k in frame), None)
        else:
            vector = frame
        try:
            check_vector(vector, size, where)
        except Invalid:
            raise Invalid(f"{where}: invalid {channel} keyframe at {time_key}")


def check_animation(data, key, where):
    if not isinstance(data, dict):
        raise Invalid(f"{where}: must be a JSON object")
    fmt = data.get("format_version")
    if not isinstance(fmt, str) or not fmt.strip():
        raise Invalid(f"{where}: format_version is missing")
    if fmt not in SUPPORTED_FORMATS:
        raise Invalid(f"{where}: format_version {fmt!r} is not supported (use one of {sorted(SUPPORTED_FORMATS)})")
    animations = data.get("animations")
    if not isinstance(animations, dict):
        raise Invalid(f"{where}: animations object is missing")
    animation = animations.get(key)
    if not isinstance(animation, dict):
        raise Invalid(f"{where}: animation key not found: {key}")
    length = as_float(animation.get("animation_length"))
    if length is None or length <= 0 or length > MAX_ANIMATION_SECONDS:
        raise Invalid(f"{where}: animation_length must be > 0 and <= {int(MAX_ANIMATION_SECONDS)}")
    if "loop" in animation and not isinstance(animation["loop"], bool):
        raise Invalid(f"{where}: loop must be true or false")
    bones = animation.get("bones", {})
    if not isinstance(bones, dict):
        raise Invalid(f"{where}: bones must be an object")
    for name, bone in bones.items():
        if name not in BONES:
            raise Invalid(f"{where}: unsupported bone {name!r}")
        if not isinstance(bone, dict):
            raise Invalid(f"{where}: bone {name} must be an object")
        check_track(bone, "rotation", 3, f"{where}#{name}")
        check_track(bone, "position", 3, f"{where}#{name}")
        check_track(bone, "bend", 2, f"{where}#{name}")
    return length


def check_asset(namespace, asset_namespace, entry, names, where, deferred=None):
    if asset_namespace == "minecraft":
        return
    if asset_namespace != namespace:
        raise Invalid(f"{where}: cross-bundle asset reference is not allowed ({asset_namespace})")
    if entry is not None and entry not in names:
        if deferred is not None:
            deferred.append((entry, where))
        else:
            raise Invalid(f"{where}: referenced asset is missing: {entry}")


def check_events(data, length, namespace, names, where, deferred=None):
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise Invalid(f"{where}: events must be an array")
    events = data["events"]
    if len(events) > MAX_EVENTS:
        raise Invalid(f"{where}: too many events (max {MAX_EVENTS})")
    for index, event in enumerate(events):
        at = f"{where}#events[{index}]"
        if not isinstance(event, dict):
            raise Invalid(f"{at}: event must be an object")
        time = as_float(event.get("time"))
        if time is None or time < 0 or time > length:
            raise Invalid(f"{at}: invalid time (0 to animation length)")
        kind = event.get("type")
        if not isinstance(kind, str) or kind not in EVENT_TYPES:
            raise Invalid(f"{at}: unknown event type {kind!r}")
        if "volume" in event and event["volume"] is not None:
            volume = as_float(event["volume"])
            if volume is None or volume < 0 or volume > 4:
                raise Invalid(f"{at}: volume must be 0 to 4")
        if kind == "reset_skin":
            continue
        raw = event.get("asset_id")
        if not isinstance(raw, str) or not raw.strip():
            raise Invalid(f"{at}: asset_id is missing")
        asset_ns, asset_path = split_id(raw, at, "asset_id")
        if kind == "spawn_particle" and "amount" in event and event["amount"] is not None:
            if not is_int(event["amount"]) or not 1 <= event["amount"] <= 1024:
                raise Invalid(f"{at}: amount must be 1 to 1024")
        entry = None
        if kind in ("play_music", "play_sound"):
            entry = f"assets/audio/{asset_path}.ogg"
            alt = event.get("dmca")
            if isinstance(alt, str) and alt.strip().lower() not in ("", "false"):
                alt = alt.strip()
                if 0 < alt.find(":") < len(alt) - 1:
                    alt_ns, alt_path = split_id(alt, at, "dmca")
                    check_asset(namespace, alt_ns, f"assets/audio/{alt_path}.ogg", names, at, deferred)
        elif kind == "render_model":
            entry = f"assets/models/{asset_path}.json"
        elif kind == "set_skin":
            entry = f"assets/textures/{asset_path}.png"
        check_asset(namespace, asset_ns, entry, names, at, deferred)


def check_pack(pack):
    if not isinstance(pack, dict):
        raise Invalid("pack.json must be a JSON object")
    if not is_int(pack.get("schema_version")) or pack["schema_version"] != SCHEMA_VERSION:
        raise Invalid(f"pack.json schema_version is required and must be {SCHEMA_VERSION}")
    minimum = pack.get("min_mod_version")
    if not isinstance(minimum, str) or not SEMVER.match(minimum):
        raise Invalid("pack.json min_mod_version is required and must be x.y.z")
    bundle = pack.get("bundle")
    if not isinstance(bundle, dict):
        raise Invalid("pack.json has no bundle object")
    for key in ("namespace", "id", "title", "author", "version"):
        need_string(bundle, key, "pack.json bundle")
    if not IDENT.match(bundle["namespace"]) or not IDENT.match(bundle["id"]):
        raise Invalid("pack.json namespace / id may only contain a-z 0-9 _ . -")
    if not SEMVER.match(bundle["version"]):
        raise Invalid("pack.json bundle.version must be x.y.z")
    optional_string(bundle, "description", "pack.json bundle")
    if "collection" in bundle:
        collection = bundle["collection"]
        if not isinstance(collection, dict) or set(collection) != {"order", "total"}:
            raise Invalid('pack.json bundle.collection must be exactly {"order": 1, "total": 2}')
        if not is_int(collection["order"]) or not is_int(collection["total"]):
            raise Invalid("pack.json bundle.collection order and total must be whole numbers")
        if not 2 <= collection["total"] <= MAX_PARTS:
            raise Invalid(f"pack.json bundle.collection total must be 2 to {MAX_PARTS}")
        if not 1 <= collection["order"] <= collection["total"]:
            raise Invalid("pack.json bundle.collection order must be 1 to total")
    rig = bundle.get("rig_type")
    if rig is not None and (not isinstance(rig, str) or rig.strip().lower() not in ("r6", "r15")):
        raise Invalid("pack.json bundle.rig_type must be \"r6\" or \"r15\"")
    return bundle


def validate_zip(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise Invalid("release asset is not a valid zip")
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise Invalid("too many files in zip")
    seen, total, icon = set(), 0, None
    for info in infos:
        name = info.filename
        if name in seen:
            raise Invalid(f"duplicate entry: {name}")
        seen.add(name)
        if name.startswith("/") or "\\" in name or ".." in name.split("/") or ":" in name or not SAFE_PATH.match(name):
            raise Invalid(f"unsafe path: {name!r} (lowercase a-z 0-9 _ . - / only)")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise Invalid(f"symbolic link: {name}")
        if info.flag_bits & 0x1:
            raise Invalid(f"encrypted entry: {name}")
        if name.endswith("/"):
            continue
        kind = next((kind for pattern, kind in ALLOWED if pattern.match(name)), None)
        if kind is None:
            raise Invalid(f"file type or location not allowed: {name}")
        total += info.file_size
        if total > MAX_EXPANDED_BYTES:
            raise Invalid(f"expanded zip is larger than {MAX_EXPANDED_BYTES // (1024 * 1024)} MB")
        if info.file_size > 1024 * 1024 and info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
            raise Invalid(f"suspicious compression ratio: {name}")
        if kind == "json" and info.file_size > MAX_JSON_BYTES:
            raise Invalid(f"{name} is larger than {MAX_JSON_BYTES // (1024 * 1024)} MB")
        content = archive.read(info)
        if kind == "json":
            if parse_json(content) is None:
                raise Invalid(f"not valid JSON: {name}")
        elif not content.startswith(MAGIC[kind]):
            raise Invalid(f"not a real .{kind} file: {name}")
        if name == "icon.png":
            if len(content) > MAX_ICON_BYTES:
                raise Invalid("icon.png is larger than 512 KB")
            icon = content

    if "pack.json" not in seen:
        raise Invalid("pack.json must be at the zip root")
    pack = parse_json(archive.read("pack.json"))
    bundle = check_pack(pack)
    namespace = bundle["namespace"]
    deferred = [] if "collection" in bundle else None

    folders = sorted({n.split("/")[1] for n in seen if n.startswith("emotes/") and n.count("/") == 2})
    with_info = {f for f in folders if f"emotes/{f}/info.json" in seen}
    if folders and set(folders) != with_info:
        raise Invalid("emote folder without info.json: " + ", ".join(sorted(set(folders) - with_info)))
    if not folders:
        raise Invalid("bundle contains no emotes")
    if len(folders) > MAX_EMOTES:
        raise Invalid(f"too many emotes ({len(folders)}, max {MAX_EMOTES})")

    emote_ids = set()
    for folder in folders:
        base = f"emotes/{folder}/"
        info = parse_json(archive.read(base + "info.json"))
        if not isinstance(info, dict):
            raise Invalid(f"{base}info.json must be a JSON object")
        local = need_string(info, "id", base + "info.json")
        key = need_string(info, "animation", base + "info.json")
        need_string(info, "name", base + "info.json")
        rarity = need_string(info, "rarity", base + "info.json")
        if rarity.strip().upper() not in RARITIES:
            raise Invalid(f"{base}info.json: rarity must be one of {', '.join(r.lower() for r in sorted(RARITIES))}")
        if "op" in info and (not is_int(info["op"]) or not 0 <= info["op"] <= 4):
            raise Invalid(f"{base}info.json: op must be a whole number from 0 to 4")
        optional_string(info, "description", base + "info.json")
        if not RL_PATH.match(local):
            raise Invalid(f"{base}info.json: id {local!r} may only contain a-z 0-9 _ . - /")
        if local in emote_ids:
            raise Invalid(f"duplicate emote id: {local}")
        emote_ids.add(local)
        if base + "animation.json" not in seen:
            raise Invalid(f"{base}animation.json is missing")
        length = check_animation(parse_json(archive.read(base + "animation.json")), key, base + "animation.json")
        if base + "events.json" in seen:
            check_events(parse_json(archive.read(base + "events.json")), length, namespace, seen,
                         base + "events.json", deferred)
    return pack, sorted(emote_ids), icon, seen, deferred or []


def read_source(path):
    if not SOURCE_NAME.match(path.name):
        raise Invalid("file name must be your lowercase GitHub username followed by .json")
    try:
        source = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise Invalid("not valid JSON")
    repos = source.get("repos") if isinstance(source, dict) else None
    if not isinstance(source, dict) or set(source) != {"repos"} or not isinstance(repos, list):
        raise Invalid('must be exactly {"repos": ["owner/name", ...]}')
    if not 1 <= len(repos) <= MAX_REPOS_PER_SOURCE:
        raise Invalid(f"list between 1 and {MAX_REPOS_PER_SOURCE} repositories")
    seen = set()
    for repo in repos:
        if not isinstance(repo, str) or not REPO_NAME.match(repo):
            raise Invalid(f"repository must look like owner/name: {repo!r}")
        if repo.split("/")[1] in (".", ".."):
            raise Invalid(f"invalid repository name: {repo}")
        if repo.lower() in seen:
            raise Invalid(f"repository listed twice: {repo}")
        seen.add(repo.lower())
    return repos


def resolve(repo, pinned):
    if pinned:
        meta = api(f"/repositories/{pinned['repo_id']}")
        if meta["owner"]["id"] != pinned["owner_id"]:
            raise Gone("repository was transferred to another account")
    else:
        meta = api(f"/repos/{repo}")
    if meta.get("private") or meta.get("disabled"):
        raise Gone("repository is private or disabled")
    try:
        release = api(f"/repositories/{meta['id']}/releases/latest")
    except Gone:
        raise Invalid("repository has no published release")
    zips = [a for a in release.get("assets", []) if a["name"].lower().endswith(".zip") and a.get("state") == "uploaded"]
    if not 1 <= len(zips) <= MAX_ZIPS_PER_RELEASE:
        raise Invalid(f"latest release must contain 1 to {MAX_ZIPS_PER_RELEASE} .zip files (found {len(zips)})")
    return meta, release, sorted(zips, key=lambda a: a["name"])


def org_has_public_member(org, user):
    request = urllib.request.Request(f"{API}/orgs/{org}/public_members/{user}", headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "universal-emotes-catalog",
        **({"Authorization": "Bearer " + TOKEN} if TOKEN else {}),
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status == 204
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise


def check_submitter(meta, author):
    if not author:
        return
    author = author.lower()
    owner = meta["owner"]["login"].lower()
    if author == owner or (CATALOG_OWNER and author == CATALOG_OWNER):
        return
    if meta["owner"].get("type") == "Organization" and org_has_public_member(owner, author):
        return
    raise Invalid(
        f"the pull request author ({author}) does not own {meta['full_name']}; only the owner of a "
        "repository can add it (for an organization, make your membership public in the organization's People page)")


def build_entry(stem, listed, meta, release, asset, publish=False):
    if asset["size"] > MAX_ZIP_BYTES:
        raise Invalid(f"{asset['name']}: larger than {MAX_ZIP_BYTES // (1024 * 1024)} MB")
    data = download(asset["browser_download_url"], MAX_ZIP_BYTES)
    try:
        pack, emote_ids, icon, names, deferred = validate_zip(data)
    except Invalid as error:
        raise Invalid(f"{asset['name']}: {error}")
    bundle = pack["bundle"]
    description = bundle.get("description", "")
    rig_type = (bundle.get("rig_type") or "r6").strip().lower()
    entry = {
        "key": f"{listed}/{bundle['namespace']}.{bundle['id']}",
        "source": stem,
        "source_repo": listed,
        "repo": meta["full_name"],
        "repo_url": meta["html_url"],
        "official": meta["owner"]["login"].lower() == CATALOG_OWNER,
        "namespace": bundle["namespace"],
        "id": bundle["id"],
        "title": bundle["title"],
        "author": bundle["author"],
        "description": description if isinstance(description, str) else "",
        "rig_type": rig_type,
        "version": bundle["version"],
        "schema_version": pack["schema_version"],
        "min_mod_version": pack["min_mod_version"],
        "emotes": len(emote_ids),
        "emote_ids": emote_ids,
        "release_tag": release["tag_name"],
        "published_at": release.get("published_at"),
        "file_name": asset["name"],
        "download_url": asset["browser_download_url"],
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "icon_url": None,
    }
    if publish:
        entry["download_url"] = publish_zip(listed, data, entry["sha256"], asset["name"])
    if "collection" in bundle:
        entry["collection"] = dict(bundle["collection"])
        entry["key"] += f".p{bundle['collection']['order']}"
    return entry, icon, {"pack": pack, "names": names, "deferred": deferred}


def check_collection(parts):
    total = parts[0][0]["collection"]["total"]
    orders = sorted(e["collection"]["order"] for e, _, _ in parts)
    name = f"{parts[0][0]['namespace']}:{parts[0][0]['id']}"
    if orders != list(range(1, total + 1)):
        raise Invalid(f"{name}: the release must contain every part exactly once "
                      f"(found parts {orders}, expected 1 to {total})")

    def shared(pack):
        bundle = {k: v for k, v in pack["bundle"].items() if k != "collection"}
        return {**{k: v for k, v in pack.items() if k != "bundle"}, "bundle": bundle}

    first = shared(parts[0][2]["pack"])
    for entry, _, extra in parts[1:]:
        if shared(extra["pack"]) != first or entry["collection"]["total"] != total:
            raise Invalid(f"{name}: every part must have identical pack.json bundle info "
                          "(only collection.order may differ)")
    seen_emotes, seen_assets = {}, {}
    union = set()
    for entry, _, extra in parts:
        union |= extra["names"]
        for emote in entry["emote_ids"]:
            if emote in seen_emotes:
                raise Invalid(f"{name}: emote id {emote!r} is used by two parts")
            seen_emotes[emote] = entry
        for asset in (n for n in extra["names"] if n.startswith("assets/") and not n.endswith("/")):
            if asset in seen_assets:
                raise Invalid(f"{name}: {asset} exists in two parts; keep each shared file in one part only")
            seen_assets[asset] = entry
    for entry, _, extra in parts:
        for asset, where in extra["deferred"]:
            if asset not in union:
                raise Invalid(f"{where}: referenced asset is missing in every part: {asset}")


def build_entries(stem, listed, meta, release, assets, publish=False):
    built = [build_entry(stem, listed, meta, release, asset, publish) for asset in assets]
    groups = {}
    for item in built:
        groups.setdefault((item[0]["namespace"], item[0]["id"]), []).append(item)
    for parts in groups.values():
        if len(parts) > 1 and any("collection" not in e for e, _, _ in parts):
            raise Invalid("two zips in the release have the same bundle namespace and id "
                          "(parts of one bundle need bundle.collection)")
        if "collection" in parts[0][0]:
            check_collection(parts)
    return [(e, None if "collection" in e and e["collection"]["order"] != 1 else icon) for e, icon, _ in built]


def clash_with(entry, others):
    owner = entry["repo"].split("/")[0].lower()
    for other in others:
        if other["key"] == entry["key"]:
            continue
        if other.get("repo", "").lower() == entry["repo"].lower():
            continue
        if (other["namespace"], other["id"]) == (entry["namespace"], entry["id"]):
            if "collection" in entry and "collection" in other and other["source_repo"] == entry["source_repo"]:
                continue
            return other
        if other["namespace"] == entry["namespace"] and other["repo"].split("/")[0].lower() != owner:
            return other
        if other["namespace"] == entry["namespace"] and set(other.get("emote_ids", [])) & set(entry.get("emote_ids", [])):
            return other
    return None


def load_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def icon_dir(listed):
    owner, name = listed.lower().split("/")
    return ICONS / owner / name


def icon_path(listed, entry):
    return f"icons/{listed.lower()}/{entry['namespace']}.{entry['id']}.png"


def remove_icons(listed):
    shutil.rmtree(icon_dir(listed), ignore_errors=True)


def prune_icons(live):
    if not ICONS.is_dir():
        return
    for stray in ICONS.glob("*.png"):
        stray.unlink()
    for owner_dir in [d for d in ICONS.iterdir() if d.is_dir()]:
        for stray in owner_dir.glob("*.png"):
            stray.unlink()
        for name_dir in [d for d in owner_dir.iterdir() if d.is_dir()]:
            if f"{owner_dir.name}/{name_dir.name}" not in live:
                shutil.rmtree(name_dir, ignore_errors=True)
            else:
                for leftover in [d for d in name_dir.iterdir() if d.is_dir()]:
                    shutil.rmtree(leftover, ignore_errors=True)
        if not any(owner_dir.iterdir()):
            owner_dir.rmdir()


def load_catalog_files():
    state = load_json(STATE, None)
    if state is None:
        state = load_json(LEGACY_STATE, {})
    index = load_json(INDEX, None)
    if index is None:
        index = load_json(LEGACY_INDEX, {})
    return state, index.get("bundles", [])


def load_owners():
    owners = load_json(OWNERS, {})
    if not isinstance(owners, dict):
        return {}
    return {stem: uid for stem, uid in owners.items() if isinstance(uid, int) and not isinstance(uid, bool)}


def record_owners(owners):
    stems = {p.stem for p in SOURCES.glob("*.json")}
    for stem in sorted(stems - set(owners)):
        try:
            owners[stem] = int(api(f"/users/{stem}")["id"])
        except Gone:
            print(f"::warning::{stem}: no GitHub account with this name, the file has no recorded owner")
        except Exception as error:
            print(f"::warning::{stem}: owner not recorded this run ({error})")
    for stem in list(owners):
        if stem not in stems:
            owners.pop(stem)
    OWNERS.write_text(json.dumps(owners, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cmd_build():
    try:
        R2_ACCOUNTS[:] = load_r2_accounts()
    except ValueError as error:
        sys.exit(f"R2 is not configured: {error}")
    state, old_bundles = load_catalog_files()
    for pinned in state.values():
        pinned.pop("missing", None)
        pinned.pop("countdown", None)
    previous = {}
    for old_entry in old_bundles:
        previous.setdefault(old_entry.get("source_repo"), []).append(old_entry)
    entries, problems = [], []
    r2_inventory(problems)
    live, listed_keys = set(), set()
    ICONS.mkdir(exist_ok=True)
    DATA.mkdir(exist_ok=True)

    known_sources = {e.get("source") for e in old_bundles}
    for path in sorted(SOURCES.glob("*.json"), key=lambda p: (p.stem not in known_sources, p.stem)):
        stem = path.stem
        try:
            repos = read_source(path)
        except Invalid as error:
            problems.append(f"{stem}: file ignored ({error})")
            for old_entry in old_bundles:
                if old_entry.get("source") == stem:
                    entries.append(old_entry)
                    listed_keys.add(old_entry["source_repo"])
                    live.add(old_entry["source_repo"])
            continue
        for listed in repos:
            key = listed.lower()
            label = f"{stem}/{key}"
            if key in listed_keys:
                problems.append(f"{label}: ignored, this repository is already listed by another author")
                continue
            listed_keys.add(key)
            live.add(key)
            pinned = state.get(key)
            try:
                meta, release, assets = resolve(listed, pinned)
                if pinned is None:
                    pinned = state[key] = {"repo_id": meta["id"], "owner_id": meta["owner"]["id"]}
                cache_key = "|".join(f"{a['id']}:{a['updated_at']}:{a['size']}" for a in assets)
                if (key in previous and pinned.get("release") == cache_key
                        and all(e.get("icon_url") is None or e["icon_url"].endswith("/" + icon_path(key, e))
                                for e in previous[key])
                        and all(locate_url(e.get("download_url")) for e in previous[key])):
                    current = [dict(e, repo=meta["full_name"], repo_url=meta["html_url"], source=stem)
                               for e in previous[key]]
                else:
                    built = build_entries(stem, key, meta, release, assets, publish=True)
                    remove_icons(key)
                    current = []
                    for entry, icon in built:
                        if icon:
                            folder = icon_dir(key)
                            folder.mkdir(parents=True, exist_ok=True)
                            icon_name = f"{entry['namespace']}.{entry['id']}.png"
                            (folder / icon_name).write_bytes(icon)
                            if CATALOG_REPO:
                                entry["icon_url"] = (f"https://raw.githubusercontent.com/{CATALOG_REPO}/"
                                                     f"{BRANCH}/{icon_path(key, entry)}")
                        current.append(entry)
                    pinned["release"] = cache_key
                    icons = {(e["namespace"], e["id"]): e["icon_url"] for e in current if e["icon_url"]}
                    for e in current:
                        if "collection" in e and not e["icon_url"]:
                            e["icon_url"] = icons.get((e["namespace"], e["id"]))
                entries.extend(current)
            except Gone as error:
                problems.append(f"{label}: unavailable on GitHub ({error}); last published version kept, "
                                "remove it from the source file with a pull request to delete it")
                entries.extend(previous.get(key, []))
            except Invalid as error:
                problems.append(f"{label}: latest release rejected ({error})")
                entries.extend(previous.get(key, []))
            except Exception as error:
                problems.append(f"{label}: skipped this run ({error})")
                entries.extend(previous.get(key, []))

    record_owners(load_owners())

    groups = {}
    for entry in sorted(entries, key=lambda e: (not e["official"], e["source_repo"] not in previous, e["key"])):
        group = (entry["source_repo"], entry["namespace"], entry["id"]) if "collection" in entry else entry["key"]
        groups.setdefault(group, []).append(entry)
    accepted = []
    for members in groups.values():
        clash = next((c for c in (clash_with(m, accepted) for m in members) if c), None)
        if clash:
            for member in members:
                problems.append(f"{member['key']}: hidden, {member['namespace']}:{member['id']} belongs to {clash['source_repo']}")
        else:
            accepted.extend(members)
    accepted.sort(key=lambda e: (not e["official"], e["title"].lower(), e["key"]))

    for known in list(state):
        if known not in listed_keys:
            state.pop(known)
    prune_icons(live)
    try:
        referenced = {(found[0].number, found[1]) for found in
                      (locate_url(e.get("download_url")) for e in entries + accepted) if found}
        removed_repos, removed_old = prune_r2(referenced, live)
        for repo in removed_repos:
            print(f"{repo}: removed from sources/, its zips were deleted from R2")
        if removed_old:
            print(f"{removed_old} zip(s) of older releases deleted from R2")
    except Exception as error:
        problems.append(f"R2 cleanup skipped this run ({error})")

    if old_bundles != accepted or not INDEX.exists():
        INDEX.write_text(json.dumps({
            "schema": 1,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "bundles": accepted,
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    total_emotes = sum(e.get("emotes", 0) for e in accepted)
    BADGE.write_text(json.dumps({
        "schemaVersion": 1,
        "label": "Current Emote Count",
        "labelColor": "#2c2e33",
        "message": f"{total_emotes:,}",
        "color": "#555555",
        "style": "for-the-badge",
        "logo": "data:image/svg+xml;base64,PD94bWwgdmVyc2lvbj0iMS4wIiBlbmNvZGluZz0iaXNvLTg4NTktMSI/Pgo8c3ZnIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyIgeG1sbnM6eGxpbms9Imh0dHA6Ly93d3cudzMub3JnLzE5OTkveGxpbmsiIHZlcnNpb249IjEuMSIgaWQ9IkNhcGFfMSIgeD0iMHB4IiB5PSIwcHgiIHZpZXdCb3g9IjAgMCA1MTIuMDU2IDUxMi4wNTYiIHN0eWxlPSJlbmFibGUtYmFja2dyb3VuZDpuZXcgMCAwIDUxMi4wNTYgNTEyLjA1NjsiIHhtbDpzcGFjZT0icHJlc2VydmUiPgo8ZyBmaWxsPSIjRkZGRkZGIj4KCTxnPgoJCTxnPgoJCQk8cGF0aCBkPSJNNDI2LjYzNSwxODguMjI0QzQwMi45NjksOTMuOTQ2LDMwNy4zNTgsMzYuNzA0LDIxMy4wOCw2MC4zN0MxMzkuNDA0LDc4Ljg2NSw4NS45MDcsMTQyLjU0Miw4MC4zOTUsMjE4LjMwMyAgICAgQzI4LjA4MiwyMjYuOTMtNy4zMzMsMjc2LjMzMSwxLjI5NCwzMjguNjQ0YzcuNjY5LDQ2LjUwNyw0Ny45NjcsODAuNTY2LDk1LjEwMSw4MC4zNzloODB2LTMyaC04MGMtMzUuMzQ2LDAtNjQtMjguNjU0LTY0LTY0ICAgICBjMC0zNS4zNDYsMjguNjU0LTY0LDY0LTY0YzguODM3LDAsMTYtNy4xNjMsMTYtMTZjLTAuMDgtNzkuNTI5LDY0LjMyNy0xNDQuMDY1LDE0My44NTYtMTQ0LjE0NCAgICAgYzY4Ljg0NC0wLjA2OSwxMjguMTA3LDQ4LjYwMSwxNDEuNDI0LDExNi4xNDRjMS4zMTUsNi43NDQsNi43ODgsMTEuODk2LDEzLjYsMTIuOGM0My43NDIsNi4yMjksNzQuMTUxLDQ2LjczOCw2Ny45MjMsOTAuNDc5ICAgICBjLTUuNTkzLDM5LjI3OC0zOS4xMjksNjguNTIzLTc4LjgwMyw2OC43MjFoLTY0djMyaDY0YzYxLjg1Ni0wLjE4NywxMTEuODQ4LTUwLjQ4MywxMTEuNjYtMTEyLjMzOSAgICAgQzUxMS44OTksMjQ1LjE5NCw0NzYuNjU1LDIwMC40NDMsNDI2LjYzNSwxODguMjI0eiI+PC9wYXRoPgoJCQk8cGF0aCBkPSJNMjQ1LjAzNSwyNTMuNjY0bC02NCw2NGwyMi41NiwyMi41NmwzNi44LTM2LjY0djE1My40NGgzMnYtMTUzLjQ0bDM2LjY0LDM2LjY0bDIyLjU2LTIyLjU2bC02NC02NCAgICAgQzI2MS4zNTQsMjQ3LjQ2LDI1MS4yNzYsMjQ3LjQ2LDI0NS4wMzUsMjUzLjY2NHoiPjwvcGF0aD4KCQk8L2c+Cgk8L2c+CjwvZz4KPC9zdmc+Cg==",
    }, indent=2) + "\n", encoding="utf-8")
    for legacy in (LEGACY_INDEX, LEGACY_STATE):
        legacy.unlink(missing_ok=True)

    print(f"{len(accepted)} bundle(s) in the catalog")
    for account in R2_ACCOUNTS:
        if account.objects is not None:
            print(f"{account.label()}: {account.used / 1024 ** 3:.2f} / {account.limit / 1024 ** 3:.2f} GB used")
    for problem in problems:
        print("::warning::" + problem)


def base_repos(path):
    try:
        relative = path.resolve().relative_to(ROOT).as_posix()
        raw = subprocess.run(["git", "show", f"HEAD:{relative}"], cwd=ROOT, check=True,
                             capture_output=True, text=True).stdout
        return {r.lower() for r in json.loads(raw).get("repos", [])}
    except Exception:
        return set()


def ownership_error(stem, owners, author, author_id):
    if not author or author == CATALOG_OWNER:
        return None
    if stem in owners:
        if author_id is not None and owners[stem] == author_id:
            return None
        return f"sources/{stem}.json belongs to another GitHub account"
    if author == stem:
        return None
    return f"this file belongs to GitHub user {stem}; your file is sources/{author}.json"


def listed_after_pr():
    listing = {}
    for path in SOURCES.glob("*.json"):
        try:
            repos = read_source(path)
        except Invalid:
            continue
        for repo in repos:
            listing.setdefault(repo.lower(), []).append(path.stem)
    return listing


def cmd_check(files):
    state, existing = load_catalog_files()
    owners = load_owners()
    author = os.environ.get("PR_AUTHOR", "").lower()
    raw_id = os.environ.get("PR_AUTHOR_ID", "")
    author_id = int(raw_id) if raw_id.isdigit() else None
    failed = False
    listing = listed_after_pr()
    existing = [e for e in existing if e.get("source_repo") in listing]
    taken = set()
    pending = []
    for file in files:
        path = Path(file)
        try:
            if path.parent.resolve() != SOURCES.resolve():
                raise Invalid("only files directly inside sources/ may be changed")
            if not SOURCE_NAME.match(path.name):
                raise Invalid("file name must be a lowercase GitHub username followed by .json")
            problem = ownership_error(path.stem, owners, author, author_id)
            if problem:
                raise Invalid(problem)
            if not path.exists():
                print(f"OK {path.name}: removed")
                continue
            repos = read_source(path)
            for repo in repos:
                others = [s for s in listing.get(repo.lower(), []) if s != path.stem]
                if others:
                    raise Invalid(f"{repo} is also listed in sources/{others[0]}.json")
            owner_id = owners.get(path.stem, author_id)
            own_files = {path.stem} | {s for s, uid in owners.items() if owner_id is not None and uid == owner_id}
            known = set().union(*(base_repos(SOURCES / f"{s}.json") for s in own_files))
            new_repos = [r for r in repos if r.lower() not in known]
            if not new_repos:
                print(f"OK {path.name}: no new repositories")
            for listed in new_repos:
                try:
                    meta, release, assets = resolve(listed, None)
                    check_submitter(meta, author)
                    claimed = {s.get("repo_id") for k, s in state.items() if k in listing and k != listed.lower()}
                    if meta["id"] in claimed or meta["id"] in taken:
                        raise Invalid("this repository is already in the catalog")
                    taken.add(meta["id"])
                    for entry, _ in build_entries(path.stem, listed.lower(), meta, release, assets):
                        clash = clash_with(entry, existing + pending)
                        if clash:
                            raise Invalid(f"{entry['namespace']}:{entry['id']} is already used by "
                                          f"{clash['source_repo']}; choose your own namespace and id in pack.json")
                        pending.append(entry)
                        print(f"OK {listed}: {entry['title']} {entry['version']} by {entry['author']}, "
                              f"{entry['emotes']} emote(s), {entry['size'] // 1024} KB")
                except (Gone, Invalid) as error:
                    failed = True
                    print(f"::error file={file}::{listed}: {error}")
        except (Gone, Invalid) as error:
            failed = True
            print(f"::error file={file}::{error}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "build":
        cmd_build()
    elif len(sys.argv) >= 3 and sys.argv[1] == "check":
        cmd_check(sys.argv[2:])
    else:
        sys.exit("usage: workflow.py build | workflow.py check <sources/file.json>...")
