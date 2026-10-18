"""Fixed, stdlib-only text and RSS projection; its caller owns actual containment.

Run as ``python -I -S -B /worker.py``. Input is one bounded ASCII JSON header
line, the declared UTF-8 body, and EOF. No document supplies executable options.
HTML projection is a baseline reading of text, not article extraction or semantic
sanitization. Untrusted instructions remain untrusted text after projection.

HTML uses a bounded lexical stack, not a DOM. Matching end tags close through
their ancestors; optional end tags may remain open at EOF. Incomplete tokens or
raw-text sections refuse. Suppressed ancestors remain suppressed until closed.
RSS uses fixed Expat handlers with bounded scalars and no resource loader.
The supervisor must independently enforce wall time, denied network/filesystem
authority, bounded pipes, cancellation, and complete process-tree settlement.
"""

from __future__ import annotations

# ruff: noqa: EM101
# Refusal arguments are closed protocol codes, never exception message prose.
import json
import re
import sys
from html import unescape
from html.parser import HTMLParser
from typing import BinaryIO, Literal, Never, TypedDict, cast
from xml.parsers import expat

MAX_HEADER_BYTES = 256
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 240 * 1024
MAX_HTML_ELEMENTS = 50_000
MAX_HTML_DEPTH = 128
RSS_PROFILE = "scout-rss-extract@1"
RSS_STRATEGY = "rss-items@1"
RSS_MEDIA = frozenset({"text/xml", "application/xml", "application/rss+xml"})
RSS_MAX_BODY_BYTES = 1024 * 1024
RSS_MAX_ITEMS = 100
RSS_MAX_ELEMENTS = 10_000
RSS_MAX_DEPTH = 64
RSS_FIELD_BYTES = {"title": 1024, "link": 4096, "summary": 8192, "published": 128, "identifier": 1024}
RSS_TRANSFORMATIONS = (
    "utf8-bom-removed-if-present",
    "newlines-lf",
    "rss-xml-entities-decoded",
    "rss-field-surrounding-whitespace-trimmed",
    "rss-unselected-elements-and-attributes-omitted",
    "rss-description-html-text@1",
    "rss-publisher-dates-and-identifiers-unverified",
)
CPU_LIMIT_SECONDS = 2
MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
CORE_LIMIT_BYTES = 0
PROCESS_LIMIT = 1

PLAIN_STRATEGY = "plain-text@1"
HTML_STRATEGY = "html-text@1"
PLAIN_TRANSFORMATIONS = ("utf8-bom-removed-if-present", "newlines-lf")
HTML_TRANSFORMATIONS = (
    *PLAIN_TRANSFORMATIONS,
    "html5-doctype-omitted",
    "html-markup-omitted",
    "html-noncontent-omitted",
    "html-comments-omitted",
    "html-entities-decoded",
    "html-block-separators",
    "html-whitespace-collapsed",
)

WorkerCode = Literal[
    "protocol", "input_limit", "utf8", "control", "declaration", "structure", "output_limit", "resource", "internal"
]
REFUSAL_CODES: frozenset[WorkerCode] = frozenset(
    {"protocol", "input_limit", "utf8", "control", "declaration", "structure", "output_limit", "resource", "internal"}
)
_REFUSAL_LINES = {code: f'{{"status":"refused","code":"{code}"}}\n'.encode("ascii") for code in REFUSAL_CODES}
_READ_BYTES = 64 * 1024
_SURROGATE_START = 0xD800
_SURROGATE_END = 0xDFFF
_FORBIDDEN_CONTROLS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_DOCTYPE = re.compile(r"doctype[ \t\r\n]+html[ \t\r\n]*", re.IGNORECASE | re.ASCII)
_VOID = frozenset(
    ["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"]
)
_OMITTED = frozenset(["head", "script", "style", "template", "svg", "math"])
_BLOCK = frozenset(
    [
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "caption",
        "dd",
        "details",
        "dialog",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    ]
)
_OPTIONAL_SIBLINGS = {
    "p": ({"p"}, {"div", "section", "article", "td", "th"}),
    "li": ({"li"}, {"ul", "ol"}),
    "dt": ({"dt", "dd"}, {"dl"}),
    "dd": ({"dt", "dd"}, {"dl"}),
    "tr": ({"tr"}, {"table", "tbody", "thead", "tfoot"}),
    "td": ({"td", "th"}, {"tr", "table"}),
    "th": ({"td", "th"}, {"tr", "table"}),
}


class ExtractionResult(TypedDict):
    """Closed successful protocol payload, still entirely attributed source data."""

    status: Literal["ok"]
    text: str
    strategy: str
    transformations: list[str]


class RssChannelResult(TypedDict):
    """Selected publisher channel claims in the bounded RSS return."""

    title: str
    link: str
    summary: str


class RssItemResult(TypedDict):
    """An item locator and publisher claims, without new contact authority."""

    title: str
    link: str
    summary: str
    published: str | None
    identifier: str | None


class RssExtractionResult(TypedDict):
    """Separate RSS protocol shape; no coverage or custody claim."""

    status: Literal["ok"]
    strategy: str
    transformations: list[str]
    channel: RssChannelResult
    items: list[RssItemResult]
    omitted_elements: int


class WorkerRefusal(ValueError):  # noqa: N818 - Matches the closed protocol refusal vocabulary.
    """A content-free closed refusal; never carry a provider's exception text."""

    def __init__(self, code: WorkerCode) -> None:
        """Retain only a recognized protocol code."""
        if code not in REFUSAL_CODES:
            code = "internal"
        self.code = code
        super().__init__(code)


def _normalize(text: str) -> str:
    if _FORBIDDEN_CONTROLS.search(text):
        raise WorkerRefusal("control")
    return text.replace("\r\n", "\n").replace("\r", "\n")


class _TextProjection(HTMLParser):
    """Count structure before retaining it and discard all attribute values."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.parts: list[str] = []
        self.elements = 0
        self.output_bytes = 0

    def _append(self, text: str) -> None:
        if any(tag in _OMITTED for tag in self.stack):
            return
        self.output_bytes += len(text.encode("utf-8"))
        if self.output_bytes > MAX_OUTPUT_BYTES:
            raise WorkerRefusal("output_limit")
        self.parts.append(text)

    def _start(self, tag: str, *, empty: bool) -> None:
        self.elements += 1
        if tag == "body" and "head" in self.stack:
            index = self.stack.index("head")
            if any(item in _OMITTED for item in self.stack[index + 1 :]):
                raise WorkerRefusal("structure")
            del self.stack[index:]
        if not any(item in _OMITTED for item in self.stack) and tag in _OPTIONAL_SIBLINGS:
            siblings, boundaries = _OPTIONAL_SIBLINGS[tag]
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index] in boundaries:
                    break
                if self.stack[index] in siblings:
                    del self.stack[index:]
                    break
        if self.elements > MAX_HTML_ELEMENTS or len(self.stack) + 1 > MAX_HTML_DEPTH:
            raise WorkerRefusal("structure")
        if tag in _BLOCK or tag in _OMITTED:
            self._append("\n")
        if not empty and tag not in _VOID:
            self.stack.append(tag)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        self._start(tag, empty=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in _OMITTED:
            raise WorkerRefusal("structure")
        self._start(tag, empty=True)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            if any(item in _OMITTED for item in self.stack[index + 1 :]):
                raise WorkerRefusal("structure")
            del self.stack[index:]
        if tag in _BLOCK or tag in _OMITTED:
            self._append("\n")

    def handle_data(self, data: str) -> None:
        self._append(data)

    def handle_entityref(self, name: str) -> None:
        self._append(_normalize(unescape(f"&{name};")))

    def handle_charref(self, name: str) -> None:
        try:
            value = int(name[1:], 16) if name.lower().startswith("x") else int(name, 10)
            text = chr(value)
        except (ValueError, OverflowError):
            raise WorkerRefusal("control") from None
        if _SURROGATE_START <= value <= _SURROGATE_END:
            raise WorkerRefusal("control")
        self._append(_normalize(text))

    def handle_decl(self, decl: str) -> None:
        if _DOCTYPE.fullmatch(decl) is None:
            raise WorkerRefusal("declaration")

    def handle_pi(self, data: str) -> None:
        del data
        raise WorkerRefusal("declaration")

    def unknown_decl(self, data: str) -> None:
        del data
        raise WorkerRefusal("declaration")

    def parse_html_declaration(self, i: int) -> int:
        if not self.rawdata.startswith("<!--", i) and self.rawdata[i : i + 9].lower() != "<!doctype":
            raise WorkerRefusal("declaration")
        return super().parse_html_declaration(i)

    def result(self) -> str:
        if self.rawdata or any(tag in _OMITTED for tag in self.stack):
            raise WorkerRefusal("structure")
        self.close()
        lines = (" ".join(line.split()) for line in "".join(self.parts).split("\n"))
        return "\n".join(line for line in lines if line)


def _serialize(result: ExtractionResult | RssExtractionResult) -> bytes:
    output = (json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(output) > MAX_OUTPUT_BYTES:
        raise WorkerRefusal("output_limit")
    return output


def extract_text(body: bytes, media_type: str) -> ExtractionResult:
    """Project strict UTF-8 bytes without effects; raise a fixed refusal on loss of bounds.

    Remove only a leading UTF-8 BOM, normalize CRLF/CR to LF, and refuse C0/C1
    controls except TAB/LF/CR. Plain text otherwise retains its whitespace. HTML
    decodes entities, omits noncontent scopes and comments, and collapses each
    block's whitespace. Neither path removes source-authored instructions.
    """
    if type(body) is not bytes or type(media_type) is not str or media_type not in {"text/plain", "text/html"}:
        raise WorkerRefusal("protocol")
    if len(body) > MAX_BODY_BYTES:
        raise WorkerRefusal("input_limit")
    try:
        text = _normalize(body.decode("utf-8-sig", errors="strict"))
    except UnicodeDecodeError:
        raise WorkerRefusal("utf8") from None
    if media_type == "text/html":
        parser = _TextProjection()
        parser.feed(text)
        text = parser.result()
        strategy, transformations = HTML_STRATEGY, HTML_TRANSFORMATIONS
    else:
        strategy, transformations = PLAIN_STRATEGY, PLAIN_TRANSFORMATIONS
    result: ExtractionResult = {
        "status": "ok",
        "text": text,
        "strategy": strategy,
        "transformations": list(transformations),
    }
    _serialize(result)
    return result


_RSS_CHANNEL_FIELDS = {"title": "title", "link": "link", "description": "summary"}
_RSS_ITEM_FIELDS = {**_RSS_CHANNEL_FIELDS, "pubDate": "published", "guid": "identifier"}
_RSS_MIN_EXPAT_VERSION = (2, 6, 0)
_XML_WHITESPACE = " \t\r\n"
_RSS_HTML_NUMERIC_REFERENCE = re.compile(r"&#(x[0-9a-f]+|[0-9]+);?", re.IGNORECASE | re.ASCII)


def _deny_xml_declaration(*_args: object) -> Never:
    raise WorkerRefusal("declaration")


def _rss_xml_declaration(version: str, encoding: str | None, standalone: int) -> None:
    del standalone
    if version != "1.0" or encoding is None or encoding.lower() != "utf-8":
        raise WorkerRefusal("declaration")


def _rss_namespace(_prefix: str | None, uri: str | None) -> None:
    _normalize(uri or "")


class _RssDescriptionProjection(_TextProjection):
    """Check discarded HTML attributes before numeric-reference remapping can erase controls."""

    def _check_attributes(self, attrs: list[tuple[str, str | None]]) -> None:
        for _name, value in attrs:
            if value is not None:
                _normalize(value)
        # The selected scalar bounds this lexical scan before HTMLParser sees it.
        # Scan source spelling once: &amp;#x85; must remain inert, not decode twice.
        for reference in _RSS_HTML_NUMERIC_REFERENCE.finditer(self.get_starttag_text() or ""):
            name = reference.group(1)
            try:
                value = int(name[1:], 16) if name.lower().startswith("x") else int(name, 10)
                text = chr(value)
            except (ValueError, OverflowError):
                raise WorkerRefusal("control") from None
            if _SURROGATE_START <= value <= _SURROGATE_END:
                raise WorkerRefusal("control")
            _normalize(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._check_attributes(attrs)
        super().handle_starttag(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._check_attributes(attrs)
        super().handle_startendtag(tag, attrs)


class _RssProjection:
    """Retain only direct RSS scalars; bound every element, including omitted subtrees."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.elements = 0
        self.omitted_elements = 0
        self.channel_seen = False
        self.channel: dict[str, str] = {}
        self.current_item: dict[str, str] | None = None
        self.items: list[RssItemResult] = []
        self.field: str | None = None
        self.field_target: dict[str, str] | None = None
        self.field_parts: list[str] = []
        self.field_bytes = 0

    def _start_field(self, field: str, target: dict[str, str]) -> None:
        if field in target:
            raise WorkerRefusal("structure")
        self.field = field
        self.field_target = target
        self.field_parts = []
        self.field_bytes = 0

    def start(self, name: str, attrs: dict[str, str]) -> None:
        self.elements += 1
        if self.elements > RSS_MAX_ELEMENTS or len(self.stack) + 1 > RSS_MAX_DEPTH:
            raise WorkerRefusal("structure")
        for value in attrs.values():
            _normalize(value)
        if self.field is not None:
            raise WorkerRefusal("structure")
        self._select(name, attrs)
        self.stack.append(name)

    def _select(self, name: str, attrs: dict[str, str]) -> None:
        if not self.stack:
            if name != "rss" or attrs.get("version") != "2.0":
                raise WorkerRefusal("structure")
        elif self.stack == ["rss"] and name == "channel":
            if self.channel_seen:
                raise WorkerRefusal("structure")
            self.channel_seen = True
        elif self.stack == ["rss", "channel"] and name == "item":
            if len(self.items) >= RSS_MAX_ITEMS:
                raise WorkerRefusal("structure")
            self.current_item = {}
        elif self.stack == ["rss", "channel"] and name in _RSS_CHANNEL_FIELDS:
            self._start_field(_RSS_CHANNEL_FIELDS[name], self.channel)
        elif self.stack == ["rss", "channel", "item"] and name in _RSS_ITEM_FIELDS:
            if self.current_item is None:
                raise WorkerRefusal("structure")
            self._start_field(_RSS_ITEM_FIELDS[name], self.current_item)
        else:
            self.omitted_elements += 1

    def data(self, data: str) -> None:
        data = _normalize(data)
        if self.field is None:
            if self.stack in (["rss"], ["rss", "channel"], ["rss", "channel", "item"]) and data.strip(_XML_WHITESPACE):
                raise WorkerRefusal("structure")
            return
        self.field_bytes += len(data.encode("utf-8"))
        if self.field_bytes > RSS_FIELD_BYTES[self.field]:
            raise WorkerRefusal("output_limit")
        self.field_parts.append(data)

    def _finish_field(self) -> None:
        if self.field is None or self.field_target is None:
            raise WorkerRefusal("structure")
        text = "".join(self.field_parts).strip(_XML_WHITESPACE)
        if self.field == "summary":
            parser = _RssDescriptionProjection()
            parser.feed(text)
            text = parser.result()
        if len(text.encode("utf-8")) > RSS_FIELD_BYTES[self.field]:
            raise WorkerRefusal("output_limit")
        self.field_target[self.field] = text
        self.field = None
        self.field_target = None
        self.field_parts = []
        self.field_bytes = 0

    def _finish_item(self) -> None:
        fields = self.current_item
        if fields is None or not fields.get("link") or not (fields.get("title") or fields.get("summary")):
            raise WorkerRefusal("structure")
        self.items.append(
            {
                "title": fields.get("title", ""),
                "link": fields["link"],
                "summary": fields.get("summary", ""),
                "published": fields.get("published"),
                "identifier": fields.get("identifier"),
            }
        )
        self.current_item = None

    def end(self, _name: str) -> None:
        if self.field is not None:
            self._finish_field()
        elif self.stack == ["rss", "channel", "item"]:
            self._finish_item()
        self.stack.pop()

    def result(self) -> RssExtractionResult:
        if (
            self.stack
            or not self.channel_seen
            or any(not self.channel.get(field) for field in ("title", "link", "summary"))
        ):
            raise WorkerRefusal("structure")
        return {
            "status": "ok",
            "strategy": RSS_STRATEGY,
            "transformations": list(RSS_TRANSFORMATIONS),
            "channel": {
                "title": self.channel["title"],
                "link": self.channel["link"],
                "summary": self.channel["summary"],
            },
            "items": self.items,
            "omitted_elements": self.omitted_elements,
        }


def extract_rss(body: bytes) -> RssExtractionResult:
    """Project bounded UTF-8 RSS scalars without contacting publisher locators.

    XML-decoded field construction is bounded before whitespace trimming and
    description projection. Every omitted subtree still receives structural and
    control checks; unsupported declarations refuse the complete document.
    """
    if type(body) is not bytes:
        raise WorkerRefusal("protocol")
    if len(body) > RSS_MAX_BODY_BYTES:
        raise WorkerRefusal("input_limit")
    try:
        text = _normalize(body.decode("utf-8-sig", errors="strict"))
    except UnicodeDecodeError:
        raise WorkerRefusal("utf8") from None
    if expat.version_info < _RSS_MIN_EXPAT_VERSION:
        raise WorkerRefusal("resource")
    parser = expat.ParserCreate(namespace_separator="\x1f")
    try:
        parser.SetReparseDeferralEnabled(True)  # noqa: FBT003 - Expat requires a positional boolean.
        if not parser.GetReparseDeferralEnabled():
            raise WorkerRefusal("resource")
    except AttributeError:
        raise WorkerRefusal("resource") from None
    if not parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER):
        raise WorkerRefusal("resource")
    projection = _RssProjection()
    parser.StartElementHandler = projection.start
    parser.EndElementHandler = projection.end
    parser.CharacterDataHandler = projection.data
    parser.XmlDeclHandler = _rss_xml_declaration
    parser.StartNamespaceDeclHandler = _rss_namespace
    parser.StartDoctypeDeclHandler = _deny_xml_declaration
    parser.EntityDeclHandler = _deny_xml_declaration
    parser.ExternalEntityRefHandler = _deny_xml_declaration
    parser.ProcessingInstructionHandler = _deny_xml_declaration
    try:
        parser.Parse(text, True)  # noqa: FBT003 - Expat requires a positional final-input flag.
    except expat.ExpatError:
        raise WorkerRefusal("structure") from None
    result = projection.result()
    _serialize(result)
    return result


def _header_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkerRefusal("protocol")
        result[key] = value
    return result


def _request_header(fields: dict[str, object]) -> tuple[str, int]:
    """Select one exact private protocol without widening legacy text media."""
    if set(fields) == {"media_type", "body_bytes"}:
        allowed_media, maximum = frozenset({"text/plain", "text/html"}), MAX_BODY_BYTES
    elif set(fields) == {"profile", "media_type", "body_bytes"} and fields["profile"] == RSS_PROFILE:
        allowed_media, maximum = RSS_MEDIA, RSS_MAX_BODY_BYTES
    else:
        raise WorkerRefusal("protocol")
    media_type, size = fields["media_type"], fields["body_bytes"]
    if type(media_type) is not str or media_type not in allowed_media or type(size) is not int:
        raise WorkerRefusal("protocol")
    if not 0 <= size <= maximum:
        raise WorkerRefusal("input_limit")
    return media_type, size


def _read_request(source: BinaryIO) -> tuple[bytes, str]:
    header = source.readline(MAX_HEADER_BYTES + 1)
    if len(header) > MAX_HEADER_BYTES or not header.endswith(b"\n"):
        raise WorkerRefusal("protocol")
    try:
        value = json.loads(header.decode("ascii"), object_pairs_hook=_header_pairs)
    except (UnicodeDecodeError, ValueError):
        raise WorkerRefusal("protocol") from None
    if type(value) is not dict:
        raise WorkerRefusal("protocol")
    fields = cast("dict[str, object]", value)
    media_type, size = _request_header(fields)
    parts: list[bytes] = []
    remaining = size
    while remaining:
        part = source.read(min(remaining, _READ_BYTES))
        if not part:
            raise WorkerRefusal("protocol")
        parts.append(part)
        remaining -= len(part)
    if source.read(1):
        raise WorkerRefusal("protocol")
    return b"".join(parts), media_type


def run(source: BinaryIO, destination: BinaryIO) -> int:
    """Consume exactly one request and write one bounded response, without limits setup."""
    try:
        body, media_type = _read_request(source)
        output = _serialize(extract_rss(body) if media_type in RSS_MEDIA else extract_text(body, media_type))
        exit_code = 0
    except WorkerRefusal as error:
        output, exit_code = _REFUSAL_LINES[error.code], 2
    except MemoryError:
        output, exit_code = _REFUSAL_LINES["resource"], 2
    except Exception:  # noqa: BLE001 - Standalone protocol must not disclose provider input or exception text.
        output, exit_code = _REFUSAL_LINES["internal"], 2
    destination.write(output)
    destination.flush()
    return exit_code


def _set_resource_limits() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_SECONDS, CPU_LIMIT_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (CORE_LIMIT_BYTES, CORE_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_NPROC, (PROCESS_LIMIT, PROCESS_LIMIT))


def main() -> int:
    """Set process ceilings before reading input; imports never alter caller limits."""
    try:
        _set_resource_limits()
    except Exception:  # noqa: BLE001 - Missing or refused platform limits must fail closed.
        sys.stdout.buffer.write(_REFUSAL_LINES["resource"])
        sys.stdout.buffer.flush()
        return 2
    return run(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
