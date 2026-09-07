from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from media_meta import dimension_text, normalize_ratio, ratio_code, read_dimensions


BASE_TOKEN = "PqOdbu9eIa6UiVsed73cMUKonib"
LARK_FALLBACK = r"C:\Users\rose\AppData\Roaming\npm\lark-cli.cmd"

TEMPLATE_TABLE = "命名模板库"
PREVIEW_TABLE = "预览整理表"
MEMORY_TABLE = "命名修正记忆"
NORMALIZATION_EXPLANATION_FIELD = "归一化说明"
NORMALIZATION_RULES_FIELD = "归一化规则"
RATIO_FORMAT_FIELD = "比例格式"
RESERVED_VALUE_DICTIONARY_KEYS = {RATIO_FORMAT_FIELD}
SCHEMA_CACHE_VERSION = 1
BATCH_RECORD_LIMIT = 200

TEMPLATE_FIELDS = [
    "项目名称",
    "模板名称",
    "命名模板",
    "字段取值字典",
    NORMALIZATION_EXPLANATION_FIELD,
    NORMALIZATION_RULES_FIELD,
    "优先级",
    "是否启用",
]

MEMORY_FIELDS = [
    "项目名称",
    "原始命名片段",
    "初始建议新文件名",
    "人工最终新文件名",
    "修正原因",
    "记录时间",
]

PREVIEW_FIELDS = [
    "项目名称",
    "文件路径",
    "文件原名",
    "是否命中",
    "建议新文件名",
    "初始建议新文件名",
    "执行状态",
    "存档路径",
    "处理备注",
]

MEDIA_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".webm",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".mp3",
    ".wav",
    ".aac",
    ".flac",
}

EXCLUDED_NAMES = {
    "thumbs.db",
    ".ds_store",
    "desktop.ini",
    "命名规范.txt",
    "整理预览.md",
    "操作记录.csv",
}

MEDIA_FIELD_NAMES = {"比例", "尺寸", "分辨率"}

AUTO_RULE_PREFIX = "自动-"
AUTO_RULE_STATUS = "待确认"
DEFAULT_RATIOS = ["11", "169", "916", "45"]
DEFAULT_LANGUAGES = ["EN", "RU"]


class AppError(RuntimeError):
    pass


@dataclass(frozen=True)
class FieldSpec:
    name: str
    optional: bool
    prefix: str


@dataclass
class Template:
    raw: str
    fields: list[FieldSpec]
    suffix: str


def find_lark_cli() -> str:
    found = shutil.which("lark-cli")
    if found:
        return found
    fallback = Path(LARK_FALLBACK)
    if fallback.exists():
        return str(fallback)
    raise AppError("lark-cli not found. Install lark-cli or configure the fallback path.")


def run_lark(args: list[str]) -> dict[str, Any]:
    cli = find_lark_cli()
    argv = [cli, *args, "--as", "user"]
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stdout = completed.stdout.strip()
    if completed.returncode != 0:
        detail = completed.stderr.strip() or stdout or f"exit code {completed.returncode}"
        raise AppError(f"lark-cli failed: {detail}")
    if not stdout:
        return {"ok": True}
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AppError(f"lark-cli returned non-JSON output: {stdout[:500]}") from exc
    if payload.get("ok") is False:
        raise AppError(json.dumps(payload.get("error", payload), ensure_ascii=False))
    return payload


def _local_state_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    base = Path(root) if root else Path.home()
    return base / "ad_creative_naming_organizer"


def _schema_cache_path(base_token: str) -> Path:
    digest = hashlib.sha256(base_token.encode("utf-8")).hexdigest()[:16]
    return _local_state_dir() / f"schema_{digest}.json"


def _load_schema_cache(base_token: str) -> dict[str, Any] | None:
    cache_path = _schema_cache_path(base_token)
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        isinstance(payload, dict)
        and payload.get("version") == SCHEMA_CACHE_VERSION
        and payload.get("base_token") == base_token
        and isinstance(payload.get("tables"), dict)
    ):
        return payload
    return None


def _save_schema_cache(base_token: str, tables: dict[str, str]) -> None:
    cache_dir = _local_state_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = _schema_cache_path(base_token)
    temp_path = cache_path.with_suffix(".tmp")
    payload = {
        "version": SCHEMA_CACHE_VERSION,
        "base_token": base_token,
        "tables": tables,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(cache_path)
    except OSError:
        try:
            temp_path.unlink()
        except OSError:
            pass


def ensure_schema(
    base_token: str = BASE_TOKEN,
    refresh: bool = False,
) -> dict[str, str]:
    if not refresh:
        cached = _load_schema_cache(base_token)
        if cached is not None:
            return dict(cached["tables"])
    tables = _ensure_schema_online(base_token)
    _save_schema_cache(base_token, tables)
    return tables


def _ensure_schema_online(base_token: str) -> dict[str, str]:
    payload = run_lark(
        ["base", "+table-list", "--base-token", base_token, "--limit", "100"]
    )
    tables = {
        item["name"]: item["id"]
        for item in payload["data"]["tables"]
        if item["name"] != "命名规范库"
    }

    template_fields = [
        {"name": "项目名称", "type": "text"},
        {"name": "模板名称", "type": "text"},
        {"name": "命名模板", "type": "text"},
        {"name": "字段取值字典", "type": "text"},
        {"name": NORMALIZATION_EXPLANATION_FIELD, "type": "text"},
        {"name": NORMALIZATION_RULES_FIELD, "type": "text"},
        {"name": "示例", "type": "text"},
        {
            "name": "优先级",
            "type": "number",
            "style": {"type": "plain", "precision": 0},
        },
        {"name": "是否启用", "type": "text"},
    ]
    memory_fields = [
        {"name": "项目名称", "type": "text"},
        {"name": "原始命名片段", "type": "text"},
        {"name": "初始建议新文件名", "type": "text"},
        {"name": "人工最终新文件名", "type": "text"},
        {"name": "修正原因", "type": "text"},
        {"name": "记录时间", "type": "text"},
    ]

    for table_name, fields in (
        (TEMPLATE_TABLE, template_fields),
        (MEMORY_TABLE, memory_fields),
    ):
        if table_name not in tables:
            payload = run_lark(
                [
                    "base",
                    "+table-create",
                    "--base-token",
                    base_token,
                    "--name",
                    table_name,
                    "--fields",
                    json.dumps(fields, ensure_ascii=False),
                ]
            )
            table = payload.get("data", {}).get("table") or payload.get("data", {})
            table_id = table.get("table_id") or table.get("id")
            if not table_id:
                raise AppError(f"Created table {table_name} but did not get a table id.")
            tables[table_name] = table_id

    ensure_fields(
        base_token,
        tables[TEMPLATE_TABLE],
        [
            {"name": NORMALIZATION_EXPLANATION_FIELD, "type": "text"},
            {"name": NORMALIZATION_RULES_FIELD, "type": "text"},
        ],
    )

    preview_extra_fields = [
        {"name": "初始建议新文件名", "type": "text"},
        {"name": "执行状态", "type": "text"},
        {"name": "存档路径", "type": "text"},
        {"name": "处理备注", "type": "text"},
    ]
    preview_table_id = tables.get(PREVIEW_TABLE)
    if not preview_table_id:
        raise AppError(f"Missing required table: {PREVIEW_TABLE}")
    existing_fields = list_fields(base_token, preview_table_id)
    existing_names = {field["name"] for field in existing_fields}
    for field in preview_extra_fields:
        if field["name"] not in existing_names:
            run_lark(
                [
                    "base",
                    "+field-create",
                    "--base-token",
                    base_token,
                    "--table-id",
                    preview_table_id,
                    "--json",
                    json.dumps(field, ensure_ascii=False),
                ]
            )

    return tables


def list_fields(base_token: str, table_id: str) -> list[dict[str, Any]]:
    payload = run_lark(
        [
            "base",
            "+field-list",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--limit",
            "200",
        ]
    )
    return payload.get("data", {}).get("fields", [])


def ensure_fields(
    base_token: str,
    table_id: str,
    fields: list[dict[str, Any]],
) -> None:
    existing_names = {field["name"] for field in list_fields(base_token, table_id)}
    for field in fields:
        if field["name"] in existing_names:
            continue
        run_lark(
            [
                "base",
                "+field-create",
                "--base-token",
                base_token,
                "--table-id",
                table_id,
                "--json",
                json.dumps(field, ensure_ascii=False),
            ]
        )


def _project_filter(project: str) -> dict[str, Any]:
    return {
        "logic": "and",
        "conditions": [["项目名称", "==", project]],
    }


def _active_preview_filter(project: str) -> dict[str, Any]:
    return {
        "logic": "and",
        "conditions": [
            ["项目名称", "==", project],
            ["执行状态", "!=", "已执行"],
        ],
    }


def list_records(
    base_token: str,
    table_id: str,
    fields: list[str] | None = None,
    filter_json: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    offset = 0
    while True:
        args = [
            "base",
            "+record-list",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--limit",
            str(BATCH_RECORD_LIMIT),
            "--offset",
            str(offset),
            "--format",
            "json",
        ]
        if fields:
            for field_name in fields:
                args.extend(["--field-id", field_name])
        if filter_json is not None:
            args.extend(
                [
                    "--filter-json",
                    json.dumps(filter_json, ensure_ascii=False),
                ]
            )
        payload = run_lark(args)
        data = payload.get("data", {})
        rows = data.get("data", [])
        response_fields = data.get("fields", [])
        record_ids = data.get("record_id_list", [])
        for index, row in enumerate(rows):
            record: dict[str, Any] = {
                "record_id": record_ids[index] if index < len(record_ids) else None
            }
            for field_index, field_name in enumerate(response_fields):
                value = row[field_index] if field_index < len(row) else None
                record[field_name] = value
            records.append(record)
        if not data.get("has_more"):
            break
        offset += len(rows)
    return records


def _run_lark_with_json(args: list[str], payload: dict[str, Any]) -> dict[str, Any]:
    fd, raw_name = tempfile.mkstemp(
        dir=str(Path.cwd()),
        prefix="ad_naming_payload_",
        suffix=".json",
    )
    temp_name = Path(raw_name).name
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    try:
        return run_lark([*args, "--json", f"@./{temp_name}"])
    finally:
        try:
            (Path.cwd() / temp_name).unlink()
        except OSError:
            pass


def _batch_cell(field_name: str, value: Any) -> Any:
    if field_name == "是否启用":
        text = text_value(value)
        return [text] if text else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = text_value(value)
    return text or None


def _ordered_fields(records: list[dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    for record in records:
        for key in record:
            if key in {"record_id", "冲突"} or key in fields:
                continue
            fields.append(key)
    return fields


def batch_create_maps(
    base_token: str,
    table_id: str,
    records: list[dict[str, Any]],
) -> None:
    for start in range(0, len(records), BATCH_RECORD_LIMIT):
        chunk = records[start : start + BATCH_RECORD_LIMIT]
        field_names = _ordered_fields(chunk)
        rows = [
            [_batch_cell(name, record.get(name)) for name in field_names]
            for record in chunk
        ]
        _run_lark_with_json(
            [
                "base",
                "+record-batch-create",
                "--base-token",
                base_token,
                "--table-id",
                table_id,
            ],
            {"fields": field_names, "rows": rows},
        )


def batch_update_maps(
    base_token: str,
    table_id: str,
    updates: dict[str, dict[str, Any]],
) -> None:
    items = list(updates.items())
    for start in range(0, len(items), BATCH_RECORD_LIMIT):
        chunk = dict(items[start : start + BATCH_RECORD_LIMIT])
        _run_lark_with_json(
            [
                "base",
                "+record-batch-update",
                "--base-token",
                base_token,
                "--table-id",
                table_id,
            ],
            {"update_records": chunk},
        )


def text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        if not value:
            return ""
        value = value[0]
    if isinstance(value, dict):
        value = value.get("text") or value.get("name") or value.get("value") or ""
    return str(value).strip()


def number_value(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def parse_template(raw: str) -> Template:
    fields: list[FieldSpec] = []
    parts = re.split(r"(\{[^{}]+\})", (raw or "").strip())
    prefix = ""
    for part in parts:
        match = re.fullmatch(r"\{([^{}]+)\}", part)
        if not match:
            prefix += part
            continue
        token = match.group(1).strip()
        optional = token.endswith("?")
        name = token[:-1].strip() if optional else token
        if not name:
            raise AppError(f"Empty field name in template: {raw}")
        fields.append(FieldSpec(name=name, optional=optional, prefix=prefix))
        prefix = ""
    return Template(raw=raw, fields=fields, suffix=prefix)


def parse_value_dictionary(raw: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        separator = None
        for candidate in ("=", ":", "："):
            if candidate in line:
                separator = candidate
                break
        if separator is None:
            continue
        name, raw_values = line.split(separator, 1)
        name = name.strip()
        if not name:
            continue
        if name in RESERVED_VALUE_DICTIONARY_KEYS:
            continue
        parsed = [value.strip() for value in raw_values.split(",") if value.strip()]
        values[name] = parsed
    return values


def ratio_format_from_dictionary(raw: str) -> str:
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        separator = None
        for candidate in ("=", ":", "："):
            if candidate in line:
                separator = candidate
                break
        if separator is None:
            continue
        name, raw_value = line.split(separator, 1)
        if name.strip() == RATIO_FORMAT_FIELD:
            value = raw_value.strip()
            if value:
                return value
    return "三码"


def parse_normalization_rules(raw: str) -> list[tuple[str, str]]:
    rules: list[tuple[str, str]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if "=>" not in line:
            continue
        pattern, replacement = line.split("=>", 1)
        pattern = pattern.strip()
        replacement = replacement.strip()
        if pattern and replacement:
            rules.append((pattern, replacement))
    return rules


def compile_normalization_rules(
    raw: str,
) -> list[tuple[re.Pattern[str], str]]:
    rules: list[tuple[re.Pattern[str], str]] = []
    for pattern_text, replacement in parse_normalization_rules(raw):
        try:
            rules.append((re.compile(pattern_text), replacement))
        except re.error:
            continue
    return rules


def _apply_one_normalization_rule(
    pattern: str | re.Pattern[str],
    replacement: str,
    text: str,
) -> str:
    try:
        if isinstance(pattern, re.Pattern):
            return pattern.sub(replacement, text)
        return re.sub(pattern, replacement, text)
    except re.error:
        return text


def apply_normalization_rules(
    stem: str,
    rules: list[tuple[str | re.Pattern[str], str]],
) -> str:
    for pattern, replacement in rules:
        stem = _apply_one_normalization_rule(pattern, replacement, stem)
    return stem


def _strict_regex(template: Template, values: dict[str, list[str]]) -> re.Pattern[str]:
    regex = "^"
    for index, field in enumerate(template.fields):
        known = values.get(field.name) or []
        if known:
            alternatives = sorted({v for v in known if v}, key=len, reverse=True)
            pattern = "(?:" + "|".join(re.escape(v) for v in alternatives) + ")"
        else:
            pattern = ".+?"
        group = f"(?P<f{index}>{pattern})"
        if index == 0:
            regex += re.escape(field.prefix) + group
        elif field.optional:
            regex += "(?:" + re.escape(field.prefix) + group + ")?"
        else:
            regex += re.escape(field.prefix) + group
    regex += re.escape(template.suffix) + "$"
    return re.compile(regex)


def _compile_value_patterns(
    values: dict[str, list[str]],
) -> dict[str, list[tuple[str, re.Pattern[str]]]]:
    compiled: dict[str, list[tuple[str, re.Pattern[str]]]] = {}
    for name, choices in values.items():
        patterns: list[tuple[str, re.Pattern[str]]] = []
        for choice in choices:
            if not choice:
                continue
            patterns.append(
                (
                    choice,
                    re.compile(
                        rf"(?<![A-Za-z0-9]){re.escape(choice)}(?![A-Za-z0-9])",
                        re.IGNORECASE,
                    ),
                )
            )
        if patterns:
            compiled[name] = patterns
    return compiled


def _known_value_assignments(
    stem: str,
    value_patterns: dict[str, list[tuple[str, re.Pattern[str]]]],
) -> tuple[dict[str, str], list[tuple[int, int]]]:
    assigned: dict[str, str] = {}
    spans: list[tuple[int, int]] = []
    entries: list[tuple[int, int, str, str]] = []
    for name, patterns in value_patterns.items():
        for choice, pattern in patterns:
            for match in pattern.finditer(stem):
                entries.append((len(choice), match.start(), name, choice))
    entries.sort(key=lambda item: (-item[0], item[1]))
    for _, start, name, choice in entries:
        end = start + len(choice)
        if any(start < span_end and span_start < end for span_start, span_end in spans):
            continue
        if name not in assigned:
            assigned[name] = choice
            spans.append((start, end))
    spans.sort()
    return assigned, spans


def _looks_like_ratio(token: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:11|45|916|169|16x9|9x16|1x1|4x5|16:9|9:16|1:1|4:5)",
            token,
            re.IGNORECASE,
        )
    )


def _looks_like_dimension(token: str) -> bool:
    return bool(re.fullmatch(r"\d{2,5}x\d{2,5}", token, re.IGNORECASE))


def _looks_like_language(token: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]{2,4}[0-9]*", token))


def _looks_like_id(token: str) -> bool:
    return bool(re.fullmatch(r"(?=.*\d)[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*", token))


def _classify_token(token: str, field_name: str) -> bool:
    normalized = field_name.lower()
    if normalized in {"比例"}:
        return _looks_like_ratio(token)
    if normalized in {"尺寸", "分辨率"}:
        return _looks_like_dimension(token)
    if normalized in {"语言", "language", "locale"}:
        return _looks_like_language(token)
    if normalized in {"日期", "date"}:
        return bool(re.fullmatch(r"\d{2,8}", token))
    if normalized in {"素材id", "编号", "id", "material_id"}:
        return _looks_like_id(token)
    if normalized in {"主题", "topic", "theme"}:
        return bool(token) and not token.isdigit()
    return False


def _mask_spans(stem: str, spans: list[tuple[int, int]]) -> str:
    chars = list(stem)
    for start, end in spans:
        for index in range(start, min(end, len(chars))):
            chars[index] = " "
    return "".join(chars)


def _clean_free_prefix(stem: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return stem.strip("_- ")

    chars: list[str] = []
    span_index = 0
    for index, char in enumerate(stem):
        while span_index < len(spans) and index >= spans[span_index][1]:
            span_index += 1
        if span_index < len(spans) and spans[span_index][0] <= index < spans[span_index][1]:
            continue
        chars.append(char)

    cleaned: list[str] = []
    separator_chars = set("-_()（） .")
    separator_text = "-_()（） ."
    for char in "".join(chars).strip(separator_text):
        if (
            char in separator_chars
            and cleaned
            and cleaned[-1] in separator_chars
        ):
            if char == "_" and cleaned[-1] != "_":
                cleaned[-1] = "_"
            continue
        cleaned.append(char)

    while cleaned and cleaned[-1] in separator_chars:
        cleaned.pop()
    return "".join(cleaned)


def loose_parse(
    template: Template,
    values: dict[str, list[str]],
    stem: str,
    value_patterns: dict[str, list[tuple[str, re.Pattern[str]]]] | None = None,
) -> tuple[dict[str, str], list[str]]:
    if value_patterns is None:
        value_patterns = _compile_value_patterns(values)
    assigned, spans = _known_value_assignments(stem, value_patterns)
    field_names = [field.name for field in template.fields]

    if "比例" in field_names and "比例" not in assigned:
        match = re.search(
            r"(?<!\d)(?:11|45|916|169|16x9|9x16|1x1|4x5|16:9|9:16|1:1|4:5)(?!\d)",
            stem,
            re.IGNORECASE,
        )
        if match:
            assigned["比例"] = match.group(0).replace("x", "x").replace("X", "x")
            spans.append(match.span())
    if any(name in field_names for name in ("尺寸", "分辨率")):
        match = re.search(r"(?<!\d)(\d{2,5})x(\d{2,5})(?!\d)", stem, re.IGNORECASE)
        if match:
            dimension_name = "尺寸" if "尺寸" in field_names else "分辨率"
            if dimension_name not in assigned:
                assigned[dimension_name] = f"{match.group(1)}x{match.group(2)}"
                spans.append(match.span())

    masked = _mask_spans(stem, spans)
    free_prefix = _clean_free_prefix(stem, spans)
    tokens = [token for token in re.split(r"[-_() ]+", masked) if token]
    used_tokens: set[int] = set()

    for field_name in field_names:
        if field_name in assigned:
            continue
        if field_name in values:
            continue
        for index, token in enumerate(tokens):
            if index in used_tokens:
                continue
            if _classify_token(token, field_name):
                assigned[field_name] = token
                used_tokens.add(index)
                break

    for field_name in field_names:
        if field_name in assigned or field_name in values:
            continue
        if field_name in {"自由前缀", "前缀"}:
            if free_prefix:
                assigned[field_name] = free_prefix

    missing: list[str] = []
    for field in template.fields:
        if not field.optional and not text_value(assigned.get(field.name)):
            missing.append(field.name)
    return assigned, missing


def fill_media_fields(
    fields: dict[str, str],
    template: Template,
    path: Path,
    ratio_format: str | None = "三码",
) -> list[str]:
    dimensions = None
    missing: list[str] = []
    for field in template.fields:
        if text_value(fields.get(field.name)):
            continue
        if field.name not in MEDIA_FIELD_NAMES:
            continue
        if dimensions is None:
            dimensions = read_dimensions(path)
        if dimensions is None:
            missing.append(field.name)
            continue
        width, height = dimensions
        if field.name == "比例":
            value = ratio_code(width, height, ratio_format)
            if value:
                fields[field.name] = value
            else:
                missing.append(field.name)
        elif field.name in {"尺寸", "分辨率"}:
            value = dimension_text(width, height)
            if value:
                fields[field.name] = value
            else:
                missing.append(field.name)
    return missing


def render_template(template: Template, fields: dict[str, str]) -> str:
    if not template.fields:
        return ""
    first = template.fields[0]
    output = first.prefix + text_value(fields.get(first.name))
    for field in template.fields[1:]:
        value = text_value(fields.get(field.name))
        if value:
            output += field.prefix + value
        elif not field.optional:
            output += field.prefix
    output += template.suffix
    return output


def generate_for_template(
    template: Template,
    values: dict[str, list[str]],
    stem: str,
    path: Path,
    ratio_format: str | None = "三码",
    value_patterns: dict[str, list[tuple[str, re.Pattern[str]]]] | None = None,
) -> tuple[str | None, str]:
    fields, missing = loose_parse(
        template,
        values,
        stem,
        value_patterns,
    )
    media_missing = fill_media_fields(fields, template, path, ratio_format)
    missing = [name for name in missing if not text_value(fields.get(name))]
    missing.extend(media_missing)
    missing = list(dict.fromkeys(missing))
    if "比例" in fields and fields["比例"]:
        fields["比例"] = normalize_ratio(fields["比例"], ratio_format)
    if missing:
        return None, f"缺少字段: {', '.join(missing)}"
    return render_template(template, fields), ""


def choose_template(templates: list[dict[str, Any]], stem: str) -> dict[str, Any] | None:
    for template in templates:
        if template["_strict"].fullmatch(stem) is not None:
            return template
    return templates[0] if templates else None


def _memory_tokens(fragment: str) -> list[str]:
    return re.findall(r"[^\W_]+", fragment)


def build_memory_index(
    memories: list[dict[str, Any]],
) -> tuple[dict[str, list[int]], list[int]]:
    index: dict[str, list[int]] = {}
    tokenless: list[int] = []
    for position, memory in enumerate(memories):
        fragment = text_value(memory.get("原始命名片段"))
        if not fragment:
            continue
        tokens = _memory_tokens(fragment)
        if not tokens:
            tokenless.append(position)
            continue
        for token in dict.fromkeys(tokens):
            index.setdefault(token, []).append(position)
    return index, tokenless


def apply_memory(
    memories: list[dict[str, Any]],
    stem: str,
    generated: str,
    memory_index: dict[str, list[int]] | None = None,
    tokenless_positions: list[int] | None = None,
) -> tuple[str, str]:
    if memory_index is None:
        candidates = range(len(memories))
    else:
        stem_tokens = set(_memory_tokens(stem))
        candidates_set: set[int] = set(tokenless_positions or [])
        for token in stem_tokens:
            candidates_set.update(memory_index.get(token, ()))
        candidates = sorted(candidates_set)

    for position in candidates:
        memory = memories[position]
        fragment = text_value(memory.get("原始命名片段"))
        if fragment and fragment in stem:
            final_name = text_value(memory.get("人工最终新文件名"))
            if final_name:
                reason = text_value(memory.get("修正原因")) or "命中命名修正记忆"
                return final_name, reason
    return generated, ""


def is_hidden_file(path: Path) -> bool:
    if path.name.startswith("."):
        return True
    try:
        attributes = path.stat().st_file_attributes
    except AttributeError:
        return False
    return bool(attributes & 0x2)


def scan_media_files(folder: str) -> list[Path]:
    root = Path(folder).resolve()
    if not root.exists() or not root.is_dir():
        raise AppError(f"Folder does not exist or is not a directory: {folder}")
    files: list[Path] = []
    for child in root.iterdir():
        if not child.is_file():
            continue
        if is_hidden_file(child) or child.name.lower() in EXCLUDED_NAMES:
            continue
        if child.suffix.lower() in MEDIA_EXTENSIONS:
            files.append(child)
    return sorted(files, key=lambda path: path.name.lower())


def _key_for_record(record: dict[str, Any]) -> tuple[str, str]:
    return (
        text_value(record.get("项目名称")),
        text_value(record.get("文件路径")),
    )


def _field_text(
    fields: dict[str, Any],
    name: str,
    default: str = "",
) -> str:
    return text_value(fields.get(name, default))


def build_preview_record(
    project: str,
    path: Path,
    original_name: str,
    is_hit: bool,
    suggested_name: str,
    initial_name: str,
    remark: str,
    existing: dict[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    record = {
        "项目名称": project,
        "文件路径": str(path),
        "文件原名": original_name,
        "是否命中": "命中" if is_hit else "未命中",
        "建议新文件名": suggested_name,
        "初始建议新文件名": initial_name,
        "执行状态": "待审核",
        "存档路径": "",
        "处理备注": remark,
    }
    changed = True
    if existing:
        old_initial = _field_text(existing, "初始建议新文件名")
        old_suggestion = _field_text(existing, "建议新文件名")
        if old_initial and old_suggestion and old_initial != old_suggestion:
            record["建议新文件名"] = old_suggestion
            record["初始建议新文件名"] = old_initial
            record["处理备注"] = _field_text(existing, "处理备注", remark)
            changed = False
    return record, changed


def preview(project: str, folder: str, base_token: str = BASE_TOKEN) -> dict[str, Any]:
    tables = ensure_schema(base_token)
    template_records = list_records(
        base_token,
        tables[TEMPLATE_TABLE],
        fields=TEMPLATE_FIELDS,
        filter_json=_project_filter(project),
    )
    templates: list[dict[str, Any]] = []
    for record in template_records:
        if _field_text(record, "是否启用") != "启用":
            continue
        raw_template = _field_text(record, "命名模板")
        if not raw_template:
            continue
        parsed = parse_template(raw_template)
        priority = number_value(record.get("优先级"))
        raw_values = _field_text(record, "字段取值字典")
        ratio_format = ratio_format_from_dictionary(raw_values)
        values = parse_value_dictionary(raw_values)
        if "比例" in values:
            values["比例"] = [
                normalize_ratio(value, ratio_format)
                for value in values["比例"]
            ]
        templates.append(
            {
                **record,
                "_parsed": parsed,
                "_values": values,
                "_strict": _strict_regex(parsed, values),
                "_value_patterns": _compile_value_patterns(values),
                "_priority": priority if priority is not None else 1e9,
                "_ratio_format": ratio_format,
            }
        )
    templates.sort(key=lambda item: item["_priority"])
    if not templates:
        raise AppError(f"项目 {project!r} 在命名模板库中没有启用模板")

    normalization_rules: list[tuple[str | re.Pattern[str], str]] = []
    for template in templates:
        normalization_rules.extend(
            compile_normalization_rules(
                _field_text(template, NORMALIZATION_RULES_FIELD)
            )
        )

    memory_records = list_records(
        base_token,
        tables[MEMORY_TABLE],
        fields=MEMORY_FIELDS,
        filter_json=_project_filter(project),
    )
    memories = list(memory_records)
    memories.sort(
        key=lambda record: _field_text(record, "记录时间"),
        reverse=True,
    )
    memory_index, tokenless_positions = build_memory_index(memories)

    existing_records = list_records(
        base_token,
        tables[PREVIEW_TABLE],
        fields=PREVIEW_FIELDS,
        filter_json=_active_preview_filter(project),
    )
    existing_by_key = {
        _key_for_record(record): record
        for record in existing_records
    }

    files = scan_media_files(folder)
    to_create: list[dict[str, Any]] = []
    to_update: dict[str, dict[str, Any]] = {}
    created = 0
    updated = 0
    hit_count = 0
    generated_count = 0
    pending_count = 0
    memory_count = 0
    normalization_count = 0

    for path in files:
        original_name = path.name
        original_stem = path.stem
        stem = apply_normalization_rules(original_stem, normalization_rules)
        normalization_changed = stem != original_stem
        is_hit = False
        suggested = original_name
        remark = ""
        memory_hit = False

        matched = choose_template(templates, stem)
        if matched["_strict"].fullmatch(stem) is not None:
            if normalization_changed:
                suggested = stem + path.suffix
                remark = "已按归一化规则调整文件名"
            else:
                is_hit = True
        else:
            generated, generation_remark = generate_for_template(
                matched["_parsed"],
                matched["_values"],
                stem,
                path,
                matched["_ratio_format"],
                matched["_value_patterns"],
            )
            if generated is not None:
                generated = generated + path.suffix
                suggested, memory_reason = apply_memory(
                    memories,
                    stem,
                    generated,
                    memory_index,
                    tokenless_positions,
                )
                remark = memory_reason or generation_remark
                memory_hit = bool(memory_reason)
            else:
                suggested = "【待确认】"
                remark = generation_remark

        if normalization_changed:
            normalization_count += 1

        key = (project, str(path))
        existing = existing_by_key.get(key)
        record, _ = build_preview_record(
            project,
            path,
            original_name,
            is_hit,
            suggested,
            suggested,
            remark,
            existing,
        )
        if existing is None:
            to_create.append(record)
            created += 1
        elif any(
            text_value(record.get(field_name))
            != text_value(existing.get(field_name))
            for field_name in PREVIEW_FIELDS
        ):
            record_id = existing.get("record_id")
            if record_id:
                to_update[record_id] = record
                updated += 1
            else:
                to_create.append(record)
                created += 1
        if is_hit:
            hit_count += 1
        elif suggested == "【待确认】":
            pending_count += 1
        else:
            generated_count += 1
        if memory_hit:
            memory_count += 1

    if to_create:
        batch_create_maps(base_token, tables[PREVIEW_TABLE], to_create)
    if to_update:
        batch_update_maps(base_token, tables[PREVIEW_TABLE], to_update)

    return {
        "ok": True,
        "project": project,
        "folder": str(Path(folder).resolve()),
        "scanned": len(files),
        "hit": hit_count,
        "generated": generated_count,
        "pending": pending_count,
        "memory_applied": memory_count,
        "normalization_applied": normalization_count,
        "preview_created": created,
        "preview_updated": updated,
    }


def _resolve_target(
    source_dir: Path,
    requested_name: str,
    source_path: Path,
    used_targets: set[str],
) -> Path | None:
    requested = Path(requested_name)
    stem = requested.stem
    suffix = requested.suffix
    candidate = source_dir / requested.name
    same_source = os.path.normcase(str(candidate.resolve())) == os.path.normcase(
        str(source_path.resolve())
    )
    counter = 2
    while (candidate.exists() and not same_source) or (
        os.path.normcase(str(candidate)) in used_targets
    ):
        if counter > 999:
            return None
        candidate = source_dir / f"{stem}_{counter:02d}{suffix}"
        counter += 1
        same_source = os.path.normcase(str(candidate.resolve())) == os.path.normcase(
            str(source_path.resolve())
        )
    used_targets.add(os.path.normcase(str(candidate)))
    return candidate


def _unique_archive_path(archive_dir: Path, original_name: str) -> Path:
    candidate = archive_dir / original_name
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while candidate.exists():
        candidate = archive_dir / f"{stem}_{counter:02d}{suffix}"
        counter += 1
    return candidate


def _create_archive_dir(source_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = source_dir / f"_原始存档_{timestamp}"
    candidate = base
    counter = 2
    while candidate.exists():
        candidate = source_dir / f"_原始存档_{timestamp}_{counter:02d}"
        counter += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def _memory_key(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        _field_text(record, "项目名称"),
        _field_text(record, "原始命名片段"),
        _field_text(record, "初始建议新文件名"),
        _field_text(record, "人工最终新文件名"),
    )


def _path_is_within(path_text: str, folder: Path) -> bool:
    if not path_text:
        return False
    try:
        path = Path(path_text).resolve()
        return path == folder or folder in path.parents
    except OSError:
        return False


def _memory_record_fields(
    project: str,
    fragment: str,
    initial_name: str,
    final_name: str,
    reason: str,
    existing_keys: set[tuple[str, str, str, str]],
) -> dict[str, Any] | None:
    key = (project, fragment, initial_name, final_name)
    if key in existing_keys:
        return None
    existing_keys.add(key)
    return {
        "项目名称": project,
        "原始命名片段": fragment,
        "初始建议新文件名": initial_name,
        "人工最终新文件名": final_name,
        "修正原因": reason or "人工已修改建议新文件名",
        "记录时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _values_from_templates(
    templates: list[dict[str, Any]],
    field_name: str,
    defaults: list[str],
) -> list[str]:
    values = list(defaults)
    for template in templates:
        dictionary = parse_value_dictionary(
            _field_text(template, "字段取值字典")
        )
        for value in dictionary.get(field_name, []):
            if value and value not in values:
                values.append(value)
    return values


def _alternation(values: list[str]) -> str:
    return "|".join(
        re.escape(value)
        for value in dict.fromkeys(values)
    )


def _memory_stems(memory: dict[str, Any]) -> tuple[str, str]:
    original = Path(text_value(memory.get("原始命名片段"))).stem
    final = Path(text_value(memory.get("人工最终新文件名"))).stem
    return original, final


def _next_template_priority(templates: list[dict[str, Any]]) -> int:
    priorities = [
        number_value(template.get("优先级"))
        for template in templates
        if number_value(template.get("优先级")) is not None
    ]
    return int(max(priorities, default=0)) + 1


def _existing_rule_signatures(
    templates: list[dict[str, Any]],
) -> set[tuple[str, str]]:
    signatures: set[tuple[str, str]] = set()
    for template in templates:
        template_name = _field_text(template, "命名模板")
        for pattern, replacement in parse_normalization_rules(
            _field_text(template, NORMALIZATION_RULES_FIELD)
        ):
            signatures.add(
                (template_name, f"{pattern} => {replacement}")
            )
    return signatures


def _rule_conflict_note(
    original: str,
    desired: str,
    templates: list[dict[str, Any]],
) -> str:
    notes: list[str] = []
    for template in templates:
        template_name = _field_text(template, "模板名称") or "未命名模板"
        for pattern, replacement in parse_normalization_rules(
            _field_text(template, NORMALIZATION_RULES_FIELD)
        ):
            transformed = apply_normalization_rules(
                original,
                [(pattern, replacement)],
            )
            if transformed != original and transformed != desired:
                notes.append(
                    f"与现有模板 {template_name} 的规则 {pattern} 重叠，"
                    f"会先得到 {transformed}"
                )
    return "；".join(dict.fromkeys(notes))


def _make_candidate_rule(
    project: str,
    rule_name: str,
    naming_template: str,
    dictionary_text: str,
    description: str,
    pattern: str,
    replacement: str,
    example: str,
    note: str = "",
) -> dict[str, Any]:
    remark = "根据命名修正记忆自动归纳，待确认。"
    if note:
        remark += f" {note}"
    return {
        "项目名称": project,
        "模板名称": f"{AUTO_RULE_PREFIX}{rule_name}",
        "命名模板": naming_template,
        "字段取值字典": dictionary_text,
        "归一化说明": description,
        "归一化规则": f"{pattern} => {replacement}",
        "示例": example,
        "是否启用": AUTO_RULE_STATUS,
        "备注": remark,
    }


def _candidate_rules_for_memory(
    project: str,
    original: str,
    final: str,
    ratio_values: list[str],
    language_values: list[str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if not original or not final or original == final:
        return candidates

    ratio_alt = _alternation(ratio_values)
    language_alt = _alternation(language_values)
    dictionary_ratio = f"比例: {','.join(ratio_values)}"

    for ratio in sorted(set(ratio_values), key=len, reverse=True):
        original_prefix = original[: -len(ratio) - 1]
        final_prefix = final[: -len(ratio) - 1]
        if (
            original.endswith(f"-{ratio}")
            and final.endswith(f"_{ratio}")
            and original_prefix == final_prefix
        ):
            candidates.append(
                _make_candidate_rule(
                    project,
                    "比例前连字符改下划线",
                    "{自由前缀}_{比例}",
                    dictionary_ratio,
                    "当文件名末尾的比例前是连字符时，将连字符改为下划线。",
                    rf"-(?=(?:{ratio_alt})$)",
                    "_",
                    f"{original} => {final}",
                )
            )
            break

    for ratio in sorted(set(ratio_values), key=len, reverse=True):
        converted = re.sub(
            rf"[\(（]\s*{re.escape(ratio)}\s*[\)）]",
            f"_{ratio}",
            original,
        )
        if converted == final:
            candidates.append(
                _make_candidate_rule(
                    project,
                    "括号比例转下划线",
                    "{自由前缀}_{比例}",
                    dictionary_ratio,
                    "将半角或全角括号包裹的比例改成下划线连接。",
                    rf"[\(（]\s*({ratio_alt})\s*[\)）]",
                    r"_\1",
                    f"{original} => {final}",
                )
            )
            break

    if re.fullmatch(r".+_\d", original):
        for ratio in sorted(set(ratio_values), key=len, reverse=True):
            if final == original + f"_{ratio}":
                candidates.append(
                    _make_candidate_rule(
                        project,
                        f"缺失比例补默认{ratio}",
                        "{自由前缀}_{比例}",
                        dictionary_ratio,
                        f"当文件名以“_个位数”结尾且不是已知比例时，保留该数字并补 _{ratio}。",
                        r"^(.+_)(\d)$",
                        rf"\1\2_{ratio}",
                        f"{original} => {final}",
                        "规则较窄，启用前请复核 _个位数 是否总表示缺失比例。",
                    )
                )
                break

    date_match = re.fullmatch(
        rf"(.+?)_(\d{{2}})-(\d{{2,4}})-({language_alt})",
        original,
    )
    if date_match and date_match.group(3) in set(ratio_values):
        desired = (
            f"{date_match.group(1)}_{date_match.group(2)}"
            f"_{date_match.group(4)}-{date_match.group(3)}"
        )
        if desired == final:
            dictionary = (
                f"{dictionary_ratio}\n语言: {','.join(language_values)}"
            )
            candidates.append(
                _make_candidate_rule(
                    project,
                    "日期语言比例",
                    "{自由前缀}_{语言}-{比例}",
                    dictionary,
                    "将 _日期-比例-语言 调整为 _日期_语言-比例。",
                    rf"^(.+?)_(\d{{2}})-(\d{{2,4}})-({language_alt})$",
                    r"\1_\2_\4-\3",
                    f"{original} => {final}",
                )
            )

    return candidates


def auto_extract_rules_for_project(
    base_token: str,
    tables: dict[str, str],
    project: str,
    memories: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    template_records = list_records(
        base_token,
        tables[TEMPLATE_TABLE],
        fields=TEMPLATE_FIELDS,
        filter_json=_project_filter(project),
    )
    project_templates = list(template_records)
    existing_signatures = _existing_rule_signatures(project_templates)
    enabled_templates = [
        record
        for record in project_templates
        if _field_text(record, "是否启用") == "启用"
    ]

    ratio_values = _values_from_templates(
        enabled_templates or project_templates,
        "比例",
        DEFAULT_RATIOS,
    )
    language_values = _values_from_templates(
        enabled_templates or project_templates,
        "语言",
        DEFAULT_LANGUAGES,
    )

    if memories is None:
        memories = list_records(
            base_token,
            tables[MEMORY_TABLE],
            fields=MEMORY_FIELDS,
            filter_json=_project_filter(project),
        )
    if not memories:
        return {
            "rules_extracted": 0,
            "rules_skipped": 0,
            "rules_conflicts": 0,
        }

    seen_signatures = set(existing_signatures)
    candidates: list[dict[str, Any]] = []
    skipped = 0
    conflicts = 0

    for memory in memories:
        original, final = _memory_stems(memory)
        for candidate in _candidate_rules_for_memory(
            project,
            original,
            final,
            ratio_values,
            language_values,
        ):
            signature = (
                candidate["命名模板"],
                candidate["归一化规则"],
            )
            if signature in seen_signatures:
                skipped += 1
                continue
            conflict_note = _rule_conflict_note(
                original,
                final,
                project_templates,
            )
            if conflict_note:
                candidate["备注"] += f" 冲突说明：{conflict_note}"
                candidate["冲突"] = True
            candidates.append(candidate)
            seen_signatures.add(signature)

    priority = _next_template_priority(project_templates)
    for offset, candidate in enumerate(candidates):
        candidate["优先级"] = priority + offset
    if candidates:
        batch_create_maps(base_token, tables[TEMPLATE_TABLE], candidates)
    conflicts = sum(1 for candidate in candidates if candidate.get("冲突"))

    return {
        "rules_extracted": len(candidates),
        "rules_skipped": skipped,
        "rules_conflicts": conflicts,
    }


def _queue_preview_update(
    updates: dict[str, dict[str, Any]],
    record_id: str | None,
    fields: dict[str, Any],
    existing: dict[str, Any],
) -> None:
    if not record_id:
        return
    if all(
        text_value(existing.get(key)) == text_value(value)
        for key, value in fields.items()
    ):
        return
    updates[record_id] = fields


def apply(project: str, folder: str, base_token: str = BASE_TOKEN) -> dict[str, Any]:
    tables = ensure_schema(base_token)
    preview_records = list_records(
        base_token,
        tables[PREVIEW_TABLE],
        fields=PREVIEW_FIELDS,
        filter_json=_active_preview_filter(project),
    )
    records = list(preview_records)

    memory_records = list_records(
        base_token,
        tables[MEMORY_TABLE],
        fields=MEMORY_FIELDS,
        filter_json=_project_filter(project),
    )
    existing_memory_keys = {_memory_key(record) for record in memory_records}

    source_dir = Path(folder)
    if not source_dir.exists() or not source_dir.is_dir():
        raise AppError(f"Folder does not exist or is not a directory: {folder}")
    source_dir = source_dir.resolve()
    records = [
        record
        for record in records
        if _path_is_within(_field_text(record, "文件路径"), source_dir)
    ]

    archive_dir: Path | None = None
    used_targets: set[str] = set()
    renamed = 0
    skipped = 0
    conflicts = 0
    memory_written = 0
    preview_updates: dict[str, dict[str, Any]] = {}
    new_memories: list[dict[str, Any]] = []

    for record in records:
        status = _field_text(record, "执行状态")
        if status in {"已执行", "冲突"}:
            continue
        record_id = record.get("record_id")
        source_text = _field_text(record, "文件路径")
        original_name = _field_text(record, "文件原名")
        suggested_name = _field_text(record, "建议新文件名")
        initial_name = _field_text(record, "初始建议新文件名")

        if not source_text or not suggested_name:
            _queue_preview_update(
                preview_updates,
                record_id,
                {
                    "项目名称": project,
                    "执行状态": "跳过",
                    "处理备注": "缺少文件路径或建议新文件名",
                },
                record,
            )
            skipped += 1
            continue

        source_path = Path(source_text)
        if not source_path.exists() or not source_path.is_file():
            _queue_preview_update(
                preview_updates,
                record_id,
                {
                    "项目名称": project,
                    "执行状态": "跳过",
                    "处理备注": "源文件不存在",
                },
                record,
            )
            skipped += 1
            continue

        if suggested_name != initial_name:
            fragment = Path(original_name).stem if original_name else Path(source_path).stem
            reason = _field_text(record, "处理备注") or "人工已修改建议新文件名"
            memory_record = _memory_record_fields(
                project,
                fragment,
                initial_name,
                suggested_name,
                reason,
                existing_memory_keys,
            )
            if memory_record is not None:
                new_memories.append(memory_record)
                memory_written += 1

        if suggested_name == original_name:
            _queue_preview_update(
                preview_updates,
                record_id,
                {
                    "项目名称": project,
                    "执行状态": "跳过",
                    "处理备注": "已符合命名，无需处理",
                },
                record,
            )
            skipped += 1
            continue

        target = _resolve_target(
            source_dir,
            Path(suggested_name).name,
            source_path,
            used_targets,
        )
        if target is None:
            _queue_preview_update(
                preview_updates,
                record_id,
                {
                    "项目名称": project,
                    "执行状态": "冲突",
                    "处理备注": "目标文件名冲突，无法自动去重",
                },
                record,
            )
            conflicts += 1
            continue

        if archive_dir is None:
            archive_dir = _create_archive_dir(source_dir)
        archive_path = _unique_archive_path(archive_dir, original_name or source_path.name)
        try:
            shutil.copy2(source_path, archive_path)
            shutil.copy2(source_path, target)
            source_path.unlink()
        except OSError as exc:
            _queue_preview_update(
                preview_updates,
                record_id,
                {
                    "项目名称": project,
                    "执行状态": "冲突",
                    "处理备注": f"文件复制或原文件删除失败: {exc}",
                },
                record,
            )
            conflicts += 1
            continue

        _queue_preview_update(
            preview_updates,
            record_id,
            {
                "项目名称": project,
                "执行状态": "已执行",
                "存档路径": str(archive_path),
                "处理备注": "已复制存档并生成改名副本，并移除原文件",
            },
            record,
        )
        renamed += 1

    if preview_updates:
        batch_update_maps(base_token, tables[PREVIEW_TABLE], preview_updates)
    if new_memories:
        batch_create_maps(base_token, tables[MEMORY_TABLE], new_memories)

    rule_summary = {
        "rules_extracted": 0,
        "rules_skipped": 0,
        "rules_conflicts": 0,
    }
    if memory_written:
        rule_summary = auto_extract_rules_for_project(
            base_token,
            tables,
            project,
            new_memories,
        )

    return {
        "ok": True,
        "project": project,
        "folder": str(source_dir.resolve()),
        "renamed": renamed,
        "skipped": skipped,
        "conflicts": conflicts,
        "memory_written": memory_written,
        "rules_extracted": rule_summary["rules_extracted"],
        "rules_skipped": rule_summary["rules_skipped"],
        "rules_conflicts": rule_summary["rules_conflicts"],
        "archive_path": str(archive_dir) if archive_dir else None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview and apply Feishu Base based ad creative naming."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview_parser = subparsers.add_parser("preview", help="Generate preview records.")
    preview_parser.add_argument("--project", required=True)
    preview_parser.add_argument("--folder", required=True)

    apply_parser = subparsers.add_parser("apply", help="Apply confirmed preview records.")
    apply_parser.add_argument("--project", required=True)
    apply_parser.add_argument("--folder", required=True)

    schema_parser = subparsers.add_parser("ensure-schema", help="Ensure Base schema exists.")
    schema_parser.add_argument("--base-token", default=BASE_TOKEN)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "preview":
            result = preview(args.project, args.folder)
        elif args.command == "apply":
            result = apply(args.project, args.folder)
        elif args.command == "ensure-schema":
            tables = ensure_schema(args.base_token, refresh=True)
            result = {"ok": True, "tables": tables}
        else:
            parser.error("Unknown command")
            return 2
    except AppError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
