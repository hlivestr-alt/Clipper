from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from product_broll import PRODUCT_ALIASES, PRODUCT_FOLDERS, PRODUCT_LABELS, canonical_product
from utils import lm_studio_openai_chat_kwargs


log = logging.getLogger("proya.product_information")

SCHEMA_VERSION = 2
LLM_PROMPT_VERSION = 1
SUPPORTED_EXTENSIONS = {".pdf", ".docx"}
MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 300
HIGH_CONFIDENCE = 0.85
ROLE_LABELS = {
    "ingredient": (
        "ingredient",
        "ingredients",
        "active ingredient",
        "active ingredients",
        "key ingredient",
        "key ingredients",
        "kandungan",
        "kandungan utama",
        "bahan aktif",
        "komposisi",
    ),
    "benefit": (
        "benefit",
        "benefits",
        "function",
        "functions",
        "fungsi",
        "manfaat",
        "kegunaan",
        "keunggulan",
    ),
    "usage": (
        "how to use",
        "directions",
        "usage",
        "cara pakai",
        "cara penggunaan",
        "petunjuk penggunaan",
    ),
    "descriptor": (
        "description",
        "product description",
        "descriptor",
        "deskripsi",
        "tentang produk",
    ),
    "cta": (
        "cta",
        "call to action",
        "recommendation",
        "recommended text",
        "rekomendasi",
        "ajakan",
    ),
    "product_name": (
        "product name",
        "nama produk",
    ),
}
LLM_ROLES = frozenset(ROLE_LABELS)


_ROLE_PREFIXES = sorted(
    ((label.casefold(), role) for role, labels in ROLE_LABELS.items() for label in labels),
    key=lambda item: len(item[0]),
    reverse=True,
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•●▪◦‣]|(?:\d+|[a-zA-Z])[.)])\s*")
_SPACE_RE = re.compile(r"\s+")
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s*%?\b")


@dataclass(frozen=True)
class SourceBlock:
    text: str
    locator: dict[str, Any]
    heading: bool = False


def information_root(cfg=None) -> Path:
    raw = getattr(cfg, "PRODUCT_INFORMATION_DIR", "assets/information") if cfg is not None else "assets/information"
    path = Path(str(raw or "assets/information"))
    if not path.is_absolute():
        path = Path.cwd() / path
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def information_index_path(cfg=None) -> Path:
    raw = getattr(cfg, "WORKING_DIR", "working") if cfg is not None else "working"
    root = Path(str(raw or "working"))
    if not root.is_absolute():
        root = Path.cwd() / root
    root.mkdir(parents=True, exist_ok=True)
    return (root / "product_information_index.json").resolve()


def load_product_information_index(cfg=None, *, refresh_if_changed: bool = True) -> dict[str, Any]:
    if refresh_if_changed and (
        _source_snapshot(cfg) != _cached_snapshot(cfg)
        or _extractor_fingerprint(cfg) != _cached_extractor_fingerprint(cfg)
    ):
        return scan_product_information(cfg)
    path = information_index_path(cfg)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return scan_product_information(cfg)
    if not isinstance(payload, dict):
        return scan_product_information(cfg)
    for source in payload.get("sources", []) or []:
        if isinstance(source, dict):
            source["cached"] = True
    return payload


def product_information_revision(cfg=None) -> str:
    return str(load_product_information_index(cfg).get("revision") or "")


def scan_product_information(cfg=None, *, force: bool = False) -> dict[str, Any]:
    root = information_root(cfg)
    target = information_index_path(cfg)
    extractor_fingerprint = _extractor_fingerprint(cfg)
    previous = _read_index(target)
    previous_sources = {
        str(item.get("path") or ""): item
        for item in previous.get("sources", [])
        if isinstance(item, dict)
    }

    sources: list[dict[str, Any]] = []
    for path in _source_files(root):
        relative = path.relative_to(root).as_posix()
        identity = _file_identity(path, relative)
        cached = previous_sources.get(relative)
        if (
            not force
            and isinstance(cached, dict)
            and cached.get("sha256") == identity["sha256"]
            and cached.get("schema_version") == SCHEMA_VERSION
            and cached.get("extractor_fingerprint") == extractor_fingerprint
        ):
            reused = dict(cached)
            reused["cached"] = True
            sources.append(reused)
            continue
        sources.append(_extract_source(path, root, identity, cfg, extractor_fingerprint))

    index = _build_index(root, sources, extractor_fingerprint)
    _write_json_atomic(target, index)
    return index


def product_information_status(cfg=None) -> dict[str, Any]:
    index = load_product_information_index(cfg)
    return {
        "schema_version": index.get("schema_version", SCHEMA_VERSION),
        "revision": index.get("revision", ""),
        "scanned_at": index.get("scanned_at", ""),
        "root": index.get("root", str(information_root(cfg))),
        "sources": [_public_source(item) for item in index.get("sources", []) if isinstance(item, dict)],
        "products": index.get("product_summary", []),
        "unassigned_count": len(index.get("unassigned", []) or []),
        "conflict_count": len(index.get("conflicts", []) or []),
        "unassigned": list(index.get("unassigned", []) or [])[:100],
        "conflicts": list(index.get("conflicts", []) or [])[:100],
        "warnings": index.get("warnings", []),
    }


def facts_for_product(index: dict[str, Any], product_key: str, roles: Iterable[str] | None = None) -> list[dict[str, Any]]:
    product = (index.get("products") or {}).get(str(product_key or ""), {})
    facts = product.get("facts", []) if isinstance(product, dict) else []
    allowed = {str(role) for role in roles or []}
    return [
        dict(fact)
        for fact in facts
        if isinstance(fact, dict)
        and bool(fact.get("eligible", False))
        and (not allowed or str(fact.get("role") or "") in allowed)
    ]


def _source_files(root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.casefold() in SUPPORTED_EXTENSIONS
            and not path.name.startswith("~$")
            and not any(part.startswith(".") for part in path.relative_to(root).parts)
        ),
        key=lambda item: item.relative_to(root).as_posix().casefold(),
    )


def _source_snapshot(cfg=None) -> list[dict[str, Any]]:
    root = information_root(cfg)
    snapshot = []
    for path in _source_files(root):
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot.append({
            "path": path.relative_to(root).as_posix(),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        })
    return snapshot


def _cached_snapshot(cfg=None) -> list[dict[str, Any]]:
    payload = _read_index(information_index_path(cfg))
    snapshot = payload.get("source_snapshot")
    return snapshot if isinstance(snapshot, list) else []


def _cached_extractor_fingerprint(cfg=None) -> str:
    payload = _read_index(information_index_path(cfg))
    return str(payload.get("extractor_fingerprint") or "")


def _llm_enabled(cfg=None) -> bool:
    return bool(getattr(cfg, "PRODUCT_INFORMATION_LLM_ENABLED", False))


def _extractor_fingerprint(cfg=None) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "llm_enabled": _llm_enabled(cfg),
        "llm_prompt_version": LLM_PROMPT_VERSION,
        "model": str(
            getattr(
                cfg,
                "LM_STUDIO_MOMENT_MODEL_ID",
                getattr(cfg, "LM_STUDIO_MODEL", ""),
            )
            or ""
        ),
        "max_input_chars": int(getattr(cfg, "PRODUCT_INFORMATION_LLM_MAX_INPUT_CHARS", 6000) or 6000),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _file_identity(path: Path, relative: str) -> dict[str, Any]:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": relative,
        "extension": path.suffix.casefold(),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def _extract_source(
    path: Path,
    root: Path,
    identity: dict[str, Any],
    cfg,
    extractor_fingerprint: str,
) -> dict[str, Any]:
    source = {
        "schema_version": SCHEMA_VERSION,
        **identity,
        "extractor_fingerprint": extractor_fingerprint,
        "extraction_method": "llm" if _llm_enabled(cfg) else "rules",
        "status": "ok",
        "cached": False,
        "page_count": 0,
        "warnings": [],
        "products": [],
        "facts": [],
        "unassigned": [],
    }
    if int(identity.get("size") or 0) > MAX_SOURCE_BYTES:
        source["status"] = "oversized"
        source["warnings"].append("File exceeds the 50 MB import limit.")
        return source
    try:
        if path.suffix.casefold() == ".pdf":
            blocks, page_count, warnings = _extract_pdf_blocks(path)
        else:
            blocks, page_count, warnings = _extract_docx_blocks(path)
        source["page_count"] = page_count
        source["warnings"].extend(warnings)
        if _llm_enabled(cfg):
            try:
                facts, unassigned, llm_warnings = _facts_from_blocks_with_llm(blocks, path, root, cfg)
                source["warnings"].extend(llm_warnings)
            except Exception as exc:
                log.warning("LLM product-information extraction failed for %s: %s", path, exc)
                source["extraction_method"] = "rules_fallback"
                source["warnings"].append(
                    f"LLM extraction failed; rule-based fallback used: {type(exc).__name__}: {exc}"
                )
                facts, unassigned = _facts_from_blocks(blocks, path, root)
        else:
            facts, unassigned = _facts_from_blocks(blocks, path, root)
        source["facts"] = facts
        source["unassigned"] = unassigned
        source["products"] = sorted({str(item.get("product") or "") for item in facts if item.get("product")})
        if not blocks:
            source["status"] = "image_only" if path.suffix.casefold() == ".pdf" else "empty"
            source["warnings"].append("No searchable text was found.")
        elif not facts:
            source["status"] = "no_eligible_facts"
            source["warnings"].append("Text was extracted, but no product facts could be assigned confidently.")
    except Exception as exc:
        source["status"] = "error"
        source["warnings"].append(f"{type(exc).__name__}: {exc}")
    return source


def _extract_pdf_blocks(path: Path) -> tuple[list[SourceBlock], int, list[str]]:
    import pdfplumber

    blocks: list[SourceBlock] = []
    warnings: list[str] = []
    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) > MAX_PDF_PAGES:
            raise ValueError(f"PDF exceeds the {MAX_PDF_PAGES}-page import limit")
        for page_number, page in enumerate(pdf.pages, start=1):
            text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
            if not text.strip():
                warnings.append(f"Page {page_number} has no searchable text.")
            for line_number, line in enumerate(text.splitlines(), start=1):
                clean = _clean_text(line)
                if clean:
                    blocks.append(SourceBlock(
                        text=clean,
                        locator={"kind": "pdf", "page": page_number, "line": line_number},
                        heading=_looks_like_heading(clean),
                    ))
            try:
                tables = page.extract_tables() or []
            except Exception as exc:
                warnings.append(f"Page {page_number} table extraction failed: {exc}")
                tables = []
            for table_number, table in enumerate(tables, start=1):
                for row_number, row in enumerate(table or [], start=1):
                    cells = [_clean_text(cell) for cell in row or [] if _clean_text(cell)]
                    if cells:
                        blocks.append(SourceBlock(
                            text=": ".join(cells),
                            locator={
                                "kind": "pdf_table",
                                "page": page_number,
                                "table": table_number,
                                "row": row_number,
                            },
                            heading=False,
                        ))
        return blocks, len(pdf.pages), warnings


def _extract_docx_blocks(path: Path) -> tuple[list[SourceBlock], int, list[str]]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(path)
    blocks: list[SourceBlock] = []
    paragraph_number = 0
    table_number = 0
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            paragraph_number += 1
            paragraph = Paragraph(child, document)
            text = _clean_text(paragraph.text)
            if text:
                style = str(getattr(paragraph.style, "name", "") or "").casefold()
                blocks.append(SourceBlock(
                    text=text,
                    locator={"kind": "docx_paragraph", "paragraph": paragraph_number},
                    heading=style.startswith("heading") or style == "title" or _looks_like_heading(text),
                ))
        elif child.tag.endswith("}tbl"):
            table_number += 1
            table = Table(child, document)
            for row_number, row in enumerate(table.rows, start=1):
                cells = [_clean_text(cell.text) for cell in row.cells if _clean_text(cell.text)]
                if cells:
                    blocks.append(SourceBlock(
                        text=": ".join(cells),
                        locator={"kind": "docx_table", "table": table_number, "row": row_number},
                        heading=False,
                    ))
    return blocks, 0, []


def _facts_from_blocks_with_llm(
    blocks: list[SourceBlock],
    path: Path,
    root: Path,
    cfg,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    if not blocks:
        return [], [], []

    facts: list[dict[str, Any]] = []
    unassigned: list[dict[str, Any]] = []
    warnings: list[str] = []
    indexed_blocks = list(enumerate(blocks))
    for batch_number, batch in enumerate(_llm_block_batches(indexed_blocks, cfg), start=1):
        payload = {
            "source_file": path.relative_to(root).as_posix(),
            "fallback_product_from_filename": canonical_product(path.stem) or "",
            "allowed_products": [
                {
                    "key": key,
                    "label": PRODUCT_LABELS.get(key, key),
                    "aliases": list(PRODUCT_ALIASES.get(key, ())),
                }
                for key in PRODUCT_FOLDERS
            ],
            "allowed_roles": sorted(LLM_ROLES),
            "blocks": [
                {
                    "block_id": block_id,
                    "heading": block.heading,
                    "text": block.text,
                }
                for block_id, block in batch
            ],
        }
        raw = _call_information_llm(payload, cfg)
        parsed = _parse_llm_payload(raw)
        batch_facts, batch_unassigned, rejected = _validate_llm_payload(
            parsed,
            blocks,
            path,
            root,
        )
        facts.extend(batch_facts)
        unassigned.extend(batch_unassigned)
        if rejected:
            warnings.append(
                f"LLM batch {batch_number} rejected {rejected} unsupported or unverifiable item(s)."
            )

    return _dedupe_source_facts(facts), _dedupe_unassigned(unassigned), warnings


def _llm_block_batches(
    indexed_blocks: list[tuple[int, SourceBlock]],
    cfg,
) -> list[list[tuple[int, SourceBlock]]]:
    limit = max(2000, int(getattr(cfg, "PRODUCT_INFORMATION_LLM_MAX_INPUT_CHARS", 6000) or 6000))
    batches: list[list[tuple[int, SourceBlock]]] = []
    current: list[tuple[int, SourceBlock]] = []
    current_chars = 0
    last_heading: tuple[int, SourceBlock] | None = None
    for item in indexed_blocks:
        block_id, block = item
        if block.heading:
            last_heading = item
        item_chars = len(block.text) + 80
        if current and current_chars + item_chars > limit:
            batches.append(current)
            current = [last_heading] if last_heading is not None and last_heading not in current[-2:] else []
            current_chars = sum(len(entry.text) + 80 for _, entry in current)
        if not current or current[-1][0] != block_id:
            current.append(item)
            current_chars += item_chars
    if current:
        batches.append(current)
    return batches


def _call_information_llm(payload: dict[str, Any], cfg) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is not installed") from exc

    model_id = str(
        getattr(
            cfg,
            "LM_STUDIO_MOMENT_MODEL_ID",
            getattr(cfg, "LM_STUDIO_MODEL", "qwen/qwen3.6-27b"),
        )
        or "qwen/qwen3.6-27b"
    )
    client = OpenAI(
        base_url=getattr(cfg, "LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1"),
        api_key=getattr(cfg, "LM_STUDIO_API_KEY", "lm-studio"),
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You extract evidence-backed product facts from PDF/DOCX text. "
                "Return only one JSON object with keys facts and unassigned. "
                "Each facts item must contain block_id, product, role, text, confidence. "
                "Each unassigned item must contain block_id, role, text, reason. "
                "Use only allowed product keys and roles. Copy fact text exactly from one source block; "
                "never paraphrase, combine blocks, infer a claim, or invent a product. Use document headings "
                "and nearby context to assign a product. Split ingredient lists into separate facts when each "
                "ingredient appears verbatim. Put ambiguous claims in unassigned. Confidence must be 0 to 1."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]
    response = client.chat.completions.create(
        model=model_id,
        messages=messages,
        response_format=_information_response_format(),
        max_tokens=max(512, int(getattr(cfg, "PRODUCT_INFORMATION_LLM_MAX_TOKENS", 8192) or 8192)),
        timeout=float(
            getattr(
                cfg,
                "PRODUCT_INFORMATION_LLM_TIMEOUT",
                getattr(cfg, "LM_STUDIO_TIMEOUT", 360),
            )
            or 360
        ),
        **lm_studio_openai_chat_kwargs(cfg, model_id=model_id, temperature=0.0),
    )
    return str(response.choices[0].message.content or "").strip()


def _information_response_format() -> dict[str, Any]:
    fact_properties = {
        "block_id": {"type": "integer"},
        "product": {"type": "string", "enum": list(PRODUCT_FOLDERS)},
        "role": {"type": "string", "enum": sorted(LLM_ROLES)},
        "text": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    }
    unassigned_properties = {
        "block_id": {"type": "integer"},
        "role": {"type": "string", "enum": sorted(LLM_ROLES)},
        "text": {"type": "string"},
        "reason": {"type": "string"},
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "product_information_extraction",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "facts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": fact_properties,
                            "required": list(fact_properties),
                            "additionalProperties": False,
                        },
                    },
                    "unassigned": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": unassigned_properties,
                            "required": list(unassigned_properties),
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["facts", "unassigned"],
                "additionalProperties": False,
            },
        },
    }


def _parse_llm_payload(raw: str) -> dict[str, Any]:
    cleaned = re.sub(r"```(?:json)?", "", str(raw or ""), flags=re.IGNORECASE).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise ValueError("LLM response did not contain a JSON object")
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("LLM response must be a JSON object")
    if not isinstance(payload.get("facts", []), list) or not isinstance(payload.get("unassigned", []), list):
        raise ValueError("LLM response facts and unassigned must be arrays")
    return payload


def _validate_llm_payload(
    payload: dict[str, Any],
    blocks: list[SourceBlock],
    path: Path,
    root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    facts: list[dict[str, Any]] = []
    unassigned: list[dict[str, Any]] = []
    rejected = 0
    relative = path.relative_to(root).as_posix()

    for item in payload.get("facts", []):
        if not isinstance(item, dict):
            rejected += 1
            continue
        block = _referenced_block(item, blocks)
        product = str(item.get("product") or "").strip().casefold().replace(" ", "_")
        role = str(item.get("role") or "").strip().casefold()
        text = _clean_text(item.get("text"))
        if (
            block is None
            or product not in PRODUCT_FOLDERS
            or role not in LLM_ROLES
            or not _text_is_supported(text, block.text)
        ):
            rejected += 1
            continue
        try:
            confidence = max(0.0, min(0.99, float(item.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            rejected += 1
            continue
        facts.append(_fact(
            product=product,
            role=role,
            text=text,
            source_file=relative,
            locator=block.locator,
            excerpt=block.text,
            confidence=confidence,
            extraction_method="llm",
        ))

    for item in payload.get("unassigned", []):
        if not isinstance(item, dict):
            rejected += 1
            continue
        block = _referenced_block(item, blocks)
        role = str(item.get("role") or "").strip().casefold()
        text = _clean_text(item.get("text"))
        if block is None or role not in LLM_ROLES or not _text_is_supported(text, block.text):
            rejected += 1
            continue
        unassigned.append({
            "role": role,
            "text": text,
            "source_file": relative,
            "locator": dict(block.locator),
            "source_excerpt": block.text,
            "reason": str(item.get("reason") or "product_ambiguous")[:120],
            "extraction_method": "llm",
        })
    return facts, unassigned, rejected


def _referenced_block(item: dict[str, Any], blocks: list[SourceBlock]) -> SourceBlock | None:
    try:
        block_id = int(item.get("block_id"))
    except (TypeError, ValueError):
        return None
    return blocks[block_id] if 0 <= block_id < len(blocks) else None


def _text_is_supported(text: str, source_text: str) -> bool:
    needle = _normalized_fact_text(text)
    haystack = _normalized_fact_text(source_text)
    return bool(needle) and needle in haystack and 2 <= len(text) <= 240


def _dedupe_unassigned(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    output = []
    for item in items:
        key = (
            item.get("role"),
            _normalized_fact_text(item.get("text")),
            item.get("source_file"),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _facts_from_blocks(
    blocks: list[SourceBlock],
    path: Path,
    root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    fallback_product = canonical_product(path.stem)
    current_product = fallback_product
    current_product_confidence = 0.92 if fallback_product else 0.0
    current_role = ""
    facts: list[dict[str, Any]] = []
    unassigned: list[dict[str, Any]] = []
    relative = path.relative_to(root).as_posix()

    for block in blocks:
        text = _clean_text(block.text)
        if not text:
            continue
        products = _products_in_text(text)
        if len(products) == 1 and (block.heading or len(text) <= 90):
            current_product = products[0]
            current_product_confidence = 0.98 if block.heading else 0.90
            if block.heading and _looks_like_product_name(text):
                facts.append(_fact(
                    product=current_product,
                    role="product_name",
                    text=text.rstrip(":"),
                    source_file=relative,
                    locator=block.locator,
                    excerpt=text,
                    confidence=current_product_confidence,
                ))

        role, payload = _role_and_payload(text)
        if role:
            current_role = role
            if not payload:
                continue
        elif block.heading:
            current_role = ""
            continue
        elif current_role:
            role, payload = current_role, text
        else:
            continue

        fragments = _fact_fragments(payload, role)
        for fragment in fragments:
            if not current_product:
                unassigned.append({
                    "role": role,
                    "text": fragment,
                    "source_file": relative,
                    "locator": block.locator,
                    "source_excerpt": text,
                    "reason": "product_ambiguous",
                })
                continue
            confidence = min(0.99, current_product_confidence + (0.04 if current_role == role else 0.0))
            facts.append(_fact(
                product=current_product,
                role=role,
                text=fragment,
                source_file=relative,
                locator=block.locator,
                excerpt=text,
                confidence=confidence,
            ))
    return _dedupe_source_facts(facts), unassigned


def _role_and_payload(text: str) -> tuple[str, str]:
    normalized = text.casefold().strip()
    for label, role in _ROLE_PREFIXES:
        if normalized == label or normalized.rstrip(":") == label:
            return role, ""
        match = re.match(rf"^{re.escape(label)}\s*[:\-–]\s*(.+)$", text.strip(), flags=re.IGNORECASE)
        if match:
            return role, match.group(1).strip()
    return "", ""


def _fact_fragments(text: str, role: str) -> list[str]:
    clean = _BULLET_RE.sub("", _clean_text(text)).strip(" :;-")
    if not clean:
        return []
    if role == "ingredient":
        parts = re.split(r"\s*(?:,|;|\n|\s+[•●]\s+)\s*", clean)
    else:
        parts = re.split(r"\s*(?:;|\n|\s+[•●]\s+)\s*", clean)
    output = []
    for part in parts:
        fragment = _BULLET_RE.sub("", _clean_text(part)).strip(" :;-")
        if 2 <= len(fragment) <= 240:
            output.append(fragment)
    return output or ([clean] if len(clean) <= 240 else [])


def _fact(
    *,
    product: str,
    role: str,
    text: str,
    source_file: str,
    locator: dict[str, Any],
    excerpt: str,
    confidence: float,
    extraction_method: str = "rules",
) -> dict[str, Any]:
    seed = json.dumps(
        [product, role, _normalized_fact_text(text), source_file, locator],
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "id": hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16],
        "product": product,
        "role": role,
        "text": _clean_text(text),
        "source_file": source_file,
        "locator": dict(locator),
        "source_excerpt": _clean_text(excerpt)[:500],
        "confidence": round(float(confidence), 3),
        "eligible": bool(confidence >= HIGH_CONFIDENCE),
        "conflicted": False,
        "extraction_method": extraction_method,
    }


def _build_index(
    root: Path,
    sources: list[dict[str, Any]],
    extractor_fingerprint: str,
) -> dict[str, Any]:
    products = {
        key: {"product_key": key, "label": PRODUCT_LABELS.get(key, key.replace("_", " ").title()), "facts": []}
        for key in PRODUCT_FOLDERS
    }
    unassigned: list[dict[str, Any]] = []
    warnings: list[str] = []
    for source in sources:
        for fact in source.get("facts", []) or []:
            if isinstance(fact, dict) and fact.get("product") in products:
                products[str(fact["product"])]["facts"].append(dict(fact))
        unassigned.extend(item for item in source.get("unassigned", []) or [] if isinstance(item, dict))
        if source.get("status") not in {"ok", "no_eligible_facts"}:
            warnings.append(f"{source.get('path')}: {source.get('status')}")
        if source.get("extraction_method") == "rules_fallback":
            warnings.append(f"{source.get('path')}: LLM unavailable; rule-based fallback used")

    conflicts = []
    for product in products.values():
        product["facts"] = _dedupe_product_facts(product["facts"])
        conflicts.extend(_mark_conflicts(product["facts"]))
    conflicted_ids = {
        str(fact_id)
        for conflict in conflicts
        for fact_id in conflict.get("fact_ids", []) or []
        if fact_id
    }
    if conflicted_ids:
        for source in sources:
            for fact in source.get("facts", []) or []:
                if isinstance(fact, dict) and str(fact.get("id") or "") in conflicted_ids:
                    fact["conflicted"] = True
                    fact["eligible"] = False

    product_summary = []
    for key in PRODUCT_FOLDERS:
        facts = products[key]["facts"]
        counts: dict[str, int] = {}
        for fact in facts:
            if fact.get("eligible"):
                role = str(fact.get("role") or "")
                counts[role] = counts.get(role, 0) + 1
        product_summary.append({
            "product_key": key,
            "label": products[key]["label"],
            "eligible_fact_count": sum(counts.values()),
            "fact_counts": counts,
        })

    source_snapshot = [
        {
            "path": str(item.get("path") or ""),
            "size": int(item.get("size") or 0),
            "mtime_ns": int(item.get("mtime_ns") or 0),
        }
        for item in sources
    ]
    revision_payload = {
        "schema_version": SCHEMA_VERSION,
        "sources": [{"path": item.get("path"), "sha256": item.get("sha256"), "status": item.get("status")} for item in sources],
        "facts": {
            key: [
                {
                    "id": fact.get("id"),
                    "text": fact.get("text"),
                    "role": fact.get("role"),
                    "eligible": fact.get("eligible"),
                    "conflicted": fact.get("conflicted"),
                }
                for fact in products[key]["facts"]
            ]
            for key in PRODUCT_FOLDERS
        },
    }
    revision = hashlib.sha256(
        json.dumps(revision_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "extractor_fingerprint": extractor_fingerprint,
        "revision": revision,
        "scanned_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "root": str(root),
        "source_snapshot": source_snapshot,
        "sources": sources,
        "products": products,
        "product_summary": product_summary,
        "unassigned": unassigned,
        "conflicts": conflicts,
        "warnings": warnings,
    }


def _mark_conflicts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    conflicts = []
    product_names = [item for item in facts if str(item.get("role") or "") == "product_name"]
    distinct_names = {_normalized_fact_text(item.get("text")) for item in product_names}
    if len(distinct_names) > 1:
        for item in product_names:
            item["conflicted"] = True
            item["eligible"] = False
        conflicts.append({
            "product": product_names[0].get("product"),
            "role": "product_name",
            "key": "product_identity",
            "fact_ids": [item.get("id") for item in product_names],
            "reason": "product_identity_conflict",
        })
    for fact in facts:
        role = str(fact.get("role") or "")
        if role not in {"benefit", "usage", "descriptor", "product_name"}:
            continue
        text = str(fact.get("text") or "")
        numbers = tuple(_NUMBER_RE.findall(text))
        if not numbers:
            continue
        base = _NUMBER_RE.sub("#", _normalized_fact_text(text))
        groups.setdefault((role, base), []).append(fact)

    for (role, base), grouped in groups.items():
        number_sets = {tuple(_NUMBER_RE.findall(str(item.get("text") or ""))) for item in grouped}
        if len(number_sets) <= 1:
            continue
        for item in grouped:
            item["conflicted"] = True
            item["eligible"] = False
        conflicts.append({
            "product": grouped[0].get("product"),
            "role": role,
            "key": base,
            "fact_ids": [item.get("id") for item in grouped],
            "reason": "numeric_values_conflict",
        })
    return conflicts


def _dedupe_source_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    output = []
    for fact in facts:
        key = (
            fact.get("product"),
            fact.get("role"),
            _normalized_fact_text(fact.get("text")),
            fact.get("source_file"),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(fact)
    return output


def _dedupe_product_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for fact in facts:
        key = (str(fact.get("role") or ""), _normalized_fact_text(fact.get("text")))
        current = best.get(key)
        if current is None or float(fact.get("confidence") or 0.0) > float(current.get("confidence") or 0.0):
            best[key] = fact
    return sorted(best.values(), key=lambda item: (str(item.get("role")), str(item.get("text")).casefold()))


def _products_in_text(text: str) -> list[str]:
    direct = canonical_product(text)
    if direct:
        return [direct]
    normalized = f" {_normalized_fact_text(text)} "
    products = []
    for key in PRODUCT_FOLDERS:
        aliases = (key.replace("_", " "), *PRODUCT_ALIASES.get(key, ()))
        if any(
            f" {_normalized_fact_text(alias)} " in normalized
            for alias in aliases
            if _normalized_fact_text(alias)
        ):
            products.append(key)
    return products


def _looks_like_product_name(text: str) -> bool:
    normalized = text.casefold()
    return "proya" in normalized or any(label.casefold() in normalized for label in PRODUCT_LABELS.values())


def _looks_like_heading(text: str) -> bool:
    clean = text.strip()
    if not clean or len(clean) > 100:
        return False
    if clean.endswith(":"):
        return True
    letters = [char for char in clean if char.isalpha()]
    return bool(letters) and sum(char.isupper() for char in letters) / len(letters) >= 0.72


def _clean_text(value: Any) -> str:
    return _SPACE_RE.sub(" ", str(value or "").replace("\u00a0", " ")).strip()


def _normalized_fact_text(value: Any) -> str:
    return re.sub(r"[^\w%]+", " ", _clean_text(value).casefold()).strip()


def _public_source(source: dict[str, Any]) -> dict[str, Any]:
    facts = [item for item in source.get("facts", []) or [] if isinstance(item, dict)]
    counts: dict[str, int] = {}
    for fact in facts:
        if fact.get("eligible"):
            role = str(fact.get("role") or "")
            counts[role] = counts.get(role, 0) + 1
    return {
        "path": source.get("path", ""),
        "extension": source.get("extension", ""),
        "size": source.get("size", 0),
        "sha256": source.get("sha256", ""),
        "status": source.get("status", "error"),
        "cached": bool(source.get("cached", False)),
        "extraction_method": source.get("extraction_method", "rules"),
        "page_count": source.get("page_count", 0),
        "warnings": source.get("warnings", []),
        "products": source.get("products", []),
        "eligible_fact_count": sum(counts.values()),
        "fact_counts": counts,
        "unassigned_count": len(source.get("unassigned", []) or []),
    }


def _read_index(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)
